from aria2p import API as Aria2API, Client as Aria2Client
import asyncio
from datetime import datetime
import os
import logging
import math
import time
import urllib.parse
from urllib.parse import urlparse

import requests
from pyrogram import Client, filters
from pyrogram.types import (
    Message,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from pyrogram.enums import ChatMemberStatus
from pyrogram.errors import FloodWait, RPCError

from flask import Flask, render_template
from threading import Thread

# -------------------------------------------------
# Pyrogram ID limits fix (for very large negative IDs)
# -------------------------------------------------
try:
    import pyrogram.utils
    pyrogram.utils.MIN_CHAT_ID = -999999999999
    pyrogram.utils.MIN_CHANNEL_ID = -100999999999999
except Exception:
    pass

# -------------------------------------------------
# Logging setup
# -------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s - %(name)s - %(levelname)s] %(message)s - %(filename)s:%(lineno)d"
)
logger = logging.getLogger(__name__)

logging.getLogger("pyrogram.session").setLevel(logging.ERROR)
logging.getLogger("pyrogram.connection").setLevel(logging.ERROR)
logging.getLogger("pyrogram.dispatcher").setLevel(logging.ERROR)

# -------------------------------------------------
# aria2 RPC
# -------------------------------------------------
aria2 = Aria2API(
    Aria2Client(
        host="http://localhost",
        port=6800,
        secret=""
    )
)

ARIA2_OPTS = {
    "max-tries": "50",
    "retry-wait": "3",
    "continue": "true",
    "allow-overwrite": "true",
    "min-split-size": "4M",
    "split": "10"
}
try:
    aria2.set_global_options(ARIA2_OPTS)
except Exception as e:
    logger.error(f"Failed to set aria2 global options: {e}")

# -------------------------------------------------
# ENV vars
# -------------------------------------------------
API_ID = os.environ.get("TELEGRAM_API", "")
if not API_ID:
    logger.error("TELEGRAM_API variable is missing! Exiting now")
    raise SystemExit(1)

API_HASH = os.environ.get("TELEGRAM_HASH", "")
if not API_HASH:
    logger.error("TELEGRAM_HASH variable is missing! Exiting now")
    raise SystemExit(1)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
if not BOT_TOKEN:
    logger.error("BOT_TOKEN variable is missing! Exiting now")
    raise SystemExit(1)

DUMP_CHAT_ID_RAW = os.environ.get("DUMP_CHAT_ID", "")
if not DUMP_CHAT_ID_RAW:
    logger.error("DUMP_CHAT_ID variable is missing! Exiting now")
    raise SystemExit(1)
try:
    DUMP_CHAT_ID = int(DUMP_CHAT_ID_RAW)
except ValueError:
    logger.error(f"DUMP_CHAT_ID must be integer, got: {DUMP_CHAT_ID_RAW}")
    raise SystemExit(1)

FSUB_ID_RAW = os.environ.get("FSUB_ID", "")
if not FSUB_ID_RAW:
    logger.error("FSUB_ID variable is missing! Exiting now")
    raise SystemExit(1)
try:
    FSUB_ID = int(FSUB_ID_RAW)
except ValueError:
    logger.error(f"FSUB_ID must be integer, got: {FSUB_ID_RAW}")
    raise SystemExit(1)

USER_SESSION_STRING = os.environ.get("USER_SESSION_STRING", "")
if not USER_SESSION_STRING:
    logger.info("USER_SESSION_STRING variable is missing! Bot will split files in 2 GB…")
    USER_SESSION_STRING = None

# -------------------------------------------------
# Pyrogram clients
# -------------------------------------------------
app = Client("jetbot", api_id=int(API_ID), api_hash=API_HASH, bot_token=BOT_TOKEN)

user = None
SPLIT_SIZE = 2 * 1024 * 1024 * 1024  # ~2 GB
if USER_SESSION_STRING:
    user = Client("jetu", api_id=int(API_ID), api_hash=API_HASH, session_string=USER_SESSION_STRING)
    SPLIT_SIZE = 4 * 1024 * 1024 * 1024  # ~4 GB

# -------------------------------------------------
# Tera API (boogafantastic)
# -------------------------------------------------
TERA_API_BASE = os.environ.get(
    "TERA_API_URL",
    "https://teraapi.boogafantastic.workers.dev/api"
)

VALID_DOMAINS = [
    "terabox.com", "nephobox.com", "4funbox.com", "mirrobox.com",
    "momerybox.com", "teraboxapp.com", "1024tera.com",
    "terabox.app", "gibibox.com", "goaibox.com", "terasharelink.com",
    "teraboxlink.com", "terafileshare.com",
    "freeterabox.com", "terabox.fun", "tibibox.com"
]

# -------------------------------------------------
# Helpers
# -------------------------------------------------
def is_valid_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return any(parsed.netloc.endswith(domain) for domain in VALID_DOMAINS)
    except Exception:
        return False


def format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.2f} KB"
    if size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.2f} MB"
    return f"{size / (1024 * 1024 * 1024):.2f} GB"


def is_probably_media_url(u: str) -> bool:
    if not isinstance(u, str):
        return False
    u = u.lower()
    media_exts = (
        ".mp4", ".mkv", ".webm", ".mov", ".avi",
        ".mp3", ".m4a", ".flac", ".wav",
        ".m3u8"
    )
    if any(u.split("?", 1)[0].endswith(ext) for ext in media_exts):
        return True
    # allow HLS-like /play/ or /download/ urls
    if "m3u8" in u or "hls" in u or "/download" in u:
        return True
    return False


async def is_user_member(client: Client, user_id: int) -> bool:
    try:
        member = await client.get_chat_member(FSUB_ID, user_id)
        if member.status in [
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        ]:
            return True
        return False
    except RPCError as e:
        logger.error(f"Error checking membership for {user_id}: {e}")
        # If fsub chat invalid, don't block user completely:
        return True
    except Exception as e:
        logger.error(f"Unexpected error in is_user_member: {e}")
        return True


def pick_media_url_from_api(data: dict, original_url: str) -> str:
    """
    Try to extract a real media URL (mp4/mkv/m3u8…) from boogafantastic's API JSON.
    If nothing usable is found, fall back to original share URL.
    """
    if not isinstance(data, dict):
        return original_url

    # 1) Direct common keys
    for key in ["download_url", "download", "raw_url", "raw", "hls", "m3u8", "url"]:
        val = data.get(key)
        if isinstance(val, str) and is_probably_media_url(val):
            return val

    candidates = []

    # 2) JSON lists like files / medias / items / data
    for list_key in ["files", "medias", "items", "data", "sources"]:
        arr = data.get(list_key)
        if isinstance(arr, list):
            for item in arr:
                if isinstance(item, dict):
                    for v in item.values():
                        if isinstance(v, str) and is_probably_media_url(v):
                            candidates.append(v)

    # 3) Scan nested dict values for any media-like URL
    def scan(obj):
        if isinstance(obj, dict):
            for v in obj.values():
                scan(v)
        elif isinstance(obj, list):
            for v in obj:
                scan(v)
        elif isinstance(obj, str):
            if is_probably_media_url(obj):
                candidates.append(obj)

    scan(data)

    # De-duplicate & pick best
    unique = []
    for c in candidates:
        if c not in unique:
            unique.append(c)

    if unique:
        # Prefer https and longer URL
        unique.sort(key=lambda x: (not x.startswith("https"), -len(x)))
        return unique[0]

    return original_url


def call_tera_api(share_url: str) -> str:
    """
    Call boogafantastic's API and return a media URL (or original share URL).
    """
    try:
        encoded = urllib.parse.quote(share_url, safe="")
        api_url = f"{TERA_API_BASE}?url={encoded}"
        logger.info(f"[API] Calling {api_url}")
        resp = requests.get(api_url, timeout=25)
        resp.raise_for_status()

        try:
            data = resp.json()
        except Exception:
            # If not JSON, just fallback
            logger.warning("[API] Non-JSON response, falling back to original URL")
            return share_url

        media_url = pick_media_url_from_api(data, share_url)
        logger.info(f"[API] Picked media URL: {media_url}")
        return media_url
    except Exception as e:
        logger.error(f"[API] Failed to call tera API: {e}")
        return share_url


async def safe_edit(message, text):
    try:
        await message.edit_text(text)
    except FloodWait as e:
        await asyncio.sleep(e.value)
        try:
            await message.edit_text(text)
        except Exception as ee:
            logger.error(f"Failed to edit after FloodWait: {ee}")
    except RPCError as e:
        # Ignore MESSAGE_NOT_MODIFIED etc
        if "MESSAGE_NOT_MODIFIED" not in str(e):
            logger.error(f"Failed to edit message: {e}")
    except Exception as e:
        logger.error(f"Failed to update status message: {e}")


# -------------------------------------------------
# Bot commands
# -------------------------------------------------
@app.on_message(filters.command("start") & filters.private)
async def start_command(client: Client, message: Message):
    join_button = InlineKeyboardButton("ᴊᴏɪɴ ❤️🚀", url="https://t.me/jetmirror")
    developer_button = InlineKeyboardButton("ᴅᴇᴠᴇʟᴏᴘᴇʀ ⚡️", url="https://t.me/rtx5069")
    repo_btn = InlineKeyboardButton("ʀᴇᴘᴏ 🌐", url="https://github.com/Hrishi2861/Terabox-Downloader-Bot")

    user_mention = message.from_user.mention
    reply_markup = InlineKeyboardMarkup([[join_button, developer_button], [repo_btn]])

    final_msg = (
        f"ᴡᴇʟᴄᴏᴍᴇ, {user_mention}.\n\n"
        "🌟 ɪ ᴀᴍ ᴀ ᴛᴇʀᴀʙᴏx ᴅᴏᴡɴʟᴏᴀᴅᴇʀ ʙᴏᴛ.\n"
        "sᴇɴᴅ ᴍᴇ ᴀɴʏ ᴛᴇʀᴀʙᴏx ʟɪɴᴋ, ɪ ᴡɪʟʟ ᴅᴏᴡɴʟᴏᴀᴅ ɪᴛ\n"
        "ᴀɴᴅ sᴇɴᴅ ɪᴛ ᴛᴏ ʏᴏᴜ ✨."
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


# -------------------------------------------------
# Main handler (all non-command text in private)
# -------------------------------------------------
@app.on_message(filters.private & filters.text)
async def handle_message(client: Client, message: Message):
    if not message.from_user:
        return

    # ignore commands here (we already have /start handler)
    if message.text.startswith("/"):
        return

    user_id = message.from_user.id

    # Force-subscribe check
    if not await is_user_member(client, user_id):
        join_button = InlineKeyboardButton("ᴊᴏɪɴ ❤️🚀", url="https://t.me/jetmirror")
        reply_markup = InlineKeyboardMarkup([[join_button]])
        await message.reply_text(
            "ʏᴏᴜ ᴍᴜsᴛ ᴊᴏɪɴ ᴍʏ ᴄʜᴀɴɴᴇʟ ᴛᴏ ᴜsᴇ ᴍᴇ.",
            reply_markup=reply_markup
        )
        return

    # Extract URL from message
    url = None
    for word in message.text.split():
        if is_valid_url(word):
            url = word
            break

    if not url:
        await message.reply_text("Please provide a valid Terabox link.")
        return

    # 1) Call boogafantastic API to get media URL
    media_url = call_tera_api(url)

    status_message = await message.reply_text("sᴇɴᴅɪɴɢ ʏᴏᴜ ᴛʜᴇ ᴍᴇᴅɪᴀ...🤤")

    # 2) Add to aria2
    try:
        download = aria2.add_uris([media_url])
    except Exception as e:
        logger.error(f"aria2.add_uris failed: {e}")
        await status_message.edit_text(f"❌ Failed to start download:\n`{e}`")
        return

    start_time = datetime.now()

    # 3) Poll download
    while True:
        await asyncio.sleep(5)
        try:
            download.update()
        except Exception as e:
            logger.error(f"Download update failed: {e}")
            break

        # Stop conditions
        if download.is_complete:
            break

        if download.is_removed or download.status == "error":
            logger.error(f"Download failed/removed. Status={download.status}")
            await safe_edit(status_message, "❌ Download failed or was removed.")
            return

        total = download.total_length or 0
        completed = download.completed_length or 0
        if total <= 0:
            progress = 0.0
        else:
            progress = completed * 100 / total

        elapsed_time = datetime.now() - start_time
        elapsed_minutes, elapsed_seconds = divmod(elapsed_time.seconds, 60)

        bar_filled = int(progress / 10)
        bar = "★" * bar_filled + "☆" * (10 - bar_filled)

        status_text = (
            f"┏ ғɪʟᴇɴᴀᴍᴇ: {download.name or 'Unknown'}\n"
            f"┠ [{bar}] {progress:.2f}%\n"
            f"┠ ᴘʀᴏᴄᴇssᴇᴅ: {format_size(completed)} ᴏғ {format_size(total)}\n"
            f"┠ sᴛᴀᴛᴜs: 📥 Downloading\n"
            f"┠ ᴇɴɢɪɴᴇ: <b><u>Aria2c v1.37.0</u></b>\n"
            f"┠ sᴘᴇᴇᴅ: {format_size(download.download_speed)}/s\n"
            f"┠ ᴇᴛᴀ: {download.eta} | ᴇʟᴀᴘsᴇᴅ: {elapsed_minutes}m {elapsed_seconds}s\n"
            f"┖ ᴜsᴇʀ: <a href='tg://user?id={user_id}'>{message.from_user.first_name}</a> | ɪᴅ: {user_id}\n"
        )

        await safe_edit(status_message, status_text)

    # 4) Download finished
    if not download.files:
        await safe_edit(status_message, "❌ Download finished but no files found.")
        return

    file_path = download.files[0].path
    if not os.path.exists(file_path):
        await safe_edit(status_message, "❌ Downloaded file not found on disk.")
        return

    file_size = os.path.getsize(file_path)

    caption = (
        f"✨ {download.name}\n"
        f"👤 ʟᴇᴇᴄʜᴇᴅ ʙʏ : <a href='tg://user?id={user_id}'>{message.from_user.first_name}</a>\n"
        f"📥 ᴜsᴇʀ ʟɪɴᴋ: tg://user?id={user_id}\n\n"
        "[ᴘᴏᴡᴇʀᴇᴅ ʙʏ ᴊᴇᴛ-ᴍɪʀʀᴏʀ ❤️🚀](https://t.me/JetMirror)"
    )

    last_update_time = time.time()
    UPDATE_INTERVAL = 15

    async def upload_status(text: str):
        nonlocal last_update_time
        now = time.time()
        if now - last_update_time >= UPDATE_INTERVAL:
            await safe_edit(status_message, text)
            last_update_time = now

    async def upload_progress(current, total):
        progress = (current / total) * 100 if total else 0
        elapsed_time = datetime.now() - start_time
        elapsed_minutes, elapsed_seconds = divmod(elapsed_time.seconds, 60)

        bar_filled = int(progress / 10)
        bar = "★" * bar_filled + "☆" * (10 - bar_filled)

        text = (
            f"┏ ғɪʟᴇɴᴀᴍᴇ: {download.name}\n"
            f"┠ [{bar}] {progress:.2f}%\n"
            f"┠ ᴘʀᴏᴄᴇssᴇᴅ: {format_size(current)} ᴏғ {format_size(total)}\n"
            f"┠ sᴛᴀᴛᴜs: 📤 Uploading to Telegram\n"
            f"┠ ᴇɴɢɪɴᴇ: <b><u>PyroFork v2.2.11</u></b>\n"
            f"┠ sᴘᴇᴇᴅ: {format_size(current / elapsed_time.seconds if elapsed_time.seconds > 0 else 0)}/s\n"
            f"┠ ᴇʟᴀᴘsᴇᴅ: {elapsed_minutes}m {elapsed_seconds}s\n"
            f"┖ ᴜsᴇʀ: <a href='tg://user?id={user_id}'>{message.from_user.first_name}</a> | ɪᴅ: {user_id}\n"
        )
        await upload_status(text)

    async def split_video_with_ffmpeg(input_path, output_prefix, split_size):
        """
        Split big videos into <= split_size using ffprobe + xtra (ffmpeg).
        """
        try:
            original_ext = os.path.splitext(input_path)[1].lower() or ".mp4"
            start_split = datetime.now()
            last_progress_update = time.time()

            # Get total duration
            proc = await asyncio.create_subprocess_exec(
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", input_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await proc.communicate()
            total_duration = float(stdout.decode().strip())

            file_size_local = os.path.getsize(input_path)
            parts = math.ceil(file_size_local / split_size)
            if parts == 1:
                return [input_path]

            duration_per_part = total_duration / parts
            split_files = []

            for i in range(parts):
                current_time_local = time.time()
                if current_time_local - last_progress_update >= UPDATE_INTERVAL:
                    elapsed = datetime.now() - start_split
                    status_text = (
                        f"✂️ Splitting {os.path.basename(input_path)}\n"
                        f"Part {i+1}/{parts}\n"
                        f"Elapsed: {elapsed.seconds // 60}m {elapsed.seconds % 60}s"
                    )
                    await upload_status(status_text)
                    last_progress_update = current_time_local

                output_path = f"{output_prefix}.{i+1:03d}{original_ext}"
                cmd = [
                    "xtra", "-y", "-ss", str(i * duration_per_part),
                    "-i", input_path, "-t", str(duration_per_part),
                    "-c", "copy", "-map", "0",
                    "-avoid_negative_ts", "make_zero",
                    output_path
                ]

                proc = await asyncio.create_subprocess_exec(*cmd)
                await proc.wait()
                split_files.append(output_path)

            return split_files
        except Exception as e:
            logger.error(f"Split error: {e}")
            raise

    async def send_file_to_dump_and_user(path, cap, part_info: str = ""):
        full_caption = cap + (f"\n\n{part_info}" if part_info else "")
        uploader = user if USER_SESSION_STRING else app

        # 1) send to dump
        try:
            sent = await uploader.send_video(
                DUMP_CHAT_ID,
                path,
                caption=full_caption,
                progress=upload_progress
            )
        except RPCError as e:
            logger.error(f"BadRequest while sending to dump chat {DUMP_CHAT_ID}: {e}")
            # fallback: send directly to user
            try:
                await app.send_video(
                    message.chat.id,
                    path,
                    caption=full_caption,
                    progress=upload_progress
                )
            except Exception as e2:
                logger.error(f"Fallback direct send failed: {e2}")
                raise
        else:
            # 2) forward/copy to user
            try:
                await app.copy_message(
                    chat_id=message.chat.id,
                    from_chat_id=DUMP_CHAT_ID,
                    message_id=sent.id,
                    caption=full_caption
                )
            except Exception as e:
                logger.warning(f"Could not forward from dump to user: {e}")
                # fallback: send again directly
                try:
                    await app.send_video(
                        message.chat.id,
                        path,
                        caption=full_caption,
                        progress=upload_progress
                    )
                except Exception as e2:
                    logger.error(f"Final send to user failed: {e2}")
                    raise

    # 5) Handle upload (with optional splitting)
    try:
        if file_size > SPLIT_SIZE:
            await upload_status(
                f"✂️ Splitting {download.name} ({format_size(file_size)})"
            )
            split_files = await split_video_with_ffmpeg(
                file_path,
                os.path.splitext(file_path)[0],
                SPLIT_SIZE
            )
            try:
                for idx, part in enumerate(split_files, start=1):
                    await upload_status(
                        f"📤 Uploading part {idx}/{len(split_files)}\n"
                        f"{os.path.basename(part)}"
                    )
                    part_info = f"Part {idx}/{len(split_files)}"
                    await send_file_to_dump_and_user(part, caption, part_info)
            finally:
                for part in split_files:
                    try:
                        os.remove(part)
                    except Exception:
                        pass
        else:
            await upload_status(
                f"📤 Uploading {download.name}\n"
                f"Size: {format_size(file_size)}"
            )
            await send_file_to_dump_and_user(file_path, caption)
    except Exception as e:
        logger.error(f"Upload failed: {e}")
        await safe_edit(status_message, f"❌ Upload failed:\n`{e}`")
    finally:
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass

    try:
        await status_message.delete()
        await message.delete()
    except Exception as e:
        logger.error(f"Cleanup error: {e}")


# -------------------------------------------------
# Flask keep-alive
# -------------------------------------------------
flask_app = Flask(__name__)


@flask_app.route("/")
def home():
    return render_template("index.html")


def run_flask():
    flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))


def keep_alive():
    Thread(target=run_flask).start()


async def start_user_client():
    if user:
        await user.start()
        logger.info("User client started.")


def run_user():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(start_user_client())


# -------------------------------------------------
# Main
# -------------------------------------------------
if __name__ == "__main__":
    keep_alive()

    if user:
        logger.info("Starting user client...")
        Thread(target=run_user).start()

    logger.info("Starting bot client...")
    app.run()
