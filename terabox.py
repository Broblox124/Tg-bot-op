# terabox.py (patched with MIN_CHAT_ID fix + safer uploads)
from aria2p import API as Aria2API, Client as Aria2Client
import asyncio
from dotenv import load_dotenv
from datetime import datetime
import os
import logging
import math
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup
from pyrogram.enums import ChatMemberStatus
from pyrogram.errors import FloodWait, BadRequest, RPCError
from pyrogram import utils as py_utils  # <-- for MIN_CHAT_ID / MIN_CHANNEL_ID tweak
import time
import urllib.parse
from urllib.parse import urlparse
from flask import Flask, render_template
from threading import Thread
import requests

# ---------- Pyrogram ID range fix ----------
# Allow larger (more negative) IDs for chats/channels
py_utils.MIN_CHAT_ID = -999999999999
py_utils.MIN_CHANNEL_ID = -100999999999999

# ---------- load env / logging ----------
load_dotenv('config.env', override=True)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s - %(name)s - %(levelname)s] %(message)s - %(filename)s:%(lineno)d"
)

logger = logging.getLogger(__name__)

logging.getLogger("pyrogram.session").setLevel(logging.ERROR)
logging.getLogger("pyrogram.connection").setLevel(logging.ERROR)
logging.getLogger("pyrogram.dispatcher").setLevel(logging.ERROR)

# ---------- aria2 client ----------
aria2 = Aria2API(
    Aria2Client(
        host="http://localhost",
        port=6800,
        secret=""
    )
)
options = {
    "max-tries": "50",
    "retry-wait": "3",
    "continue": "true",
    "allow-overwrite": "true",
    "min-split-size": "4M",
    "split": "10"
}

try:
    aria2.set_global_options(options)
except Exception as e:
    logger.warning(f"Could not set aria2 global options at startup: {e}")

# ---------- env / config ----------
API_ID = os.environ.get('TELEGRAM_API', '')
if len(API_ID) == 0:
    logging.error("TELEGRAM_API variable is missing! Exiting now")
    exit(1)

API_HASH = os.environ.get('TELEGRAM_HASH', '')
if len(API_HASH) == 0:
    logging.error("TELEGRAM_HASH variable is missing! Exiting now")
    exit(1)

BOT_TOKEN = os.environ.get('BOT_TOKEN', '')
if len(BOT_TOKEN) == 0:
    logging.error("BOT_TOKEN variable is missing! Exiting now")
    exit(1)

DUMP_CHAT_ID = os.environ.get('DUMP_CHAT_ID', '')
if len(DUMP_CHAT_ID) == 0:
    logging.error("DUMP_CHAT_ID variable is missing! Exiting now")
    exit(1)
# Try int cast; if it fails, we keep it as string (for @username)
try:
    DUMP_CHAT_ID = int(DUMP_CHAT_ID)
except Exception:
    pass

FSUB_ID = os.environ.get('FSUB_ID', '')
if len(FSUB_ID) == 0:
    logging.error("FSUB_ID variable is missing! Exiting now")
    exit(1)
else:
    try:
        FSUB_ID = int(FSUB_ID)
    except Exception:
        logging.error("FSUP_ID must be an integer chat id.")
        exit(1)

USER_SESSION_STRING = os.environ.get('USER_SESSION_STRING', '')
if len(USER_SESSION_STRING) == 0:
    logging.info("USER_SESSION_STRING variable is missing! Bot will split Files in 2Gb...")
    USER_SESSION_STRING = None

# Provide a configurable Terabox->direct link API endpoint (default boogafantastic)
TERA_API = os.environ.get('TERA_API', 'https://teraapi.boogafantastic.workers.dev/')

app = Client("jetbot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# We will NOT use user session for sending (avoids PEER_ID_INVALID issues)
user = None
SPLIT_SIZE = 2093796556  # still used for splitting threshold

VALID_DOMAINS = [
    'terabox.com', 'nephobox.com', '4funbox.com', 'mirrobox.com',
    'momerybox.com', 'teraboxapp.com', '1024tera.com',
    'terabox.app', 'gibibox.com', 'goaibox.com', 'terasharelink.com',
    'teraboxlink.com', 'terafileshare.com', 'terafileshare.net'
]

last_update_time = 0

# ---------- helper: membership ----------
async def is_user_member(client, user_id):
    try:
        member = await client.get_chat_member(FSUB_ID, user_id)
        if member.status in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]:
            return True
        else:
            return False
    except Exception as e:
        logging.error(f"Error checking membership status for user {user_id}: {e}")
        return False

def is_valid_url(url):
    parsed_url = urlparse(url)
    return any(parsed_url.netloc.endswith(domain) for domain in VALID_DOMAINS)

def format_size(size):
    if size < 1024:
        return f"{size} B"
    elif size < 1024 * 1024:
        return f"{size / 1024:.2f} KB"
    elif size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.2f} MB"
    else:
        return f"{size / (1024 * 1024 * 1024):.2f} GB"

# ---------- uploader helper ----------
async def safe_send_to_dump_and_forward(bot: Client, target_chat_id, user_chat_id, file_path, caption, progress_cb=None):
    """
    1) Try sending to dump channel (DUMP_CHAT_ID) using bot.
    2) If that fails (CHANNEL_INVALID, etc), send directly to user's chat using bot.
    """
    # Step 1: send to dump
    try:
        sent = await bot.send_video(
            target_chat_id,
            file_path,
            caption=caption,
            progress=progress_cb
        )
        # Try to forward/copy to user chat
        try:
            await bot.send_video(user_chat_id, sent.video.file_id, caption=caption)
        except Exception as e:
            logger.warning(f"Could not forward from dump to user: {e}")
            try:
                await bot.send_video(user_chat_id, file_path, caption=caption, progress=progress_cb)
            except Exception as e2:
                logger.error(f"Fallback sending direct to user failed: {e2}")
        return True

    except BadRequest as e:
        logger.error(f"BadRequest while sending to dump chat {target_chat_id}: {e}")
        # Step 2: fallback direct to user
        try:
            await bot.send_video(user_chat_id, file_path, caption=caption, progress=progress_cb)
            return False
        except Exception as e2:
            logger.error(f"Fallback direct send failed: {e2}")
            raise

    except RPCError as e:
        logger.error(f"RPCError while sending to dump chat {target_chat_id}: {e}")
        try:
            await bot.send_video(user_chat_id, file_path, caption=caption, progress=progress_cb)
            return False
        except Exception as e2:
            logger.error(f"Fallback direct send failed: {e2}")
            raise

# ---------- bot commands ----------
@app.on_message(filters.command("start"))
async def start_command(client: Client, message: Message):
    join_button = InlineKeyboardButton("ᴊᴏɪɴ ❤️🚀", url="https://t.me/jetmirror")
    developer_button = InlineKeyboardButton("ᴅᴇᴠᴇʟᴏᴘᴇʀ ⚡️", url="https://t.me/rtx5069")
    repo69 = InlineKeyboardButton("ʀᴇᴘᴏ 🌐", url="https://github.com/Hrishi2861/Terabox-Downloader-Bot")
    user_mention = message.from_user.mention
    reply_markup = InlineKeyboardMarkup([[join_button, developer_button], [repo69]])
    final_msg = (
        f"ᴡᴇʟᴄᴏᴍᴇ, {user_mention}.\n\n"
        "🌟 ɪ ᴀᴍ ᴀ ᴛᴇʀᴀʙᴏx ᴅᴏᴡɴʟᴏᴀᴅᴇʀ ʙᴏᴛ. "
        "sᴇɴᴅ ᴍᴇ ᴀɴʏ ᴛᴇʀᴀʙᴏx ʟɪɴᴋ ɪ ᴡɪʟʟ ᴅᴏᴡɴʟᴏᴀᴅ ᴡɪᴛʜɪɴ ғᴇᴡ sᴇᴄᴏɴᴅs ᴀɴᴅ sᴇɴᴅ ɪᴛ ᴛᴏ ʏᴏᴜ ✨."
    )
    video_file_id = "/app/Jet-Mirror.mp4"
    if os.path.exists(video_file_id):
        await client.send_video(
            chat_id=message.chat.id,
            video=video_file_id,
            caption=final_msg,
            reply_markup=reply_markup
        )
    else:
        await message.reply_text(final_msg, reply_markup=reply_markup)

async def update_status_message(status_message, text):
    try:
        await status_message.edit_text(text)
    except Exception as e:
        logger.error(f"Failed to update status message: {e}")

# ---------- main handler ----------
@app.on_message(filters.text)
async def handle_message(client: Client, message: Message):
    if message.text.startswith('/'):
        return
    if not message.from_user:
        return

    user_id = message.from_user.id
    is_member = await is_user_member(client, user_id)

    if not is_member:
        join_button = InlineKeyboardButton("ᴊᴏɪɴ ❤️🚀", url="https://t.me/jetmirror")
        reply_markup = InlineKeyboardMarkup([[join_button]])
        await message.reply_text("ʏᴏᴜ ᴍᴜsᴛ ᴊᴏɪɴ ᴍʏ ᴄʜᴀɴɴᴇʟ ᴛᴏ ᴜsᴇ ᴍᴇ.", reply_markup=reply_markup)
        return

    url = None
    for word in message.text.split():
        if is_valid_url(word):
            url = word
            break

    if not url:
        await message.reply_text("Please provide a valid Terabox link.")
        return

    encoded_url = urllib.parse.quote(url)
    # Use configured TERA_API (boogafantastic) to wrap the link
    final_url = f"{TERA_API}?url={encoded_url}"

    try:
        download = aria2.add_uris([final_url])
    except Exception as e:
        logger.error(f"aria2.add_uris failed: {e}")
        return await message.reply_text(f"❌ Failed to start download: {e}")

    status_message = await message.reply_text("sᴇɴᴅɪɴɢ ʏᴏᴜ ᴛʜᴇ ᴍᴇᴅɪᴀ...🤤")

    start_time = datetime.now()

    while not download.is_complete:
        await asyncio.sleep(7)
        try:
            download.update()
        except Exception as e:
            logger.warning(f"Failed to update aria2 download object: {e}")

        progress = getattr(download, "progress", 0.0)
        completed = getattr(download, "completed_length", 0)
        total = getattr(download, "total_length", 0)
        speed = getattr(download, "download_speed", 0)
        filename = getattr(download, "name", "") or ""

        if not filename:
            try:
                if download.files and len(download.files) > 0:
                    filename = os.path.basename(download.files[0].path or "") or ""
            except Exception:
                filename = ""

        elapsed_time = datetime.now() - start_time
        elapsed_minutes, elapsed_seconds = divmod(elapsed_time.seconds, 60)
        try:
            eta = download.eta
        except Exception:
            eta = "unknown"

        status_text = (
            f"┏ ғɪʟᴇɴᴀᴍᴇ: {filename}\n"
            f"┠ [{'★' * int(progress / 10)}{'☆' * (10 - int(progress / 10))}] {progress:.2f}%\n"
            f"┠ ᴘʀᴏᴄᴇssᴇᴅ: {format_size(int(completed))} ᴏғ {format_size(int(total))}\n"
            f"┠ sᴛᴀᴛᴜs: 📥 Downloading\n"
            f"┠ ᴇɴɢɪɴᴇ: <b><u>Aria2c</u></b>\n"
            f"┠ sᴘᴇᴇᴅ: {format_size(int(speed))}/s\n"
            f"┠ ᴇᴛᴀ: {eta} | ᴇʟᴀᴘsᴇᴅ: {elapsed_minutes}m {elapsed_seconds}s\n"
            f"┖ ᴜsᴇʀ: <a href='tg://user?id={user_id}'>{message.from_user.first_name}</a> | ɪᴅ: {user_id}\n"
        )

        while True:
            try:
                await update_status_message(status_message, status_text)
                break
            except FloodWait as e:
                logger.error(f"Flood wait detected! Sleeping for {e.value} seconds")
                await asyncio.sleep(e.value)
            except Exception as e:
                logger.warning(f"Could not edit status message: {e}")
                break

        # safety: break if file exists even if aria2 metadata is weird
        try:
            if download.is_complete:
                break
            if int(total or 0) == 0:
                if download.files and len(download.files) > 0:
                    fpath_check = download.files[0].path
                    if fpath_check and os.path.exists(fpath_check) and os.path.getsize(fpath_check) > 0:
                        logger.info("Detected file even though total_length==0; going to upload.")
                        break
        except Exception:
            pass

    # get file path
    try:
        fpath = None
        if download.files and len(download.files) > 0:
            fpath = download.files[0].path
        if not fpath and download.name:
            fpath = os.path.join(os.getcwd(), download.name)
        if not fpath or not os.path.exists(fpath):
            await status_message.edit_text("❌ Download finished but file not found or empty.")
            return
    except Exception as e:
        logger.error(f"Error getting download file path: {e}")
        return await status_message.edit_text(f"❌ Error retrieving downloaded file: {e}")

    fname = os.path.basename(fpath)
    fsize = os.path.getsize(fpath)
    caption = (
        f"✨ {fname}\n"
        f"👤 ʟᴇᴇᴄʜᴇᴅ ʙʏ : <a href='tg://user?id={user_id}'>{message.from_user.first_name}</a>\n"
        f"📥 ᴜsᴇʀ ʟɪɴᴋ: tg://user?id={user_id}\n\n"
        "[ᴘᴏᴡᴇʀᴇᴅ ʙʏ ᴊᴇᴛ-ᴍɪʀʀᴏʀ ❤️🚀](https://t.me/JetMirror)"
    )

    async def upload_progress(current, total):
        progress = (current / total) * 100 if total > 0 else 0
        elapsed_time = datetime.now() - start_time
        elapsed_minutes, elapsed_seconds = divmod(elapsed_time.seconds, 60)
        status_text = (
            f"┏ ғɪʟᴇɴᴀᴍᴇ: {fname}\n"
            f"┠ [{'★' * int(progress / 10)}{'☆' * (10 - int(progress / 10))}] {progress:.2f}%\n"
            f"┠ ᴘʀᴏᴄᴇssᴇᴅ: {format_size(int(current))} ᴏғ {format_size(int(total))}\n"
            f"┠ sᴛᴀᴛᴜs: 📤 Uploading to Telegram\n"
            f"┠ ᴇɴɢɪɴᴇ: <b><u>Pyrogram</u></b>\n"
            f"┠ ᴇʟᴀᴘsᴇᴅ: {elapsed_minutes}m {elapsed_seconds}s\n"
        )
        try:
            await update_status_message(status_message, status_text)
        except Exception as e:
            logger.debug(f"Could not update upload status: {e}")

    async def handle_upload():
        try:
            file_size = os.path.getsize(fpath)
        except Exception as e:
            logger.error(f"Could not stat file before upload: {e}")
            await status_message.edit_text("❌ Internal error: cannot read downloaded file.")
            return

        # For now, ignore splitting and just upload whole file
        try:
            await update_status_message(status_message, f"📤 Uploading {fname} ({format_size(file_size)})")
            sent_via_dump = await safe_send_to_dump_and_forward(
                app, DUMP_CHAT_ID, message.chat.id, fpath, caption, progress_cb=upload_progress
            )
            if sent_via_dump:
                await status_message.edit_text("✅ Uploaded to dump channel and delivered to you.")
            else:
                await status_message.edit_text("✅ Uploaded directly to you (dump channel unavailable).")
        except Exception as e:
            logger.error(f"Upload failed: {e}")
            await status_message.edit_text(f"❌ Upload failed: {e}")

        try:
            if os.path.exists(fpath):
                os.remove(fpath)
        except Exception as e:
            logger.warning(f"Could not remove file after upload: {e}")

    await handle_upload()

    try:
        await status_message.delete()
        await message.delete()
    except Exception:
        pass

# ---------- simple web keepalive ----------
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return render_template("index.html") if os.path.exists("templates/index.html") else "OK"

def run_flask():
    flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

def keep_alive():
    Thread(target=run_flask).start()

async def start_user_client():
    # we are not using user client for now, but keep hook in case you enable it later
    if user:
        await user.start()
        logger.info("User client started.")

def run_user():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(start_user_client())

if __name__ == "__main__":
    keep_alive()

    if user:
        logger.info("Starting user client...")
        Thread(target=run_user).start()

    logger.info("Starting bot client...")
    app.run()
