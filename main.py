# -*- coding: utf-8 -*-
"""
hosting_bot.py — DarkBD File Hosting Bot
----------------------------------------
Refactored to run as a background daemon thread inside CipherElite
(or any other host process).

- No Flask / no web server → no port conflict with CipherElite.
- Polling runs in a daemon thread → doesn't block CipherElite.
- Auto-starts when imported (set AUTOSTART_HOSTING_BOT=0 to disable).
- Standalone run still works: `python hosting_bot.py`
"""

import telebot
import subprocess
import os
import uuid
import zipfile
import tempfile
import shutil
from telebot import types
import time
from datetime import datetime, timedelta
import psutil
import sqlite3
import json
import logging
import signal
import threading
import re
import sys
import atexit
import requests


# --- Configuration -----------------------------------------------------------
# ⚠️ HOST_BOT_TOKEN is intentionally different from CipherElite's BOT_TOKEN
#    so the two bots don't clash. Get a fresh token from @BotFather.
TOKEN = os.environ.get('HOST_BOT_TOKEN', '').strip() or '8941610394:AAF0_O3D-4pGF1LgrcypLqosiXHJOULFfhw'
if not TOKEN:
    raise SystemExit(
        "❌ HOST_BOT_TOKEN environment variable is not set.\n"
        "   Set it in your deployment panel:  HOST_BOT_TOKEN=<file_hosting_bot_token>\n"
        "   Get a fresh token from @BotFather (revoke the old one first)."
    )

OWNER_ID = int(os.environ.get('HOST_OWNER_ID', '5076047031'))
ADMIN_ID = int(os.environ.get('HOST_ADMIN_ID', str(OWNER_ID)))
YOUR_USERNAME = os.environ.get('HOST_CONTACT', '@mrdarkvipx')
UPDATE_CHANNEL = os.environ.get('HOST_CHANNEL', 'https://t.me/numbervirtualfast')

# Folder setup
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_BOTS_DIR = os.path.join(BASE_DIR, 'upload_bots')
IROTECH_DIR = os.path.join(BASE_DIR, 'inf')
DATABASE_PATH = os.path.join(IROTECH_DIR, 'bot_data.db')

# File upload limits
FREE_USER_LIMIT = 3
SUBSCRIBED_USER_LIMIT = 15
ADMIN_LIMIT = 999
OWNER_LIMIT = float('inf')

# ---------- File type rules ----------
SCRIPT_EXTS  = {'.py', '.js'}
ARCHIVE_EXTS = {'.zip'}
CONFIG_EXTS  = {
    '.env', '.json', '.yaml', '.yml', '.toml', '.ini', '.cfg', '.conf',
    '.txt', '.xml', '.properties', '.sh', '.bash', '.bat', '.cmd',
    '.csv', '.md', '.sql', '.pem', '.key', '.crt', '.cer', '.log',
    '.lock', '.gitignore', '.npmrc', '.babelrc', '.eslintrc',
}
ALL_SUPPORTED_EXTS = SCRIPT_EXTS | ARCHIVE_EXTS | CONFIG_EXTS

# Environment whitelist for subprocess (prevents bot secrets leaking to user scripts)
SAFE_ENV_KEYS = {
    'PATH', 'HOME', 'USER', 'USERNAME', 'LOGNAME',
    'LANG', 'LC_ALL', 'LC_CTYPE', 'LANGUAGE',
    'TZ', 'TMPDIR', 'TMP', 'TEMP', 'SHELL',
    'PYTHONPATH', 'PYTHONHOME', 'PYTHONIOENCODING', 'PYTHONUNBUFFERED', 'PYTHONDONTWRITEBYTECODE',
    'NODE_PATH', 'NODE_ENV',
    'TERM', 'COLORTERM',
    'SYSTEMROOT', 'SYSTEMDRIVE', 'WINDIR', 'COMSPEC', 'PATHEXT',
    'APPDATA', 'LOCALAPPDATA', 'PROGRAMFILES', 'PROGRAMFILES(X86)',
    'PROGRAMDATA', 'ALLUSERSPROFILE', 'PUBLIC',
    'COMPUTERNAME', 'HOSTNAME', 'OS', 'PROCESSOR_ARCHITECTURE',
}

os.makedirs(UPLOAD_BOTS_DIR, exist_ok=True)
os.makedirs(IROTECH_DIR, exist_ok=True)

bot = telebot.TeleBot(TOKEN)

# --- Data structures ---
bot_scripts = {}          # key: "<user_id>_<file_id>"
user_subscriptions = {}
user_files = {}           # user_id -> list of {file_id, file_name, file_type}
active_users = set()
admin_ids = {ADMIN_ID, OWNER_ID}
bot_locked = False

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("HostingBot")

# --- Command Button Layouts ---
COMMAND_BUTTONS_LAYOUT_USER_SPEC = [
    ["📢 Updates Channel"],
    ["📤 Upload File", "📂 Check Files"],
    ["⚡ Bot Speed", "📊 Statistics"],
    ["📞 Contact Owner"]
]
ADMIN_COMMAND_BUTTONS_LAYOUT_USER_SPEC = [
    ["📢 Updates Channel"],
    ["📤 Upload File", "📂 Check Files"],
    ["⚡ Bot Speed", "📊 Statistics"],
    ["💳 Subscriptions", "📢 Broadcast"],
    ["🔒 Lock Bot", "🟢 Running All Code"],
    ["👑 Admin Panel", "📞 Contact Owner"]
]

# --- Database Setup ---
DB_LOCK = threading.Lock()


def _new_file_id():
    """Short unique upload ID."""
    return uuid.uuid4().hex[:12]


def init_db():
    logger.info(f"Initializing database at: {DATABASE_PATH}")
    try:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS subscriptions
                     (user_id INTEGER PRIMARY KEY, expiry TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS active_users
                     (user_id INTEGER PRIMARY KEY)''')
        c.execute('''CREATE TABLE IF NOT EXISTS admins
                     (user_id INTEGER PRIMARY KEY)''')

        c.execute("PRAGMA table_info(user_files)")
        cols = [row[1] for row in c.fetchall()]
        if not cols:
            c.execute('''CREATE TABLE user_files (
                user_id INTEGER,
                file_id TEXT,
                file_name TEXT,
                file_type TEXT,
                PRIMARY KEY (user_id, file_id)
            )''')
        elif 'file_id' not in cols:
            logger.warning("Migrating user_files table to new schema (adding file_id).")
            c.execute('ALTER TABLE user_files RENAME TO user_files_legacy')
            c.execute('''CREATE TABLE user_files (
                user_id INTEGER,
                file_id TEXT,
                file_name TEXT,
                file_type TEXT,
                PRIMARY KEY (user_id, file_id)
            )''')
            legacy = c.execute("SELECT user_id, file_name, file_type FROM user_files_legacy").fetchall()
            for uid, fname, ftype in legacy:
                c.execute(
                    'INSERT INTO user_files (user_id, file_id, file_name, file_type) VALUES (?, ?, ?, ?)',
                    (uid, _new_file_id(), fname, ftype)
                )
            c.execute('DROP TABLE user_files_legacy')

        c.execute('INSERT OR IGNORE INTO admins (user_id) VALUES (?)', (OWNER_ID,))
        if ADMIN_ID != OWNER_ID:
            c.execute('INSERT OR IGNORE INTO admins (user_id) VALUES (?)', (ADMIN_ID,))
        conn.commit()
        conn.close()
        logger.info("Database initialized successfully.")
    except Exception as e:
        logger.error(f"❌ Database initialization error: {e}", exc_info=True)


def load_data():
    logger.info("Loading data from database...")
    try:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()

        c.execute('SELECT user_id, expiry FROM subscriptions')
        for user_id, expiry in c.fetchall():
            try:
                user_subscriptions[user_id] = {'expiry': datetime.fromisoformat(expiry)}
            except ValueError:
                logger.warning(f"⚠️ Invalid expiry for user {user_id}: {expiry}. Skipping.")

        c.execute('SELECT user_id, file_id, file_name, file_type FROM user_files')
        for user_id, file_id, file_name, file_type in c.fetchall():
            user_files.setdefault(user_id, []).append({
                'file_id': file_id,
                'file_name': file_name,
                'file_type': file_type,
            })

        c.execute('SELECT user_id FROM active_users')
        active_users.update(uid for (uid,) in c.fetchall())

        c.execute('SELECT user_id FROM admins')
        admin_ids.update(uid for (uid,) in c.fetchall())

        conn.close()
        logger.info(
            f"Data loaded: {len(active_users)} users, "
            f"{len(user_subscriptions)} subscriptions, {len(admin_ids)} admins."
        )
    except Exception as e:
        logger.error(f"❌ Error loading data: {e}", exc_info=True)


init_db()
load_data()


# ---------- Helper functions ----------
def sanitize_filename(name: str) -> str:
    """Sanitize a filename so it can NEVER escape its folder."""
    if not name:
        return ""
    name = str(name).replace('\\', '/')
    name = name.split('/')[-1]
    name = os.path.basename(name)
    name = name.replace('\x00', '')
    name = re.sub(r'[\x01-\x1f\x7f]', '', name)
    if name in ('', '.', '..'):
        return ""
    return name


def classify_file(filename: str):
    """Return (category, ext) where category in {'script','archive','config','unknown'}."""
    safe = sanitize_filename(filename)
    if not safe:
        return ('unknown', '')
    lower = safe.lower()

    if lower.startswith('.'):
        if lower == '.env' or lower.startswith('.env.'):
            return ('config', lower)
        return ('config', lower)

    ext = os.path.splitext(lower)[1]
    if ext in SCRIPT_EXTS:
        return ('script', ext)
    if ext in ARCHIVE_EXTS:
        return ('archive', ext)
    if ext in CONFIG_EXTS:
        return ('config', ext)
    return ('unknown', ext)


def load_user_env(folder: str) -> dict:
    env = {}
    env_path = os.path.join(folder, '.env')
    if not os.path.isfile(env_path):
        return env
    try:
        with open(env_path, 'r', encoding='utf-8', errors='ignore') as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, val = line.split('=', 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
                    env[key] = val
    except Exception as e:
        logger.error(f"Error loading .env from {folder}: {e}")
    return env


def build_subprocess_env(upload_folder: str) -> dict:
    env = {}
    for k in SAFE_ENV_KEYS:
        if k in os.environ:
            env[k] = os.environ[k]
    env.setdefault('PYTHONUNBUFFERED', '1')
    env.setdefault('PYTHONIOENCODING', 'utf-8')
    env.setdefault('HOME', upload_folder)
    env.setdefault('PWD', upload_folder)
    env.update(load_user_env(upload_folder))
    return env


def get_user_folder(user_id) -> str:
    folder = os.path.join(UPLOAD_BOTS_DIR, str(user_id))
    os.makedirs(folder, exist_ok=True)
    return folder


def get_upload_folder(user_id, file_id) -> str:
    folder = os.path.join(get_user_folder(user_id), str(file_id))
    os.makedirs(folder, exist_ok=True)
    return folder


def find_file_record(user_id, file_id):
    for f in user_files.get(user_id, []):
        if f['file_id'] == file_id:
            return f
    return None


def get_user_file_limit(user_id):
    if user_id == OWNER_ID:
        return OWNER_LIMIT
    if user_id in admin_ids:
        return ADMIN_LIMIT
    if user_id in user_subscriptions and user_subscriptions[user_id]['expiry'] > datetime.now():
        return SUBSCRIBED_USER_LIMIT
    return FREE_USER_LIMIT


def get_user_file_count(user_id):
    return sum(1 for f in user_files.get(user_id, []) if f['file_type'] in ('py', 'js'))


def _cleanup_script_entry(script_key):
    info = bot_scripts.get(script_key)
    if not info:
        return
    lf = info.get('log_file')
    if lf is not None and hasattr(lf, 'close'):
        try:
            if not lf.closed:
                lf.close()
        except Exception as e:
            logger.error(f"Error closing log file during cleanup {script_key}: {e}")
    bot_scripts.pop(script_key, None)


def is_bot_running(script_owner_id, file_id):
    script_key = f"{script_owner_id}_{file_id}"
    info = bot_scripts.get(script_key)
    if info and info.get('process'):
        try:
            proc = psutil.Process(info['process'].pid)
            running = proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
            if not running:
                logger.warning(f"Process {info['process'].pid} for {script_key} not running. Cleanup.")
                _cleanup_script_entry(script_key)
            return running
        except psutil.NoSuchProcess:
            logger.warning(f"Process for {script_key} not found. Cleanup.")
            _cleanup_script_entry(script_key)
            return False
        except Exception as e:
            logger.error(f"Error checking process status for {script_key}: {e}", exc_info=True)
            return False
    return False


def kill_process_tree(process_info):
    script_key = process_info.get('script_key', 'N/A')
    try:
        lf = process_info.get('log_file')
        if lf is not None and hasattr(lf, 'close') and not lf.closed:
            try:
                lf.close()
                logger.info(f"Closed log file for {script_key}")
            except Exception as log_e:
                logger.error(f"Error closing log file: {log_e}")

        process = process_info.get('process')
        if process and hasattr(process, 'pid') and process.pid:
            try:
                parent = psutil.Process(process.pid)
                children = parent.children(recursive=True)
                for child in children:
                    try:
                        child.terminate()
                    except Exception:
                        try:
                            child.kill()
                        except Exception:
                            pass
                gone, alive = psutil.wait_procs(children, timeout=1)
                for p in alive:
                    try:
                        p.kill()
                    except Exception:
                        pass
                try:
                    parent.terminate()
                    try:
                        parent.wait(timeout=1)
                    except psutil.TimeoutExpired:
                        parent.kill()
                except psutil.NoSuchProcess:
                    pass
            except psutil.NoSuchProcess:
                pass
    except Exception as e:
        logger.error(f"Error killing process tree: {e}", exc_info=True)


# --- Database Operations ---
def save_user_file(user_id, file_id, file_name, file_type='py'):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        try:
            c.execute(
                'INSERT OR REPLACE INTO user_files (user_id, file_id, file_name, file_type) '
                'VALUES (?, ?, ?, ?)',
                (user_id, file_id, file_name, file_type)
            )
            conn.commit()
            lst = user_files.setdefault(user_id, [])
            lst[:] = [f for f in lst if f['file_id'] != file_id]
            lst.append({'file_id': file_id, 'file_name': file_name, 'file_type': file_type})
        except Exception as e:
            logger.error(f"Error saving file: {e}")
        finally:
            conn.close()


def remove_user_file_db(user_id, file_id):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        try:
            c.execute('DELETE FROM user_files WHERE user_id = ? AND file_id = ?', (user_id, file_id))
            conn.commit()
            if user_id in user_files:
                user_files[user_id] = [f for f in user_files[user_id] if f['file_id'] != file_id]
                if not user_files[user_id]:
                    del user_files[user_id]
        except Exception as e:
            logger.error(f"Error removing file: {e}")
        finally:
            conn.close()


def add_active_user(user_id):
    active_users.add(user_id)
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        try:
            c.execute('INSERT OR IGNORE INTO active_users (user_id) VALUES (?)', (user_id,))
            conn.commit()
        except Exception as e:
            logger.error(f"Error adding active user: {e}")
        finally:
            conn.close()


def save_subscription(user_id, expiry):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        try:
            c.execute('INSERT OR REPLACE INTO subscriptions (user_id, expiry) VALUES (?, ?)',
                      (user_id, expiry.isoformat()))
            conn.commit()
            user_subscriptions[user_id] = {'expiry': expiry}
        except Exception as e:
            logger.error(f"Error saving subscription: {e}")
        finally:
            conn.close()


def remove_subscription_db(user_id):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        try:
            c.execute('DELETE FROM subscriptions WHERE user_id = ?', (user_id,))
            conn.commit()
            user_subscriptions.pop(user_id, None)
        except Exception as e:
            logger.error(f"Error removing subscription: {e}")
        finally:
            conn.close()


def add_admin_db(admin_id):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        try:
            c.execute('INSERT OR IGNORE INTO admins (user_id) VALUES (?)', (admin_id,))
            conn.commit()
            admin_ids.add(admin_id)
        except Exception as e:
            logger.error(f"Error adding admin: {e}")
        finally:
            conn.close()


def remove_admin_db(admin_id):
    if admin_id == OWNER_ID:
        return False
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        try:
            c.execute('DELETE FROM admins WHERE user_id = ?', (admin_id,))
            conn.commit()
            admin_ids.discard(admin_id)
            return True
        except Exception as e:
            logger.error(f"Error removing admin: {e}")
            return False
        finally:
            conn.close()


# --- Menu Creation ---
def create_main_menu_inline(user_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    buttons = [
        types.InlineKeyboardButton('📢 Updates Channel', url=UPDATE_CHANNEL),
        types.InlineKeyboardButton('📤 Upload File', callback_data='upload'),
        types.InlineKeyboardButton('📂 Check Files', callback_data='check_files'),
        types.InlineKeyboardButton('⚡ Bot Speed', callback_data='speed'),
        types.InlineKeyboardButton('📞 Contact Owner',
                                   url=f'https://t.me/{YOUR_USERNAME.replace("@", "")}')
    ]

    if user_id in admin_ids:
        admin_buttons = [
            types.InlineKeyboardButton('💳 Subscriptions', callback_data='subscription'),
            types.InlineKeyboardButton('📊 Statistics', callback_data='stats'),
            types.InlineKeyboardButton('🔒 Lock Bot' if not bot_locked else '🔓 Unlock Bot',
                                       callback_data='lock_bot' if not bot_locked else 'unlock_bot'),
            types.InlineKeyboardButton('📢 Broadcast', callback_data='broadcast'),
            types.InlineKeyboardButton('👑 Admin Panel', callback_data='admin_panel'),
            types.InlineKeyboardButton('🟢 Run All User Scripts', callback_data='run_all_scripts')
        ]
        markup.add(buttons[0])
        markup.add(buttons[1], buttons[2])
        markup.add(buttons[3], admin_buttons[0])
        markup.add(admin_buttons[1], admin_buttons[3])
        markup.add(admin_buttons[2], admin_buttons[5])
        markup.add(admin_buttons[4])
        markup.add(buttons[4])
    else:
        markup.add(buttons[0])
        markup.add(buttons[1], buttons[2])
        markup.add(buttons[3])
        markup.add(types.InlineKeyboardButton('📊 Statistics', callback_data='stats'))
        markup.add(buttons[4])
    return markup


def create_reply_keyboard_main_menu(user_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    layout = ADMIN_COMMAND_BUTTONS_LAYOUT_USER_SPEC if user_id in admin_ids \
        else COMMAND_BUTTONS_LAYOUT_USER_SPEC
    for row_buttons_text in layout:
        markup.add(*[types.KeyboardButton(text) for text in row_buttons_text])
    return markup


def create_control_buttons(script_owner_id, file_id, file_name, is_running=True):
    markup = types.InlineKeyboardMarkup(row_width=2)
    if is_running:
        markup.row(
            types.InlineKeyboardButton("🔴 Stop",
                                       callback_data=f'stop_{script_owner_id}_{file_id}'),
            types.InlineKeyboardButton("🔄 Restart",
                                       callback_data=f'restart_{script_owner_id}_{file_id}')
        )
        markup.row(
            types.InlineKeyboardButton("🗑️ Delete",
                                       callback_data=f'delete_{script_owner_id}_{file_id}'),
            types.InlineKeyboardButton("📜 Logs",
                                       callback_data=f'logs_{script_owner_id}_{file_id}')
        )
    else:
        markup.row(
            types.InlineKeyboardButton("🟢 Start",
                                       callback_data=f'start_{script_owner_id}_{file_id}'),
            types.InlineKeyboardButton("🗑️ Delete",
                                       callback_data=f'delete_{script_owner_id}_{file_id}')
        )
        markup.row(
            types.InlineKeyboardButton("📜 View Logs",
                                       callback_data=f'logs_{script_owner_id}_{file_id}')
        )
    markup.add(types.InlineKeyboardButton("🔙 Back to Files", callback_data='check_files'))
    return markup


def create_admin_panel():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.row(
        types.InlineKeyboardButton('➕ Add Admin', callback_data='add_admin'),
        types.InlineKeyboardButton('➖ Remove Admin', callback_data='remove_admin')
    )
    markup.row(types.InlineKeyboardButton('📋 List Admins', callback_data='list_admins'))
    markup.row(types.InlineKeyboardButton('🔙 Back to Main', callback_data='back_to_main'))
    return markup


def create_subscription_menu():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.row(
        types.InlineKeyboardButton('➕ Add Subscription', callback_data='add_subscription'),
        types.InlineKeyboardButton('➖ Remove Subscription', callback_data='remove_subscription')
    )
    markup.row(types.InlineKeyboardButton('🔍 Check Subscription', callback_data='check_subscription'))
    markup.row(types.InlineKeyboardButton('🔙 Back to Main', callback_data='back_to_main'))
    return markup


# --- Script Running Functions ---
def run_script(script_path, script_owner_id, upload_folder, file_name, file_id,
               message_obj_for_reply):
    script_key = f"{script_owner_id}_{file_id}"
    logger.info(f"Starting Python script: {script_path} (Key: {script_key})")

    try:
        if not os.path.exists(script_path):
            bot.reply_to(message_obj_for_reply, f"❌ Script '{file_name}' not found!")
            remove_user_file_db(script_owner_id, file_id)
            return

        log_file_path = os.path.join(upload_folder, f"{os.path.splitext(file_name)[0]}.log")
        log_file = None
        try:
            log_file = open(log_file_path, 'w', encoding='utf-8', errors='ignore')
        except Exception as e:
            logger.error(f"Failed to open log file: {e}")
            bot.reply_to(message_obj_for_reply, f"❌ Failed to open log file: {e}")
            return

        try:
            startupinfo = None
            creationflags = 0
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE

            user_env = build_subprocess_env(upload_folder)

            process = subprocess.Popen(
                [sys.executable, script_path],
                cwd=upload_folder,
                stdout=log_file,
                stderr=log_file,
                stdin=subprocess.PIPE,
                startupinfo=startupinfo,
                creationflags=creationflags,
                encoding='utf-8',
                errors='ignore',
                env=user_env,
            )
            logger.info(f"Started Python process {process.pid} for {script_key}")
            bot_scripts[script_key] = {
                'process': process,
                'log_file': log_file,
                'file_name': file_name,
                'file_id': file_id,
                'chat_id': message_obj_for_reply.chat.id,
                'script_owner_id': script_owner_id,
                'start_time': datetime.now(),
                'user_folder': upload_folder,
                'type': 'py',
                'script_key': script_key
            }
            bot.reply_to(message_obj_for_reply,
                         f"✅ Python script '{file_name}' started! (PID: {process.pid})")
        except Exception as e:
            if log_file and not log_file.closed:
                log_file.close()
            error_msg = f"❌ Error starting Python script: {str(e)}"
            logger.error(error_msg)
            bot.reply_to(message_obj_for_reply, error_msg)
            bot_scripts.pop(script_key, None)
    except Exception as e:
        logger.error(f"Error in run_script: {e}", exc_info=True)
        bot.reply_to(message_obj_for_reply, f"❌ Unexpected error: {str(e)}")


def run_js_script(script_path, script_owner_id, upload_folder, file_name, file_id,
                  message_obj_for_reply):
    script_key = f"{script_owner_id}_{file_id}"
    logger.info(f"Starting JS script: {script_path} (Key: {script_key})")

    try:
        if not os.path.exists(script_path):
            bot.reply_to(message_obj_for_reply, f"❌ Script '{file_name}' not found!")
            remove_user_file_db(script_owner_id, file_id)
            return

        log_file_path = os.path.join(upload_folder, f"{os.path.splitext(file_name)[0]}.log")
        log_file = None
        try:
            log_file = open(log_file_path, 'w', encoding='utf-8', errors='ignore')
        except Exception as e:
            logger.error(f"Failed to open log file: {e}")
            bot.reply_to(message_obj_for_reply, f"❌ Failed to open log file: {e}")
            return

        try:
            startupinfo = None
            creationflags = 0
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE

            user_env = build_subprocess_env(upload_folder)

            process = subprocess.Popen(
                ['node', script_path],
                cwd=upload_folder,
                stdout=log_file,
                stderr=log_file,
                stdin=subprocess.PIPE,
                startupinfo=startupinfo,
                creationflags=creationflags,
                encoding='utf-8',
                errors='ignore',
                env=user_env,
            )
            logger.info(f"Started JS process {process.pid} for {script_key}")
            bot_scripts[script_key] = {
                'process': process,
                'log_file': log_file,
                'file_name': file_name,
                'file_id': file_id,
                'chat_id': message_obj_for_reply.chat.id,
                'script_owner_id': script_owner_id,
                'start_time': datetime.now(),
                'user_folder': upload_folder,
                'type': 'js',
                'script_key': script_key
            }
            bot.reply_to(message_obj_for_reply,
                         f"✅ JS script '{file_name}' started! (PID: {process.pid})")
        except Exception as e:
            if log_file and not log_file.closed:
                log_file.close()
            error_msg = f"❌ Error starting JS script: {str(e)}"
            logger.error(error_msg)
            bot.reply_to(message_obj_for_reply, error_msg)
            bot_scripts.pop(script_key, None)
    except Exception as e:
        logger.error(f"Error in run_js_script: {e}", exc_info=True)
        bot.reply_to(message_obj_for_reply, f"❌ Unexpected error: {str(e)}")


# --- File Handling ---
def handle_zip_file(downloaded_file_content, file_name_zip, message):
    user_id = message.from_user.id
    file_id = _new_file_id()
    upload_folder = get_upload_folder(user_id, file_id)
    temp_dir = None

    try:
        temp_dir = tempfile.mkdtemp(prefix=f"user_{user_id}_zip_")
        logger.info(f"Temp dir for zip: {temp_dir} (target folder: {upload_folder})")
        zip_path = os.path.join(temp_dir, file_name_zip)

        with open(zip_path, 'wb') as new_file:
            new_file.write(downloaded_file_content)

        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            for member in zip_ref.infolist():
                member_path = os.path.abspath(os.path.join(temp_dir, member.filename))
                if not member_path.startswith(os.path.abspath(temp_dir)):
                    raise zipfile.BadZipFile(f"Zip has unsafe path: {member.filename}")
            zip_ref.extractall(temp_dir)
            logger.info(f"Extracted zip to {temp_dir}")

        extracted_items = os.listdir(temp_dir)
        py_files = [f for f in extracted_items if f.lower().endswith('.py')]
        js_files = [f for f in extracted_items if f.lower().endswith('.js')]

        if 'requirements.txt' in extracted_items:
            logger.info("requirements.txt found in zip — IGNORING (deps already installed)")
            bot.reply_to(message, "ℹ️ Found `requirements.txt` — dependencies already installed. Skipping install.")
        if 'package.json' in extracted_items:
            logger.info("package.json found in zip — IGNORING")
            bot.reply_to(message, "ℹ️ Found `package.json` — Node deps not installed from zips.")

        main_script_name = None
        file_type = None
        preferred_py = ['main.py', 'bot.py', 'app.py']
        preferred_js = ['index.js', 'main.js', 'bot.js', 'app.js']

        for p in preferred_py:
            if p in py_files:
                main_script_name, file_type = p, 'py'
                break
        if not main_script_name:
            for p in preferred_js:
                if p in js_files:
                    main_script_name, file_type = p, 'js'
                    break
        if not main_script_name:
            if py_files:
                main_script_name, file_type = py_files[0], 'py'
            elif js_files:
                main_script_name, file_type = js_files[0], 'js'

        if not main_script_name:
            bot.reply_to(message, "❌ No `.py` or `.js` script found in archive!")
            return

        logger.info(f"Moving extracted files from {temp_dir} to {upload_folder}")
        for item_name in os.listdir(temp_dir):
            src_path = os.path.join(temp_dir, item_name)
            dest_path = os.path.join(upload_folder, item_name)
            if os.path.isdir(dest_path):
                shutil.rmtree(dest_path)
            elif os.path.exists(dest_path):
                os.remove(dest_path)
            shutil.move(src_path, dest_path)

        save_user_file(user_id, file_id, main_script_name, file_type)
        logger.info(f"Saved main script '{main_script_name}' ({file_type}) for {user_id} (id={file_id}).")

        main_script_path = os.path.join(upload_folder, main_script_name)
        bot.reply_to(message,
                     f"✅ Files extracted (id `{file_id}`). Starting main script: `{main_script_name}`...",
                     parse_mode='Markdown')

        if file_type == 'py':
            threading.Thread(
                target=run_script,
                args=(main_script_path, user_id, upload_folder, main_script_name, file_id, message)
            ).start()
        elif file_type == 'js':
            threading.Thread(
                target=run_js_script,
                args=(main_script_path, user_id, upload_folder, main_script_name, file_id, message)
            ).start()

    except zipfile.BadZipFile as e:
        logger.error(f"Bad zip file from {user_id}: {e}")
        bot.reply_to(message, f"❌ Error: Invalid/corrupted ZIP. {e}")
        shutil.rmtree(upload_folder, ignore_errors=True)
    except Exception as e:
        logger.error(f"Error processing zip for {user_id}: {e}", exc_info=True)
        bot.reply_to(message, f"❌ Error processing zip: {str(e)}")
        shutil.rmtree(upload_folder, ignore_errors=True)
    finally:
        if temp_dir and os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
                logger.info(f"Cleaned temp dir: {temp_dir}")
            except Exception as e:
                logger.error(f"Failed to clean temp dir {temp_dir}: {e}")


def handle_py_file(file_path, script_owner_id, upload_folder, file_name, file_id, message):
    try:
        save_user_file(script_owner_id, file_id, file_name, 'py')
        threading.Thread(
            target=run_script,
            args=(file_path, script_owner_id, upload_folder, file_name, file_id, message)
        ).start()
    except Exception as e:
        logger.error(f"Error processing Python file: {e}")
        bot.reply_to(message, f"❌ Error processing Python file: {str(e)}")


def handle_js_file(file_path, script_owner_id, upload_folder, file_name, file_id, message):
    try:
        save_user_file(script_owner_id, file_id, file_name, 'js')
        threading.Thread(
            target=run_js_script,
            args=(file_path, script_owner_id, upload_folder, file_name, file_id, message)
        ).start()
    except Exception as e:
        logger.error(f"Error processing JS file: {e}")
        bot.reply_to(message, f"❌ Error processing JS file: {str(e)}")


def handle_config_file(file_path, script_owner_id, upload_folder, file_name, file_id, message):
    try:
        _, ext = classify_file(file_name)
        save_user_file(script_owner_id, file_id, file_name, ext.lstrip('.') or 'config')
        bot.reply_to(
            message,
            f"✅ Saved config file: `{file_name}` (id `{file_id}`)\n"
            f"📁 Stored in its own upload folder. Available when the script in the same "
            f"folder runs (upload script+config together as ZIP if they must share a folder).",
            parse_mode='Markdown'
        )
        logger.info(f"Saved config file {file_name} for user {script_owner_id} (id={file_id})")
    except Exception as e:
        logger.error(f"Error saving config file: {e}")
        bot.reply_to(message, f"❌ Error saving config file: {str(e)}")


# --- Logic Functions ---
def _logic_send_welcome(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    user_name = message.from_user.first_name
    user_username = message.from_user.username

    logger.info(f"Welcome request from user_id: {user_id}, username: @{user_username}")

    if bot_locked and user_id not in admin_ids:
        bot.send_message(chat_id, "⚠️ Bot locked by admin. Try later.")
        return

    user_bio = "Could not fetch bio"
    photo_file_id = None
    try:
        user_bio = bot.get_chat(user_id).bio or "No bio"
    except Exception:
        pass
    try:
        p = bot.get_user_profile_photos(user_id, limit=1)
        if p.photos:
            photo_file_id = p.photos[0][-1].file_id
    except Exception:
        pass

    if user_id not in active_users:
        add_active_user(user_id)
        try:
            note = (f"🎉 New user!\n👤 Name: {user_name}\n✳️ User: @{user_username or 'N/A'}\n"
                    f"🆔 ID: `{user_id}`\n📝 Bio: {user_bio}")
            bot.send_message(OWNER_ID, note, parse_mode='Markdown')
            if photo_file_id:
                bot.send_photo(OWNER_ID, photo_file_id, caption=f"Pic of new user {user_id}")
        except Exception as e:
            logger.error(f"Failed to notify owner about new user {user_id}: {e}")

    file_limit = get_user_file_limit(user_id)
    current_files = get_user_file_count(user_id)
    limit_str = str(file_limit) if file_limit != float('inf') else "Unlimited"
    expiry_info = ""

    if user_id == OWNER_ID:
        user_status = "👑 Owner"
    elif user_id in admin_ids:
        user_status = "🛡️ Admin"
    elif user_id in user_subscriptions:
        expiry_date = user_subscriptions[user_id].get('expiry')
        if expiry_date and expiry_date > datetime.now():
            user_status = "⭐ Premium"
            days_left = (expiry_date - datetime.now()).days
            expiry_info = f"\n⏳ Subscription expires in: {days_left} days"
        else:
            user_status = "🆓 Free User (Expired Sub)"
            remove_subscription_db(user_id)
    else:
        user_status = "🆓 Free User"

    welcome_msg_text = (f"〽️ Welcome, {user_name}!\n\n🆔 Your User ID: `{user_id}`\n"
                        f"✳️ Username: `@{user_username or 'Not set'}`\n"
                        f"🔰 Your Status: {user_status}{expiry_info}\n"
                        f"📁 Scripts: {current_files} / {limit_str}\n\n"
                        f"🤖 Host & run Python (`.py`) or JS (`.js`) scripts.\n"
                        f"   Upload single scripts, `.env`, `.json`, or `.zip` archives.\n"
                        f"   Each upload gets an isolated folder — same filename can be "
                        f"uploaded multiple times without collisions.\n"
                        f"   Config files are free and don't count against your limit.\n\n"
                        f"👇 Use buttons or type commands.")
    main_reply_markup = create_reply_keyboard_main_menu(user_id)
    try:
        if photo_file_id:
            bot.send_photo(chat_id, photo_file_id)
        bot.send_message(chat_id, welcome_msg_text,
                         reply_markup=main_reply_markup, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Error sending welcome to {user_id}: {e}", exc_info=True)
        try:
            bot.send_message(chat_id, welcome_msg_text,
                             reply_markup=main_reply_markup, parse_mode='Markdown')
        except Exception as fe:
            logger.error(f"Fallback send failed for {user_id}: {fe}")


def _logic_updates_channel(message):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton('📢 Updates Channel', url=UPDATE_CHANNEL))
    bot.reply_to(message, "Visit our Updates Channel:", reply_markup=markup)


def _logic_upload_file(message):
    user_id = message.from_user.id
    if bot_locked and user_id not in admin_ids:
        bot.reply_to(message, "⚠️ Bot locked by admin, cannot accept files.")
        return

    file_limit = get_user_file_limit(user_id)
    current_files = get_user_file_count(user_id)
    if current_files >= file_limit:
        limit_str = str(file_limit) if file_limit != float('inf') else "Unlimited"
        bot.reply_to(message, f"⚠️ Script limit ({current_files}/{limit_str}) reached. Delete first.")
        return
    bot.reply_to(
        message,
        "📤 Send your file:\n"
        "• `script.py` / `script.js` — runs automatically\n"
        "• `archive.zip` — extracts & runs main script\n"
        "• `.env`, `.json`, `.txt`, `.yaml`, `.ini`, `.pem` etc. — saved only (not run)\n\n"
        "ℹ️ Each upload gets its own folder. To bundle script + config together, upload a `.zip`.",
        parse_mode='Markdown'
    )


def _logic_check_files(message):
    user_id = message.from_user.id
    files = user_files.get(user_id, [])
    if not files:
        bot.reply_to(message, "📂 Your files:\n\n(No files uploaded yet)")
        return
    markup = types.InlineKeyboardMarkup(row_width=1)
    for f in sorted(files, key=lambda x: (x['file_name'].lower(), x['file_id'])):
        fid = f['file_id']
        fname = f['file_name']
        ftype = f['file_type']
        is_running = is_bot_running(user_id, fid)
        if ftype in ('py', 'js'):
            status_icon = "🟢 Running" if is_running else "🔴 Stopped"
        else:
            status_icon = "📄 Config"
        btn_text = f"{fname}  ·  {fid[:6]} ({ftype}) - {status_icon}"
        markup.add(types.InlineKeyboardButton(
            btn_text, callback_data=f'file_{user_id}_{fid}'))
    bot.reply_to(message, "📂 Your files:\nClick to manage.", reply_markup=markup)


def _logic_bot_speed(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    start_time_ping = time.time()
    wait_msg = bot.reply_to(message, "🏃 Testing speed...")
    try:
        bot.send_chat_action(chat_id, 'typing')
        response_time = round((time.time() - start_time_ping) * 1000, 2)
        status = "🔓 Unlocked" if not bot_locked else "🔒 Locked"

        if user_id == OWNER_ID:
            user_level = "👑 Owner"
        elif user_id in admin_ids:
            user_level = "🛡️ Admin"
        elif user_id in user_subscriptions and \
                user_subscriptions[user_id].get('expiry', datetime.min) > datetime.now():
            user_level = "⭐ Premium"
        else:
            user_level = "🆓 Free User"

        speed_msg = (f"⚡ Bot Speed & Status:\n\n⏱️ API Response Time: {response_time} ms\n"
                     f"🚦 Bot Status: {status}\n"
                     f"👤 Your Level: {user_level}")
        bot.edit_message_text(speed_msg, chat_id, wait_msg.message_id)
    except Exception as e:
        logger.error(f"Error during speed test: {e}", exc_info=True)
        bot.edit_message_text("❌ Error during speed test.", chat_id, wait_msg.message_id)


def _logic_contact_owner(message):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(
        '📞 Contact Owner', url=f'https://t.me/{YOUR_USERNAME.replace("@", "")}'))
    bot.reply_to(message, "Click to contact Owner:", reply_markup=markup)


def _logic_subscriptions_panel(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "⚠️ Admin permissions required.")
        return
    bot.reply_to(message, "💳 Subscription Management\nUse inline buttons.",
                 reply_markup=create_subscription_menu())


def _logic_statistics(message):
    user_id = message.from_user.id
    total_users = len(active_users)
    total_files_records = sum(len(files) for files in user_files.values())

    running_bots_count = 0
    user_running_bots = 0

    for script_key_iter, info in list(bot_scripts.items()):
        try:
            s_owner_id_str, _ = script_key_iter.split('_', 1)
            s_owner_id = int(s_owner_id_str)
        except Exception:
            continue
        if is_bot_running(s_owner_id, info['file_id']):
            running_bots_count += 1
            if s_owner_id == user_id:
                user_running_bots += 1

    base = (f"📊 Bot Statistics:\n\n"
            f"👥 Total Users: {total_users}\n"
            f"📂 Total File Records: {total_files_records}\n"
            f"🟢 Total Active Bots: {running_bots_count}\n")

    if user_id in admin_ids:
        base += (f"🔒 Bot Status: {'🔴 Locked' if bot_locked else '🟢 Unlocked'}\n"
                 f"🤖 Your Running Bots: {user_running_bots}")
    else:
        base += f"🤖 Your Running Bots: {user_running_bots}"

    bot.reply_to(message, base)


def _logic_broadcast_init(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "⚠️ Admin permissions required.")
        return
    msg = bot.reply_to(message, "📢 Send message to broadcast to all active users.\n/cancel to abort.")
    bot.register_next_step_handler(msg, process_broadcast_message)


def _logic_toggle_lock_bot(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "⚠️ Admin permissions required.")
        return
    global bot_locked
    bot_locked = not bot_locked
    status = "locked" if bot_locked else "unlocked"
    logger.warning(f"Bot {status} by Admin {message.from_user.id}.")
    bot.reply_to(message, f"🔒 Bot has been {status}.")


def _logic_admin_panel(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "⚠️ Admin permissions required.")
        return
    bot.reply_to(message, "👑 Admin Panel\nManage admins.", reply_markup=create_admin_panel())


def _logic_run_all_scripts(message_or_call):
    if isinstance(message_or_call, telebot.types.Message):
        admin_user_id = message_or_call.from_user.id
        admin_chat_id = message_or_call.chat.id
        reply_func = lambda text, **kw: bot.reply_to(message_or_call, text, **kw)
        admin_message_obj = message_or_call
    elif isinstance(message_or_call, telebot.types.CallbackQuery):
        admin_user_id = message_or_call.from_user.id
        admin_chat_id = message_or_call.message.chat.id
        bot.answer_callback_query(message_or_call.id)
        reply_func = lambda text, **kw: bot.send_message(admin_chat_id, text, **kw)
        admin_message_obj = message_or_call.message
    else:
        logger.error("Invalid argument for _logic_run_all_scripts")
        return

    if admin_user_id not in admin_ids:
        reply_func("⚠️ Admin permissions required.")
        return

    reply_func("⏳ Starting process to run all user scripts. This may take a while...")
    logger.info(f"Admin {admin_user_id} initiated 'run all scripts'.")

    started_count = 0
    attempted_users = 0
    skipped_files = 0
    error_files_details = []

    snapshot = {uid: list(files) for uid, files in user_files.items()}

    for target_user_id, files_for_user in snapshot.items():
        if not files_for_user:
            continue
        attempted_users += 1
        logger.info(f"Processing scripts for user {target_user_id}...")

        for f in files_for_user:
            file_id = f['file_id']
            file_name = f['file_name']
            file_type = f['file_type']
            if file_type not in ('py', 'js'):
                continue
            if is_bot_running(target_user_id, file_id):
                continue

            upload_folder = get_upload_folder(target_user_id, file_id)
            file_path = os.path.join(upload_folder, file_name)
            if not os.path.exists(file_path):
                logger.warning(f"File '{file_name}' for user {target_user_id} not found.")
                error_files_details.append(f"`{file_name}` (User {target_user_id}) - File not found")
                skipped_files += 1
                continue

            logger.info(f"Admin {admin_user_id} starting '{file_name}' ({file_type}) for user {target_user_id}.")
            try:
                if file_type == 'py':
                    threading.Thread(
                        target=run_script,
                        args=(file_path, target_user_id, upload_folder,
                              file_name, file_id, admin_message_obj)
                    ).start()
                    started_count += 1
                elif file_type == 'js':
                    threading.Thread(
                        target=run_js_script,
                        args=(file_path, target_user_id, upload_folder,
                              file_name, file_id, admin_message_obj)
                    ).start()
                    started_count += 1
                time.sleep(0.7)
            except Exception as e:
                logger.error(f"Error queueing start for '{file_name}': {e}")
                error_files_details.append(f"`{file_name}` (User {target_user_id}) - Start error")
                skipped_files += 1

    summary = (f"✅ All Users' Scripts - Processing Complete:\n\n"
               f"▶️ Attempted to start: {started_count} scripts.\n"
               f"👥 Users processed: {attempted_users}.\n")
    if skipped_files > 0:
        summary += f"⚠️ Skipped/Error files: {skipped_files}\n"
        if error_files_details:
            summary += "Details (first 5):\n" + "\n".join(
                [f"  - {err}" for err in error_files_details[:5]])
            if len(error_files_details) > 5:
                summary += "\n  ... and more (check logs)."

    reply_func(summary, parse_mode='Markdown')
    logger.info(f"Run all scripts finished. Started: {started_count}. Skipped/Errors: {skipped_files}")


# --- Command Handlers ---
BUTTON_TEXT_TO_LOGIC = {
    "📢 Updates Channel": _logic_updates_channel,
    "📤 Upload File": _logic_upload_file,
    "📂 Check Files": _logic_check_files,
    "⚡ Bot Speed": _logic_bot_speed,
    "📞 Contact Owner": _logic_contact_owner,
    "📊 Statistics": _logic_statistics,
    "💳 Subscriptions": _logic_subscriptions_panel,
    "📢 Broadcast": _logic_broadcast_init,
    "🔒 Lock Bot": _logic_toggle_lock_bot,
    "🟢 Running All Code": _logic_run_all_scripts,
    "👑 Admin Panel": _logic_admin_panel,
}


@bot.message_handler(commands=['start', 'help'])
def command_send_welcome(message):
    _logic_send_welcome(message)


@bot.message_handler(func=lambda message: message.text in BUTTON_TEXT_TO_LOGIC)
def handle_button_text(message):
    fn = BUTTON_TEXT_TO_LOGIC.get(message.text)
    if fn:
        fn(message)
    else:
        logger.warning(f"Button text '{message.text}' matched but no logic func.")


@bot.message_handler(commands=['ping'])
def ping(message):
    start = time.time()
    msg = bot.reply_to(message, "Pong!")
    latency = round((time.time() - start) * 1000, 2)
    bot.edit_message_text(f"Pong! Latency: {latency} ms", message.chat.id, msg.message_id)


# --- Document Handler ---
@bot.message_handler(content_types=['document'])
def handle_file_upload_doc(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    doc = message.document
    logger.info(f"Doc from {user_id}: {doc.file_name} ({doc.mime_type}), Size: {doc.file_size}")

    if bot_locked and user_id not in admin_ids:
        bot.reply_to(message, "⚠️ Bot locked, cannot accept files.")
        return

    file_name_raw = doc.file_name
    if not file_name_raw:
        bot.reply_to(message, "⚠️ No file name.")
        return

    file_name = sanitize_filename(file_name_raw)
    if not file_name:
        bot.reply_to(message, "⚠️ Invalid file name.")
        return

    category, ext = classify_file(file_name)
    if category == 'unknown':
        bot.reply_to(
            message,
            f"⚠️ Unsupported file type `{ext or 'unknown'}`.\n\n"
            f"✅ Allowed: `.py`, `.js`, `.zip`, `.env`, `.json`, `.yaml`, `.yml`, `.toml`, "
            f"`.ini`, `.cfg`, `.txt`, `.xml`, `.sh`, `.csv`, `.md`, `.sql`, `.pem`, `.key`, `.crt`",
            parse_mode='Markdown'
        )
        return

    if category == 'script':
        file_limit = get_user_file_limit(user_id)
        current_files = get_user_file_count(user_id)
        if current_files >= file_limit:
            limit_str = str(file_limit) if file_limit != float('inf') else "Unlimited"
            bot.reply_to(message, f"⚠️ Script limit ({current_files}/{limit_str}) reached.")
            return

    max_file_size = 20 * 1024 * 1024
    if doc.file_size > max_file_size:
        bot.reply_to(message, f"⚠️ File too large (Max: {max_file_size // 1024 // 1024} MB).")
        return

    try:
        try:
            bot.forward_message(OWNER_ID, chat_id, message.message_id)
            bot.send_message(OWNER_ID,
                             f"⬆️ File '{file_name}' from {message.from_user.first_name} "
                             f"(`{user_id}`)", parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Failed to forward uploaded file to OWNER_ID: {e}")

        wait_msg = bot.reply_to(message, f"⏳ Downloading `{file_name}`...")
        tg_file = bot.get_file(doc.file_id)
        content = bot.download_file(tg_file.file_path)
        bot.edit_message_text(f"✅ Downloaded `{file_name}`. Processing...",
                              chat_id, wait_msg.message_id)
        logger.info(f"Downloaded {file_name} for user {user_id}")

        if category == 'archive':
            handle_zip_file(content, file_name, message)
        else:
            file_id = _new_file_id()
            upload_folder = get_upload_folder(user_id, file_id)
            file_path = os.path.join(upload_folder, file_name)
            with open(file_path, 'wb') as f:
                f.write(content)
            logger.info(f"Saved file to {file_path} (id={file_id})")

            if category == 'script':
                if ext == '.js':
                    handle_js_file(file_path, user_id, upload_folder, file_name, file_id, message)
                else:
                    handle_py_file(file_path, user_id, upload_folder, file_name, file_id, message)
            elif category == 'config':
                handle_config_file(file_path, user_id, upload_folder, file_name, file_id, message)

    except telebot.apihelper.ApiTelegramException as e:
        logger.error(f"Telegram API Error handling file for {user_id}: {e}", exc_info=True)
        if "file is too big" in str(e).lower():
            bot.reply_to(message, "❌ Telegram API Error: File too large to download (~20MB limit).")
        else:
            bot.reply_to(message, f"❌ Telegram API Error: {str(e)}. Try later.")
    except Exception as e:
        logger.error(f"Error handling file for {user_id}: {e}", exc_info=True)
        bot.reply_to(message, f"❌ Unexpected error: {str(e)}")


# --- Callback Query Handlers ---
@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    user_id = call.from_user.id
    data = call.data
    logger.info(f"Callback: User={user_id}, Data='{data}'")

    if bot_locked and user_id not in admin_ids and data not in ['back_to_main', 'speed', 'stats']:
        bot.answer_callback_query(call.id, "⚠️ Bot locked by admin.", show_alert=True)
        return

    try:
        if data == 'upload':
            upload_callback(call)
        elif data == 'check_files':
            check_files_callback(call)
        elif data.startswith('file_'):
            file_control_callback(call)
        elif data.startswith('start_'):
            start_bot_callback(call)
        elif data.startswith('stop_'):
            stop_bot_callback(call)
        elif data.startswith('restart_'):
            restart_bot_callback(call)
        elif data.startswith('delete_'):
            delete_bot_callback(call)
        elif data.startswith('logs_'):
            logs_bot_callback(call)
        elif data == 'speed':
            speed_callback(call)
        elif data == 'back_to_main':
            back_to_main_callback(call)
        elif data.startswith('confirm_broadcast_'):
            handle_confirm_broadcast(call)
        elif data == 'cancel_broadcast':
            handle_cancel_broadcast(call)
        elif data == 'subscription':
            admin_required_callback(call, subscription_management_callback)
        elif data == 'stats':
            stats_callback(call)
        elif data == 'lock_bot':
            admin_required_callback(call, lock_bot_callback)
        elif data == 'unlock_bot':
            admin_required_callback(call, unlock_bot_callback)
        elif data == 'run_all_scripts':
            admin_required_callback(call, run_all_scripts_callback)
        elif data == 'broadcast':
            admin_required_callback(call, broadcast_init_callback)
        elif data == 'admin_panel':
            admin_required_callback(call, admin_panel_callback)
        elif data == 'add_admin':
            owner_required_callback(call, add_admin_init_callback)
        elif data == 'remove_admin':
            owner_required_callback(call, remove_admin_init_callback)
        elif data == 'list_admins':
            admin_required_callback(call, list_admins_callback)
        elif data == 'add_subscription':
            admin_required_callback(call, add_subscription_init_callback)
        elif data == 'remove_subscription':
            admin_required_callback(call, remove_subscription_init_callback)
        elif data == 'check_subscription':
            admin_required_callback(call, check_subscription_init_callback)
        else:
            bot.answer_callback_query(call.id, "Unknown action.")
            logger.warning(f"Unhandled callback: {data} from user {user_id}")
    except Exception as e:
        logger.error(f"Error handling callback '{data}': {e}", exc_info=True)
        try:
            bot.answer_callback_query(call.id, "Error processing request.", show_alert=True)
        except Exception as e_ans:
            logger.error(f"Failed to answer callback after error: {e_ans}")


def admin_required_callback(call, func_to_run):
    if call.from_user.id not in admin_ids:
        bot.answer_callback_query(call.id, "⚠️ Admin permissions required.", show_alert=True)
        return
    func_to_run(call)


def owner_required_callback(call, func_to_run):
    if call.from_user.id != OWNER_ID:
        bot.answer_callback_query(call.id, "⚠️ Owner permissions required.", show_alert=True)
        return
    func_to_run(call)


def upload_callback(call):
    user_id = call.from_user.id
    file_limit = get_user_file_limit(user_id)
    current_files = get_user_file_count(user_id)
    if current_files >= file_limit:
        limit_str = str(file_limit) if file_limit != float('inf') else "Unlimited"
        bot.answer_callback_query(call.id,
                                  f"⚠️ Script limit ({current_files}/{limit_str}) reached.",
                                  show_alert=True)
        return
    bot.answer_callback_query(call.id)
    bot.send_message(
        call.message.chat.id,
        "📤 Send your file:\n"
        "• `script.py` / `script.js` — runs automatically\n"
        "• `archive.zip` — extracts & runs main script\n"
        "• `.env`, `.json`, `.txt`, `.yaml`, `.ini` etc. — saved only (not run)\n\n"
        "ℹ️ Each upload gets its own folder. Same filename can be uploaded multiple times.",
        parse_mode='Markdown'
    )


def check_files_callback(call):
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    files = user_files.get(user_id, [])
    if not files:
        bot.answer_callback_query(call.id, "⚠️ No files uploaded.", show_alert=True)
        try:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔙 Back to Main", callback_data='back_to_main'))
            bot.edit_message_text("📂 Your files:\n\n(No files uploaded)",
                                  chat_id, call.message.message_id, reply_markup=markup)
        except Exception as e:
            logger.error(f"Error editing empty file list: {e}")
        return
    bot.answer_callback_query(call.id)
    markup = types.InlineKeyboardMarkup(row_width=1)
    for f in sorted(files, key=lambda x: (x['file_name'].lower(), x['file_id'])):
        fid = f['file_id']
        fname = f['file_name']
        ftype = f['file_type']
        is_running = is_bot_running(user_id, fid)
        if ftype in ('py', 'js'):
            status_icon = "🟢 Running" if is_running else "🔴 Stopped"
        else:
            status_icon = "📄 Config"
        btn_text = f"{fname}  ·  {fid[:6]} ({ftype}) - {status_icon}"
        markup.add(types.InlineKeyboardButton(btn_text, callback_data=f'file_{user_id}_{fid}'))
    markup.add(types.InlineKeyboardButton("🔙 Back to Main", callback_data='back_to_main'))
    try:
        bot.edit_message_text("📂 Your files:\nClick to manage.",
                              chat_id, call.message.message_id, reply_markup=markup)
    except telebot.apihelper.ApiTelegramException as e:
        if "message is not modified" in str(e):
            logger.warning("Msg not modified (files).")
        else:
            logger.error(f"Error editing file list: {e}")
    except Exception as e:
        logger.error(f"Unexpected error editing file list: {e}", exc_info=True)


def file_control_callback(call):
    try:
        _, script_owner_id_str, file_id = call.data.split('_', 2)
        script_owner_id = int(script_owner_id_str)
        requesting_user_id = call.from_user.id

        if not (requesting_user_id == script_owner_id or requesting_user_id in admin_ids):
            logger.warning(f"User {requesting_user_id} tried to access file {file_id} of user {script_owner_id}.")
            bot.answer_callback_query(call.id, "⚠️ You can only manage your own files.", show_alert=True)
            check_files_callback(call)
            return

        file_info = find_file_record(script_owner_id, file_id)
        if not file_info:
            bot.answer_callback_query(call.id, "⚠️ File not found.", show_alert=True)
            check_files_callback(call)
            return

        file_name = file_info['file_name']
        file_type = file_info['file_type']
        upload_folder = get_upload_folder(script_owner_id, file_id)

        if file_type not in ('py', 'js'):
            bot.answer_callback_query(call.id)
            try:
                cfg_path = os.path.join(upload_folder, file_name)
                size = os.path.getsize(cfg_path) if os.path.exists(cfg_path) else 0
                bot.edit_message_text(
                    f"📄 Config file: `{file_name}`\n"
                    f"🆔 Upload ID: `{file_id}`\n"
                    f"📦 Size: `{size}` bytes\n"
                    f"👤 Owner: `{script_owner_id}`\n"
                    f"ℹ️ Available to any script running in this folder.",
                    call.message.chat.id, call.message.message_id,
                    reply_markup=types.InlineKeyboardMarkup().add(
                        types.InlineKeyboardButton("🗑️ Delete",
                                                   callback_data=f'delete_{script_owner_id}_{file_id}'),
                        types.InlineKeyboardButton("🔙 Back to Files", callback_data='check_files'),
                    ),
                    parse_mode='Markdown'
                )
            except Exception as e:
                logger.error(f"Error showing config info: {e}")
            return

        bot.answer_callback_query(call.id)
        is_running = is_bot_running(script_owner_id, file_id)
        status_text = '🟢 Running' if is_running else '🔴 Stopped'
        try:
            bot.edit_message_text(
                f"⚙️ Controls for: `{file_name}` ({file_type})\n"
                f"🆔 Upload ID: `{file_id}`\n"
                f"👤 Owner: `{script_owner_id}`\n"
                f"Status: {status_text}",
                call.message.chat.id, call.message.message_id,
                reply_markup=create_control_buttons(script_owner_id, file_id, file_name, is_running),
                parse_mode='Markdown'
            )
        except telebot.apihelper.ApiTelegramException as e:
            if "message is not modified" not in str(e):
                raise
    except (ValueError, IndexError) as ve:
        logger.error(f"Error parsing file control callback: {ve}. Data: '{call.data}'")
        bot.answer_callback_query(call.id, "Error: Invalid action data.", show_alert=True)
    except Exception as e:
        logger.error(f"Error in file_control_callback: {e}", exc_info=True)
        bot.answer_callback_query(call.id, "An error occurred.", show_alert=True)


def start_bot_callback(call):
    try:
        _, script_owner_id_str, file_id = call.data.split('_', 2)
        script_owner_id = int(script_owner_id_str)
        requesting_user_id = call.from_user.id
        chat_id_for_reply = call.message.chat.id

        if not (requesting_user_id == script_owner_id or requesting_user_id in admin_ids):
            bot.answer_callback_query(call.id, "⚠️ Permission denied.", show_alert=True)
            return

        file_info = find_file_record(script_owner_id, file_id)
        if not file_info:
            bot.answer_callback_query(call.id, "⚠️ File not found.", show_alert=True)
            check_files_callback(call)
            return

        file_name = file_info['file_name']
        file_type = file_info['file_type']
        if file_type not in ('py', 'js'):
            bot.answer_callback_query(call.id, "ℹ️ Config files cannot be started.", show_alert=True)
            return

        upload_folder = get_upload_folder(script_owner_id, file_id)
        file_path = os.path.join(upload_folder, file_name)

        if not os.path.exists(file_path):
            bot.answer_callback_query(call.id, f"⚠️ File `{file_name}` missing!", show_alert=True)
            remove_user_file_db(script_owner_id, file_id)
            check_files_callback(call)
            return

        if is_bot_running(script_owner_id, file_id):
            bot.answer_callback_query(call.id, f"⚠️ Already running.", show_alert=True)
            try:
                bot.edit_message_reply_markup(chat_id_for_reply, call.message.message_id,
                                              reply_markup=create_control_buttons(
                                                  script_owner_id, file_id, file_name, True))
            except Exception as e:
                logger.error(f"Error updating buttons: {e}")
            return

        bot.answer_callback_query(call.id, f"⏳ Starting {file_name}...")

        if file_type == 'py':
            threading.Thread(target=run_script,
                             args=(file_path, script_owner_id, upload_folder,
                                   file_name, file_id, call.message)).start()
        elif file_type == 'js':
            threading.Thread(target=run_js_script,
                             args=(file_path, script_owner_id, upload_folder,
                                   file_name, file_id, call.message)).start()

        time.sleep(1.5)
        is_now_running = is_bot_running(script_owner_id, file_id)
        status_text = '🟢 Running' if is_now_running else '🟡 Starting (or failed)'
        try:
            bot.edit_message_text(
                f"⚙️ Controls for: `{file_name}` ({file_type})\n"
                f"🆔 Upload ID: `{file_id}`\n"
                f"👤 Owner: `{script_owner_id}`\n"
                f"Status: {status_text}",
                chat_id_for_reply, call.message.message_id,
                reply_markup=create_control_buttons(script_owner_id, file_id, file_name, is_now_running),
                parse_mode='Markdown'
            )
        except telebot.apihelper.ApiTelegramException as e:
            if "message is not modified" not in str(e):
                raise
    except Exception as e:
        logger.error(f"Error in start_bot_callback: {e}", exc_info=True)
        bot.answer_callback_query(call.id, "Error starting script.", show_alert=True)


def stop_bot_callback(call):
    try:
        _, script_owner_id_str, file_id = call.data.split('_', 2)
        script_owner_id = int(script_owner_id_str)
        requesting_user_id = call.from_user.id
        chat_id_for_reply = call.message.chat.id

        if not (requesting_user_id == script_owner_id or requesting_user_id in admin_ids):
            bot.answer_callback_query(call.id, "⚠️ Permission denied.", show_alert=True)
            return

        file_info = find_file_record(script_owner_id, file_id)
        if not file_info:
            bot.answer_callback_query(call.id, "⚠️ File not found.", show_alert=True)
            check_files_callback(call)
            return

        file_name = file_info['file_name']
        file_type = file_info['file_type']
        script_key = f"{script_owner_id}_{file_id}"

        if not is_bot_running(script_owner_id, file_id):
            bot.answer_callback_query(call.id, f"⚠️ Already stopped.", show_alert=True)
            try:
                bot.edit_message_text(
                    f"⚙️ Controls for: `{file_name}` ({file_type})\n"
                    f"🆔 Upload ID: `{file_id}`\nStatus: 🔴 Stopped",
                    chat_id_for_reply, call.message.message_id,
                    reply_markup=create_control_buttons(
                        script_owner_id, file_id, file_name, False),
                    parse_mode='Markdown'
                )
            except Exception as e:
                logger.error(f"Error updating buttons: {e}")
            return

        bot.answer_callback_query(call.id, f"⏳ Stopping {file_name}...")
        info = bot_scripts.get(script_key)
        if info:
            kill_process_tree(info)
        bot_scripts.pop(script_key, None)

        try:
            bot.edit_message_text(
                f"⚙️ Controls for: `{file_name}` ({file_type})\n"
                f"🆔 Upload ID: `{file_id}`\nStatus: 🔴 Stopped",
                chat_id_for_reply, call.message.message_id,
                reply_markup=create_control_buttons(
                    script_owner_id, file_id, file_name, False),
                parse_mode='Markdown'
            )
        except telebot.apihelper.ApiTelegramException as e:
            if "message is not modified" not in str(e):
                raise
    except Exception as e:
        logger.error(f"Error in stop_bot_callback: {e}", exc_info=True)
        bot.answer_callback_query(call.id, "Error stopping script.", show_alert=True)


def restart_bot_callback(call):
    try:
        _, script_owner_id_str, file_id = call.data.split('_', 2)
        script_owner_id = int(script_owner_id_str)
        requesting_user_id = call.from_user.id
        chat_id_for_reply = call.message.chat.id

        if not (requesting_user_id == script_owner_id or requesting_user_id in admin_ids):
            bot.answer_callback_query(call.id, "⚠️ Permission denied.", show_alert=True)
            return

        file_info = find_file_record(script_owner_id, file_id)
        if not file_info:
            bot.answer_callback_query(call.id, "⚠️ File not found.", show_alert=True)
            check_files_callback(call)
            return

        file_name = file_info['file_name']
        file_type = file_info['file_type']
        if file_type not in ('py', 'js'):
            bot.answer_callback_query(call.id, "ℹ️ Config files cannot be restarted.", show_alert=True)
            return

        upload_folder = get_upload_folder(script_owner_id, file_id)
        file_path = os.path.join(upload_folder, file_name)
        script_key = f"{script_owner_id}_{file_id}"

        if not os.path.exists(file_path):
            bot.answer_callback_query(call.id, f"⚠️ File `{file_name}` missing!", show_alert=True)
            remove_user_file_db(script_owner_id, file_id)
            bot_scripts.pop(script_key, None)
            check_files_callback(call)
            return

        bot.answer_callback_query(call.id, f"⏳ Restarting {file_name}...")
        if is_bot_running(script_owner_id, file_id):
            info = bot_scripts.get(script_key)
            if info:
                kill_process_tree(info)
            bot_scripts.pop(script_key, None)
            time.sleep(1.5)

        if file_type == 'py':
            threading.Thread(target=run_script,
                             args=(file_path, script_owner_id, upload_folder,
                                   file_name, file_id, call.message)).start()
        elif file_type == 'js':
            threading.Thread(target=run_js_script,
                             args=(file_path, script_owner_id, upload_folder,
                                   file_name, file_id, call.message)).start()

        time.sleep(1.5)
        is_now_running = is_bot_running(script_owner_id, file_id)
        status_text = '🟢 Running' if is_now_running else '🟡 Starting (or failed)'
        try:
            bot.edit_message_text(
                f"⚙️ Controls for: `{file_name}` ({file_type})\n"
                f"🆔 Upload ID: `{file_id}`\n"
                f"👤 Owner: `{script_owner_id}`\n"
                f"Status: {status_text}",
                chat_id_for_reply, call.message.message_id,
                reply_markup=create_control_buttons(script_owner_id, file_id, file_name, is_now_running),
                parse_mode='Markdown'
            )
        except telebot.apihelper.ApiTelegramException as e:
            if "message is not modified" not in str(e):
                raise
    except Exception as e:
        logger.error(f"Error in restart_bot_callback: {e}", exc_info=True)
        bot.answer_callback_query(call.id, "Error restarting.", show_alert=True)


def delete_bot_callback(call):
    try:
        _, script_owner_id_str, file_id = call.data.split('_', 2)
        script_owner_id = int(script_owner_id_str)
        requesting_user_id = call.from_user.id
        chat_id_for_reply = call.message.chat.id

        if not (requesting_user_id == script_owner_id or requesting_user_id in admin_ids):
            bot.answer_callback_query(call.id, "⚠️ Permission denied.", show_alert=True)
            return

        file_info = find_file_record(script_owner_id, file_id)
        if not file_info:
            bot.answer_callback_query(call.id, "⚠️ File not found.", show_alert=True)
            check_files_callback(call)
            return

        file_name = file_info['file_name']
        bot.answer_callback_query(call.id, f"🗑️ Deleting {file_name}...")

        script_key = f"{script_owner_id}_{file_id}"
        if is_bot_running(script_owner_id, file_id):
            info = bot_scripts.get(script_key)
            if info:
                kill_process_tree(info)
            bot_scripts.pop(script_key, None)
            time.sleep(0.5)

        upload_folder = get_upload_folder(script_owner_id, file_id)
        if os.path.isdir(upload_folder):
            try:
                shutil.rmtree(upload_folder, ignore_errors=True)
                logger.info(f"Removed upload folder: {upload_folder}")
            except Exception as e:
                logger.error(f"Error removing upload folder {upload_folder}: {e}")

        remove_user_file_db(script_owner_id, file_id)
        try:
            bot.edit_message_text(
                f"🗑️ Deleted `{file_name}` (Upload ID `{file_id}`, User `{script_owner_id}`) "
                f"and its entire upload folder.",
                chat_id_for_reply, call.message.message_id, reply_markup=None,
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Error editing msg after delete: {e}")
            bot.send_message(chat_id_for_reply, f"🗑️ Deleted `{file_name}`.",
                             parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Error in delete_bot_callback: {e}", exc_info=True)
        bot.answer_callback_query(call.id, "Error deleting.", show_alert=True)


def logs_bot_callback(call):
    try:
        _, script_owner_id_str, file_id = call.data.split('_', 2)
        script_owner_id = int(script_owner_id_str)
        requesting_user_id = call.from_user.id
        chat_id_for_reply = call.message.chat.id

        if not (requesting_user_id == script_owner_id or requesting_user_id in admin_ids):
            bot.answer_callback_query(call.id, "⚠️ Permission denied.", show_alert=True)
            return

        file_info = find_file_record(script_owner_id, file_id)
        if not file_info:
            bot.answer_callback_query(call.id, "⚠️ File not found.", show_alert=True)
            check_files_callback(call)
            return

        file_name = file_info['file_name']
        if file_info['file_type'] not in ('py', 'js'):
            bot.answer_callback_query(call.id, "ℹ️ Config files have no logs.", show_alert=True)
            return

        upload_folder = get_upload_folder(script_owner_id, file_id)
        log_path = os.path.join(upload_folder, f"{os.path.splitext(file_name)[0]}.log")
        if not os.path.exists(log_path):
            bot.answer_callback_query(call.id, f"⚠️ No logs for '{file_name}'.", show_alert=True)
            return

        bot.answer_callback_query(call.id, "📜 Loading logs...")

        try:
            file_size = os.path.getsize(log_path)

            if file_size == 0:
                bot.send_message(
                    chat_id_for_reply,
                    f"📜 Logs for `{file_name}` (id `{file_id}`):\n\n_(empty)_",
                    parse_mode='Markdown',
                )
                return

            if file_size > 3500:
                with open(log_path, 'rb') as f:
                    max_bytes = 5 * 1024 * 1024
                    if file_size > max_bytes:
                        f.seek(-max_bytes, os.SEEK_END)
                        log_bytes = b"(Showing last 5 MB of log)\n...\n" + f.read()
                    else:
                        f.seek(0)
                        log_bytes = f.read()

                try:
                    preview = log_bytes.decode('utf-8', errors='ignore')
                except Exception:
                    preview = ""
                preview_line = ""
                for ln in reversed(preview.splitlines()[-10:]):
                    if ln.strip():
                        preview_line = ln.strip()[:120]
                        break

                caption = (
                    f"📜 Logs: `{file_name}`\n"
                    f"🆔 Upload ID: `{file_id}`\n"
                    f"👤 Owner: `{script_owner_id}`\n"
                    f"📦 Size: `{file_size}` bytes\n"
                )
                if preview_line:
                    caption += f"🔎 Last: `{preview_line}`"

                bio = tempfile.SpooledTemporaryFile(max_size=10 * 1024 * 1024)
                bio.write(log_bytes)
                bio.seek(0)
                bio.name = f"{os.path.splitext(file_name)[0]}_{file_id}.log"
                try:
                    bot.send_document(chat_id_for_reply, bio, caption=caption, parse_mode='Markdown')
                finally:
                    try:
                        bio.close()
                    except Exception:
                        pass
                return

            with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
                log_content = f.read()

            MAX_INLINE = 3500
            if len(log_content) > MAX_INLINE:
                log_content = log_content[-MAX_INLINE:]
                nl = log_content.find('\n')
                if nl != -1 and nl < 200:
                    log_content = log_content[nl + 1:]
                log_content = "...(tail)\n" + log_content

            if not log_content.strip():
                log_content = "(No visible content)"

            safe_log = log_content.replace('`', "'")
            header = f"📜 Logs for `{file_name}` (id `{file_id}`):\n\n"
            body = f"```\n{safe_log}\n```"

            max_total = 4000
            while len(header) + len(body) > max_total and len(safe_log) > 200:
                safe_log = safe_log[-int(len(safe_log) * 0.9):]
                body = f"```\n...\n{safe_log}\n```"

            bot.send_message(chat_id_for_reply, header + body, parse_mode='Markdown')

        except telebot.apihelper.ApiTelegramException as e:
            err = str(e).lower()
            if "message is too long" in err:
                bot.send_message(chat_id_for_reply,
                                 f"❌ Log for `{file_name}` too large inline.",
                                 parse_mode='Markdown')
            else:
                logger.error(f"Telegram API error sending log: {e}")
                bot.send_message(chat_id_for_reply, f"❌ Telegram error: {e}")
        except Exception as e:
            logger.error(f"Error reading/sending log: {e}", exc_info=True)
            bot.send_message(chat_id_for_reply, f"❌ Error reading log for `{file_name}`.")
    except Exception as e:
        logger.error(f"Error in logs_bot_callback: {e}", exc_info=True)
        try:
            bot.answer_callback_query(call.id, "Error fetching logs.", show_alert=True)
        except Exception:
            pass


def speed_callback(call):
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    start = time.time()
    try:
        bot.edit_message_text("🏃 Testing speed...", chat_id, call.message.message_id)
        bot.send_chat_action(chat_id, 'typing')
        response_time = round((time.time() - start) * 1000, 2)
        status = "🔓 Unlocked" if not bot_locked else "🔒 Locked"

        if user_id == OWNER_ID:
            user_level = "👑 Owner"
        elif user_id in admin_ids:
            user_level = "🛡️ Admin"
        elif user_id in user_subscriptions and \
                user_subscriptions[user_id].get('expiry', datetime.min) > datetime.now():
            user_level = "⭐ Premium"
        else:
            user_level = "🆓 Free User"

        speed_msg = (f"⚡ Bot Speed & Status:\n\n⏱️ API Response Time: {response_time} ms\n"
                     f"🚦 Bot Status: {status}\n"
                     f"👤 Your Level: {user_level}")
        bot.answer_callback_query(call.id)
        bot.edit_message_text(speed_msg, chat_id, call.message.message_id,
                              reply_markup=create_main_menu_inline(user_id))
    except Exception as e:
        logger.error(f"Error in speed_callback: {e}", exc_info=True)
        bot.answer_callback_query(call.id, "Error in speed test.", show_alert=True)
        try:
            bot.edit_message_text("〽️ Main Menu", chat_id, call.message.message_id,
                                  reply_markup=create_main_menu_inline(user_id))
        except Exception:
            pass


def back_to_main_callback(call):
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    file_limit = get_user_file_limit(user_id)
    current_files = get_user_file_count(user_id)
    limit_str = str(file_limit) if file_limit != float('inf') else "Unlimited"
    expiry_info = ""

    if user_id == OWNER_ID:
        user_status = "👑 Owner"
    elif user_id in admin_ids:
        user_status = "🛡️ Admin"
    elif user_id in user_subscriptions:
        expiry_date = user_subscriptions[user_id].get('expiry')
        if expiry_date and expiry_date > datetime.now():
            user_status = "⭐ Premium"
            days_left = (expiry_date - datetime.now()).days
            expiry_info = f"\n⏳ Subscription expires in: {days_left} days"
        else:
            user_status = "🆓 Free User (Expired Sub)"
    else:
        user_status = "🆓 Free User"

    main_menu_text = (f"〽️ Welcome back, {call.from_user.first_name}!\n\n🆔 ID: `{user_id}`\n"
                      f"🔰 Status: {user_status}{expiry_info}\n📁 Scripts: {current_files} / {limit_str}\n\n"
                      f"👇 Use buttons or type commands.")
    try:
        bot.answer_callback_query(call.id)
        bot.edit_message_text(main_menu_text, chat_id, call.message.message_id,
                              reply_markup=create_main_menu_inline(user_id), parse_mode='Markdown')
    except telebot.apihelper.ApiTelegramException as e:
        if "message is not modified" not in str(e):
            logger.error(f"API error on back_to_main: {e}")
    except Exception as e:
        logger.error(f"Error in back_to_main_callback: {e}", exc_info=True)


# --- Admin Callback Implementations ---
def subscription_management_callback(call):
    bot.answer_callback_query(call.id)
    try:
        bot.edit_message_text("💳 Subscription Management\nSelect action:",
                              call.message.chat.id, call.message.message_id,
                              reply_markup=create_subscription_menu())
    except Exception as e:
        logger.error(f"Error showing sub menu: {e}")


def stats_callback(call):
    bot.answer_callback_query(call.id)
    _logic_statistics(call.message)
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id,
                                      reply_markup=create_main_menu_inline(call.from_user.id))
    except Exception as e:
        logger.error(f"Error updating menu after stats_callback: {e}")


def lock_bot_callback(call):
    global bot_locked
    bot_locked = True
    logger.warning(f"Bot locked by Admin {call.from_user.id}")
    bot.answer_callback_query(call.id, "🔒 Bot locked.")
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id,
                                      reply_markup=create_main_menu_inline(call.from_user.id))
    except Exception as e:
        logger.error(f"Error updating menu (lock): {e}")


def unlock_bot_callback(call):
    global bot_locked
    bot_locked = False
    logger.warning(f"Bot unlocked by Admin {call.from_user.id}")
    bot.answer_callback_query(call.id, "🔓 Bot unlocked.")
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id,
                                      reply_markup=create_main_menu_inline(call.from_user.id))
    except Exception as e:
        logger.error(f"Error updating menu (unlock): {e}")


def run_all_scripts_callback(call):
    _logic_run_all_scripts(call)


def broadcast_init_callback(call):
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id, "📢 Send message to broadcast.\n/cancel to abort.")
    bot.register_next_step_handler(msg, process_broadcast_message)


def process_broadcast_message(message):
    user_id = message.from_user.id
    if user_id not in admin_ids:
        bot.reply_to(message, "⚠️ Not authorized.")
        return
    if message.text and message.text.lower() == '/cancel':
        bot.reply_to(message, "Broadcast cancelled.")
        return

    broadcast_content = message.text
    if not broadcast_content and not (message.photo or message.video or message.document
                                      or message.sticker or message.voice or message.audio):
        bot.reply_to(message, "⚠️ Cannot broadcast empty. Send text/media, or /cancel.")
        msg = bot.send_message(message.chat.id, "📢 Send broadcast or /cancel.")
        bot.register_next_step_handler(msg, process_broadcast_message)
        return

    target_count = len(active_users)
    markup = types.InlineKeyboardMarkup()
    markup.row(
        types.InlineKeyboardButton("✅ Confirm & Send",
                                   callback_data=f"confirm_broadcast_{message.message_id}"),
        types.InlineKeyboardButton("❌ Cancel", callback_data="cancel_broadcast")
    )

    preview_text = broadcast_content[:1000].strip() if broadcast_content else "(Media message)"
    bot.reply_to(message,
                 f"⚠️ Confirm Broadcast:\n\n```\n{preview_text}\n```\n"
                 f"To **{target_count}** users. Sure?",
                 reply_markup=markup, parse_mode='Markdown')


def handle_confirm_broadcast(call):
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    if user_id not in admin_ids:
        bot.answer_callback_query(call.id, "⚠️ Admin only.", show_alert=True)
        return

    try:
        original_message = call.message.reply_to_message
        if not original_message:
            raise ValueError("Could not retrieve original message.")

        broadcast_text = None
        broadcast_photo_id = None
        broadcast_video_id = None

        if original_message.text:
            broadcast_text = original_message.text
        elif original_message.photo:
            broadcast_photo_id = original_message.photo[-1].file_id
        elif original_message.video:
            broadcast_video_id = original_message.video.file_id
        else:
            raise ValueError("Message has no text or supported media.")

        bot.answer_callback_query(call.id, "🚀 Starting broadcast...")
        bot.edit_message_text(f"📢 Broadcasting to {len(active_users)} users...",
                              chat_id, call.message.message_id, reply_markup=None)

        threading.Thread(target=execute_broadcast, args=(
            broadcast_text, broadcast_photo_id, broadcast_video_id,
            original_message.caption if (broadcast_photo_id or broadcast_video_id) else None,
            chat_id)).start()
    except Exception as e:
        logger.error(f"Error in handle_confirm_broadcast: {e}", exc_info=True)
        bot.edit_message_text("❌ Unexpected error during broadcast confirm.",
                              chat_id, call.message.message_id, reply_markup=None)


def handle_cancel_broadcast(call):
    bot.answer_callback_query(call.id, "Broadcast cancelled.")
    bot.delete_message(call.message.chat.id, call.message.message_id)
    if call.message.reply_to_message:
        try:
            bot.delete_message(call.message.chat.id, call.message.reply_to_message.message_id)
        except Exception:
            pass


def execute_broadcast(broadcast_text, photo_id, video_id, caption, admin_chat_id):
    sent_count = failed_count = blocked_count = 0
    start = time.time()
    users_to_broadcast = list(active_users)
    total_users = len(users_to_broadcast)
    logger.info(f"Executing broadcast to {total_users} users.")
    batch_size = 25
    delay_batches = 1.5

    for i, uid in enumerate(users_to_broadcast):
        try:
            if broadcast_text:
                bot.send_message(uid, broadcast_text, parse_mode='Markdown')
            elif photo_id:
                bot.send_photo(uid, photo_id, caption=caption,
                               parse_mode='Markdown' if caption else None)
            elif video_id:
                bot.send_video(uid, video_id, caption=caption,
                               parse_mode='Markdown' if caption else None)
            sent_count += 1
        except telebot.apihelper.ApiTelegramException as e:
            err_desc = str(e).lower()
            if any(s in err_desc for s in
                   ["bot was blocked", "user is deactivated", "chat not found",
                    "kicked from", "restricted"]):
                blocked_count += 1
            elif "flood control" in err_desc or "too many requests" in err_desc:
                retry_after = 5
                m = re.search(r"retry after (\d+)", err_desc)
                if m:
                    retry_after = int(m.group(1)) + 1
                logger.warning(f"Flood control. Sleeping {retry_after}s...")
                time.sleep(retry_after)
                try:
                    if broadcast_text:
                        bot.send_message(uid, broadcast_text, parse_mode='Markdown')
                    elif photo_id:
                        bot.send_photo(uid, photo_id, caption=caption,
                                       parse_mode='Markdown' if caption else None)
                    elif video_id:
                        bot.send_video(uid, video_id, caption=caption,
                                       parse_mode='Markdown' if caption else None)
                    sent_count += 1
                except Exception as e_retry:
                    logger.error(f"Broadcast retry failed to {uid}: {e_retry}")
                    failed_count += 1
            else:
                logger.error(f"Broadcast failed to {uid}: {e}")
                failed_count += 1
        except Exception as e:
            logger.error(f"Unexpected broadcast error to {uid}: {e}")
            failed_count += 1

        if (i + 1) % batch_size == 0 and i < total_users - 1:
            time.sleep(delay_batches)
        elif i % 5 == 0:
            time.sleep(0.2)

    duration = round(time.time() - start, 2)
    result_msg = (f"📢 Broadcast Complete!\n\n✅ Sent: {sent_count}\n❌ Failed: {failed_count}\n"
                  f"🚫 Blocked/Inactive: {blocked_count}\n👥 Targets: {total_users}\n"
                  f"⏱️ Duration: {duration}s")
    logger.info(result_msg)
    try:
        bot.send_message(admin_chat_id, result_msg)
    except Exception as e:
        logger.error(f"Failed to send broadcast result to admin: {e}")


def admin_panel_callback(call):
    bot.answer_callback_query(call.id)
    try:
        bot.edit_message_text("👑 Admin Panel\nManage admins.",
                              call.message.chat.id, call.message.message_id,
                              reply_markup=create_admin_panel())
    except Exception as e:
        logger.error(f"Error showing admin panel: {e}")


def add_admin_init_callback(call):
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id,
                           "👑 Enter User ID to promote to Admin.\n/cancel to abort.")
    bot.register_next_step_handler(msg, process_add_admin_id)


def process_add_admin_id(message):
    if message.from_user.id != OWNER_ID:
        bot.reply_to(message, "⚠️ Owner only.")
        return
    if message.text.lower() == '/cancel':
        bot.reply_to(message, "Admin promotion cancelled.")
        return

    try:
        new_admin_id = int(message.text.strip())
        if new_admin_id <= 0:
            raise ValueError("ID must be positive")
        if new_admin_id == OWNER_ID:
            bot.reply_to(message, "⚠️ Owner is already Owner.")
            return
        if new_admin_id in admin_ids:
            bot.reply_to(message, f"⚠️ User `{new_admin_id}` already Admin.")
            return
        add_admin_db(new_admin_id)
        logger.warning(f"Admin {new_admin_id} added by Owner {message.from_user.id}.")
        bot.reply_to(message, f"✅ User `{new_admin_id}` promoted to Admin.")
        try:
            bot.send_message(new_admin_id, "🎉 Congrats! You are now an Admin.")
        except Exception as e:
            logger.error(f"Failed to notify new admin: {e}")
    except ValueError:
        bot.reply_to(message, "⚠️ Invalid ID. Send numerical ID or /cancel.")
        msg = bot.send_message(message.chat.id, "👑 Enter User ID to promote or /cancel.")
        bot.register_next_step_handler(msg, process_add_admin_id)
    except Exception as e:
        logger.error(f"Error processing add admin: {e}", exc_info=True)
        bot.reply_to(message, "Error.")


def remove_admin_init_callback(call):
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id,
                           "👑 Enter User ID of Admin to remove.\n/cancel to abort.")
    bot.register_next_step_handler(msg, process_remove_admin_id)


def process_remove_admin_id(message):
    if message.from_user.id != OWNER_ID:
        bot.reply_to(message, "⚠️ Owner only.")
        return
    if message.text.lower() == '/cancel':
        bot.reply_to(message, "Admin removal cancelled.")
        return

    try:
        admin_id_remove = int(message.text.strip())
        if admin_id_remove <= 0:
            raise ValueError("ID must be positive")
        if admin_id_remove == OWNER_ID:
            bot.reply_to(message, "⚠️ Owner cannot remove self.")
            return
        if admin_id_remove not in admin_ids:
            bot.reply_to(message, f"⚠️ User `{admin_id_remove}` not Admin.")
            return
        if remove_admin_db(admin_id_remove):
            logger.warning(f"Admin {admin_id_remove} removed by Owner {message.from_user.id}.")
            bot.reply_to(message, f"✅ Admin `{admin_id_remove}` removed.")
            try:
                bot.send_message(admin_id_remove, "ℹ️ You are no longer an Admin.")
            except Exception as e:
                logger.error(f"Failed to notify removed admin: {e}")
        else:
            bot.reply_to(message, f"❌ Failed to remove admin `{admin_id_remove}`.")
    except ValueError:
        bot.reply_to(message, "⚠️ Invalid ID. Send numerical ID or /cancel.")
        msg = bot.send_message(message.chat.id, "👑 Enter Admin ID to remove or /cancel.")
        bot.register_next_step_handler(msg, process_remove_admin_id)
    except Exception as e:
        logger.error(f"Error processing remove admin: {e}", exc_info=True)
        bot.reply_to(message, "Error.")


def list_admins_callback(call):
    bot.answer_callback_query(call.id)
    try:
        s = "\n".join(
            f"- `{aid}` {'(Owner)' if aid == OWNER_ID else ''}"
            for aid in sorted(list(admin_ids))
        )
        if not s:
            s = "(No Owner/Admins configured!)"
        bot.edit_message_text(f"👑 Current Admins:\n\n{s}",
                              call.message.chat.id, call.message.message_id,
                              reply_markup=create_admin_panel(), parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Error listing admins: {e}")


def add_subscription_init_callback(call):
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id,
                           "💳 Enter User ID & days (e.g., `12345678 30`).\n/cancel to abort.")
    bot.register_next_step_handler(msg, process_add_subscription_details)


def process_add_subscription_details(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "⚠️ Not authorized.")
        return
    if message.text.lower() == '/cancel':
        bot.reply_to(message, "Sub add cancelled.")
        return

    try:
        parts = message.text.split()
        if len(parts) != 2:
            raise ValueError("Incorrect format")
        sub_user_id = int(parts[0].strip())
        days = int(parts[1].strip())
        if sub_user_id <= 0 or days <= 0:
            raise ValueError("User ID/days must be positive")

        current_expiry = user_subscriptions.get(sub_user_id, {}).get('expiry')
        start_date = datetime.now()
        if current_expiry and current_expiry > start_date:
            start_date = current_expiry
        new_expiry = start_date + timedelta(days=days)
        save_subscription(sub_user_id, new_expiry)

        logger.info(f"Sub for {sub_user_id} by admin {message.from_user.id}. Expiry: {new_expiry:%Y-%m-%d}")
        bot.reply_to(message,
                     f"✅ Sub for `{sub_user_id}` by {days} days.\nNew expiry: {new_expiry:%Y-%m-%d}")
        try:
            bot.send_message(sub_user_id,
                             f"🎉 Sub activated/extended by {days} days! Expires: {new_expiry:%Y-%m-%d}.")
        except Exception as e:
            logger.error(f"Failed to notify user of new sub: {e}")
    except ValueError as e:
        bot.reply_to(message, f"⚠️ Invalid: {e}. Format: `ID days` or /cancel.")
        msg = bot.send_message(message.chat.id, "💳 Enter User ID & days, or /cancel.")
        bot.register_next_step_handler(msg, process_add_subscription_details)
    except Exception as e:
        logger.error(f"Error processing add sub: {e}", exc_info=True)
        bot.reply_to(message, "Error.")


def remove_subscription_init_callback(call):
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id, "💳 Enter User ID to remove sub.\n/cancel to abort.")
    bot.register_next_step_handler(msg, process_remove_subscription_id)


def process_remove_subscription_id(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "⚠️ Not authorized.")
        return
    if message.text.lower() == '/cancel':
        bot.reply_to(message, "Sub removal cancelled.")
        return

    try:
        sub_user_id_remove = int(message.text.strip())
        if sub_user_id_remove <= 0:
            raise ValueError("ID must be positive")
        if sub_user_id_remove not in user_subscriptions:
            bot.reply_to(message, f"⚠️ User `{sub_user_id_remove}` no active sub in memory.")
            return
        remove_subscription_db(sub_user_id_remove)
        logger.warning(f"Sub removed for {sub_user_id_remove} by admin {message.from_user.id}.")
        bot.reply_to(message, f"✅ Sub for `{sub_user_id_remove}` removed.")
        try:
            bot.send_message(sub_user_id_remove, "ℹ️ Your subscription removed by admin.")
        except Exception as e:
            logger.error(f"Failed to notify user: {e}")
    except ValueError:
        bot.reply_to(message, "⚠️ Invalid ID. Send numerical ID or /cancel.")
        msg = bot.send_message(message.chat.id, "💳 Enter User ID to remove sub from, or /cancel.")
        bot.register_next_step_handler(msg, process_remove_subscription_id)
    except Exception as e:
        logger.error(f"Error processing remove sub: {e}", exc_info=True)
        bot.reply_to(message, "Error.")


def check_subscription_init_callback(call):
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id, "💳 Enter User ID to check sub.\n/cancel to abort.")
    bot.register_next_step_handler(msg, process_check_subscription_id)


def process_check_subscription_id(message):
    if message.from_user.id not in admin_ids:
        bot.reply_to(message, "⚠️ Not authorized.")
        return
    if message.text.lower() == '/cancel':
        bot.reply_to(message, "Sub check cancelled.")
        return

    try:
        sub_user_id_check = int(message.text.strip())
        if sub_user_id_check <= 0:
            raise ValueError("ID must be positive")
        if sub_user_id_check in user_subscriptions:
            expiry_dt = user_subscriptions[sub_user_id_check].get('expiry')
            if expiry_dt:
                if expiry_dt > datetime.now():
                    days_left = (expiry_dt - datetime.now()).days
                    bot.reply_to(message,
                                 f"✅ User `{sub_user_id_check}` active sub.\n"
                                 f"Expires: {expiry_dt:%Y-%m-%d %H:%M:%S} ({days_left} days left).")
                else:
                    bot.reply_to(message,
                                 f"⚠️ User `{sub_user_id_check}` expired "
                                 f"(On: {expiry_dt:%Y-%m-%d %H:%M:%S}).")
                    remove_subscription_db(sub_user_id_check)
            else:
                bot.reply_to(message, f"⚠️ User `{sub_user_id_check}` in list, expiry missing.")
        else:
            bot.reply_to(message, f"ℹ️ User `{sub_user_id_check}` no active sub record.")
    except ValueError:
        bot.reply_to(message, "⚠️ Invalid ID. Send numerical ID or /cancel.")
        msg = bot.send_message(message.chat.id, "💳 Enter User ID to check, or /cancel.")
        bot.register_next_step_handler(msg, process_check_subscription_id)
    except Exception as e:
        logger.error(f"Error processing check sub: {e}", exc_info=True)
        bot.reply_to(message, "Error.")


# --- Cleanup Function ---
def cleanup():
    logger.warning("Shutdown. Cleaning up child processes...")
    keys = list(bot_scripts.keys())
    if not keys:
        logger.info("No child scripts running.")
        return
    logger.info(f"Stopping {len(keys)} child scripts...")
    for key in keys:
        info = bot_scripts.get(key)
        if info:
            logger.info(f"Stopping: {key}")
            kill_process_tree(info)
    logger.warning("Cleanup finished.")


atexit.register(cleanup)


# =============================================================================
# Background polling — this is what makes CipherElite run us automatically.
# =============================================================================
_polling_thread = None
_polling_stop = threading.Event()


def _polling_loop():
    """Telegram polling worker. Runs in a daemon thread."""
    logger.info("🚀 Hosting-bot polling thread starting...")

    # Kill any stale webhook so getUpdates works (409 fix).
    try:
        bot.remove_webhook()
        logger.info("✅ Any existing webhook removed.")
    except Exception as e:
        logger.warning(f"remove_webhook failed (non-fatal): {e}")

    # Give Telegram a moment after deleteWebhook.
    _polling_stop.wait(2)

    while not _polling_stop.is_set():
        try:
            bot.infinity_polling(
                logger_level=logging.INFO,
                timeout=60,
                long_polling_timeout=30,
                skip_pending=True,
            )
        except requests.exceptions.ReadTimeout:
            logger.warning("Polling ReadTimeout. Restarting in 5s...")
            _polling_stop.wait(5)
        except requests.exceptions.ConnectionError as ce:
            logger.error(f"Polling ConnectionError: {ce}. Retrying in 15s...")
            _polling_stop.wait(15)
        except telebot.apihelper.ApiTelegramException as ae:
            s = str(ae).lower()
            if "webhook" in s or "409" in s:
                logger.warning("409 webhook conflict detected. Removing webhook and retrying...")
                try:
                    bot.remove_webhook()
                except Exception:
                    pass
                _polling_stop.wait(3)
            else:
                logger.error(f"Telegram API error: {ae}. Retrying in 10s...")
                _polling_stop.wait(10)
        except Exception as e:
            logger.critical(f"💥 Unrecoverable polling error: {e}", exc_info=True)
            logger.info("Restarting polling in 30s...")
            _polling_stop.wait(30)

    logger.warning("Polling thread stopped.")


def start_hosting_bot_in_thread():
    """Start the hosting bot's polling loop in a daemon thread.

    Called automatically on import (unless AUTOSTART_HOSTING_BOT=0).
    Returns the thread object.
    """
    global _polling_thread

    if _polling_thread is not None and _polling_thread.is_alive():
        logger.info("Hosting bot thread already running.")
        return _polling_thread

    _polling_stop.clear()
    _polling_thread = threading.Thread(
        target=_polling_loop,
        name="HostingBotPolling",
        daemon=True,
    )
    _polling_thread.start()
    logger.info(f"✅ Hosting bot started in background thread: {_polling_thread.name}")
    return _polling_thread


def stop_hosting_bot():
    """Signal the polling thread to stop."""
    global _polling_thread
    _polling_stop.set()
    if _polling_thread and _polling_thread.is_alive():
        _polling_thread.join(timeout=5)
    logger.info("Hosting bot stop signalled.")


# --- Auto-start hook ---
# When CipherElite's main.py does `import hosting_bot`, this fires.
# Set AUTOSTART_HOSTING_BOT=0 in env to disable.
if os.environ.get('AUTOSTART_HOSTING_BOT', '1') == '1' and __name__ != '__main__':
    try:
        start_hosting_bot_in_thread()
    except Exception as _auto_e:
        logger.error(f"Auto-start of hosting bot failed: {_auto_e}", exc_info=True)


# --- Standalone run (python hosting_bot.py) ---
if __name__ == '__main__':
    logger.info("=" * 40 + "\n🤖 Hosting Bot — Standalone Start\n" +
                f"🐍 Python: {sys.version.split()[0]}\n" +
                f"🔧 Base Dir: {BASE_DIR}\n" +
                f"📁 Upload Dir: {UPLOAD_BOTS_DIR}\n" +
                f"📊 Data Dir: {IROTECH_DIR}\n" +
                f"🔑 Owner ID: {OWNER_ID}\n" +
                f"🛡️ Admins: {admin_ids}\n" + "=" * 40)
    start_hosting_bot_in_thread()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        logger.info("Interrupted — shutting down.")
    finally:
        stop_hosting_bot()
        cleanup()