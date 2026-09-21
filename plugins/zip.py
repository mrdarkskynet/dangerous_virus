# =============================================================================
#  CipherElite Userbot Plugin
#
#  Plugin Name:    zip
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
#.      update on: 25/07/2026 by Marlboro CLOVE
#
#  Added: .sh (shell) + .py (python eval) — both support file replies:
#         reply to a media/file message with `.sh` / `.py` and the file
#         content runs as the command / code.
#
#  Thank you for respecting open-source software!
# =============================================================================

VERSION = "1.1.0"
CATEGORY = "utilities"

import asyncio
import contextlib
import io
import os
import re
import time
import traceback
import zipfile
import shutil
from pathlib import Path
from telethon import events
from telethon.types import Message
from utils.utils import CipherElite
from utils.decorators import rishabh
from plugins.bot import add_handler

# ---- execution limits (sh / py) -------------------------------------------
SH_TIMEOUT = 120          # seconds, shell command hard limit
PY_TIMEOUT = 120          # seconds, python exec hard limit
MAX_TEXT_READ = 2 * 1024 * 1024   # 2 MB — bigger files are not read as text
MAX_OUT_CHARS = 3500      # output longer than this is sent as a .txt file

# files this plugin will refuse to delete — plugin code, tests and bot config.
# `.rm <path>` and `.sh rm -rf ...` both go through _protect_deletion().
PROTECTED_PATHS = (
    "zip.py",
    "scan_conflicts2.py",
    "startup.py",
    "session",
    ".session",
    "config.py",
    "config/config.py",
    "utils/decorators.py",
)

# directories that hold the bot's own code — wiping these kills the bot.
PROTECTED_DIRS = ("plugins", "utils", "config", "startup", "database")

# verbs / flags that remove data — `rm`, `shred`, `os.remove(...)`, ...
# (`find -delete` and `git clean` are handled separately: those words also show
#  up in harmless commands like `grep -r delete ...` or `npm run clean`)
_DELETE_VERBS = (
    "rm", "rmdir", "rmtree", "unlink", "shred", "remove", "removedirs",
    "erase", "truncate", "wipe", "purge",
)


def _protect_deletion(command):
    """
    Return a reason string if `command` looks like it would delete one of the
    bot's own files or code directories, else None. Covers `.rm plugins/zip.py`
    as well as `.sh rm -rf plugins/`, `.sh find . -delete`, `.sh python -c ...`.
    """
    # quotes are dropped so `rm -f "zip.py"` and `os.remove('zip.py')` match too
    cmd = (command or "").lower().replace('"', " ").replace("'", " ")

    def has_verb(word):
        # a leading `-` or `.` counts as a boundary, so `os.remove(` is caught too
        return bool(re.search(rf"(?<![\w/]){re.escape(word)}(?![\w-])", cmd))

    destructive = (
        any(has_verb(v) for v in _DELETE_VERBS)
        or has_verb("-delete")
        or bool(re.search(r"(?<![\w-])git\s+clean(?![\w-])", cmd))
    )

    if not destructive:
        return None

    for name in PROTECTED_PATHS:
        base = name.rsplit("/", 1)[-1]
        if base in cmd:
            return f"`{name}` is a protected bot file (plugin code / test / config)"

    tokens = re.findall(r"[A-Za-z0-9_./*-]+", cmd)

    # broad wipes that name no file at all — `rm -rf .`, `rm -f *`, `git clean -fdx`
    if any(t in (".", "..", "*", "./*") for t in tokens):
        return ("this command deletes from the current directory, where the bot's "
                "code lives")
    if any(t == "*" or t.endswith("/*") for t in tokens):
        return "this command wildcard-deletes files in the bot's working directory"
    if re.search(r"(?<![\w-])rm\s+(?:-\S+\s+)*-\S*[rR]", cmd):
        return "recursive `rm -r` / `rm -rf` commands are not allowed"
    if re.search(r"(?<![\w-])git\s+clean(?![\w-])", cmd):
        return "`git clean` removes untracked files, which includes the bot's code"

    # `rm -rf plugins/` — no filename given, so check the directories too
    for token in tokens:
        clean = token.strip("/").strip("*")
        if clean in _DELETE_VERBS or clean.startswith("-") or not clean:
            continue
        parts = [p for p in clean.split("/") if p and p not in (".", "..")]
        if parts and parts[0] in PROTECTED_DIRS:
            return (f"`{clean}` is inside the `{parts[0]}/` directory, which holds the "
                    "bot's own code")

    return None


# values of these env keys are never printed in the output
SECRET_KEYS = (
    "API_ID", "API_HASH", "SESSION", "SESSION_STRING", "STRING_SESSION",
    "BOT_TOKEN", "BOT_SESSION", "ASSISTANT_SESSION", "OWNER_ID", "SUDO_USERS",
    "HEROKU_API_KEY", "HEROKU_APP_NAME", "TOKEN", "PASSWORD", "SECRET",
    "MONGO", "DATABASE_URL", "DB_URI", "PRIVATE_KEY", "KEY",
)


def init(client_instance):
    commands = [
        ".zip - Zip the replied media",
        ".unzip - Unzip the replied zip file",
        ".ls [path] - List files in directory",
        ".mkdir <path> - Create a directory",
        ".rm <path> - Remove a file or directory",
        ".mv <src> <dst> - Move/rename a file",
        ".cp <src> <dst> - Copy a file",
        ".size <path> - Get file/folder size",
        ".storage - Show disk storage info",
        ".findfile <name> - Search for files by name",
        ".sh <command> - Run a shell command (or reply to a .sh/.txt file)",
        ".py <code> - Run python code (or reply to a .py/.txt file)"
    ]
    description = "📦 Archive & File Manager - zip, unzip, shell, python & files"
    add_handler("zip", commands, description)


def get_size(path):
    """Calculate total size of file or directory in MB"""
    try:
        if os.path.isfile(path):
            return os.path.getsize(path) / (1024 * 1024)
        elif os.path.isdir(path):
            total = 0
            for dirpath, dirnames, filenames in os.walk(path):
                for f in filenames:
                    fp = os.path.join(dirpath, f)
                    total += os.path.getsize(fp)
            return total / (1024 * 1024)
    except Exception:
        pass
    return 0


def format_size(size_mb):
    """Format size in MB to human readable"""
    if size_mb < 1:
        return f"{size_mb * 1024:.2f} KB"
    elif size_mb < 1024:
        return f"{size_mb:.2f} MB"
    else:
        return f"{size_mb / 1024:.2f} GB"


# =============================================================================
#  Helpers for .sh / .py
# =============================================================================

def _redact(text):
    """Strip secrets (session string, tokens, api hash) from output/captions."""
    if not text:
        return text
    for key in SECRET_KEYS:
        value = os.environ.get(key)
        if value and len(str(value)) > 4:
            text = text.replace(str(value), "•" * 8)
    return text


def _clip(text, limit=2000):
    """Truncate very long text (used for captions / status messages)."""
    text = "" if text is None else str(text)
    if len(text) > limit:
        return text[:limit] + f"… (+{len(text) - limit} chars)"
    return text


def _safe_repr(obj, limit=300):
    """Safe + short repr()."""
    try:
        text = repr(obj)
    except Exception as e:
        return f"<unprintable: {e}>"
    return _clip(text, limit)


async def send_output(event, header, text, prefix="output"):
    """Send output — inline as a message when short, as a .txt file when long."""
    text = "" if text is None else str(text)
    if len(text) <= MAX_OUT_CHARS:
        await event.reply(f"{header}```\n{text}\n```")
        return

    fname = f"{prefix}_{int(time.time())}.txt"
    try:
        with open(fname, "w", encoding="utf-8") as f:
            f.write(text)
        await event.reply(
            f"{header}\n📄 Output was too long ({len(text)} chars) — sending it as a file."
        )
        await event.reply(f"📄 `{fname}`", file=fname)
    except Exception as e:
        await event.reply(f"{header}```\n{_clip(text, MAX_OUT_CHARS)}\n```")
    finally:
        if os.path.exists(fname):
            try:
                os.remove(fname)
            except Exception:
                pass


async def _read_code_from_message(event, allowed_exts):
    """
    Reads the replied file's content when no text was given with the command.
    This is what powers "reply to a file + send .sh / .py".

    Returns: (code, error)
    """
    reply = await event.get_reply_message()
    if not reply or not reply.media:
        return None, "❌ No command given. Send text with the command, or reply to a file and send the command."

    size = getattr(getattr(reply, "file", None), "size", 0) or 0
    if size and size > MAX_TEXT_READ:
        return None, f"❌ File is too large ({format_size(size / (1024 * 1024))}) — the text-read limit is 2 MB."

    fname = getattr(getattr(reply, "file", None), "name", "") or ""
    path = None
    try:
        path = await reply.download_media(f"codeload_{int(time.time())}")
        if not path or not os.path.exists(str(path)):
            return None, "❌ Could not download the file."

        if allowed_exts and fname:
            ext = os.path.splitext(fname)[1].lower()
            if ext and ext not in allowed_exts:
                return None, f"❌ `{ext}` files are not supported. Allowed: {', '.join(allowed_exts)}"

        with open(path, "r", encoding="utf-8", errors="replace") as f:
            code = f.read()

        if not code.strip():
            return None, "❌ The file is empty."
        return code, None
    except UnicodeDecodeError:
        return None, "❌ This does not look like a text file (binary files cannot be read)."
    except Exception as e:
        return None, f"❌ Error while reading the file: {str(e)}"
    finally:
        if path and os.path.exists(str(path)):
            try:
                os.remove(str(path))
            except Exception:
                pass


def _args_or_file(event, arg):
    """Detect whether text was given after the command — handles both cases."""
    arg = (arg or "").strip()
    if arg:
        return arg
    if event.reply_to_msg_id:
        return ""          # read it from the replied file
    return None


async def run_shell(command, timeout=SH_TIMEOUT):
    """Run a shell command — returns stdout, stderr, return code and elapsed time."""
    start = time.time()
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            with contextlib.suppress(Exception):
                proc.kill()
            await proc.wait()
            return None, "", f"⏱ Command did not finish within {timeout}s — killed it.", time.time() - start
        return proc.returncode, out.decode("utf-8", errors="replace"), err.decode("utf-8", errors="replace"), time.time() - start
    except Exception as e:
        return None, "", f"{type(e).__name__}: {e}", time.time() - start


async def _exec_python(code, ns):
    """
    Run python code — `await` is supported, the value of the last expression is
    returned IPython-style, and print() output is captured.

    Returns: (ok, output_text, result_repr)
    """
    import ast
    import sys

    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False, traceback.format_exc(), None

    last = None
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        last = ast.Expression(tree.body.pop().value)
        ast.fix_missing_locations(last)

    buf, ebuf = io.StringIO(), io.StringIO()
    ns["__builtins__"] = __builtins__

    async def _runner():
        exec(compile(tree, "<cipherelite>", "exec"), ns)
        if last is not None:
            return eval(compile(last, "<cipherelite>", "eval"), ns)
        return None

    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(ebuf):
            result = await asyncio.wait_for(_runner(), timeout=PY_TIMEOUT)
    except asyncio.TimeoutError:
        with contextlib.suppress(Exception):
            ebuf.write(f"\n⏱ Code did not finish within {PY_TIMEOUT}s — cancelled it.")
        return False, buf.getvalue() + ebuf.getvalue(), None
    except Exception:
        tb = traceback.format_exc().replace('File "<cipherelite>"', "File \"<your code>\"")
        return False, buf.getvalue() + ebuf.getvalue() + tb, None

    out = buf.getvalue()
    if ebuf.getvalue():
        out += ("\n" if out else "") + "⚠️ stderr:\n" + ebuf.getvalue()
    return True, out, result


# =============================================================================


async def register_commands():

    @CipherElite.on(events.NewMessage(pattern=r"\.zip"))
    @rishabh()
    async def zip_files(event: Message):
        if not event.reply_to_msg_id:
            return await event.reply("❌ Reply to a message to zip it.")

        reply = await event.get_reply_message()
        if not reply.media:
            return await event.reply("❌ Reply to a media message to zip it.")

        elite = await event.reply("🔄 Zipping...")
        start = time.time()
        download_path = await reply.download_media(f"temp_{round(time.time())}")

        zip_path = f"zipped_{int(time.time())}.zip"
        try:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zip_file:
                zip_file.write(download_path, os.path.basename(download_path))

            await elite.edit("✅ Zipped Successfully. Uploading...")
            await event.reply(
                f"**✓ Zipped in {time.time() - start:.2f}s**\n📦 File size: {format_size(get_size(zip_path))}",
                file=zip_path
            )
        except Exception as e:
            await elite.edit(f"❌ Error: {str(e)}")
        finally:
            if os.path.exists(zip_path):
                os.remove(zip_path)
            if os.path.exists(download_path):
                os.remove(download_path)
            await elite.delete()

    @CipherElite.on(events.NewMessage(pattern=r"\.unzip"))
    @rishabh()
    async def unzip_file(event: Message):
        if not event.reply_to_msg_id:
            return await event.reply("❌ Reply to a message to unzip it.")

        reply = await event.get_reply_message()
        if not reply.media:
            return await event.reply("❌ Reply to a zip file to unzip it.")

        elite = await event.reply("🔄 Unzipping...")
        start = time.time()
        download_path = await reply.download_media(f"temp_{round(time.time())}")

        unzip_dir = f"unzipped_{int(time.time())}"
        os.makedirs(unzip_dir, exist_ok=True)

        try:
            with zipfile.ZipFile(download_path, "r") as zip_file:
                zip_file.extractall(unzip_dir)

            await elite.edit("✅ Unzipped Successfully. Uploading files...")
            uploaded = 0

            for root, _, files in os.walk(unzip_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    try:
                        await event.reply(
                            f"📄 **{file}** ({format_size(get_size(file_path))})",
                            file=file_path
                        )
                        uploaded += 1
                    except Exception as e:
                        print(f"Error uploading {file}: {e}")
                    finally:
                        if os.path.exists(file_path):
                            os.remove(file_path)

            await elite.edit(f"✅ **Successfully uploaded {uploaded} files in {time.time() - start:.2f}s!**")
        except Exception as e:
            await elite.edit(f"❌ Error: {str(e)}")
        finally:
            shutil.rmtree(unzip_dir, ignore_errors=True)
            if os.path.exists(download_path):
                os.remove(download_path)

    @CipherElite.on(events.NewMessage(pattern=r"\.ls(?:\s+(.+))?"))
    @rishabh()
    async def list_files(event: Message):
        path = event.pattern_match.group(1) or "."

        if not os.path.exists(path):
            return await event.reply(f"❌ Path not found: `{path}`")

        if not os.path.isdir(path):
            return await event.reply(f"❌ Not a directory: `{path}`")

        try:
            items = os.listdir(path)
            if not items:
                return await event.reply(f"📁 Directory is empty: `{path}`")

            msg = f"📁 **Directory:** `{os.path.abspath(path)}`\n\n"
            folders = [i for i in items if os.path.isdir(os.path.join(path, i))]
            files = [i for i in items if os.path.isfile(os.path.join(path, i))]

            if folders:
                msg += "📂 **Folders:**\n"
                for f in folders[:20]:
                    msg += f"  └ `{f}/`\n"

            if files:
                msg += "\n📄 **Files:**\n"
                for f in files[:20]:
                    fpath = os.path.join(path, f)
                    size = format_size(get_size(fpath))
                    msg += f"  └ `{f}` ({size})\n"

            if len(items) > 40:
                msg += f"\n*... and {len(items) - 40} more items*"

            await event.reply(msg)
        except Exception as e:
            await event.reply(f"❌ Error: {str(e)}")

    @CipherElite.on(events.NewMessage(pattern=r"\.mkdir\s+(.+)"))
    @rishabh()
    async def make_dir(event: Message):
        path = event.pattern_match.group(1).strip()

        try:
            os.makedirs(path, exist_ok=True)
            await event.reply(f"✅ **Directory created:** `{os.path.abspath(path)}`")
        except Exception as e:
            await event.reply(f"❌ Error: {str(e)}")

    @CipherElite.on(events.NewMessage(pattern=r"\.rm\s+(.+)"))
    @rishabh()
    async def remove_file(event: Message):
        path = event.pattern_match.group(1).strip()

        blocked = _protect_deletion("rm " + path)
        if blocked:
            return await event.reply(
                f"🛡 **Blocked:** {blocked} — it will not be deleted."
            )

        if not os.path.exists(path):
            return await event.reply(f"❌ Path not found: `{path}`")

        try:
            if os.path.isfile(path):
                os.remove(path)
                await event.reply(f"✅ **File removed:** `{path}`")
            elif os.path.isdir(path):
                shutil.rmtree(path)
                await event.reply(f"✅ **Directory removed:** `{path}`")
        except Exception as e:
            await event.reply(f"❌ Error: {str(e)}")

    @CipherElite.on(events.NewMessage(pattern=r"\.mv\s+(.+?)\s+(.+)"))
    @rishabh()
    async def move_file(event: Message):
        src = event.pattern_match.group(1).strip()
        dst = event.pattern_match.group(2).strip()

        if not os.path.exists(src):
            return await event.reply(f"❌ Source not found: `{src}`")

        try:
            shutil.move(src, dst)
            await event.reply(f"✅ **Moved:** `{src}` → `{dst}`")
        except Exception as e:
            await event.reply(f"❌ Error: {str(e)}")

    @CipherElite.on(events.NewMessage(pattern=r"\.cp\s+(.+?)\s+(.+)"))
    @rishabh()
    async def copy_file(event: Message):
        src = event.pattern_match.group(1).strip()
        dst = event.pattern_match.group(2).strip()

        if not os.path.exists(src):
            return await event.reply(f"❌ Source not found: `{src}`")

        try:
            if os.path.isfile(src):
                shutil.copy2(src, dst)
            elif os.path.isdir(src):
                shutil.copytree(src, dst)
            await event.reply(f"✅ **Copied:** `{src}` → `{dst}`")
        except Exception as e:
            await event.reply(f"❌ Error: {str(e)}")

    @CipherElite.on(events.NewMessage(pattern=r"\.size\s+(.+)"))
    @rishabh()
    async def file_size(event: Message):
        path = event.pattern_match.group(1).strip()

        if not os.path.exists(path):
            return await event.reply(f"❌ Path not found: `{path}`")

        try:
            size = get_size(path)
            type_str = "📁 Directory" if os.path.isdir(path) else "📄 File"
            await event.reply(f"✅ **{type_str}:** `{path}`\n📊 **Size:** {format_size(size)}")
        except Exception as e:
            await event.reply(f"❌ Error: {str(e)}")

    @CipherElite.on(events.NewMessage(pattern=r"\.storage"))
    @rishabh()
    async def storage_info(event: Message):
        try:
            total, used, free = shutil.disk_usage("/")

            msg = (
                "💾 **STORAGE INFO**\n\n"
                f"📊 **Total:** {format_size(total / (1024**2))}\n"
                f"✅ **Used:** {format_size(used / (1024**2))}\n"
                f"🟢 **Free:** {format_size(free / (1024**2))}\n"
                f"📈 **Usage:** {(used / total * 100):.1f}%\n"
            )
            await event.reply(msg)
        except Exception as e:
            await event.reply(f"❌ Error: {str(e)}")

    @CipherElite.on(events.NewMessage(pattern=r"\.findfile\s+(.+)"))
    @rishabh()
    async def find_file(event: Message):
        search_term = event.pattern_match.group(1).strip()

        elite = await event.reply(f"🔍 Searching for `{search_term}`...")
        results = []

        try:
            for root, dirs, files in os.walk("."):
                for file in files:
                    if search_term.lower() in file.lower():
                        file_path = os.path.join(root, file)
                        results.append(file_path)
                        if len(results) >= 20:
                            break
                if len(results) >= 20:
                    break

            if not results:
                await elite.edit(f"❌ No files found matching: `{search_term}`")
                return

            msg = f"✅ **Found {len(results)} file(s):**\n\n"
            for r in results:
                size = format_size(get_size(r))
                msg += f"📄 `{r}` ({size})\n"

            await elite.edit(msg)
        except Exception as e:
            await elite.edit(f"❌ Error: {str(e)}")

    # =========================================================================
    #  .sh — shell command (from text or from a replied file)
    # =========================================================================
    @CipherElite.on(events.NewMessage(pattern=r"\.sh(?![\w.-])(?:\s+([\s\S]*))?"))
    @rishabh()
    async def shell_exec(event: Message):
        arg = _args_or_file(event, event.pattern_match.group(1))
        if arg is None:
            return await event.reply(
                "🎭 **Cipher Elite Shell**\n\n"
                "❌ No command given.\n\n"
                "**Usage:**\n"
                "• `.sh ls -la`\n"
                "• `.sh pip list | head -20`\n"
                "• Or reply to a `.sh` / `.txt` file and send just `.sh`\n\n"
                "🤖 **Powered by Cipher Elite**"
            )

        if arg == "":          # read from the replied file
            code, err = await _read_code_from_message(event, {".sh", ".bash", ".txt", ".py", ""})
            if err:
                return await event.reply(f"🎭 **Cipher Elite Shell**\n\n{err}")
            command, source = code, "📄 replied file"
        else:
            command, source = arg, "✍️ message"

        blocked = _protect_deletion(command)
        if blocked:
            return await event.reply(
                "🎭 **Cipher Elite Shell**\n\n"
                f"🛡 **Blocked:** {blocked}.\n\n"
                "💡 Rename or move it first if you really want it gone.\n"
                "🤖 **Powered by Cipher Elite**"
            )

        status = await event.reply(f"🔄 **Running command...**\n\n`{_clip(_redact(command), 400)}`")

        try:
            rc, out, err, elapsed = await run_shell(command)
            body = ""
            if out.strip():
                body += f"📤 **stdout**\n```\n{_redact(out.strip())}\n```\n"
            if err.strip():
                body += f"\n⚠️ **stderr**\n```\n{_redact(err.strip())}\n```\n"
            if rc is None:
                head = f"⏱ **Timed out / killed**  •  ⏳ {elapsed:.2f}s"
            elif rc == 0:
                head = f"✅ **Exit code:** 0  •  ⏳ {elapsed:.2f}s  •  {source}"
            else:
                head = f"❌ **Exit code:** {rc}  •  ⏳ {elapsed:.2f}s  •  {source}"

            text = f"🎭 **Cipher Elite Shell**\n\n{head}\n\n{body or '_no output_'}"
            if len(text) > 3900:
                await send_output(event, "🎭 **Cipher Elite Shell**\n\n" + head + "\n\n",
                                  (out or "") + ("\n--- stderr ---\n" + err if err.strip() else ""),
                                  prefix="shell")
            else:
                await event.reply(text)
        except Exception as e:
            await event.reply(f"🎭 **Cipher Elite Error**\n\n❌ **Error:** {str(e)}")
        finally:
            with contextlib.suppress(Exception):
                await status.delete()

    # =========================================================================
    #  .py — python code (from text or from a replied file)
    # =========================================================================
    @CipherElite.on(events.NewMessage(pattern=r"\.py(?![\w.-])(?:\s+([\s\S]*))?"))
    @rishabh()
    async def python_eval(event: Message):
        arg = _args_or_file(event, event.pattern_match.group(1))
        if arg is None:
            return await event.reply(
                "🎭 **Cipher Elite Python**\n\n"
                "❌ No code given.\n\n"
                "**Usage:**\n"
                "• `.py print(2 + 2)`\n"
                "• `.py await event.reply('hello')`\n"
                "• `.py` + replied `.py` / `.txt` file → runs the code from that file\n\n"
                "**Available:** `event`, `client`, `CipherElite`, `asyncio`, `reply()`\n"
                "🤖 **Powered by Cipher Elite**"
            )

        if arg == "":          # read from the replied file
            code, err = await _read_code_from_message(event, {".py", ".txt", ".python", ""})
            if err:
                return await event.reply(f"🎭 **Cipher Elite Python**\n\n{err}")
            source = "📄 replied file"
        else:
            code, source = arg, "✍️ message"

        status = await event.reply(f"🔄 **Running python code...**\n\n`{_clip(_redact(code), 400)}`")

        try:
            ns = {
                "event": event,
                "client": event.client,
                "CipherElite": CipherElite,
                "asyncio": asyncio,
                "reply": lambda *a, **k: event.reply(*a, **k),
            }
            ok, out, result = await _exec_python(code, ns)
            icon = "✅" if ok else "❌"
            head = f"{icon} **Python**  •  {source}"

            body = ""
            if out.strip():
                body += f"📤 **Output**\n```\n{_redact(out.strip())}\n```\n"
            if result is not None:
                body += f"\n🔙 **Result:** `{_safe_repr(result)}`\n"
            if not ok:
                head = f"❌ **Python Error**  •  {source}"

            text = f"🎭 **Cipher Elite Python**\n\n{head}\n\n{body or '_no output_'}"
            if len(text) > 3900:
                await send_output(event, "🎭 **Cipher Elite Python**\n\n" + head + "\n\n",
                                  _redact(out), prefix="python")
            else:
                await event.reply(text)
        except Exception as e:
            await event.reply(f"🎭 **Cipher Elite Error**\n\n❌ **Error:** {str(e)}")
        finally:
            with contextlib.suppress(Exception):
                await status.delete()
