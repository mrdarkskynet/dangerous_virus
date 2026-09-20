# =============================================================================
#  CipherElite Userbot Plugin
#
#  Plugin Name:    autoreact
#  Author:         CipherElite Dev (@rishabhops)
#  Repository:     https://github.com/rishabhops/CipherElite
#
#  License:        MIT
#
#  IMPORTANT:
#    • If you copy, fork, or include this plugin in your own bot,
#      you MUST keep this header intact.
#    • You MUST give proper credit to the CipherElite Userbot author:
#        – GitHub:    https://github.com/rishabhops/CipherElite
#        – Telegram:  @thanosceo
#
#  What it does:
#    Auto-reacts to incoming messages with ONE random emoji picked from every
#    reaction Telegram currently offers. The live list is fetched from
#    messages.getAvailableReactions, so new reactions Telegram adds later are
#    picked up automatically — the hardcoded list below is only a fallback.
#
#  Thank you for respecting open-source software!
# =============================================================================

VERSION = "1.0.0"
CATEGORY = "utilities"

import json
import os
import random
import time
from telethon import events
from telethon.tl import functions, types
from telethon.tl.types import (
    Channel,
    Chat,
    Message,
    MessageEmpty,
    MessageService,
)
from utils.utils import CipherElite
from utils.decorators import rishabh
from plugins.bot import add_handler

# -----------------------------------------------------------------------------
#  Settings
# -----------------------------------------------------------------------------
STATE_FILE = "autoreact.json"     # persists per-chat settings across restarts
LIVE_TTL = 6 * 60 * 60            # re-fetch the global reaction list every 6h
CHAT_TTL = 10 * 60                # re-check a chat's allowed reactions every 10m
DEFAULT_MODE = "all"              # all | normal | premium
DEFAULT_INTERVAL = 1.5            # minimum seconds between two reactions
BIG_ANIMATION_CHANCE = 0.10       # 10% of reactions use the big animation
VALID_MODES = ("all", "normal", "premium")
MAX_LOG_ENTRIES = 15

# Fallback emoji set — used ONLY when messages.getAvailableReactions fails
# (offline, FloodWait, old layer). The live fetch is always preferred.
FALLBACK_REACTIONS = (
    "👍", "👎", "❤", "🔥", "🥰", "👏", "😁", "🤔", "🤯", "😱",
    "🤬", "😢", "🎉", "🤩", "🤮", "💩", "🙏", "👌", "🕊", "🤡",
    "🥱", "🥴", "😍", "🐳", "❤️‍🔥", "🌚", "🌭", "💯", "🤣", "⚡",
    "🍌", "😐", "🍾", "💋", "🙈", "😴", "😭", "🤓", "👻", "👨‍💻",
    "👀", "🎃", "🙉", "😇", "😨", "🤝", "✍", "🤗", "🫡", "🎅",
    "🎄", "☃", "💅", "🤪", "🗿", "🆒", "💘", "🙊", "🦄", "😘",
    "💊", "😎", "👾", "🤷‍♂", "🤷", "🤷‍♀", "😡",
)

# -----------------------------------------------------------------------------
#  In-memory state
# -----------------------------------------------------------------------------
STATE = {"chats": {}}
_LIVE_CACHE = {"ts": 0.0, "items": None}     # [(emoji, is_premium), ...]
_CHAT_CACHE = {}                             # key -> (ts, allowed_set or None)
_LAST_SENT = {}                              # key -> timestamp of last reaction
_SENT_LOG = []                               # [(timestamp, chat_key, emoji), ...]


# -----------------------------------------------------------------------------
#  Persistence
# -----------------------------------------------------------------------------

def _load_state():
    """Read per-chat settings from disk. Never raises — a bad file is ignored."""
    global STATE
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("chats"), dict):
                STATE = data
    except Exception:
        STATE = {"chats": {}}


def _save_state():
    """Write settings atomically so a crash mid-write cannot corrupt the file."""
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(STATE, f, indent=2, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except Exception:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass


def _chat_entry(key):
    """Return (and create) the settings dict for one chat."""
    entry = STATE["chats"].get(str(key))
    if entry is None:
        entry = {"enabled": False, "mode": DEFAULT_MODE, "interval": DEFAULT_INTERVAL, "count": 0}
        STATE["chats"][str(key)] = entry
    return entry


# -----------------------------------------------------------------------------
#  Keys & helpers
# -----------------------------------------------------------------------------

def _key_of(event):
    """A stable string key for the chat an event belongs to."""
    peer_id = getattr(event, "peer_id", None)
    if peer_id is not None:
        return str(getattr(peer_id, "channel_id", None)
                   or getattr(peer_id, "chat_id", None)
                   or getattr(peer_id, "user_id", None))
    return str(getattr(event, "chat_id", 0))


def _client_of(event):
    """The client to send API calls with (falls back to the userbot client)."""
    return getattr(event, "client", None) or CipherElite


def _chat_name(event):
    chat = getattr(event, "chat", None)
    return getattr(chat, "title", None) or getattr(chat, "first_name", None) or _key_of(event)


def _sender_id(event):
    return getattr(getattr(event, "sender_id", None), "__int__", lambda: None)() \
        if hasattr(event, "sender_id") else None


# -----------------------------------------------------------------------------
#  Reaction pools
# -----------------------------------------------------------------------------

async def _get_live_reactions(client):
    """
    Fetch every reaction Telegram currently supports.

    Returns [(emoji, is_premium), ...] or None when the call fails.
    Cached for LIVE_TTL seconds — Telegram adds new reactions over time and this
    picks them up without a plugin update.
    """
    now = time.time()
    if _LIVE_CACHE["items"] is not None and now - _LIVE_CACHE["ts"] < LIVE_TTL:
        return _LIVE_CACHE["items"]

    try:
        res = await client(functions.messages.GetAvailableReactionsRequest(hash=0))
        raw = getattr(res, "reactions", None)
        if raw:
            items = []
            for r in raw:
                emoji = getattr(r, "reaction", None)
                if emoji:
                    items.append((emoji, bool(getattr(r, "premium", False)),
                                  bool(getattr(r, "inactive", False))))
            if items:
                _LIVE_CACHE.update(ts=now, items=items)
                return items
    except Exception:
        pass
    return _LIVE_CACHE["items"]


def _fallback_items():
    return [(e, False, False) for e in FALLBACK_REACTIONS]


async def _chat_allowed(event):
    """
    Return the set of emoji this chat allows, or None when the chat allows all
    reactions (channels/groups can restrict the list). Cached per chat.
    """
    key = _key_of(event)
    now = time.time()
    cached = _CHAT_CACHE.get(key)
    if cached and now - cached[0] < CHAT_TTL:
        return cached[1]

    allowed = None
    try:
        client = _client_of(event)
        chat = getattr(event, "chat", None)
        if isinstance(chat, Channel):
            res = await client(functions.channels.GetFullChannelRequest(channel=chat))
        elif isinstance(chat, Chat):
            res = await client(functions.messages.GetFullChatRequest(chat_id=chat.id))
        else:
            res = None

        full = getattr(res, "full_chat", None)
        if full is not None and hasattr(full, "available_reactions"):
            ar = getattr(full, "available_reactions", None)
            if isinstance(ar, types.ChatReactionsNone):
                allowed = set()
            elif isinstance(ar, types.ChatReactionsSome):
                allowed = {getattr(r, "emoticon", None) for r in (ar.reactions or [])}
                allowed.discard(None)
            else:                     # ChatReactionsAll or unset -> everything
                allowed = None
    except Exception:
        allowed = None

    _CHAT_CACHE[key] = (now, allowed)
    return allowed


def _build_pool(mode, allowed, items):
    """
    Apply the mode filter and the chat restriction to a reaction list.

    The chat restriction is a hard limit: when it filters everything out the
    pool comes back EMPTY and the caller must not react — falling back to the
    global list there would send an emoji the chat rejects.
    """
    pool = [(e, p) for (e, p, inactive) in items if not inactive]

    if mode == "normal":
        pool = [x for x in pool if not x[1]] or pool
    elif mode == "premium":
        pool = [x for x in pool if x[1]] or pool

    if allowed is not None:
        return [x for x in pool if x[0] in allowed]

    # no chat restriction -> the mode pool, or the raw list as a last resort
    return pool or [(e, False) for e in FALLBACK_REACTIONS]


async def pick_reaction(event, mode=DEFAULT_MODE):
    """
    Pick ONE random emoji out of every reaction currently available.
    Returns (emoji, pool_size, source) — source says where the list came from.
    emoji is None when this chat allows no reaction at all.
    """
    items = await _get_live_reactions(_client_of(event))
    if items:
        source = "live (Telegram)"
    else:
        items, source = _fallback_items(), "fallback list"

    allowed = await _chat_allowed(event)
    pool = _build_pool(mode, allowed, items)
    if not pool:
        return None, 0, source
    return random.choice(pool)[0], len(pool), source


async def send_reaction(event, emoji, big=False):
    """
    Send one reaction. Returns True only when Telegram accepted it.

    Telegram rejects an emoji that the chat does not allow, and it can report
    that as a False result instead of raising — so the return value is checked
    as well as exceptions.
    """
    try:
        result = await _client_of(event)(functions.messages.SendReactionRequest(
            peer=event.chat_id,
            msg_id=event.id,
            big=big,
            add_to_recent=True,
            reaction=[types.ReactionEmoji(emoticon=emoji)],
        ))
        return result is not False
    except Exception:
        return False


# -----------------------------------------------------------------------------
#  The background reaction loop
# -----------------------------------------------------------------------------

async def _handle_message(event):
    """Decide whether this message deserves a reaction, then react to it."""
    try:
        key = _key_of(event)
        entry = STATE["chats"].get(str(key))
        if not entry or not entry.get("enabled"):
            return

        # --- skip anything that is not a normal incoming message ---
        if getattr(event, "out", False):
            return
        if getattr(event, "via_bot_id", None):
            return
        if getattr(event, "grouped_id", None):
            return                       # albums: only the first part is reactable
        if event.message is None or isinstance(event.message, (MessageService, MessageEmpty)):
            return
        sender = getattr(event, "sender", None)
        if getattr(sender, "bot", False):
            return
        if getattr(event, "sender_id", None) is not None and \
                event.sender_id == getattr(CipherElite, "_self_id", None):
            return                       # never react to your own messages

        msg_date = getattr(event.message, "date", None)
        if msg_date is not None and time.time() - getattr(msg_date, "timestamp", lambda: 0)() > 300:
            return                       # ignore old history being synced

        # --- throttle ---
        interval = float(entry.get("interval", DEFAULT_INTERVAL))
        last = _LAST_SENT.get(key, 0)
        if time.time() - last < interval:
            return

        mode = entry.get("mode", DEFAULT_MODE)
        emoji, pool_size, _ = await pick_reaction(event, mode)
        if not emoji:
            return                       # this chat allows no usable reaction

        if not await send_reaction(event, emoji, random.random() < BIG_ANIMATION_CHANCE):
            # emoji not usable in this chat — retry once with the plain fallback
            retry = random.choice(FALLBACK_REACTIONS)
            if not await send_reaction(event, retry):
                return
            emoji = retry

        _LAST_SENT[key] = time.time()
        entry["count"] = int(entry.get("count", 0)) + 1
        entry["last"] = emoji
        _SENT_LOG.append((time.time(), str(_chat_name(event)), emoji))
        del _SENT_LOG[:-MAX_LOG_ENTRIES]
        _save_state()
    except Exception:
        # a reaction must never crash the update loop
        return


# -----------------------------------------------------------------------------
#  Output formatting
# -----------------------------------------------------------------------------

def _mode_text(mode):
    return {
        "all": "🎲 **all** — every reaction Telegram offers",
        "normal": "🙂 **normal** — free reactions only",
        "premium": "⭐ **premium** — premium-only reactions",
    }.get(mode, mode)


def _usage():
    return (
        "🎭 **Cipher Elite Auto React**\n\n"
        "**Usage:**\n"
        "• `.autoreact on` — start reacting in this chat\n"
        "• `.autoreact on premium` — pick a mode: `all` / `normal` / `premium`\n"
        "• `.autoreact off` — stop reacting in this chat\n"
        "• `.autoreact interval 5` — minimum seconds between reactions\n"
        "• `.autoreact status` — settings, pool size and recent reactions\n"
        "• `.autoreact test` — react to this message right now\n\n"
        "🤖 **Powered by Cipher Elite**"
    )


async def _status_text(event, pool_size=None, source=None):
    key = _key_of(event)
    entry = STATE["chats"].get(str(key)) or _chat_entry(key)
    enabled = entry.get("enabled", False)

    head = "🟢 **ENABLED**" if enabled else "🔴 **DISABLED**"
    text = (
        "🎭 **Cipher Elite Auto React**\n\n"
        f"📍 **Chat:** {_chat_name(event)}\n"
        f"{head}\n"
        f"{_mode_text(entry.get('mode', DEFAULT_MODE))}\n"
        f"⏱ **Interval:** {entry.get('interval', DEFAULT_INTERVAL)}s\n"
        f"✅ **Reactions sent here:** {entry.get('count', 0)}\n"
    )
    if entry.get("last"):
        text += f"🕐 **Last used:** {entry['last']}\n"
    if pool_size:
        text += f"🎲 **Emoji pool:** {pool_size} reactions ({source})\n"

    if _SENT_LOG:
        text += "\n🧾 **Recent reactions:**\n"
        for _, chat, emoji in reversed(_SENT_LOG[-8:]):
            text += f"  └ {emoji}  `{chat[:24]}`\n"

    text += "\n🤖 **Powered by Cipher Elite**"
    return text


# -----------------------------------------------------------------------------


def init(client_instance):
    commands = [
        ".autoreact on [all|normal|premium] - Start reacting to every new message in this chat",
        ".autoreact off - Stop reacting in this chat",
        ".autoreact status - Show settings, emoji pool size and recent reactions",
        ".autoreact interval <seconds> - Set the minimum gap between two reactions",
        ".autoreact test - Send one random reaction to this message right now"
    ]
    description = "🎭 Auto React - random reaction from every Telegram emoji"
    add_handler("autoreact", commands, description)


async def register_commands():

    # =========================================================================
    #  .autoreact — settings
    # =========================================================================
    @CipherElite.on(events.NewMessage(pattern=r"\.autoreact(?![\w.-])(?:\s+([\s\S]*))?"))
    @rishabh()
    async def autoreact_cmd(event: Message):
        args = (event.pattern_match.group(1) or "").strip()
        parts = args.split()
        sub = parts[0].lower() if parts else "status"
        key = _key_of(event)
        entry = _chat_entry(key)

        try:
            if sub in ("on", "enable", "start"):
                mode = DEFAULT_MODE
                if len(parts) > 1:
                    mode = parts[1].lower()
                    if mode not in VALID_MODES:
                        return await event.reply(
                            "🎭 **Cipher Elite Auto React**\n\n"
                            f"❌ **Unknown mode:** `{mode}`\n"
                            f"✅ **Valid modes:** `{'`, `'.join(VALID_MODES)}`"
                        )
                entry["enabled"] = True
                entry["mode"] = mode
                _save_state()

                emoji, pool_size, source = await pick_reaction(event, mode)
                sample = (f"👉 Sample reaction: {emoji}\n" if emoji
                          else "⚠️ **This chat currently allows no reaction from this mode.**\n")
                await event.reply(
                    "🎭 **Cipher Elite Auto React**\n\n"
                    f"✅ **Enabled** in `{_chat_name(event)}`\n"
                    f"{_mode_text(mode)}\n"
                    f"🎲 **Emoji pool:** {pool_size} reactions ({source})\n"
                    f"⏱ **Interval:** {entry.get('interval', DEFAULT_INTERVAL)}s\n\n"
                    + sample +
                    "🤖 **Powered by Cipher Elite**"
                )

            elif sub in ("off", "disable", "stop"):
                entry["enabled"] = False
                _save_state()
                await event.reply(
                    "🎭 **Cipher Elite Auto React**\n\n"
                    f"🔴 **Disabled** in `{_chat_name(event)}`\n"
                    f"✅ Total reactions sent here: {entry.get('count', 0)}\n"
                    "🤖 **Powered by Cipher Elite**"
                )

            elif sub in ("mode",):
                if len(parts) < 2 or parts[1].lower() not in VALID_MODES:
                    return await event.reply(
                        "🎭 **Cipher Elite Auto React**\n\n"
                        f"❌ **Usage:** `.autoreact mode <{'|'.join(VALID_MODES)}>`"
                    )
                entry["mode"] = parts[1].lower()
                _save_state()
                await event.reply(
                    "🎭 **Cipher Elite Auto React**\n\n"
                    f"✅ **Mode set to** {_mode_text(entry['mode'])}\n"
                    "🤖 **Powered by Cipher Elite**"
                )

            elif sub in ("interval", "delay"):
                if len(parts) < 2:
                    return await event.reply(
                        "🎭 **Cipher Elite Auto React**\n\n"
                        "❌ **Usage:** `.autoreact interval <seconds>`  (min 0.5, max 300)"
                    )
                try:
                    value = float(parts[1])
                except ValueError:
                    return await event.reply(
                        "🎭 **Cipher Elite Auto React**\n\n"
                        f"❌ `{parts[1]}` is not a number."
                    )
                if not 0.5 <= value <= 300:
                    return await event.reply(
                        "🎭 **Cipher Elite Auto React**\n\n"
                        "❌ **Interval must be between 0.5 and 300 seconds.**"
                    )
                entry["interval"] = value
                _save_state()
                await event.reply(
                    "🎭 **Cipher Elite Auto React**\n\n"
                    f"✅ **Interval set to** {value}s\n"
                    "🤖 **Powered by Cipher Elite**"
                )

            elif sub in ("test", "demo", "preview"):
                emoji, pool_size, source = await pick_reaction(event, entry.get("mode", DEFAULT_MODE))
                if not emoji:
                    return await event.reply(
                        "🎭 **Cipher Elite Auto React**\n\n"
                        "❌ **No usable reaction in this chat.**\n"
                        "💡 Reactions may be turned off by the admins, or the allowed "
                        f"list does not overlap with the `{entry.get('mode', DEFAULT_MODE)}` mode.\n"
                        "🤖 **Powered by Cipher Elite**"
                    )
                ok = await send_reaction(event, emoji, big=True)
                await event.reply(
                    "🎭 **Cipher Elite Auto React**\n\n"
                    + (f"✅ Reacted to this message with {emoji}\n" if ok
                       else f"❌ Could not react with {emoji} — this chat may not allow it.\n")
                    + f"🎲 **Emoji pool:** {pool_size} reactions ({source})\n"
                    "🤖 **Powered by Cipher Elite**"
                )

            elif sub in ("status", "info", ""):
                emoji, pool_size, source = await pick_reaction(event, entry.get("mode", DEFAULT_MODE))
                await event.reply(await _status_text(event, pool_size, source))

            elif sub in ("help",):
                await event.reply(_usage())

            else:
                await event.reply(
                    "🎭 **Cipher Elite Auto React**\n\n"
                    f"❌ **Unknown option:** `{sub}`\n\n" + _usage().split("\n\n", 1)[1]
                )

        except Exception as e:
            await event.reply(
                "🎭 **Cipher Elite Error**\n\n"
                f"❌ **Error:** {str(e)}\n"
                "💡 **Try `.autoreact help`**"
            )

    # =========================================================================
    #  Background handler — runs on every incoming message, never replies
    # =========================================================================
    @CipherElite.on(events.NewMessage(incoming=True))
    async def autoreact_watcher(event: Message):
        await _handle_message(event)


_load_state()
