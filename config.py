import os

from dotenv import load_dotenv

load_dotenv()

TELEGRAM_API_TOKEN = os.environ.get("TELEGRAM_API_TOKEN")
TRANSLATION = os.environ.get("TRANSLATION", 'kjv')
DEFAULT_VERSE = os.environ.get("DEFAULT_VERSE", '1 John 3:16')
GETBIBLE_URL = os.environ.get("GETBIBLE_URL", 'https://getBible.net/')
WELCOME_MESSAGE = os.environ.get("WELCOME_MESSAGE", "Welcome to the official getBible.net telegram bot.\n"
                                                    "/help for more info.")
HELP_MESSAGE = os.environ.get("HELP_MESSAGE", "*Available commands:*\n\n"
                                              "  _You can use a reference to get verses like:_\n"
                                              "- `/bible 1 John 3:16`\n"
                                              "- `/bible John 3:16-19;1 John 3:10-17`\n"
                                              "- `/bible Gen 1:1-5 codex`\n"
                                              "- `/bible Ps 1:1-5 aov`\n\n"
                                              "- /search: Search the Scriptures (soon..)\n"
                                              "- /help: To get this help message again")
