# import the Telegram API token from config.py
from config import (
    HELP_MESSAGE,
    WELCOME_MESSAGE
)
# import the required Telegram modules
from telegram import Update
from telegram.constants import ParseMode, MessageLimit
from telegram.ext import (
    ContextTypes,
)
import logging

# import the required module
from modules.get_bible import bible
from modules.utils import send_typing_action


# handle unknown command
async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return None


# entry command
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=WELCOME_MESSAGE
    )

    await context.bot.delete_message(
        chat_id=update.effective_chat.id,
        message_id=update.message.message_id
    )


# send a typing indicator in the chat
@send_typing_action
# handle the getting of the Bible text
async def get(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Return the referenced scripture, shortened if it's too long."""
    scripture = bible(update.message.text)

    # Check if scripture length exceeds the maximum allowed message length
    if len(scripture) > MessageLimit.MAX_TEXT_LENGTH:
        notice = "...\n\n..._response was shortened due to Telegram message length limitation_"
        # Shorten scripture to fit within limits with space for notice
        text_to_send = scripture[:MessageLimit.MAX_TEXT_LENGTH - len(notice)] + notice
    else:
        text_to_send = scripture

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=text_to_send,
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True
    )

    await context.bot.delete_message(
        chat_id=update.effective_chat.id,
        message_id=update.message.message_id
    )


# send a typing indicator in the chat
@send_typing_action
# handle search command
async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info(f"Searching for: {update.message.text}")
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=("The search function for Telegram is not yet finished.\n\n"
              "You can search the Scriptures at https://getBible.life/search")
    )
    await context.bot.delete_message(
        chat_id=update.effective_chat.id,
        message_id=update.message.id
    )


# help command
async def bot_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        parse_mode=ParseMode.MARKDOWN,
        text=HELP_MESSAGE
    )
    await context.bot.delete_message(
        chat_id=update.effective_chat.id,
        message_id=update.message.id
    )
