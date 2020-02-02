# import the Telegram API token from config.py
from config import TELEGRAM_API_TOKEN

import logging

# import the required Telegram modules
from telegram.ext import (
    ApplicationBuilder,
    filters,
    CommandHandler,
    MessageHandler
)

# import the required module
from modules.commands import *

logging.basicConfig(level=logging.WARN)

if __name__ == '__main__':
    # we start the bot application
    application = ApplicationBuilder().token(TELEGRAM_API_TOKEN).build()
    # entry command for all private chats
    application.add_handler(CommandHandler('start', start))
    # all handlers to get the Bible text [/get, /getBible, /Bible]
    application.add_handler(CommandHandler('get', get))
    application.add_handler(CommandHandler('getbible', get))
    application.add_handler(CommandHandler('bible', get))
    # the search handle
    application.add_handler(CommandHandler('search', search))
    # the help handle
    application.add_handler(CommandHandler('help', bot_help))
    # add a message handler to handle unknown commands or messages
    application.add_handler(MessageHandler(filters.ALL, unknown))
    # start polling for now (will add webhook later)
    application.run_polling()
