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
"""Handlers for showing orders and reacting to query buttons attached to them."""
import typing
from decimal import Decimal
from functools import wraps
from time import time

import pymongo
from aiogram import types
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import any_state
from aiogram.utils.exceptions import MessageCantBeDeleted
from aiogram.utils.exceptions import MessageNotModified
from bson.decimal128 import Decimal128
from bson.objectid import ObjectId
from motor.core import AgnosticBaseCursor as Cursor

from src import money
from src import states
from src.bot import dp
from src.bot import tg
from src.config import config
from src.database import database
from src.database import database_user
from src.escrow import get_escrow_instance
from src.escrow.escrow_offer import EscrowOffer
from src.handlers.base import orders_list
from src.handlers.base import private_handler
from src.handlers.base import show_order
from src.i18n import i18n
from src.i18n import plural_i18n

OrderType = typing.Mapping[str, typing.Any]


def order_handler(
    handler: typing.Callable[[types.CallbackQuery, OrderType], typing.Any]
):
    """Simplify handling callback queries attached to order.

    Add order of ``OrderType`` to arguments of ``handler``.
    """

    @wraps(handler)
    async def decorator(call: types.CallbackQuery):
        order_id = call.data.split()[1]
        order = await database.orders.find_one({"_id": ObjectId(order_id)})
        if not order:
            await call.answer(i18n("order_not_found"))
            return

        return await handler(call, order)

    return decorator


async def show_orders(
    call: types.CallbackQuery,
    cursor: Cursor,
    start: int,
    quantity: int,
    buttons_data: str,
    invert: bool,
    user_id: typing.Optional[int] = None,
):
    """Send list of orders.

    :param cursor: Cursor of MongoDB query to orders.
    :param chat_id: Telegram chat ID.
    :param start: Start index.
    :param quantity: Quantity of orders in cursor.
    :param buttons_data: Beginning of callback data of left/right buttons.
    :param user_id: If cursor is user-specific, Telegram ID of user
        who created all orders in cursor.
    :param invert: Invert all prices.
    """
    if start >= quantity > 0:
        await call.answer(i18n("no_more_orders"))
        return

    try:
        await call.answer()
        await orders_list(
            cursor,
            call.message.chat.id,
            start,
            quantity,
            buttons_data,
            user_id=user_id,
            message_id=call.message.message_id,
            invert=invert,
        )
    except MessageNotModified:
        await call.answer(i18n("no_previous_orders"))


@dp.callback_query_handler(
    lambda call: call.data.startswith("get_order "), state=any_state
)
@order_handler
async def get_order_button(call: types.CallbackQuery, order: OrderType):
    """Choose order from order book."""
    await call.answer()
    await show_order(order, call.message.chat.id, call.from_user.id, show_id=True)


@private_handler(commands=["id"])
@private_handler(regexp="(ID: )?[a-f0-9]{24}")
async def get_order_command(message: types.Message):
    """Get order from ID.

    Order ID is indicated after **/id** or **ID:** in message text.
    """
    args = message.text.split()
    order_id = args[1] if len(args) > 1 else args[0]
    order = await database.orders.find_one({"_id": ObjectId(order_id)})
    if not order:
        await tg.send_message(message.chat.id, i18n("order_not_found"))
        return
    await show_order(order, message.chat.id, message.from_user.id)


@dp.callback_query_handler(
    lambda call: call.data.startswith(("invert ", "revert ")), state=any_state
)
@order_handler
async def invert_button(call: types.CallbackQuery, order: OrderType):
    """React to invert button query."""
    args = call.data.split()

    invert = args[0] == "invert"
    location_message_id = int(args[2])
    edit = bool(int(args[3]))
    show_id = call.message.text.startswith("ID")

    await call.answer()
    await show_order(
        order,
        call.message.chat.id,
        call.from_user.id,
        message_id=call.message.message_id,
        location_message_id=location_message_id,
        show_id=show_id,
        invert=invert,
        edit=edit,
    )


async def aggregate_orders(buy: str, sell: str) -> typing.Tuple[Cursor, int]:
    """Aggregate and query orders with specified currency pair.

    Return cursor with unexpired orders sorted by price and creation
    time and quantity of documents in it.
    """
    query = {
        "buy": money.gateway_currency_regexp(buy),
        "sell": money.gateway_currency_regexp(sell),
        "$or": [{"archived": {"$exists": False}}, {"archived": False}],
        "expiration_time": {"$gt": time()},
    }
    cursor = database.orders.aggregate(
        [
            {"$match": query},
            {"$addFields": {"price_buy": {"$ifNull": ["$price_buy", ""]}}},
            {
                "$sort": {
                    "price_buy": pymongo.ASCENDING,
                    "start_time": pymongo.DESCENDING,
                }
            },
        ]
    )
    quantity = await database.orders.count_documents(query)
    return cursor, quantity


@dp.callback_query_handler(
    lambda call: call.data.startswith("orders "), state=any_state
)
async def orders_button(call: types.CallbackQuery):
    """React to left/right button query in order book."""
    query = {
        "$and": [
            {
                "$or": [
                    {"expiration_time": {"$exists": False}},
                    {"expiration_time": {"$gt": time()}},
                ]
            },
            {"$or": [{"archived": {"$exists": False}}, {"archived": False}]},
        ]
    }
    cursor = database.orders.find(query).sort("start_time", pymongo.DESCENDING)
    quantity = await database.orders.count_documents(query)

    args = call.data.split()
    start = max(0, int(args[1]))
    invert = bool(int(args[2]))
    await show_orders(
        call, cursor, start, quantity, "orders", invert, user_id=call.from_user.id
    )


@dp.callback_query_handler(
    lambda call: call.data.startswith("my_orders "), state=any_state
)
async def my_orders_button(call: types.CallbackQuery):
    """React to left/right button query in list of user's orders."""
    query = {"user_id": call.from_user.id}
    cursor = database.orders.find(query).sort("start_time", pymongo.DESCENDING)
    quantity = await database.orders.count_documents(query)

    args = call.data.split()
    start = max(0, int(args[1]))
    invert = bool(int(args[2]))
    await show_orders(call, cursor, start, quantity, "my_orders", invert)


@dp.callback_query_handler(
    lambda call: call.data.startswith("matched_orders "), state=any_state
)
async def matched_orders_button(call: types.CallbackQuery):
    """React to left/right button query in list of orders matched by currency pair."""
    args = call.data.split()
    start = max(0, int(args[3]))
    invert = bool(int(args[4]))
    cursor, quantity = await aggregate_orders(args[1], args[2])
    await call.answer()
    await show_orders(
        call,
        cursor,
        start,
        quantity,
        "matched_orders {} {}".format(args[1], args[2]),
        invert,
        user_id=call.from_user.id,
    )


@dp.callback_query_handler(
    lambda call: call.data.startswith("similar "), state=any_state
)
@order_handler
async def similar_button(call: types.CallbackQuery, order: OrderType):
    """React to "Similar" button by sending list of similar orders.

    Similar orders are ones that have the same currency pair.
    """
    cursor, quantity = await aggregate_orders(order["buy"], order["sell"])
    await call.answer()
    await orders_list(
        cursor,
        call.message.chat.id,
        0,
        quantity,
        "matched_orders {} {}".format(order["buy"], order["sell"]),
        user_id=call.from_user.id,
    )


@dp.callback_query_handler(lambda call: call.data.startswith("match "), state=any_state)
@order_handler
async def match_button(call: types.CallbackQuery, order: OrderType):
    """React to "Match" button by sending list of matched orders.

    Matched orders are ones that have the inverted currency pair.
    """
    cursor, quantity = await aggregate_orders(order["sell"], order["buy"])
    await call.answer()
    await orders_list(
        cursor,
        call.message.chat.id,
        0,
        quantity,
        "matched_orders {} {}".format(order["sell"], order["buy"]),
        user_id=call.from_user.id,
    )


@dp.callback_query_handler(
    lambda call: call.data.startswith("escrow "), state=any_state
)
@order_handler
async def escrow_button(call: types.CallbackQuery, order: OrderType):
    """React to "Escrow" button by starting escrow exchange."""
    if not config.ESCROW_ENABLED:
        await call.answer(i18n("escrow_unavailable"))
        return

    if call.from_user.id == order["user_id"]:
        await call.answer(i18n("escrow_starting_error"))
        return

    args = call.data.split()
    currency_arg = args[2]
    edit = bool(int(args[3]))

    if currency_arg == "sum_buy":
        sum_currency = order["buy"]
        new_currency = order["sell"]
        new_currency_arg = "sum_sell"
    elif currency_arg == "sum_sell":
        sum_currency = order["sell"]
        new_currency = order["buy"]
        new_currency_arg = "sum_buy"
    else:
        return

    keyboard = types.InlineKeyboardMarkup()
    keyboard.row(
        types.InlineKeyboardButton(
            i18n("change_to {currency}").format(currency=new_currency),
            callback_data="escrow {} {} 1".format(order["_id"], new_currency_arg),
        )
    )
    answer = i18n("send_exchange_sum {currency}").format(currency=sum_currency)
    if edit:
        cancel_data = call.message.reply_markup.inline_keyboard[1][0].callback_data
        keyboard.row(
            types.InlineKeyboardButton(i18n("cancel"), callback_data=cancel_data)
        )
        await database.escrow.update_one(
            {"pending_input_from": call.from_user.id},
            {"$set": {"sum_currency": currency_arg}},
        )
        await call.answer()
        await tg.edit_message_text(
            answer, call.message.chat.id, call.message.message_id, reply_markup=keyboard
        )
    else:
        offer_id = ObjectId()
        keyboard.row(
            types.InlineKeyboardButton(
                i18n("cancel"), callback_data=f"init_cancel {offer_id}"
            )
        )
        escrow_type = "buy" if get_escrow_instance(order["buy"]) else "sell"
        user = database_user.get()
        init_user = {
            "id": user["id"],
            "locale": user["locale"],
            "mention": user["mention"],
        }
        if "referrer" in user:
            init_user["referrer"] = user["referrer"]
        counter_user = await database.users.find_one(
            {"id": order["user_id"]},
            projection={
                "id": True,
                "locale": True,
                "mention": True,
                "referrer": True,
                "referrer_of_referrer": True,
            },
        )
        await database.escrow.delete_many({"init.send_address": {"$exists": False}})
        offer = EscrowOffer(
            **{
                "_id": offer_id,
                "order": order["_id"],
                "buy": order["buy"],
                "sell": order["sell"],
                "type": escrow_type,
                "escrow": order[escrow_type],
                "time": time(),
                "sum_currency": currency_arg,
                "init": init_user,
                "counter": counter_user,
                "pending_input_from": call.message.chat.id,
            }
        )
        await offer.insert_document()
        await call.answer()
        await tg.send_message(call.message.chat.id, answer, reply_markup=keyboard)
        await states.Escrow.amount.set()


@dp.callback_query_handler(lambda call: call.data.startswith("edit "), state=any_state)
async def edit_button(call: types.CallbackQuery):
    """React to "Edit" button by entering edit mode on order."""
    args = call.data.split()

    order_id = args[1]
    order = await database.orders.find_one(
        {"_id": ObjectId(order_id), "user_id": call.from_user.id}
    )
    if not order:
        await call.answer(i18n("edit_order_error"))
        return

    field = args[2]

    keyboard = types.InlineKeyboardMarkup()
    unset_button = types.InlineKeyboardButton(i18n("unset"), callback_data="unset")

    if field == "sum_buy":
        answer = i18n("send_new_buy_amount")
        keyboard.row(unset_button)
    elif field == "sum_sell":
        answer = i18n("send_new_sell_amount")
        keyboard.row(unset_button)
    elif field == "price":
        user = database_user.get()
        answer = i18n("new_price {of_currency} {per_currency}")
        if user.get("invert_order", False):
            answer = answer.format(of_currency=order["buy"], per_currency=order["sell"])
        else:
            answer = answer.format(of_currency=order["sell"], per_currency=order["buy"])
        keyboard.row(unset_button)
    elif field == "payment_system":
        answer = i18n("send_new_payment_system")
        keyboard.row(unset_button)
    elif field == "duration":
        answer = i18n("send_new_duration {limit}").format(
            limit=config.ORDER_DURATION_LIMIT
        )
        keyboard.row(
            types.InlineKeyboardButton(
                plural_i18n(
                    "repeat_duration_singular {days}",
                    "repeat_duration_plural {days}",
                    order["duration"],
                ).format(days=order["duration"]),
                callback_data="default_duration",
            )
        )
    elif field == "comments":
        answer = i18n("send_new_comments")
        keyboard.row(unset_button)
    else:
        answer = None

    await call.answer()
    if not answer:
        return

    user = database_user.get()
    if "edit" in user:
        await tg.delete_message(call.message.chat.id, user["edit"]["message_id"])
    result = await tg.send_message(
        call.message.chat.id,
        answer,
        reply_markup=keyboard,
    )
    await database.users.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "edit.order_message_id": call.message.message_id,
                "edit.message_id": result.message_id,
                "edit.order_id": order["_id"],
                "edit.field": field,
                "edit.location_message_id": int(args[3]),
                "edit.one_time": bool(int(args[4])),
                "edit.show_id": call.message.text.startswith("ID"),
                "state": states.field_editing.state,
            }
        },
    )


async def finish_edit(user, update_dict):
    """Update and show order after editing."""
    edit = user["edit"]
    result = await database.orders.update_one({"_id": edit["order_id"]}, update_dict)
    if result.modified_count:
        order = await database.orders.find_one({"_id": edit["order_id"]})
        try:
            await show_order(
                order,
                user["chat"],
                user["id"],
                message_id=edit["order_message_id"],
                location_message_id=edit["location_message_id"],
                show_id=edit["show_id"],
                edit=not edit["one_time"],
            )
        except MessageNotModified:
            pass
    await database.users.update_one(
        {"_id": user["_id"]}, {"$unset": {"edit": True, "state": True}}
    )


@dp.callback_query_handler(
    lambda call: call.data == "default_duration", state=states.field_editing
)
async def default_duration(call: types.CallbackQuery, state: FSMContext):
    """Repeat default duration."""
    user = database_user.get()
    order = await database.orders.find_one({"_id": user["edit"]["order_id"]})
    await call.answer()
    await finish_edit(
        user,
        {
            "$set": {
                "expiration_time": time() + order["duration"] * 24 * 60 * 60,
                "notify": True,
            }
        },
    )
    try:
        await tg.delete_message(user["chat"], user["edit"]["message_id"])
    except MessageCantBeDeleted:
        return


@dp.callback_query_handler(
    lambda call: call.data == "unset", state=states.field_editing
)
async def unset_button(call: types.CallbackQuery, state: FSMContext):
    """React to "Unset" button by unsetting the edit field."""
    user = database_user.get()
    field = user["edit"]["field"]
    if field == "price":
        unset_dict = {"price_buy": True, "price_sell": True}
    else:
        unset_dict = {field: True}
    await call.answer()
    await finish_edit(user, {"$unset": unset_dict})
    try:
        await tg.delete_message(user["chat"], user["edit"]["message_id"])
    except MessageCantBeDeleted:
        return


@private_handler(state=states.field_editing)
async def edit_field(message: types.Message, state: FSMContext):
    """Ask new value of chosen order's field during editing."""
    user = database_user.get()
    edit = user["edit"]
    field = edit["field"]
    invert = user.get("invert_order", False)
    set_dict = {}
    error = None

    if field == "sum_buy":
        try:
            transaction_sum = money.money(message.text)
        except money.MoneyValueError as exception:
            error = str(exception)
        else:
            order = await database.orders.find_one({"_id": edit["order_id"]})
            set_dict["sum_buy"] = Decimal128(transaction_sum)
            if "price_sell" in order:
                set_dict["sum_sell"] = Decimal128(
                    money.normalize(transaction_sum * order["price_sell"].to_decimal())
                )

    elif field == "sum_sell":
        try:
            transaction_sum = money.money(message.text)
        except money.MoneyValueError as exception:
            error = str(exception)
        else:
            order = await database.orders.find_one({"_id": edit["order_id"]})
            set_dict["sum_sell"] = Decimal128(transaction_sum)
            if "price_buy" in order:
                set_dict["sum_buy"] = Decimal128(
                    money.normalize(transaction_sum * order["price_buy"].to_decimal())
                )

    elif field == "price":
        try:
            price = money.money(message.text)
        except money.MoneyValueError as exception:
            error = str(exception)
        else:
            order = await database.orders.find_one({"_id": edit["order_id"]})

            if invert:
                price_sell = money.normalize(Decimal(1) / price)
                set_dict["price_buy"] = Decimal128(price)
                set_dict["price_sell"] = Decimal128(price_sell)

                if order.get("sum_currency") == "buy":
                    set_dict["sum_sell"] = Decimal128(
                        money.normalize(order["sum_buy"].to_decimal() * price_sell)
                    )
                elif "sum_sell" in order:
                    set_dict["sum_buy"] = Decimal128(
                        money.normalize(order["sum_sell"].to_decimal() * price)
                    )
            else:
                price_buy = money.normalize(Decimal(1) / price)
                set_dict["price_buy"] = Decimal128(price_buy)
                set_dict["price_sell"] = Decimal128(price)

                if order.get("sum_currency") == "sell":
                    set_dict["sum_buy"] = Decimal128(
                        money.normalize(order["sum_sell"].to_decimal() * price_buy)
                    )
                elif "sum_buy" in order:
                    set_dict["sum_sell"] = Decimal128(
                        money.normalize(order["sum_buy"].to_decimal() * price)
                    )

    elif field == "payment_system":
        payment_system = message.text.replace("\n", " ")
        if len(payment_system) >= 150:
            await tg.send_message(
                message.chat.id,
                i18n("exceeded_character_limit {limit} {sent}").format(
                    limit=150, sent=len(payment_system)
                ),
            )
            return
        set_dict["payment_system"] = payment_system

    elif field == "duration":
        try:
            duration = int(message.text)
            if duration <= 0:
                raise ValueError
        except ValueError:
            error = i18n("send_natural_number")
        else:
            if duration > config.ORDER_DURATION_LIMIT:
                error = i18n("exceeded_duration_limit {limit}").format(
                    limit=config.ORDER_DURATION_LIMIT
                )
            else:
                order = await database.orders.find_one({"_id": edit["order_id"]})
                set_dict["duration"] = duration
                set_dict["expiration_time"] = time() + duration * 24 * 60 * 60
                set_dict["notify"] = True

    elif field == "comments":
        comments = message.text
        if len(comments) >= 150:
            await tg.send_message(
                message.chat.id,
                i18n("exceeded_character_limit {limit} {sent}").format(
                    limit=150, sent=len(comments)
                ),
            )
            return
        set_dict["comments"] = comments

    if set_dict:
        await finish_edit(user, {"$set": set_dict})
        await message.delete()
        try:
            await tg.delete_message(user["chat"], edit["message_id"])
        except MessageCantBeDeleted:
            return
    elif error:
        await message.delete()
        await tg.edit_message_text(error, message.chat.id, edit["message_id"])


@dp.callback_query_handler(
    lambda call: call.data.startswith("archive "), state=any_state
)
@order_handler
async def archive_button(call: types.CallbackQuery, order: OrderType):
    """React to "Archive" or "Unarchive" button by flipping archived flag."""
    args = call.data.split()

    archived = order.get("archived")
    if archived:
        update_dict = {"$unset": {"archived": True}, "$set": {"notify": True}}
    else:
        update_dict = {"$set": {"archived": True, "notify": False}}
    order = await database.orders.find_one_and_update(
        {"_id": ObjectId(args[1]), "user_id": call.from_user.id},
        update_dict,
        return_document=pymongo.ReturnDocument.AFTER,
    )
    if not order:
        await call.answer(
            i18n("unarchive_order_error") if archived else i18n("archive_order_error")
        )
        return

    await call.answer()
    await show_order(
        order,
        call.message.chat.id,
        call.from_user.id,
        message_id=call.message.message_id,
        location_message_id=int(args[2]),
        show_id=call.message.text.startswith("ID"),
    )


@dp.callback_query_handler(
    lambda call: call.data.startswith("delete "), state=any_state
)
@order_handler
async def delete_button(call: types.CallbackQuery, order: OrderType):
    """React to "Delete" button by asking user to confirm deletion."""
    args = call.data.split()
    location_message_id = int(args[2])
    show_id = call.message.text.startswith("ID")

    keyboard = types.InlineKeyboardMarkup()
    keyboard.row(
        types.InlineKeyboardButton(
            i18n("totally_sure"),
            callback_data="confirm_delete {} {}".format(
                order["_id"], location_message_id
            ),
        )
    )
    keyboard.row(
        types.InlineKeyboardButton(
            i18n("no"),
            callback_data="revert {} {} 0 {}".format(
                order["_id"], location_message_id, int(show_id)
            ),
        )
    )

    await tg.edit_message_text(
        i18n("delete_order_confirmation"),
        call.message.chat.id,
        call.message.message_id,
        reply_markup=keyboard,
    )


@dp.callback_query_handler(
    lambda call: call.data.startswith("confirm_delete "), state=any_state
)
async def confirm_delete_button(call: types.CallbackQuery):
    """Delete order after confirmation button query."""
    order_id = call.data.split()[1]
    order = await database.orders.find_one_and_delete(
        {"_id": ObjectId(order_id), "user_id": call.from_user.id}
    )
    if not order:
        await call.answer(i18n("delete_order_error"))
        return

    location_message_id = int(call.data.split()[2])
    keyboard = types.InlineKeyboardMarkup()
    keyboard.row(
        types.InlineKeyboardButton(
            i18n("hide"), callback_data="hide {}".format(location_message_id)
        )
    )
    await tg.edit_message_text(
        i18n("order_deleted"),
        call.message.chat.id,
        call.message.message_id,
        reply_markup=keyboard,
    )


@dp.callback_query_handler(lambda call: call.data.startswith("hide "), state=any_state)
async def hide_button(call: types.CallbackQuery):
    """React to "Hide" button by deleting messages with location object and order."""
    try:
        await call.message.delete()
    except MessageCantBeDeleted:
        await call.answer(i18n("hide_order_error"))
        return
    location_message_id = call.data.split()[1]
    if location_message_id != "-1":
        await tg.delete_message(call.message.chat.id, location_message_id)
