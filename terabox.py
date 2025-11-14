# terabox.py — Updated to use boogafantastic API + robust fallback + aria2/direct-download
# Keep credits as needed.

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
from pyrogram.errors import FloodWait
import time
import urllib.parse
from urllib.parse import urlparse
from flask import Flask, render_template
from threading import Thread
import requests
import tempfile
import shutil
import sys

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s - %(name)s - %(levelname)s] %(message)s - %(filename)s:%(lineno)d"
)
logger = logging.getLogger(__name__)

# lower verbosity for pyrogram internals
logging.getLogger("pyrogram.session").setLevel(logging.ERROR)
logging.getLogger("pyrogram.connection").setLevel(logging.ERROR)
logging.getLogger("pyrogram.dispatcher").setLevel(logging.ERROR)

# ---------- Load environment ----------
# Prefer real environment variables (Koyeb/Render). If missing, load config.env if present.
load_dotenv("config.env", override=False)

# ---------- aria2 client wrapper (non-fatal if aria2 unavailable) ----------
def build_aria2_client(host="http://localhost", port=6800, secret=""):
    try:
        api = Aria2API(Aria2Client(host=host, port=port, secret=secret))
        return api
    except Exception as e:
        logger.warning(f"Could not connect to aria2 at {host}:{port} — {e}")
        return None

aria2 = build_aria2_client(host=os.environ.get("ARIA2_HOST", "http://localhost"),
                           port=int(os.environ.get("ARIA2_PORT", "6800")),
                           secret=os.environ.get("ARIA2_SECRET", ""))

# set aria2 global options if aria2 available
if aria2:
    try:
        options = {
            "max-tries": "50",
            "retry-wait": "3",
            "continue": "true",
            "allow-overwrite": "true",
            "min-split-size": "4M",
            "split": "10"
        }
        aria2.set_global_options(options)
    except Exception as e:
        logger.warning(f"Failed to set aria2 options: {e}")

# ---------- helpers ----------
VALID_DOMAINS = [
    'terabox.com', 'nephobox.com', '4funbox.com', 'mirrobox.com',
    'momerybox.com', 'teraboxapp.com', '1024tera.com',
    'terabox.app', 'gibibox.com', 'goaibox.com', 'terasharelink.com',
    'teraboxlink.com', 'terafileshare.com', 'nephobox.app'
]

def is_valid_url(url):
    try:
        parsed = urlparse(url)
        return any(parsed.netloc.endswith(d) for d in VALID_DOMAINS)
    except Exception:
        return False

def format_size(size):
    if size is None:
        return "0 B"
    size = int(size)
    if size < 1024:
        return f"{size} B"
    elif size < 1024**2:
        return f"{size/1024:.2f} KB"
    elif size < 1024**3:
        return f"{size/1024**2:.2f} MB"
    else:
        return f"{size/1024**3:.2f} GB"

# ---------- primary API (boogafantastic) ----------
BOOGA_API = "https://teraapi.boogafantastic.workers.dev/?url="

def fetch_direct_links_via_booga(share_url: str, timeout=15):
    """
    Returns dict with keys: name, urls(list), size (bytes or 0), mime (if known)
    Raises ValueError if API failed in an expected way.
    """
    encoded = urllib.parse.quote(share_url, safe="")
    api_url = BOOGA_API + encoded
    logger.debug(f"[BOOGA] calling {api_url}")
    try:
        r = requests.get(api_url, timeout=timeout)
    except Exception as e:
        raise ValueError(f"Booga API request failed: {e}")

    if r.status_code != 200:
        raise ValueError(f"Booga API returned {r.status_code}")

    try:
        data = r.json()
    except Exception as e:
        raise ValueError(f"Booga API returned non-json: {e}")

    # Expected shapes vary; common is something like {'files': [...]} or {'data': ...}
    # We'll look for common fields.
    # Look for `files` list or `list` or `playable`.
    urls = []
    name = None
    size = 0
    mime = None

    # Common patterns
    if isinstance(data, dict):
        # try multiple possible keys
        if "files" in data and isinstance(data["files"], list):
            for f in data["files"]:
                if isinstance(f, dict):
                    url = f.get("url") or f.get("dlink") or f.get("download_url")
                    if url:
                        urls.append(url)
                    if not name:
                        name = f.get("name") or f.get("server_filename") or f.get("filename")
                    size = size or int(f.get("size", 0) or 0)
        elif "list" in data and isinstance(data["list"], list):
            for f in data["list"]:
                url = f.get("dlink") or f.get("url") or f.get("download_url")
                if url:
                    urls.append(url)
                if not name:
                    name = f.get("server_filename") or f.get("name")
                size = size or int(f.get("size", 0) or 0)
        elif "url" in data and isinstance(data["url"], str):
            urls.append(data["url"])
            name = data.get("name") or name
            size = int(data.get("size", 0) or 0)
        elif data.get("playable") and isinstance(data.get("playable"), list):
            for p in data["playable"]:
                u = p.get("url") or p.get("dlink")
                if u:
                    urls.append(u)
                if not name:
                    name = p.get("name")
                size = size or int(p.get("size", 0) or 0)
        else:
            # try to flatten anything that looks like URLs
            def find_urls(obj):
                found = []
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        if isinstance(v, str) and v.startswith("http"):
                            found.append(v)
                        else:
                            found.extend(find_urls(v))
                elif isinstance(obj, list):
                    for item in obj:
                        found.extend(find_urls(item))
                return found
            urls = find_urls(data)
            # maybe first url is our dlink
    else:
        raise ValueError("Booga API returned unexpected data shape")

    # dedupe
    urls = list(dict.fromkeys(urls))

    if not urls:
        # no playable media
        raise ValueError("Booga API returned no direct download URLs")

    # set defaults
    if not name:
        # derive from first url
        name = os.path.basename(urlparse(urls[0]).path) or "download"

    return {"name": name, "urls": urls, "size": size, "mime": mime}

# ---------- fallback HTML extraction ----------
def try_extract_dlink_from_html(html: str):
    # look for data-dlink or direct link patterns
    # 1) data-dlink attr
    m = re_search(r'data-dlink="([^"]+)"', html)
    if m:
        return m
    # 2) direct file url inside scripts
    m = re_search(r'(https?://[^\s"\'<>]+dlink[^\s"\'<>]+)', html)
    if m:
        return m
    # 3) direct media (mp4, mkv, jpg, png, pdf, zip)
    m = re_search(r'(https?://[^\s"\'<>]+\.(?:mp4|mkv|mov|webm|jpg|jpeg|png|pdf|zip|rar))', html)
    if m:
        return m
    # 4) some pages use "file_url":"..."
    m = re_search(r'file_url"\s*:\s*"([^"]+)"', html)
    if m:
        return m
    return ""

def re_search(pattern, text):
    import re
    try:
        m = re.search(pattern, text)
        return m.group(1) if m else ""
    except Exception:
        return ""

# ---------- direct-download fallback (requests) ----------
def download_via_requests(url, dest_path, headers=None, timeout=(5, 600)):
    headers = headers or {"User-Agent": "Mozilla/5.0 (compatible; terabox-bot/1.0)"}
    with requests.get(url, headers=headers, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length", 0) or 0)
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
    return total

# ---------- ENV variables and Pyrogram clients ----------
API_ID = os.environ.get('TELEGRAM_API', '') or os.environ.get('API_ID', '')
if not API_ID:
    logger.error("TELEGRAM_API variable is missing! Exiting now")
    sys.exit(1)

API_HASH = os.environ.get('TELEGRAM_HASH', '') or os.environ.get('API_HASH', '')
if not API_HASH:
    logger.error("TELEGRAM_HASH variable is missing! Exiting now")
    sys.exit(1)

BOT_TOKEN = os.environ.get('BOT_TOKEN', '')
if not BOT_TOKEN:
    logger.error("BOT_TOKEN variable is missing! Exiting now")
    sys.exit(1)

DUMP_CHAT_ID = os.environ.get('DUMP_CHAT_ID', '')
if not DUMP_CHAT_ID:
    logger.error("DUMP_CHAT_ID variable is missing! Exiting now")
    sys.exit(1)
else:
    try:
        DUMP_CHAT_ID = int(DUMP_CHAT_ID)
    except:
        logger.error("DUMP_CHAT_ID must be an integer")
        sys.exit(1)

FSUB_ID = os.environ.get('FSUB_ID', '')
if not FSUB_ID:
    logger.error("FSUB_ID variable is missing! Exiting now")
    sys.exit(1)
else:
    try:
        FSUB_ID = int(FSUB_ID)
    except:
        logger.error("FSUB_ID must be an integer")
        sys.exit(1)

USER_SESSION_STRING = os.environ.get('USER_SESSION_STRING', '') or None

# pyrogram clients
app = Client("jetbot", api_id=int(API_ID), api_hash=API_HASH, bot_token=BOT_TOKEN)
user = None
SPLIT_SIZE = 2093796556
if USER_SESSION_STRING:
    user = Client("jetu", api_id=int(API_ID), api_hash=API_HASH, session_string=USER_SESSION_STRING)
    SPLIT_SIZE = 4241280205

# ---------- utility: check membership ----------
async def is_user_member(client, user_id):
    try:
        member = await client.get_chat_member(FSUB_ID, user_id)
        return member.status in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]
    except Exception as e:
        logger.error(f"Error checking membership status for user {user_id}: {e}")
        return False

# ---------- Bot handlers ----------
@app.on_message(filters.command("start"))
async def start_command(client: Client, message: Message):
    join_button = InlineKeyboardButton("ᴊᴏɪɴ ❤️🚀", url="https://t.me/jetmirror")
    developer_button = InlineKeyboardButton("ᴅᴇᴠᴇʟᴏᴘᴇʀ ⚡️", url="https://t.me/rtx5069")
    repo69 = InlineKeyboardButton("ʀᴇᴘᴏ 🌐", url="https://github.com/Hrishi2861/Terabox-Downloader-Bot")
    user_mention = message.from_user.mention
    reply_markup = InlineKeyboardMarkup([[join_button, developer_button], [repo69]])
    final_msg = f"ᴡᴇʟᴄᴏᴍᴇ, {user_mention}.\n\n🌟 Send a Terabox link and I'll fetch it for you."
    await message.reply_text(final_msg, reply_markup=reply_markup)

@app.on_message(filters.text & ~filters.edited)
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
        await message.reply_text("You must join the channel to use the bot.", reply_markup=reply_markup)
        return

    # find first valid terabox url
    url = None
    for word in message.text.split():
        if is_valid_url(word):
            url = word
            break
    if not url:
        await message.reply_text("Please send a valid Terabox link.")
        return

    status_msg = await message.reply_text("🔎 Resolving link...")

    # Attempt primary API
    try:
        res = fetch_direct_links_via_booga(url)
        logger.info(f"Booga API returned {len(res['urls'])} URL(s), name={res['name']}")
    except Exception as e:
        logger.warning(f"Booga API failed: {e}. Trying HTML fallback...")
        # html fallback
        try:
            r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            dlink = try_extract_dlink_from_html(r.text)
            if not dlink:
                await status_msg.edit_text(f"❌ Could not find direct file URL (booga + html fallback failed). Debug: {str(e)}")
                return
            res = {"name": os.path.basename(urlparse(dlink).path) or "download", "urls": [dlink], "size": 0}
            logger.info(f"HTML fallback found dlink: {dlink}")
        except Exception as ex2:
            await status_msg.edit_text(f"❌ Failed to resolve link: {e}\nFallback error: {ex2}")
            return

    # choose first available url
    download_url = res["urls"][0]
    filename = res.get("name") or os.path.basename(urlparse(download_url).path) or "download"
    size_bytes = int(res.get("size", 0) or 0)

    await status_msg.edit_text(f"📥 Starting download: {filename} ({format_size(size_bytes)})")

    # create temp path
    temp_dir = tempfile.gettempdir()
    local_path = os.path.join(temp_dir, filename)

    downloaded = False
    # 1) Try aria2 if available
    if aria2:
        try:
            logger.info("Adding to aria2 queue")
            download = aria2.add_uris([download_url])
            # send status message and poll
            start_time = datetime.now()
            # keep polling until complete or error
            while not download.is_complete and not download.is_removed and not download.is_error:
                await asyncio.sleep(3)
                download.update()
                progress = download.progress
                eta = download.eta if hasattr(download, "eta") else "?"
                await status_msg.edit_text(
                    f"┏ File: {filename}\n"
                    f"┠ [{'★' * int(progress / 10)}{'☆' * (10 - int(progress / 10))}] {progress:.2f}%\n"
                    f"┠ Processed: {format_size(download.completed_length)} of {format_size(download.total_length)}\n"
                    f"┠ Status: 📥 Downloading\n"
                    f"┠ Speed: {format_size(download.download_speed)}/s\n"
                    f"┠ ETA: {eta}"
                )
            if download.is_complete:
                # aria2 stores files in its configured dir; get the first file path
                # aria2p download.files[0].path
                try:
                    file_path = download.files[0].path
                    # copy/move to our temp file path for upload handling
                    shutil.copyfile(file_path, local_path)
                    downloaded = True
                except Exception as e:
                    logger.error(f"Failed to fetch aria2 downloaded file: {e}")
                    downloaded = False
            else:
                logger.warning("aria2 download didn't complete (error/removed). falling back.")
                downloaded = False
        except Exception as e:
            logger.warning(f"aria2 usage failed: {e}")
            downloaded = False

    # 2) Fallback to direct requests download (if aria2 missing or failed)
    if not downloaded:
        try:
            await status_msg.edit_text("⚙️ aria2 unavailable or failed — downloading directly via HTTP...")
            headers = {"User-Agent": "Mozilla/5.0 (compatible; terabox-bot/1.0)"}
            total = download_via_requests(download_url, local_path, headers=headers, timeout=(5, 600))
            size_bytes = size_bytes or total
            downloaded = True
        except Exception as e:
            await status_msg.edit_text(f"❌ Download failed: {e}")
            # cleanup
            try:
                if os.path.exists(local_path):
                    os.remove(local_path)
            except:
                pass
            return

    # 3) Upload to Telegram (with splitting if necessary)
    await status_msg.edit_text("📤 Uploading to Telegram...")

    file_size = os.path.getsize(local_path)
    caption = f"✨ {filename}\nDownloaded from: {url}"

    async def update_status(msg, text):
        try:
            await msg.edit_text(text)
        except FloodWait as e:
            logger.warning(f"FloodWait: sleeping {e.value}s")
            await asyncio.sleep(e.value)
            await update_status(msg, text)
        except Exception:
            pass

    async def upload_progress(current, total):
        try:
            progress_pct = (current / total) * 100 if total else 0
            await update_status(status_msg,
                f"📤 Uploading: {filename}\n{progress_pct:.2f}% ({format_size(current)} / {format_size(total)})"
            )
        except Exception:
            pass

    try:
        if file_size > SPLIT_SIZE:
            # attempt ffmpeg/xtra splitting if installed (original code used custom tool)
            await update_status(status_msg, f"✂️ Splitting {filename} ({format_size(file_size)})")
            # attempt to run ffmpeg split (fallback to chunked upload if not present)
            # simple chunk upload: split file into byte-chunks
            part_index = 1
            with open(local_path, "rb") as rf:
                while True:
                    chunk = rf.read(SPLIT_SIZE)
                    if not chunk:
                        break
                    part_name = f"{local_path}.part{part_index}"
                    with open(part_name, "wb") as pf:
                        pf.write(chunk)
                    # upload part
                    if USER_SESSION_STRING and user:
                        sent = await user.send_video(DUMP_CHAT_ID, part_name, caption=f"{caption}\nPart {part_index}", progress=upload_progress)
                        await app.copy_message(message.chat.id, DUMP_CHAT_ID, sent.id)
                    else:
                        sent = await app.send_video(DUMP_CHAT_ID, part_name, caption=f"{caption}\nPart {part_index}", progress=upload_progress)
                        await app.send_video(message.chat.id, sent.video.file_id, caption=f"{caption}\nPart {part_index}")
                    os.remove(part_name)
                    part_index += 1
        else:
            # single upload
            if USER_SESSION_STRING and user:
                sent = await user.send_video(DUMP_CHAT_ID, local_path, caption=caption, progress=upload_progress)
                await app.copy_message(message.chat.id, DUMP_CHAT_ID, sent.id)
            else:
                sent = await app.send_video(DUMP_CHAT_ID, local_path, caption=caption, progress=upload_progress)
                # copy/send to user chat
                await app.send_video(message.chat.id, sent.video.file_id, caption=caption)
    except Exception as e:
        logger.error(f"Upload error: {e}")
        await status_msg.edit_text(f"❌ Upload failed: {e}")
    finally:
        # cleanup
        try:
            if os.path.exists(local_path):
                os.remove(local_path)
        except:
            pass

    try:
        await status_msg.edit_text("✅ Done. File delivered (will be removed from dump/chat per your config).")
    except:
        pass

# ---------- keep-alive flask ----------
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return render_template("index.html") if os.path.exists("templates/index.html") else "OK"

def run_flask():
    flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

def keep_alive():
    Thread(target=run_flask).start()

# ---------- user client starter ----------
async def start_user_client():
    if user:
        await user.start()
        logger.info("User client started.")

def run_user():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(start_user_client())

# ---------- main ----------
if __name__ == "__main__":
    # start web server
    keep_alive()

    # start optional user client
    if user:
        logger.info("Starting user client...")
        Thread(target=run_user).start()

    logger.info("Starting bot client...")
    app.run()
