# Copyright (C) 2019  alfred richardsn
#
# This file is part of TellerBot.
#
# TellerBot is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with TellerBot.  If not, see <https://www.gnu.org/licenses/>.
"""Handlers for escrow exchange."""
import asyncio
import typing
from dataclasses import replace
from decimal import Decimal
from functools import wraps
from time import time

from aiogram import types
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import any_state
from aiogram.types import InlineKeyboardButton
from aiogram.types import InlineKeyboardMarkup
from aiogram.types import ParseMode
from aiogram.types import User
from aiogram.utils import markdown
from bson.decimal128 import Decimal128
from bson.objectid import ObjectId

from src import referral_system as rs
from src import states
from src.bot import dp
from src.bot import tg
from src.config import config
from src.database import database
from src.escrow import get_escrow_instance
from src.escrow import SUPPORTED_BANKS
from src.escrow.blockchain import StreamBlockchain
from src.escrow.escrow_offer import EscrowOffer
from src.handlers.base import private_handler
from src.handlers.base import start_keyboard
from src.i18n import i18n
from src.money import money
from src.money import MoneyValueError
from src.money import normalize


async def get_card_number(
    text: str, chat_id: int
) -> typing.Optional[typing.Tuple[str, str]]:
    """Parse first and last 4 digits from card number in ``text``.

    If parsing is unsuccessful, send warning to ``chat_id`` and return
    None. Otherwise return tuple of first and last 4 digits of card number.
    """
    if len(text) < 8:
        await tg.send_message(chat_id, i18n("send_at_least_8_digits"))
        return None
    first = text[:4]
    last = text[-4:]
    if not first.isdigit() or not last.isdigit():
        await tg.send_message(chat_id, i18n("digits_parsing_error"))
        return None
    return (first, last)


@dp.async_task
async def call_later(delay: float, callback: typing.Callable, *args, **kwargs):
    """Call ``callback(*args, **kwargs)`` asynchronously after ``delay`` seconds."""
    await asyncio.sleep(delay)
    return await callback(*args, **kwargs)


def escrow_callback_handler(*args, state=any_state, **kwargs):
    """Simplify handling callback queries during escrow exchange.

    Add offer of ``EscrowOffer`` to arguments of decorated callback query handler.
    """

    def decorator(
        handler: typing.Callable[[types.CallbackQuery, EscrowOffer], typing.Any]
    ):
        @wraps(handler)
        @dp.callback_query_handler(*args, state=state, **kwargs)
        async def wrapper(call: types.CallbackQuery):
            offer_id = call.data.split()[1]
            offer = await database.escrow.find_one({"_id": ObjectId(offer_id)})
            if not offer:
                await call.answer(i18n("offer_not_active"))
                return

            return await handler(call, EscrowOffer(**offer))

        return wrapper

    return decorator


def escrow_message_handler(*args, **kwargs):
    """Simplify handling messages during escrow exchange.

    Add offer of ``EscrowOffer`` to arguments of decorated private message handler.
    """

    def decorator(handler: typing.Callable[[types.Message, EscrowOffer], typing.Any]):
        @wraps(handler)
        @private_handler(*args, **kwargs)
        async def wrapper(message: types.Message, state: FSMContext):
            offer = await database.escrow.find_one(
                {"pending_input_from": message.from_user.id}
            )
            if not offer:
                await tg.send_message(message.chat.id, i18n("offer_not_active"))
                return

            return await handler(message, EscrowOffer(**offer))

        return wrapper

    return decorator


async def get_insurance(offer: EscrowOffer) -> Decimal:
    """Get insurance of escrow asset in ``offer`` taking limits into account."""
    offer_sum = offer[f"sum_{offer.type}"].to_decimal()
    asset = offer.escrow
    limits = await get_escrow_instance(asset).get_limits(asset)
    if not limits:
        return offer_sum
    insured = min(offer_sum, limits.single)
    cursor = database.escrow.aggregate(
        [{"$group": {"_id": 0, "insured_total": {"$sum": "$insured"}}}]
    )
    if await cursor.fetch_next:
        insured_total = cursor.next_object()["insured_total"]
        if insured_total != 0:
            insured_total = insured_total.to_decimal()
        total_difference = limits.total - insured_total - insured
        if total_difference < 0:
            insured += total_difference
    return normalize(insured)


@escrow_message_handler(state=states.Escrow.amount)
async def set_escrow_sum(message: types.Message, offer: EscrowOffer):
    """Set sum and ask for fee payment agreement."""
    try:
        offer_sum = money(message.text)
    except MoneyValueError as exception:
        await tg.send_message(message.chat.id, str(exception))
        return

    order = await database.orders.find_one({"_id": offer.order})
    order_sum = order.get(offer.sum_currency)
    if order_sum and offer_sum > order_sum.to_decimal():
        await tg.send_message(message.chat.id, i18n("exceeded_order_sum"))
        return

    update_dict = {offer.sum_currency: Decimal128(offer_sum)}
    new_currency = "sell" if offer.sum_currency == "sum_buy" else "buy"
    update_dict[f"sum_{new_currency}"] = Decimal128(
        normalize(offer_sum * order[f"price_{new_currency}"].to_decimal())
    )
    escrow_sum = update_dict[f"sum_{offer.type}"]
    escrow_fee = Decimal(config.ESCROW_FEE_PERCENTS) / Decimal("100")
    update_dict["sum_fee_up"] = Decimal128(
        normalize(escrow_sum.to_decimal() * (Decimal("1") + escrow_fee))
    )
    update_dict["sum_fee_down"] = Decimal128(
        normalize(escrow_sum.to_decimal() * (Decimal("1") - escrow_fee))
    )
    offer = replace(offer, **update_dict)  # type: ignore

    if offer.sum_currency == offer.type:
        insured = await get_insurance(offer)
        update_dict["insured"] = Decimal128(insured)
        if offer_sum > insured:
            keyboard = InlineKeyboardMarkup()
            keyboard.add(
                InlineKeyboardButton(
                    i18n("continue"), callback_data=f"accept_insurance {offer._id}"
                ),
                InlineKeyboardButton(
                    i18n("cancel"), callback_data=f"init_cancel {offer._id}"
                ),
            )
            answer = i18n("exceeded_insurance {amount} {currency}").format(
                amount=insured, currency=offer.escrow
            )
            answer += "\n" + i18n("exceeded_insurance_options")
            await tg.send_message(message.chat.id, answer, reply_markup=keyboard)
    else:
        await ask_fee(message.from_user.id, message.chat.id, offer)

    await offer.update_document({"$set": update_dict, "$unset": {"sum_currency": True}})


async def ask_fee(user_id: int, chat_id: int, offer: EscrowOffer):
    """Ask fee of any party."""
    answer = (
        i18n("ask_fee {fee_percents}").format(fee_percents=config.ESCROW_FEE_PERCENTS)
        + " "
    )
    if (user_id == offer.init["id"]) == (offer.type == "buy"):
        answer += i18n("will_pay {amount} {currency}")
        sum_fee_field = "sum_fee_up"
    else:
        answer += i18n("will_get {amount} {currency}")
        sum_fee_field = "sum_fee_down"
    keyboard = InlineKeyboardMarkup()
    keyboard.add(
        InlineKeyboardButton(i18n("yes"), callback_data=f"accept_fee {offer._id}"),
        InlineKeyboardButton(i18n("no"), callback_data=f"decline_fee {offer._id}"),
    )
    answer = answer.format(amount=offer[sum_fee_field], currency=offer.escrow)
    await tg.send_message(chat_id, answer, reply_markup=keyboard)
    await states.Escrow.fee.set()


@escrow_callback_handler(
    lambda call: call.data.startswith("accept_insurance "), state=states.Escrow.amount
)
async def accept_insurance(call: types.CallbackQuery, offer: EscrowOffer):
    """Ask for fee payment agreement after accepting partial insurance."""
    await ask_fee(call.from_user.id, call.message.chat.id, offer)


@escrow_callback_handler(
    lambda call: call.data.startswith("init_cancel "), state=states.Escrow.amount
)
async def init_cancel(call: types.CallbackQuery, offer: EscrowOffer):
    """Cancel offer on initiator's request."""
    await offer.delete_document()
    await call.answer()
    await tg.send_message(
        call.message.chat.id, i18n("escrow_cancelled"), reply_markup=start_keyboard()
    )
    await dp.current_state().finish()


async def full_card_number_request(chat_id: int, offer: EscrowOffer):
    """Ask to send full card number."""


async def ask_credentials(
    call: types.CallbackQuery,
    offer: EscrowOffer,
):
    """Update offer with ``update_dict`` and start asking transfer information.

    Ask to choose bank if user is initiator and there is a fiat
    currency. Otherwise ask receive address.
    """
    await call.answer()
    is_user_init = call.from_user.id == offer.init["id"]
    has_fiat_currency = "RUB" in {offer.buy, offer.sell}
    if has_fiat_currency:
        if is_user_init:
            keyboard = InlineKeyboardMarkup()
            for bank in SUPPORTED_BANKS:
                keyboard.row(
                    InlineKeyboardButton(bank, callback_data=f"bank {offer._id} {bank}")
                )
            await tg.send_message(
                call.message.chat.id, i18n("choose_bank"), reply_markup=keyboard
            )
            await states.Escrow.bank.set()
        else:
            if offer.type == "buy":
                request_user = offer.init
                answer_user = offer.counter
                currency = offer.sell
            else:
                request_user = offer.counter
                answer_user = offer.init
                currency = offer.buy
            keyboard = InlineKeyboardMarkup()
            keyboard.add(
                InlineKeyboardButton(
                    i18n("sent"), callback_data=f"card_sent {offer._id}"
                )
            )
            mention = markdown.link(
                answer_user["mention"], User(id=answer_user["id"]).url
            )
            await tg.send_message(
                request_user["id"],
                i18n(
                    "request_full_card_number {currency} {user}",
                    locale=request_user["locale"],
                ).format(currency=currency, user=mention),
                reply_markup=keyboard,
                parse_mode=ParseMode.MARKDOWN,
            )
            state = FSMContext(dp.storage, request_user["id"], request_user["id"])
            await state.set_state(states.Escrow.full_card.state)
            answer = i18n(
                "asked_full_card_number {user}", locale=answer_user["locale"]
            ).format(
                user=markdown.link(
                    request_user["mention"], User(id=request_user["id"]).url
                )
            )
            await tg.send_message(
                answer_user["id"],
                answer,
                parse_mode=ParseMode.MARKDOWN,
            )
        return

    await tg.send_message(
        call.message.chat.id,
        i18n("ask_address {currency}").format(
            currency=offer.sell if is_user_init else offer.buy
        ),
    )
    await offer.update_document({"$set": {"pending_input_from": call.from_user.id}})
    await states.Escrow.receive_address.set()


@escrow_callback_handler(
    lambda call: call.data.startswith("accept_fee "), state=states.Escrow.fee
)
async def pay_fee(call: types.CallbackQuery, offer: EscrowOffer):
    """Accept fee and start asking transfer information."""
    await ask_credentials(call, offer)


@escrow_callback_handler(
    lambda call: call.data.startswith("decline_fee "), state=states.Escrow.fee
)
async def decline_fee(call: types.CallbackQuery, offer: EscrowOffer):
    """Decline fee and start asking transfer information."""
    if (call.from_user.id == offer.init["id"]) == (offer.type == "buy"):
        sum_fee_field = "sum_fee_up"
    else:
        sum_fee_field = "sum_fee_down"
    await offer.update_document({"$set": {sum_fee_field: offer[f"sum_{offer.type}"]}})
    await ask_credentials(call, offer)


@escrow_callback_handler(
    lambda call: call.data.startswith("bank "), state=states.Escrow.bank
)
async def choose_bank(call: types.CallbackQuery, offer: EscrowOffer):
    """Set chosen bank and continue.

    Because bank is chosen by initiator, ask for receive address if
    they receive escrow asset.
    """
    bank = call.data.split()[2]
    if bank not in SUPPORTED_BANKS:
        await call.answer(i18n("bank_not_supported"))
        return

    update_dict = {"bank": bank}
    await call.answer()
    update_dict["pending_input_from"] = call.from_user.id
    await offer.update_document({"$set": update_dict})
    if offer.sell == "RUB":
        await tg.send_message(
            call.message.chat.id,
            i18n("send_first_and_last_4_digits_of_card_number {currency}").format(
                currency=offer.sell
            ),
        )
        await states.Escrow.receive_card_number.set()
    else:
        await tg.send_message(
            call.message.chat.id,
            i18n("ask_address {currency}").format(currency=offer.sell),
        )
        await states.Escrow.receive_address.set()


@escrow_message_handler(state=states.Escrow.full_card)
async def full_card_number_message(message: types.Message, offer: EscrowOffer):
    """React to sent message while sending full card number to fiat sender."""
    if message.from_user.id == offer.init["id"]:
        user = offer.counter
    else:
        user = offer.init
    mention = markdown.link(user["mention"], User(id=user["id"]).url)
    await tg.send_message(
        message.chat.id,
        i18n("wrong_full_card_number_receiver {user}").format(user=mention),
        parse_mode=ParseMode.MARKDOWN,
    )


@escrow_callback_handler(
    lambda call: call.data.startswith("card_sent "), state=states.Escrow.full_card
)
async def full_card_number_sent(call: types.CallbackQuery, offer: EscrowOffer):
    """Confirm that full card number is sent and ask for first and last 4 digits."""
    await offer.update_document({"$set": {"pending_input_from": call.from_user.id}})
    await call.answer()
    if call.from_user.id == offer.init["id"]:
        counter = offer.counter
        await tg.send_message(
            counter["id"], i18n("ask_address {currency}").format(currency=offer.buy)
        )
        await tg.send_message(
            call.message.chat.id,
            i18n("exchange_continued {user}").format(
                user=markdown.link(counter["mention"], User(id=counter["id"]).url)
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
        await offer.update_document({"$set": {"pending_input_from": counter["id"]}})
        counter_state = FSMContext(dp.storage, counter["id"], counter["id"])
        await counter_state.set_state(states.Escrow.receive_address.state)
        await states.Escrow.receive_card_number.set()
    else:
        await tg.send_message(
            call.message.chat.id,
            i18n("send_first_and_last_4_digits_of_card_number {currency}").format(
                currency=offer.sell if offer.type == "buy" else offer.buy
            ),
        )
        await states.Escrow.receive_card_number.set()


@escrow_message_handler(state=states.Escrow.receive_card_number)
async def set_receive_card_number(message: types.Message, offer: EscrowOffer):
    """Create address from first and last 4 digits of card number and ask send address.

    First and last 4 digits of card number are sent by fiat receiver,
    so their send address is escrow asset address.
    """
    card_number = await get_card_number(message.text, message.chat.id)
    if not card_number:
        return

    if message.from_user.id == offer.init["id"]:
        user_field = "init"
    else:
        user_field = "counter"

    await offer.update_document(
        {"$set": {f"{user_field}.receive_address": ("*" * 8).join(card_number)}}
    )
    await tg.send_message(
        message.chat.id,
        i18n("ask_address {currency}").format(currency=offer.escrow),
    )
    await states.Escrow.send_address.set()


@escrow_message_handler(state=states.Escrow.receive_address)
async def set_receive_address(message: types.Message, offer: EscrowOffer):
    """Set escrow asset receiver's address and ask for sender's information.

    If there is a fiat currency, which is indicated by existing
    ``bank`` field, and user is a fiat sender, ask their name on card.
    Otherwise ask escrow asset sender's address.
    """
    if len(message.text) >= 150:
        await tg.send_message(
            message.chat.id,
            i18n("exceeded_character_limit {limit} {sent}").format(
                limit=150, sent=len(message.text)
            ),
        )
        return

    if message.from_user.id == offer.init["id"]:
        user_field = "init"
        send_currency = offer.buy
        ask_name = offer.bank and offer.type == "sell"
    else:
        user_field = "counter"
        send_currency = offer.sell
        ask_name = offer.bank and offer.type == "buy"

    await offer.update_document(
        {"$set": {f"{user_field}.receive_address": message.text}}
    )
    if ask_name:
        await tg.send_message(
            message.chat.id,
            i18n("send_name_patronymic_surname"),
        )
        await states.Escrow.name.set()
    else:
        await tg.send_message(
            message.chat.id,
            i18n("ask_address {currency}").format(currency=send_currency),
        )
        await states.Escrow.send_address.set()


@escrow_message_handler(state=states.Escrow.send_address)
async def set_send_address(message: types.Message, offer: EscrowOffer):
    """Set send address of any party."""
    if len(message.text) >= 150:
        await tg.send_message(
            message.chat.id,
            i18n("exceeded_character_limit {limit} {sent}").format(
                limit=150, sent=len(message.text)
            ),
        )
        return

    if message.from_user.id == offer.init["id"]:
        await set_init_send_address(message.text, message, offer)
    else:
        await set_counter_send_address(message.text, message, offer)


@escrow_message_handler(state=states.Escrow.name)
async def set_name(message: types.Message, offer: EscrowOffer):
    """Set fiat sender's name on card and ask for first and last 4 digits."""
    name = message.text.split()
    if len(name) != 3:
        await tg.send_message(
            message.chat.id,
            i18n("wrong_word_count {word_count}").format(word_count=3),
        )
        return
    name[2] = name[2][0] + "."  # Leaving the first letter of surname with dot

    if offer.type == "buy":
        user_field = "counter"
        currency = offer.sell
    else:
        user_field = "init"
        currency = offer.buy

    await offer.update_document(
        {"$set": {f"{user_field}.name": " ".join(name).upper()}}
    )
    await tg.send_message(
        message.chat.id,
        i18n("send_first_and_last_4_digits_of_card_number {currency}").format(
            currency=currency
        ),
    )
    await states.Escrow.send_card_number.set()


@escrow_message_handler(state=states.Escrow.send_card_number)
async def set_send_card_number(message: types.Message, offer: EscrowOffer):
    """Set first and last 4 digits of any party."""
    card_number = await get_card_number(message.text, message.chat.id)
    if not card_number:
        return

    address = ("*" * 8).join(card_number)
    if message.from_user.id == offer.init["id"]:
        await set_init_send_address(address, message, offer)
    else:
        await set_counter_send_address(address, message, offer)


async def set_init_send_address(
    address: str, message: types.Message, offer: EscrowOffer
):
    """Set ``address`` as sender's address of initiator.

    Send offer to counteragent.
    """
    locale = offer.counter["locale"]
    buy_keyboard = InlineKeyboardMarkup()
    buy_keyboard.row(
        InlineKeyboardButton(
            i18n("show_order", locale=locale), callback_data=f"get_order {offer.order}"
        )
    )
    buy_keyboard.add(
        InlineKeyboardButton(
            i18n("accept", locale=locale), callback_data=f"accept {offer._id}"
        ),
        InlineKeyboardButton(
            i18n("decline", locale=locale), callback_data=f"decline {offer._id}"
        ),
    )
    mention = markdown.link(offer.init["mention"], User(id=offer.init["id"]).url)
    answer = i18n(
        "escrow_offer_notification {user} {sell_amount} {sell_currency} "
        "for {buy_amount} {buy_currency}",
        locale=locale,
    ).format(
        user=mention,
        sell_amount=offer.sum_sell,
        sell_currency=offer.sell,
        buy_amount=offer.sum_buy,
        buy_currency=offer.buy,
    )
    if offer.bank:
        answer += " " + i18n("using {bank}", locale=locale).format(bank=offer.bank)
    answer += "."
    update_dict = {"init.send_address": address}
    if offer.type == "sell":
        insured = await get_insurance(offer)
        update_dict["insured"] = Decimal128(insured)
        if offer[f"sum_{offer.type}"].to_decimal() > insured:
            answer += "\n" + i18n("exceeded_insurance {amount} {currency}").format(
                amount=insured, currency=offer.escrow
            )
    await offer.update_document(
        {"$set": update_dict, "$unset": {"pending_input_from": True}}
    )
    await tg.send_message(
        offer.counter["id"],
        answer,
        reply_markup=buy_keyboard,
        parse_mode=ParseMode.MARKDOWN,
    )
    sell_keyboard = InlineKeyboardMarkup()
    sell_keyboard.add(
        InlineKeyboardButton(i18n("cancel"), callback_data=f"escrow_cancel {offer._id}")
    )
    await tg.send_message(
        message.from_user.id, i18n("offer_sent"), reply_markup=sell_keyboard
    )
    await dp.current_state().finish()


@escrow_callback_handler(lambda call: call.data.startswith("accept "))
async def accept_offer(call: types.CallbackQuery, offer: EscrowOffer):
    """React to counteragent accepting offer by asking for fee payment agreement."""
    await offer.update_document(
        {"$set": {"pending_input_from": call.message.chat.id, "react_time": time()}}
    )
    await call.answer()
    await ask_fee(call.from_user.id, call.message.chat.id, offer)


@escrow_callback_handler(lambda call: call.data.startswith("decline "))
async def decline_offer(call: types.CallbackQuery, offer: EscrowOffer):
    """React to counteragent declining offer."""
    offer.react_time = time()
    await offer.delete_document()
    await tg.send_message(
        offer.init["id"],
        i18n("escrow_offer_declined", locale=offer.init["locale"]),
    )
    await call.answer()
    await tg.send_message(call.message.chat.id, i18n("offer_declined"))


def create_memo(
    offer: EscrowOffer,
    *,
    transfer: bool,
    counter_send_address: typing.Optional[str] = None,
):
    """Create memo for transfer."""
    if transfer:
        template = "from {escrow_send_address} "
    else:
        template = "to {escrow_receive_address} "
    template += (
        "for {not_escrow_amount} {not_escrow_currency} "
        "from {not_escrow_send_address} to {not_escrow_receive_address} "
        "via escrow service on https://t.me/TellerBot"
    )
    if counter_send_address is None:
        counter_send_address = offer.counter["send_address"]
    if offer.type == "buy":
        return template.format(
            **{
                "escrow_send_address": offer.init["send_address"],
                "escrow_receive_address": offer.counter["receive_address"],
                "not_escrow_amount": offer.sum_sell,
                "not_escrow_currency": offer.sell,
                "not_escrow_send_address": counter_send_address,
                "not_escrow_receive_address": offer.init["receive_address"],
            }
        )
    elif offer.type == "sell":
        return template.format(
            **{
                "escrow_send_address": counter_send_address,
                "escrow_receive_address": offer.init["receive_address"],
                "not_escrow_amount": offer.sum_buy,
                "not_escrow_currency": offer.buy,
                "not_escrow_send_address": offer.init["send_address"],
                "not_escrow_receive_address": offer.counter["receive_address"],
            }
        )


@escrow_callback_handler(lambda call: call.data.startswith("check_transaction "))
async def check_transaction(call: types.CallbackQuery, offer: EscrowOffer):
    """Start transaction check."""
    if offer.type == "buy":
        from_address = offer.init["send_address"]
    elif offer.type == "sell":
        from_address = offer.counter["send_address"]
    escrow_instance = get_escrow_instance(offer.escrow)
    await call.answer(i18n("transaction_check_starting"))
    success = await escrow_instance.check_transaction(
        offer_id=offer._id,
        from_address=from_address,
        amount_with_fee=offer["sum_fee_up"].to_decimal(),
        amount_without_fee=offer[f"sum_{offer.type}"].to_decimal(),
        asset=offer.escrow,
        memo=offer.memo,
        transaction_time=offer.transaction_time,
    )
    if not success:
        await tg.send_message(call.message.chat.id, i18n("transaction_not_found"))


async def set_counter_send_address(
    address: str, message: types.Message, offer: EscrowOffer
):
    """Set ``address`` as sender's address of counteragent.

    Ask for escrow asset transfer.
    """
    memo = create_memo(offer, transfer=False, counter_send_address=address)
    if offer.type == "buy":
        escrow_user = offer.init
        from_address = offer.init["send_address"]
        send_reply = True
    elif offer.type == "sell":
        escrow_user = offer.counter
        from_address = address
        send_reply = False
    keyboard = InlineKeyboardMarkup()
    escrow_instance = get_escrow_instance(offer.escrow)
    transaction_time = time()
    if isinstance(escrow_instance, StreamBlockchain):
        await escrow_instance.add_to_queue(
            offer_id=offer._id,
            from_address=from_address,
            amount_with_fee=offer["sum_fee_up"].to_decimal(),
            amount_without_fee=offer[f"sum_{offer.type}"].to_decimal(),
            asset=offer.escrow,
            memo=memo,
            transaction_time=transaction_time,
        )
    else:
        keyboard.add(
            InlineKeyboardButton(
                i18n("check", locale=escrow_user["locale"]),
                callback_data=f"check_transaction {offer._id}",
            )
        )
    keyboard.add(
        InlineKeyboardButton(
            i18n("cancel", locale=escrow_user["locale"]),
            callback_data=f"escrow_cancel {offer._id}",
        )
    )
    escrow_address = markdown.bold(escrow_instance.address)
    answer = i18n(
        "send {amount} {currency} {address}", locale=escrow_user["locale"]
    ).format(amount=offer.sum_fee_up, currency=offer.escrow, address=escrow_address)
    answer += " " + i18n("with_memo", locale=escrow_user["locale"])
    answer += ":\n" + markdown.code(memo)
    await tg.send_message(
        escrow_user["id"], answer, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN
    )
    if send_reply:
        keyboard = InlineKeyboardMarkup()
        keyboard.add(
            InlineKeyboardButton(
                i18n("cancel"), callback_data=f"escrow_cancel {offer._id}"
            )
        )
        await tg.send_message(
            message.chat.id,
            i18n("transfer_information_sent")
            + " "
            + i18n("transaction_completion_notification_promise"),
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN,
        )
    update = {
        "counter.send_address": address,
        "transaction_time": transaction_time,
        "memo": memo,
    }
    await offer.update_document(
        {"$set": update, "$unset": {"pending_input_from": True}}
    )
    await dp.current_state().finish()


@escrow_callback_handler(lambda call: call.data.startswith("escrow_cancel "))
async def cancel_offer(call: types.CallbackQuery, offer: EscrowOffer):
    """React to offer cancellation.

    While first party is transferring, second party can't cancel offer,
    because we can't be sure that first party hasn't already completed
    transfer before confirming.
    """
    if offer.trx_id:
        return await call.answer(i18n("cancel_after_transfer"))
    if offer.memo:
        if offer.type == "buy":
            escrow_user = offer.init
        elif offer.type == "sell":
            escrow_user = offer.counter
        if call.from_user.id != escrow_user["id"]:
            return await call.answer(i18n("cancel_before_verification"))
        escrow_instance = get_escrow_instance(offer.escrow)
        if isinstance(escrow_instance, StreamBlockchain):
            escrow_instance.remove_from_queue(offer._id)

    sell_answer = i18n("escrow_cancelled", locale=offer.init["locale"])
    buy_answer = i18n("escrow_cancelled", locale=offer.counter["locale"])
    offer.cancel_time = time()
    await offer.delete_document()
    await call.answer()
    await tg.send_message(offer.init["id"], sell_answer, reply_markup=start_keyboard())
    await tg.send_message(
        offer.counter["id"], buy_answer, reply_markup=start_keyboard()
    )
    sell_state = FSMContext(dp.storage, offer.init["id"], offer.init["id"])
    buy_state = FSMContext(dp.storage, offer.counter["id"], offer.counter["id"])
    await sell_state.finish()
    await buy_state.finish()


async def edit_keyboard(
    offer_id: ObjectId, chat_id: int, message_id: int, keyboard: InlineKeyboardMarkup
):
    """Edit inline keyboard markup of message.

    :param offer_id: Primary key value of offer document connected with message.
    :param chat_id: Telegram chat ID of message.
    :param message_id: Telegram ID of message.
    :param keyboard: New inline keyboard markup.
    """
    offer_document = await database.escrow.find_one({"_id": offer_id})
    if offer_document:
        await tg.edit_message_reply_markup(chat_id, message_id, reply_markup=keyboard)


@escrow_callback_handler(lambda call: call.data.startswith("tokens_sent "))
async def final_offer_confirmation(call: types.CallbackQuery, offer: EscrowOffer):
    """Ask not escrow asset receiver to confirm transfer."""
    if not offer.unsent:
        await call.answer(i18n("transfer_already_confirmed"))
        return
    await offer.update_document({"$unset": {"unsent": True}})

    if offer.type == "buy":
        confirm_user = offer.init
        other_user = offer.counter
        currency = offer.sell
    elif offer.type == "sell":
        confirm_user = offer.counter
        other_user = offer.init
        currency = offer.buy

    keyboard = InlineKeyboardMarkup()
    keyboard.add(
        InlineKeyboardButton(
            i18n("yes", locale=confirm_user["locale"]),
            callback_data=f"escrow_complete {offer._id}",
        )
    )
    reply = await tg.send_message(
        confirm_user["id"],
        i18n(
            "receiving_confirmation {currency} {user}", locale=confirm_user["locale"]
        ).format(currency=currency, user=other_user["send_address"]),
        reply_markup=keyboard,
    )
    keyboard.add(
        InlineKeyboardButton(
            i18n("no", locale=confirm_user["locale"]),
            callback_data=f"escrow_validate {offer._id}",
        )
    )
    await call_later(
        60 * 10,
        edit_keyboard,
        offer._id,
        confirm_user["id"],
        reply.message_id,
        keyboard,
    )
    await call.answer()
    await tg.send_message(
        other_user["id"],
        i18n(
            "complete_escrow_promise",
            locale=other_user["locale"],
        ),
        reply_markup=start_keyboard(),
    )


async def add_cashback(
    currency, amount, sum_fee_up, sum_fee_down, sender_user, recipient_user
):
    """Create cashback documents from escrow exchange."""
    fees = (
        (sum_fee_up - amount, sender_user, sender_user["send_address"]),
        (amount - sum_fee_down, recipient_user, recipient_user["receive_address"]),
    )
    cashback = []
    current_time = time()
    for fee, user, user_address in fees:
        if not fee:
            continue
        categories = (
            (rs.PERSONAL_CATEGORY, user["id"], user_address),
            (rs.REFERRED_CATEGORY, user.get("referrer"), None),
            (rs.REFERRED_BY_REFERALS_CATEGORY, user.get("referrer_of_referrer"), None),
        )
        for category, user_id, address in categories:
            if not user_id:
                break
            count = await database.users.count_documents({"referrer": user_id})
            if not count:
                continue
            document = {
                "id": user_id,
                "currency": currency,
                "amount": rs.bonus_coefficient(category, count) * fee,
                "time": current_time,
            }
            if address:
                document["address"] = address
            cashback.append(document)
    if cashback:
        await database.cashback.insert_many(cashback)


@escrow_callback_handler(lambda call: call.data.startswith("escrow_complete "))
@dp.async_task
async def complete_offer(call: types.CallbackQuery, offer: EscrowOffer):
    """Release escrow asset and finish exchange."""
    if offer.type == "buy":
        recipient_user = offer.counter
        sender_user = offer.init
        amount = offer.sum_buy.to_decimal()  # type: ignore
    elif offer.type == "sell":
        recipient_user = offer.init
        sender_user = offer.counter
        amount = offer.sum_sell.to_decimal()  # type: ignore

    sum_fee_up = offer.sum_fee_up.to_decimal()  # type: ignore
    sum_fee_down = offer.sum_fee_down.to_decimal()  # type: ignore

    await call.answer(i18n("escrow_completing"))
    escrow_instance = get_escrow_instance(offer.escrow)
    trx_url = await escrow_instance.transfer(
        recipient_user["receive_address"],
        sum_fee_down,
        offer.escrow,
        memo=create_memo(offer, transfer=True),
    )

    if sender_user["send_address"] != recipient_user["receive_address"]:
        add_cashback(
            offer.escrow, amount, sum_fee_up, sum_fee_down, sender_user, recipient_user
        )

    answer = i18n("escrow_completed", locale=sender_user["locale"])
    recipient_answer = i18n("escrow_completed", locale=recipient_user["locale"])
    recipient_answer += " " + markdown.link(
        i18n("escrow_sent {amount} {currency}", locale=recipient_user["locale"]).format(
            amount=amount, currency=offer.escrow
        ),
        trx_url,
    )
    await offer.delete_document()
    await tg.send_message(
        recipient_user["id"],
        recipient_answer,
        reply_markup=start_keyboard(),
        parse_mode=ParseMode.MARKDOWN,
    )
    await tg.send_message(sender_user["id"], answer, reply_markup=start_keyboard())


@escrow_callback_handler(lambda call: call.data.startswith("escrow_validate "))
async def validate_offer(call: types.CallbackQuery, offer: EscrowOffer):
    """Ask support for manual verification of exchange."""
    if offer.type == "buy":
        sender = offer.counter
        receiver = offer.init
        currency = offer.sell
    elif offer.type == "sell":
        sender = offer.init
        receiver = offer.counter
        currency = offer.buy

    escrow_instance = get_escrow_instance(offer.escrow)
    answer = "{0}\n{1} sender: {2}{3}\n{1} receiver: {4}{5}\nBank: {6}\nMemo: {7}"
    answer = answer.format(
        markdown.link("Unconfirmed escrow.", escrow_instance.trx_url(offer.trx_id)),
        currency,
        markdown.link(sender["mention"], User(id=sender["id"]).url),
        " ({})".format(sender["name"]) if "name" in sender else "",
        markdown.link(receiver["mention"], User(id=receiver["id"]).url),
        " ({})".format(receiver["name"]) if "name" in receiver else "",
        offer.bank,
        markdown.code(offer.memo),
    )
    await tg.send_message(config.SUPPORT_CHAT_ID, answer, parse_mode=ParseMode.MARKDOWN)
    await offer.delete_document()
    await call.answer()
    await tg.send_message(
        call.message.chat.id,
        i18n("request_validation_promise"),
        reply_markup=start_keyboard(),
    )
