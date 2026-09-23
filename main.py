import asyncio
import logging
from telethon import TelegramClient
from telethon.sessions import StringSession
from config.config import Config
from utils.thanos import thanos_protect
from startup.startup import start_bot
from core.console import ColourFormatter

# --- Auto-start File Hosting Bot (background daemon thread) ------------------
# Safe to import here: hosting_bot.py checks HOST_BOT_TOKEN env var and
# starts its own polling thread. Set AUTOSTART_HOSTING_BOT=0 to disable.
try:
    import hosting_bot  # noqa: F401
except Exception as _hosting_err:
    logging.getLogger("CipherElite").warning(
        f"File Hosting Bot failed to start: {_hosting_err}"
    )
# -----------------------------------------------------------------------------

logging.basicConfig(
    level=logging.WARNING,
    handlers=[logging.StreamHandler()]
)
logging.getLogger().handlers[0].setFormatter(ColourFormatter("cipherelite")())

# Initialize Telegram client
eliteses = thanos_protect(Config.STRING_SESSION)
client = TelegramClient(
    StringSession(eliteses),
    Config.API_ID,
    Config.API_HASH
)

if __name__ == "__main__":
    with client:
        client.loop.run_until_complete(start_bot(client))