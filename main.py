import asyncio
import logging
from telethon import TelegramClient
from telethon.sessions import StringSession
from config.config import Config
from utils.thanos import thanos_protect
from startup.startup import start_bot
from core.console import ColourFormatter

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

# --- Auto-start File Hosting Bot (background daemon thread) ------------------
# NOTE: imported AFTER Telethon client is initialized so the Telethon
# connection wins the race for the main Telegram socket. This significantly
# reduces "Fatal read error on socket transport" errors seen when both
# libraries try to grab sockets at the same moment.
#
# Set AUTOSTART_HOSTING_BOT=0 to disable.
try:
    import hosting_bot  # noqa: F401
except Exception as _hosting_err:
    logging.getLogger("CipherElite").warning(
        f"File Hosting Bot failed to start: {_hosting_err}"
    )
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    with client:
        client.loop.run_until_complete(start_bot(client))