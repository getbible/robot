# import the required modules
import asyncio
import json
from functools import wraps

# import the required Telegram modules
from telegram.constants import ChatAction


# define the send_action decorator
def send_action(action, delay=1):
    def decorator(func):
        @wraps(func)
        async def command_func(update, context, *args, **kwargs):
            await context.bot.send_chat_action(
                chat_id=update.effective_message.chat_id, action=action
            )
            await asyncio.sleep(delay)  # wait for the specified delay time
            return await func(update, context, *args, **kwargs)

        return command_func

    return decorator


send_typing_action = send_action(
    ChatAction.TYPING, delay=1
)
