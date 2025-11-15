from aria2p import API as Aria2API, Client as Aria2Client
import asyncio
from datetime import datetime
import os
import logging
import math
import time
import json
import re
import urllib.parse
from urllib.parse import urlparse

import requests
from pyrogram import Client, filters, utils as pyro_utils
from pyrogram.types import (
    Message,
    InlineKeyboardButton,
    InlineKeyboardMarkup
)
from pyrogram.enums import ChatMemberStatus
from pyrogram.errors import FloodWait, BadRequest
from flask import Flask, render_template
from threading import Thread

# --------------------
# Logging & Pyrogram limits patch
# --------------------
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s - %(name)s - %(levelname)s] %(message)s - %(filename)s:%(lineno)d"
)
logger = logging.getLogger(__name__)

logging.getLogger("pyrogram.session").setLevel(logging.ERROR)
logging.getLogger("pyrogram.connection").setLevel(logging.ERROR)
logging.getLogger("pyrogram.dispatcher").setLevel(logging.ERROR)

# Fix for big negative chat/channel IDs (your previous dev trick)
pyro_utils.MIN_CHAT_ID = -999999999999
pyro_utils.MIN_CHANNEL_ID = -100999999999999

# --------------------
# Aria2 config
# --------------------
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
aria2.set_global_options(options)

# --------------------
# Environment / config
# --------------------
API_ID = os.environ.get('TELEGRAM_API', '')
if not API_ID:
    logger.error("TELEGRAM_API variable is missing! Exiting now")
    exit(1)

API_HASH = os.environ.get('TELEGRAM_HASH', '')
if not API_HASH:
    logger.error("TELEGRAM_HASH variable is missing! Exiting now")
    exit(1)

BOT_TOKEN = os.environ.get('BOT_TOKEN', '')
if not BOT_TOKEN:
    logger.error("BOT_TOKEN variable is missing! Exiting now")
    exit(1)

DUMP_CHAT_ID = os.environ.get('DUMP_CHAT_ID', '')
if not DUMP_CHAT_ID:
    logger.error("DUMP_CHAT_ID variable is missing! Exiting now")
    exit(1)
else:
    DUMP_CHAT_ID = int(DUMP_CHAT_ID)

FSUB_ID = os.environ.get('FSUB_ID', '')
if not FSUB_ID:
    logger.error("FSUB_ID variable is missing! Exiting now")
    exit(1)
else:
    FSUB_ID = int(FSUB_ID)

USER_SESSION_STRING = os.environ.get('USER_SESSION_STRING', '')
if not USER_SESSION_STRING:
    logger.info("USER_SESSION_STRING variable is missing! Bot will split files in ~2GB chunks...")
    USER_SESSION_STRING = None

# Terabox API base (MiN3R / boogafantastic)
TERA_API = os.environ.get(
    "TERA_API",
    "https://teraapi.boogafantastic.workers.dev"
).rstrip("/")

# --------------------
# Pyrogram clients
# --------------------
app = Client("jetbot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

user = None
SPLIT_SIZE = 2093796556  # ~2GB
if USER_SESSION_STRING:
    user = Client("jetu", api_id=API_ID, api_hash=API_HASH, session_string=USER_SESSION_STRING)
    SPLIT_SIZE = 4241280205  # ~4GB

# --------------------
# Helpers / constants
# --------------------
VALID_DOMAINS = [
    'terabox.com', 'nephobox.com', '4funbox.com', 'mirrobox.com',
    'momerybox.com', 'teraboxapp.com', '1024tera.com',
    'terabox.app', 'gibibox.com', 'goaibox.com', 'terasharelink.com',
    'teraboxlink.com', 'terafileshare.com'
]

VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".ogg", ".flac"}

last_update_time = 0


async def is_user_member(client: Client, user_id: int) -> bool:
    try:
        member = await client.get_chat_member(FSUB_ID, user_id)
        return member.status in {
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER
        }
    except Exception as e:
        logger.error(f"Error checking membership status for user {user_id}: {e}")
        return False


def is_valid_url(url: str) -> bool:
    parsed_url = urlparse(url)
    return any(parsed_url.netloc.endswith(domain) for domain in VALID_DOMAINS)


def format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    elif size < 1024 * 1024:
        return f"{size / 1024:.2f} KB"
    elif size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.2f} MB"
    else:
        return f"{size / (1024 * 1024 * 1024):.2f} GB"


async def update_status_message(status_message: Message, text: str):
    try:
        await status_message.edit_text(text)
    except BadRequest as e:
        if "MESSAGE_NOT_MODIFIED" in str(e):
            # ignore, no change
            return
        logger.error(f"Failed to update status message: {e}")
    except Exception as e:
        logger.error(f"Failed to update status message: {e}")


def iterate_urls_in_json(obj):
    """Recursively yield all HTTP(S) URLs found in JSON-like data."""
    if isinstance(obj, str):
        if obj.startswith("http://") or obj.startswith("https://"):
            yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from iterate_urls_in_json(v)
    elif isinstance(obj, (list, tuple, set)):
        for v in obj:
            yield from iterate_urls_in_json(v)


def pick_media_url_from_api(data) -> str | None:
    """
    Try to pick a 'best' media URL from MiN3R/boogafantastic API JSON.
    Strategy:
      - Prefer URLs ending with video/audio extensions or m3u8.
      - Otherwise, return first HTTP URL if nothing else.
    """
    candidates = list(iterate_urls_in_json(data))
    if not candidates:
        return None

    # Strong match: obvious media extensions
    def is_media(u: str) -> bool:
        low = u.lower()
        return (
            any(low.split("?", 1)[0].endswith(ext) for ext in (
                ".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv",
                ".mp3", ".m4a", ".wav", ".ogg", ".m3u8"
            ))
        )

    media_candidates = [u for u in candidates if is_media(u)]
    if media_candidates:
        # Heuristic: choose the longest URL (often the fully signed one)
        media_candidates.sort(key=len, reverse=True)
        return media_candidates[0]

    # Fallback: just return the first URL
    return candidates[0]


def get_direct_media_url(share_url: str) -> str:
    """
    Call the TERA_API JSON endpoint and extract a direct media URL.
    Raises ValueError if it cannot find anything usable.
    """
    encoded = urllib.parse.quote(share_url, safe="")
    api_url = f"{TERA_API}/api?url={encoded}"
    logger.info(f"[API] Calling {api_url}")

    try:
        resp = requests.get(api_url, timeout=60)
    except Exception as e:
        raise ValueError(f"Failed to call Terabox API: {e}")

    content_type = resp.headers.get("content-type", "")

    # Prefer JSON
    data = None
    if "application/json" in content_type.lower():
        try:
            data = resp.json()
        except Exception as e:
            raise ValueError(f"API returned invalid JSON: {e}")
    else:
        # Try to parse JSON even if content-type is text/html (MiN3R 'raw' mode etc)
        text = resp.text.strip()
        if text.startswith("{") or text.startswith("["):
            try:
                data = json.loads(text)
            except Exception as e:
                logger.warning(f"Failed to parse JSON from HTML content-type: {e}")
                data = None

    if data is not None:
        media_url = pick_media_url_from_api(data)
        if media_url:
            logger.info(f"[API] Picked media URL: {media_url}")
            return media_url

    # If still nothing, last-resort: try to scrape HTML for a media link
    html = resp.text
    m = re.search(
        r'(https?://[^\s"\'<>]+\.(?:mp4|mkv|webm|mov|avi|flv|mp3|m4a|wav|ogg|m3u8)[^\s"\'<>]*)',
        html
    )
    if m:
        media_url = m.group(1)
        logger.info(f"[API-HTML] Fallback media URL: {media_url}")
        return media_url

    # Nothing usable
    raise ValueError("API did not return any usable media URL (maybe only images or unsupported file)")


async def send_media(client: Client, chat_id: int, file_path: str, caption: str, progress=None) -> Message:
    """
    Send file to Telegram choosing the best method (video/photo/document)
    based on file extension.
    """
    ext = os.path.splitext(file_path)[1].lower()

    if ext in IMAGE_EXTS:
        return await client.send_photo(
            chat_id,
            file_path,
            caption=caption,
            progress=progress
        )
    elif ext in VIDEO_EXTS:
        return await client.send_video(
            chat_id,
            file_path,
            caption=caption,
            supports_streaming=True,
            progress=progress
        )
    elif ext in AUDIO_EXTS:
        return await client.send_audio(
            chat_id,
            file_path,
            caption=caption,
            progress=progress
        )
    else:
        return await client.send_document(
            chat_id,
            file_path,
            caption=caption,
            progress=progress
        )


# --------------------
# /start command
# --------------------
@app.on_message(filters.command("start"))
async def start_command(client: Client, message: Message):
    join_button = InlineKeyboardButton("ᴊᴏɪɴ ❤️🚀", url="https://t.me/jetmirror")
    developer_button = InlineKeyboardButton("ᴅᴇᴠᴇʟᴏᴘᴇʀ ⚡️", url="https://t.me/rtx5069")
    repo69 = InlineKeyboardButton("ʀᴇᴘᴏ 🌐", url="https://github.com/Hrishi2861/Terabox-Downloader-Bot")
    user_mention = message.from_user.mention
    reply_markup = InlineKeyboardMarkup([[join_button, developer_button], [repo69]])

    final_msg = (
        f"ᴡᴇʟᴄᴏᴍᴇ, {user_mention}.\n\n"
        f"🌟 ɪ ᴀᴍ ᴀ ᴛᴇʀᴀʙᴏx ᴅᴏᴡɴʟᴏᴀᴅᴇʀ ʙᴏᴛ.\n"
        f"sᴇɴᴅ ᴍᴇ ᴀɴʏ ᴛᴇʀᴀʙᴏx ʟɪɴᴋ ᴀɴᴅ ɪ ᴡɪʟʟ ᴅᴏᴡɴʟᴏᴀᴅ "
        f"ɪᴛ ғᴏʀ ʏᴏᴜ ✨."
    )
    video_file_path = "/app/Jet-Mirror.mp4"

    if os.path.exists(video_file_path):
        await client.send_video(
            chat_id=message.chat.id,
            video=video_file_path,
            caption=final_msg,
            reply_markup=reply_markup
        )
    else:
        await message.reply_text(final_msg, reply_markup=reply_markup)


# --------------------
# Main handler
# --------------------
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

    # Extract terabox URL from message
    url = None
    for word in message.text.split():
        if is_valid_url(word):
            url = word
            break

    if not url:
        await message.reply_text("Please provide a valid Terabox link.")
        return

    # 1) Get direct media URL from API
    try:
        media_url = get_direct_media_url(url)
    except Exception as e:
        logger.error(f"API error for {url}: {e}")
        await message.reply_text(f"❌ API error:\n`{e}`")
        return

    # 2) Start aria2 download
    try:
        download = aria2.add_uris([media_url])
    except Exception as e:
        logger.error(f"Failed to queue aria2 download: {e}")
        await message.reply_text(f"❌ Failed to enqueue download:\n`{e}`")
        return

    status_message = await message.reply_text("sᴇɴᴅɪɴɢ ʏᴏᴜ ᴛʜᴇ ᴍᴇᴅɪᴀ...🤤")
    start_time = datetime.now()

    while not download.is_complete and not download.is_removed and not download.has_error:
        await asyncio.sleep(15)
        try:
            download.update()
        except Exception as e:
            logger.error(f"aria2 update error: {e}")
            break

        progress = download.progress or 0
        total = download.total_length or 0
        done = download.completed_length or 0

        elapsed_time = datetime.now() - start_time
        elapsed_minutes, elapsed_seconds = divmod(elapsed_time.seconds, 60)

        bar_filled = int(progress / 10) if progress <= 100 else 10
        bar = '★' * bar_filled + '☆' * (10 - bar_filled)

        status_text = (
            f"┏ ғɪʟᴇɴᴀᴍᴇ: {download.name or 'Unknown'}\n"
            f"┠ [{bar}] {progress:.2f}%\n"
            f"┠ ᴘʀᴏᴄᴇssᴇᴅ: {format_size(done)} ᴏғ {format_size(total)}\n"
            f"┠ sᴛᴀᴛᴜs: 📥 Downloading\n"
            f"┠ ᴇɴɢɪɴᴇ: <b><u>Aria2c v1.37.0</u></b>\n"
            f"┠ sᴘᴇᴇᴅ: {format_size(download.download_speed)}/s\n"
            f"┠ ᴇᴛᴀ: {download.eta} | ᴇʟᴀᴘsᴇᴅ: {elapsed_minutes}m {elapsed_seconds}s\n"
            f"┖ ᴜsᴇʀ: <a href='tg://user?id={user_id}'>{message.from_user.first_name}</a> | ɪᴅ: {user_id}\n"
        )

        while True:
            try:
                await update_status_message(status_message, status_text)
                break
            except FloodWait as e:
                logger.error(f"Flood wait detected! Sleeping for {e.value} seconds")
                await asyncio.sleep(e.value)

    if download.is_error:
        await status_message.edit_text("❌ Download failed from API / aria2.")
        return

    if not download.is_complete:
        await status_message.edit_text("❌ Download aborted.")
        return

    file_path = download.files[0].path
    caption = (
        f"✨ {download.name}\n"
        f"👤 ʟᴇᴇᴄʜᴇᴅ ʙʏ : <a href='tg://user?id={user_id}'>{message.from_user.first_name}</a>\n"
        f"📥 ᴜsᴇʀ ʟɪɴᴋ: tg://user?id={user_id}\n\n"
        "[ᴘᴏᴡᴇʀᴇᴅ ʙʏ ᴊᴇᴛ-ᴍɪʀʀᴏʀ ❤️🚀](https://t.me/JetMirror)"
    )

    last_update_time = time.time()
    UPDATE_INTERVAL = 15

    async def update_status(msg: Message, text: str):
        nonlocal last_update_time
        current_time = time.time()
        if current_time - last_update_time >= UPDATE_INTERVAL:
            try:
                await msg.edit_text(text)
                last_update_time = current_time
            except FloodWait as e:
                logger.warning(f"FloodWait: Sleeping for {e.value}s")
                await asyncio.sleep(e.value)
                await update_status(msg, text)
            except Exception as e:
                if "MESSAGE_NOT_MODIFIED" not in str(e):
                    logger.error(f"Error updating status: {e}")

    async def upload_progress(current, total):
        progress = (current / total) * 100 if total else 0
        elapsed_time = datetime.now() - start_time
        elapsed_minutes, elapsed_seconds = divmod(elapsed_time.seconds, 60)

        status_text = (
            f"┏ ғɪʟᴇɴᴀᴍᴇ: {download.name}\n"
            f"┠ [{'★' * int(progress / 10)}{'☆' * (10 - int(progress / 10))}] {progress:.2f}%\n"
            f"┠ ᴘʀᴏᴄᴇssᴇᴅ: {format_size(current)} ᴏғ {format_size(total)}\n"
            f"┠ sᴛᴀᴛᴜs: 📤 Uploading to Telegram\n"
            f"┠ ᴇɴɢɪɴᴇ: <b><u>PyroFork v2.2.11</u></b>\n"
            f"┠ ᴇʟᴀᴘsᴇᴅ: {elapsed_minutes}m {elapsed_seconds}s\n"
            f"┖ ᴜsᴇʀ: <a href='tg://user?id={user_id}'>{message.from_user.first_name}</a> | ɪᴅ: {user_id}\n"
        )
        await update_status(status_message, status_text)

    async def split_video_with_ffmpeg(input_path, output_prefix, split_size):
        try:
            original_ext = os.path.splitext(input_path)[1].lower() or '.mp4'
            start_time_ff = datetime.now()
            last_progress_update = time.time()

            proc = await asyncio.create_subprocess_exec(
                'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                '-of', 'default=noprint_wrappers=1:nokey=1', input_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await proc.communicate()
            total_duration = float(stdout.decode().strip() or "0")

            file_size = os.path.getsize(input_path)
            parts = math.ceil(file_size / split_size)

            if parts <= 1 or total_duration <= 0:
                return [input_path]

            duration_per_part = total_duration / parts
            split_files = []

            for i in range(parts):
                current_time_ff = time.time()
                if current_time_ff - last_progress_update >= UPDATE_INTERVAL:
                    elapsed = datetime.now() - start_time_ff
                    status_text_ff = (
                        f"✂️ Splitting {os.path.basename(input_path)}\n"
                        f"Part {i+1}/{parts}\n"
                        f"Elapsed: {elapsed.seconds // 60}m {elapsed.seconds % 60}s"
                    )
                    await update_status(status_message, status_text_ff)
                    last_progress_update = current_time_ff

                output_path = f"{output_prefix}.{i+1:03d}{original_ext}"
                cmd = [
                    'xtra', '-y', '-ss', str(i * duration_per_part),
                    '-i', input_path, '-t', str(duration_per_part),
                    '-c', 'copy', '-map', '0',
                    '-avoid_negative_ts', 'make_zero',
                    output_path
                ]

                proc = await asyncio.create_subprocess_exec(*cmd)
                await proc.wait()
                split_files.append(output_path)

            return split_files
        except Exception as e:
            logger.error(f"Split error: {e}")
            raise

    async def handle_upload():
        file_size = os.path.getsize(file_path)

        sender_client = user if USER_SESSION_STRING else app

        # Split if file too large
        if file_size > SPLIT_SIZE:
            await update_status(
                status_message,
                f"✂️ Splitting {download.name} ({format_size(file_size)})"
            )

            split_files = await split_video_with_ffmpeg(
                file_path,
                os.path.splitext(file_path)[0],
                SPLIT_SIZE
            )

            try:
                for i, part in enumerate(split_files):
                    part_caption = f"{caption}\n\nPart {i+1}/{len(split_files)}"
                    await update_status(
                        status_message,
                        f"📤 Uploading part {i+1}/{len(split_files)}\n"
                        f"{os.path.basename(part)}"
                    )

                    sent = await send_media(
                        sender_client,
                        DUMP_CHAT_ID,
                        part,
                        part_caption,
                        progress=upload_progress
                    )

                    try:
                        await app.copy_message(
                            message.chat.id,
                            DUMP_CHAT_ID,
                            sent.id
                        )
                    except Exception as e:
                        logger.error(f"Could not copy part {i+1} to user: {e}")

                    try:
                        os.remove(part)
                    except Exception:
                        pass
            finally:
                for part in split_files:
                    try:
                        if os.path.exists(part):
                            os.remove(part)
                    except Exception:
                        pass
        else:
            await update_status(
                status_message,
                f"📤 Uploading {download.name}\n"
                f"Size: {format_size(file_size)}"
            )

            sent = await send_media(
                sender_client,
                DUMP_CHAT_ID,
                file_path,
                caption,
                progress=upload_progress
            )

            try:
                await app.copy_message(
                    message.chat.id,
                    DUMP_CHAT_ID,
                    sent.id
                )
            except Exception as e:
                logger.error(f"Could not copy file to user: {e}")

        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass

    await handle_upload()

    try:
        await status_message.delete()
    except Exception as e:
        logger.error(f"Cleanup status message error: {e}")
    try:
        await message.delete()
    except Exception as e:
        logger.error(f"Cleanup user message error: {e}")


# --------------------
# Flask keep-alive
# --------------------
flask_app = Flask(__name__)


@flask_app.route('/')
def home():
    return render_template("index.html")


def run_flask():
    flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))


def keep_alive():
    Thread(target=run_flask, daemon=True).start()


# --------------------
# User client helper
# --------------------
async def start_user_client():
    if user:
        await user.start()
        logger.info("User client started.")


def run_user():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(start_user_client())


# --------------------
# Main entry
# --------------------
if __name__ == "__main__":
    keep_alive()

    if user:
        logger.info("Starting user client...")
        Thread(target=run_user, daemon=True).start()

    logger.info("Starting bot client...")
    app.run()
