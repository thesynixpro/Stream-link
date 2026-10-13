import asyncio
import logging
import os
import re
import secrets
import tempfile
from pathlib import Path
from urllib.parse import urlparse
from contextlib import suppress

from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telethon import TelegramClient
from mega import Mega

# -----------------------------------------------------------------------------
# Configuration (set these as environment variables in Railway)
# -----------------------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
API_ID_RAW = os.getenv("API_ID", "").strip()
API_HASH = os.getenv("API_HASH", "").strip()
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
PORT = int(os.getenv("PORT", "8080"))
MEGA_EMAIL = os.getenv("MEGA_EMAIL", "").strip()
MEGA_PASSWORD = os.getenv("MEGA_PASSWORD", "")
MAX_TRANSFER_GB = float(os.getenv("MAX_TRANSFER_GB", "2.0"))
MAX_BYTES = int(MAX_TRANSFER_GB * 1024**3)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("mega-streamer")

if not BOT_TOKEN:
    raise RuntimeError("Missing Railway variable: BOT_TOKEN")
if not API_ID_RAW.isdigit():
    raise RuntimeError("API_ID must be numeric (from my.telegram.org)")
if not API_HASH:
    raise RuntimeError("Missing Railway variable: API_HASH")
if not PUBLIC_URL.startswith("https://"):
    raise RuntimeError("PUBLIC_URL must be your Railway HTTPS domain")

API_ID = int(API_ID_RAW)
mtproto = TelegramClient("railway_mega_session", API_ID, API_HASH)
videos = {}
transfer_lock = asyncio.Lock()

PLAYER = '''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Video Stream</title><style>body{margin:0;background:#080d18;color:#f3f6ff;font:16px system-ui;padding:16px}main{max-width:900px;margin:auto;background:#121b2d;padding:20px;border-radius:18px;box-shadow:0 12px 36px #0005}h2{margin-top:0}video{width:100%;max-height:75vh;background:#000;border-radius:12px}p{color:#aebbd1}</style></head><body><main><h2>🎬 Video Stream</h2><p>🔐 Keep this streaming link private.</p><video id="v" controls playsinline preload="metadata"></video><script>document.getElementById('v').src='/media/'+location.pathname.split('/').pop()</script></main></body></html>'''


def main_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎬  Stream Videos", callback_data="stream"),
            InlineKeyboardButton("📥  MEGA Import", callback_data="mega"),
        ],
        [
            InlineKeyboardButton("🆘  Help", callback_data="help"),
            InlineKeyboardButton("🤖  About", callback_data="about"),
        ],
    ])


def mega_link(value: str) -> bool:
    try:
        parsed = urlparse(value.strip())
        return parsed.scheme in ("http", "https") and (parsed.hostname or "").lower() in {
            "mega.nz", "www.mega.nz", "mega.co.nz", "www.mega.co.nz"
        }
    except Exception:
        return False


def download_mega(url: str, outdir: str):
    # This project uses the legacy mega.py package; runtime.txt should select
    # a compatible Python version (the repository previously used Python 3.10).
    mega = Mega()
    account = mega.login(MEGA_EMAIL, MEGA_PASSWORD) if MEGA_EMAIL and MEGA_PASSWORD else mega.login()
    account.download_url(url, dest_path=outdir)


def safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._() \-\[\]]+", "_", Path(name or "video").name).strip(" .")[:160] or "video"


async def register_uploaded(chat_id, sent, path):
    token = secrets.token_urlsafe(24)
    size = Path(path).stat().st_size
    videos[token] = {
        "chat_id": chat_id,
        "message_id": sent.id,
        "mime": "video/mp4",
        "name": Path(path).name,
        "size": size,
    }
    return f"{PUBLIC_URL}/watch/{token}"


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message:
        return
    first_name = update.effective_user.first_name if update.effective_user else "there"
    await message.reply_text(
        "╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
        "       🎥  <b>STREAM LINK BOT</b>\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        f"👋 Hello, {first_name}! Welcome aboard.\n\n"
        "✨ <b>What can I do for you?</b>\n"
        "🔗 Import a public MEGA file link\n"
        "📂 Process supported MEGA folder links\n"
        "🎞️ Create a streaming link from a Telegram video\n\n"
        "👇 <b>Choose an option below</b> or send a link to begin.",
        reply_markup=main_keyboard(),
        parse_mode="HTML",
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
        "          🆘  <b>HELP CENTER</b>\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "🔹 <b>MEGA:</b> Send a public MEGA file link. Folder support depends on the link format and installed MEGA library.\n\n"
        "🔹 <b>Telegram video:</b> Send a video or a video document and the bot will return a streaming URL.\n\n"
        f"⚙️ Transfer limit: <b>{MAX_TRANSFER_GB:g} GB</b> per video.\n\n"
        "🔐 Only share files you own or have permission to copy. Keep generated streaming links private."
    )
    if update.callback_query:
        await update.callback_query.message.reply_text(text, parse_mode="HTML", reply_markup=main_keyboard())
    elif update.effective_message:
        await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=main_keyboard())


async def about_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
        "          🤖  <b>ABOUT THIS BOT</b>\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "🎬 Your MEGA-to-Telegram streaming assistant.\n"
        "⚡ Creates browser-friendly video links\n"
        "📡 Streams Telegram media with byte-range support\n"
        "🛡️ No need to share your MEGA password in chat\n\n"
        "💡 <i>Keep your bot token and private links secret.</i>"
    )
    if update.callback_query:
        await update.callback_query.message.reply_text(text, parse_mode="HTML", reply_markup=main_keyboard())
    elif update.effective_message:
        await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=main_keyboard())


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "help":
        await help_cmd(update, context)
    elif query.data == "about":
        await about_cmd(update, context)
    elif query.data == "stream":
        await query.message.reply_text(
            "🎬 <b>STREAM A VIDEO</b>\n\n"
            "📤 Send a video directly in this chat.\n"
            "🔗 I'll reply with a browser streaming link.\n\n"
            "💡 You can also send a public MEGA link to import a video.",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
    elif query.data == "mega":
        await query.message.reply_text(
            "📥 <b>MEGA IMPORT</b>\n\n"
            "🔗 Send a public MEGA file link here.\n"
            "⏳ The server downloads it temporarily, uploads it to Telegram, then returns a streaming link.\n\n"
            f"📦 Current per-video limit: <b>{MAX_TRANSFER_GB:g} GB</b>.",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )


async def mega_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.text:
        return
    match = re.search(r"https?://(?:www\.)?(?:mega\.nz|mega\.co\.nz)/\S+", msg.text.strip(), re.I)
    if not match:
        return
    url = match.group(0).rstrip(").,;")
    if not mega_link(url):
        return

    status = await msg.reply_text("📥 MEGA link received!\n⏳ Downloading to temporary server storage…")
    async with transfer_lock:
        try:
            with tempfile.TemporaryDirectory(prefix="mega-") as temp_dir:
                await asyncio.to_thread(download_mega, url, temp_dir)
                candidates = [
                    path for path in Path(temp_dir).rglob("*")
                    if path.is_file()
                    and path.suffix.lower() in {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".mpeg", ".mpg"}
                    and path.stat().st_size <= MAX_BYTES
                ]
                if not candidates:
                    await status.edit_text(
                        f"⚠️ No supported video found, or the file exceeds the {MAX_TRANSFER_GB:g} GB limit.\n\n"
                        "🔎 Check the public link and available server disk space."
                    )
                    return

                await status.edit_text(f"✅ Found {len(candidates)} video(s).\n📤 Uploading through Telegram…")
                for path in candidates:
                    sent = await mtproto.send_file(
                        msg.chat_id,
                        str(path),
                        caption=f"🎬 Imported from MEGA: {safe_name(path.name)}",
                        supports_streaming=True,
                        force_document=False,
                    )
                    link = await register_uploaded(msg.chat_id, sent, path)
                    await msg.reply_text(
                        "╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
                        "        ✅  <b>UPLOAD COMPLETE</b>\n"
                        "╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
                        f"🎞️ <b>File:</b> {safe_name(path.name)}\n"
                        f"📦 <b>Size:</b> {path.stat().st_size / 1024**2:.1f} MB\n\n"
                        f"🔗 <b>Streaming link:</b>\n{link}\n\n"
                        "▶️ Open the link in your browser to play.",
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Open Stream", url=link)]]),
                        disable_web_page_preview=True,
                    )
                await status.delete()
        except Exception as exc:
            log.exception("MEGA transfer failed")
            with suppress(Exception):
                await status.edit_text(
                    f"❌ <b>MEGA transfer failed</b> ({type(exc).__name__})\n\n"
                    f"📝 {str(exc)[:220]}\n\n"
                    "🔧 Check Railway logs, public-link access, disk capacity, and transfer limits.",
                    parse_mode="HTML",
                )


async def incoming_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return
    media = msg.video or msg.document
    if not media:
        return
    mime = getattr(media, "mime_type", "") or ""
    if msg.document and not mime.startswith("video/"):
        return

    status = await msg.reply_text("🎬 Video received!\n⏳ Preparing your streaming link…")
    try:
        original = await mtproto.get_messages(msg.chat_id, ids=msg.message_id)
        if not original or not original.media:
            raise RuntimeError("MTProto cannot access this message")
        token = secrets.token_urlsafe(24)
        size = getattr(getattr(original, "file", None), "size", None) or getattr(media, "file_size", None)
        videos[token] = {
            "chat_id": msg.chat_id,
            "message_id": msg.message_id,
            "mime": mime or "video/mp4",
            "size": size,
        }
        link = f"{PUBLIC_URL}/watch/{token}"
        await status.edit_text(
            "╭━━━━━━━━━━━━━━━━━━━━━━╮\n"
            "       ✅  <b>YOUR STREAM IS READY</b>\n"
            "╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
            "🎞️ Your video is ready to play in a browser.\n\n"
            f"🔗 <b>Streaming link:</b>\n{link}\n\n"
            "🔐 Keep this link private.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Watch Video", url=link)]]),
            disable_web_page_preview=True,
        )
    except Exception:
        log.exception("Incoming video registration failed")
        await status.edit_text("❌ Could not prepare the link. Please check the Railway logs and try again.")


async def home(request):
    return web.Response(text="🎬 Telegram Video Streamer is running.")


async def health(request):
    return web.json_response({"status": "ok", "mtproto_connected": mtproto.is_connected(), "links": len(videos)})


async def watch(request):
    if request.match_info["token"] not in videos:
        raise web.HTTPNotFound(text="Link missing or service restarted; send the video again.")
    return web.Response(text=PLAYER, content_type="text/html")


async def media(request):
    token = request.match_info["token"]
    record = videos.get(token)
    if not record:
        raise web.HTTPNotFound(text="Link missing or service restarted.")
    try:
        msg = await mtproto.get_messages(record["chat_id"], ids=record["message_id"])
        if not msg or not msg.media:
            raise web.HTTPNotFound(text="Telegram media unavailable.")
        total = getattr(getattr(msg, "file", None), "size", None) or record.get("size")
        if not total:
            raise web.HTTPBadGateway(text="Could not determine media size.")

        start, end, status_code = 0, total - 1, 200
        range_header = request.headers.get("Range")
        if range_header:
            try:
                if not range_header.startswith("bytes=") or "," in range_header:
                    raise ValueError
                left, separator, right = range_header[6:].partition("-")
                if not separator:
                    raise ValueError
                if left:
                    start = int(left)
                    end = int(right) if right else total - 1
                else:
                    suffix = int(right or "0")
                    if suffix <= 0:
                        raise ValueError
                    start = max(0, total - suffix)
                    end = total - 1
                if start < 0 or start >= total or end < start:
                    raise ValueError
                end = min(end, total - 1)
                status_code = 206
            except ValueError:
                return web.Response(status=416, headers={"Content-Range": f"bytes */{total}"})

        length = end - start + 1
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Type": record.get("mime") or "video/mp4",
            "Content-Length": str(length),
            "Cache-Control": "private, no-store",
        }
        if status_code == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{total}"
        response = web.StreamResponse(status=status_code, headers=headers)
        await response.prepare(request)
        sent = 0
        async for chunk in mtproto.iter_download(msg, offset=start, limit=length, chunk_size=512 * 1024):
            if not chunk:
                continue
            chunk = chunk[: length - sent]
            await response.write(chunk)
            sent += len(chunk)
            if sent >= length:
                break
        await response.write_eof()
        return response
    except web.HTTPException:
        raise
    except Exception:
        log.exception("Streaming failed")
        raise web.HTTPBadGateway(text="Streaming failed; check Railway logs.")


async def main():
    bot = Application.builder().token(BOT_TOKEN).build()
    bot.add_handler(CommandHandler("start", start_cmd))
    bot.add_handler(CommandHandler("help", help_cmd))
    bot.add_handler(CommandHandler("about", about_cmd))
    bot.add_handler(CallbackQueryHandler(button_handler))
    bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, mega_message))
    bot.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, incoming_video))

    await bot.initialize()
    await bot.start()
    if not bot.updater:
        raise RuntimeError("Telegram polling updater unavailable")
    await bot.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    log.info("Telegram Bot API polling started")

    await mtproto.start(bot_token=BOT_TOKEN)
    log.info("MTProto client connected")

    web_app = web.Application(client_max_size=2 * 1024 * 1024)
    web_app.router.add_get("/", home)
    web_app.router.add_get("/health", health)
    web_app.router.add_get("/watch/{token}", watch)
    web_app.router.add_get("/media/{token}", media)
    runner = web.AppRunner(web_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("HTTP server listening on port %s", PORT)

    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        if bot.updater and bot.updater.running:
            await bot.updater.stop()
        await bot.stop()
        await bot.shutdown()
        if mtproto.is_connected():
            await mtproto.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
