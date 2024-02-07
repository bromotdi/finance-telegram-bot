# Copyright (C) 2019, 2020  alfred richardsn
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
"""Handler modules with default and fallback handlers."""
import logging
import traceback

from aiogram import types
from aiogram.dispatcher.filters.state import any_state
from aiogram.utils import markdown
from aiogram.utils.exceptions import MessageNotModified

from src.bot import dp
from src.bot import tg
from src.config import config
from src.handlers import start_menu  # noqa: F401, noreorder
from src.handlers import creation  # noqa: F401
from src.handlers import escrow  # noqa: F401
from src.handlers import cashback  # noqa: F401
from src.handlers import order  # noqa: F401
from src.handlers import support  # noqa: F401
from src.handlers.base import private_handler
from src.handlers.base import start_keyboard
from src.i18n import i18n

log = logging.getLogger(__name__)


@private_handler(state=any_state)
async def default_message(message: types.Message):
    """React to message which has not passed any previous conditions."""
    await tg.send_message(
        message.chat.id, i18n("unknown_command"), reply_markup=start_keyboard()
    )


@dp.callback_query_handler(state=any_state)
async def default_callback_query(call: types.CallbackQuery):
    """React to query which has not passed any previous conditions.

    If callback query is not answered, button will stuck in loading as
    if the bot stopped working until it times out. So unknown buttons
    are better be answered accordingly.
    """
    await call.answer(i18n("unknown_button"))


@dp.errors_handler()
async def errors_handler(update: types.Update, exception: Exception):
    """Handle exceptions when calling handlers.

    Send error notification to special chat and warn user about the error.
    """
    if isinstance(exception, MessageNotModified):
        return True

    log.error("Error handling request {}".format(update.update_id), exc_info=True)

    chat_id = None
    if update.message:
        update_type = "message"
        from_user = update.message.from_user
        chat_id = update.message.chat.id
    if update.callback_query:
        update_type = "callback query"
        from_user = update.callback_query.from_user
        chat_id = update.callback_query.message.chat.id

    if chat_id is not None:
        try:
            exceptions_chat_id = config.EXCEPTIONS_CHAT_ID
        except AttributeError:
            pass
        else:
            await tg.send_message(
                exceptions_chat_id,
                "Error handling {} {} from {} ({}) in chat {}\n{}".format(
                    update_type,
                    update.update_id,
                    markdown.link(from_user.mention, from_user.url),
                    from_user.id,
                    chat_id,
                    markdown.code(traceback.format_exc(limit=-3)),
                ),
                parse_mode=types.ParseMode.MARKDOWN,
            )
        await tg.send_message(
            chat_id,
            i18n("unexpected_error"),
            reply_markup=start_keyboard(),
        )

    return True
