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
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telethon import TelegramClient
from mega import Mega

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("mega-streamer")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
API_ID_RAW = os.getenv("API_ID", "").strip()
API_HASH = os.getenv("API_HASH", "").strip()
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
PORT = int(os.getenv("PORT", "8080"))

MEGA_EMAIL = os.getenv("MEGA_EMAIL", "").strip()
MEGA_PASSWORD = os.getenv("MEGA_PASSWORD", "")

MAX_TRANSFER_GB = float(os.getenv("MAX_TRANSFER_GB", "2.0"))
MAX_BYTES = int(MAX_TRANSFER_GB * 1024**3)

if not BOT_TOKEN:
    raise RuntimeError("Missing Railway variable: BOT_TOKEN")

if not API_ID_RAW.isdigit():
    raise RuntimeError("API_ID must be numeric")

if not API_HASH:
    raise RuntimeError("Missing Railway variable: API_HASH")

if not PUBLIC_URL.startswith("https://"):
    raise RuntimeError("PUBLIC_URL must be your Railway HTTPS domain")

API_ID = int(API_ID_RAW)

mtproto = TelegramClient("railway_mega_session", API_ID, API_HASH)

videos = {}
transfer_lock = asyncio.Lock()

STREAM_DIR = Path(os.getenv("STREAM_DIR", "/tmp/mega-streams"))
STREAM_DIR.mkdir(parents=True, exist_ok=True)

PLAYER = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Telegram Video</title>
<style>
body {
    margin: 0;
    background: #0b1220;
    color: #eef2ff;
    font: 16px system-ui;
    padding: 16px;
}
main {
    max-width: 900px;
    margin: auto;
    background: #151e32;
    padding: 20px;
    border-radius: 16px;
}
video {
    width: 100%;
    max-height: 75vh;
    background: #000;
}
p { color: #bac6da; }
</style>
</head>
<body>
<main>
<h2>Telegram Video Stream</h2>
<p>Keep this link private.</p>
<video id="v" controls playsinline preload="metadata"></video>
<script>
document.getElementById('v').src =
    '/media/' + location.pathname.split('/').pop();
</script>
</main>
</body>
</html>"""


def mega_link(url):
    try:
        parsed = urlparse(url.strip())
        return (
            parsed.scheme in ("http", "https")
            and (parsed.hostname or "").lower()
            in (
                "mega.nz",
                "www.mega.nz",
                "mega.co.nz",
                "www.mega.co.nz",
            )
        )
    except Exception:
        return False


def download_mega(url, outdir):
    mega = Mega()

    if MEGA_EMAIL and MEGA_PASSWORD:
        account = mega.login(MEGA_EMAIL, MEGA_PASSWORD)
    else:
        account = mega.login()

    account.download_url(url, dest_path=outdir)


def safe_name(name):
    cleaned = re.sub(
        r"[^A-Za-z0-9._() \-\[\]]+",
        "_",
        Path(name or "video").name,
    )
    return cleaned.strip(" .")[:160] or "video"


def format_upload_message(file_name, size_bytes, stream_link):
    size_mb = size_bytes / (1024 ** 2)

    return (
        "━━━━━━━━━━━━━━━━━━━━━━╮\n"
        "        ✅  UPLOAD COMPLETE\n"
        "╰━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
        f"🎞️ File: {file_name}\n"
        f"📦 Size: {size_mb:.1f} MB\n\n"
        "🔗 Streaming link:\n"
        f"{stream_link}\n\n"
        "▶️ Open the link in your browser to play."
    )


async def start_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if update.effective_message:
        await update.effective_message.reply_text(
            "Welcome!\n\n"
            "Send me a Telegram video or a public MEGA file link.\n"
            "I will prepare a browser streaming link.\n\n"
            "Use /help for more information."
        )


async def help_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if update.effective_message:
        await update.effective_message.reply_text(
            "How to use:\n\n"
            "1. Send a video to this bot.\n"
            "2. Wait for the streaming link.\n"
            "3. Open the link in your browser.\n\n"
            "You can also send a public MEGA file link.\n\n"
            "MEGA links are downloaded to temporary Railway "
            "storage before they can be streamed. "
            "Folder support depends on the link and MEGA package."
        )


async def mega_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    msg = update.effective_message

    if not msg or not msg.text:
        return

    match = re.search(
        r"https?://(?:www\.)?(?:mega\.nz|mega\.co\.nz)/\S+",
        msg.text.strip(),
        re.I,
    )

    if not match:
        return

    url = match.group(0).rstrip(").,;")

    if not mega_link(url):
        return

    status = await msg.reply_text(
        "MEGA link received. Preparing your streaming link..."
    )

    async with transfer_lock:
        workdir = None

        try:
            workdir = Path(
                tempfile.mkdtemp(
                    prefix="mega-",
                    dir=str(STREAM_DIR),
                )
            )

            await asyncio.to_thread(
                download_mega,
                url,
                str(workdir),
            )

            supported = {
                ".mp4",
                ".mkv",
                ".mov",
                ".webm",
                ".avi",
                ".m4v",
                ".mpeg",
                ".mpg",
            }

            candidates = [
                path
                for path in workdir.rglob("*")
                if path.is_file()
                and path.suffix.lower() in supported
            ]

            candidates = [
                path
                for path in candidates
                if path.stat().st_size <= MAX_BYTES
            ]

            if not candidates:
                await status.edit_text(
                    "No supported video was found, or the video "
                    f"exceeds the configured {MAX_TRANSFER_GB:g} GB limit.\n\n"
                    "Check the MEGA link and Railway storage."
                )
                return

            links = []

            for path in candidates:
                token = secrets.token_urlsafe(24)
                size = path.stat().st_size

                mime_types = {
                    ".mp4": "video/mp4",
                    ".m4v": "video/mp4",
                    ".mkv": "video/x-matroska",
                    ".mov": "video/quicktime",
                    ".webm": "video/webm",
                    ".avi": "video/x-msvideo",
                    ".mpeg": "video/mpeg",
                    ".mpg": "video/mpeg",
                }

                videos[token] = {
                    "local_path": str(path),
                    "mime": mime_types.get(
                        path.suffix.lower(),
                        "application/octet-stream",
                    ),
                    "name": path.name,
                    "size": size,
                }

                stream_link = f"{PUBLIC_URL}/watch/{token}"

                links.append(
                    format_upload_message(
                        safe_name(path.name),
                        size,
                        stream_link,
                    )
                )

            await status.edit_text("\n\n".join(links))

        except Exception as exc:
            log.exception("MEGA stream preparation failed")

            with suppress(Exception):
                await status.edit_text(
                    "MEGA link failed.\n\n"
                    f"Error: {type(exc).__name__}: {str(exc)[:200]}\n\n"
                    "Check the public link, Railway logs, "
                    "and available storage."
                )


async def incoming_video(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    msg = update.effective_message

    if not msg:
        return

    media = msg.video or msg.document

    if not media:
        return

    mime = getattr(media, "mime_type", "") or ""

    if msg.document and not mime.startswith("video/"):
        return

    status = await msg.reply_text(
        "Video received. Preparing streaming link..."
    )

    try:
        original = await mtproto.get_messages(
            msg.chat_id,
            ids=msg.message_id,
        )

        if not original or not original.media:
            raise RuntimeError(
                "MTProto cannot access this message"
            )

        token = secrets.token_urlsafe(24)

        file_size = (
            getattr(getattr(original, "file", None), "size", None)
            or getattr(media, "file_size", None)
            or 0
        )

        file_name = (
            getattr(media, "file_name", None)
            or getattr(msg, "caption", None)
            or "Telegram video"
        )

        videos[token] = {
            "chat_id": msg.chat_id,
            "message_id": msg.message_id,
            "mime": mime or "video/mp4",
            "name": file_name,
            "size": file_size,
        }

        stream_link = f"{PUBLIC_URL}/watch/{token}"

        await status.edit_text(
            format_upload_message(
                file_name,
                file_size,
                stream_link,
            )
        )

    except Exception:
        log.exception("Incoming video registration failed")

        await status.edit_text(
            "Could not prepare the streaming link.\n"
            "Please check Railway logs."
        )


async def home(request):
    return web.Response(
        text="Telegram Video Streamer is running."
    )


async def health(request):
    return web.json_response(
        {
            "status": "ok",
            "mtproto_connected": mtproto.is_connected(),
            "links": len(videos),
        }
    )


async def watch(request):
    token = request.match_info["token"]

    if token not in videos:
        raise web.HTTPNotFound(
            text="Link missing or service restarted. Send the video again."
        )

    return web.Response(
        text=PLAYER,
        content_type="text/html",
    )


async def media(request):
    token = request.match_info["token"]
    record = videos.get(token)

    if not record:
        raise web.HTTPNotFound(
            text="Link missing or service restarted."
        )

    local_path = record.get("local_path")

    if local_path:
        path = Path(local_path)

        if not path.is_file():
            raise web.HTTPNotFound(
                text="Stream file is no longer available."
            )

        total = path.stat().st_size
        start, end, status = 0, total - 1, 200
        range_header = request.headers.get("Range")

        if range_header:
            try:
                if (
                    not range_header.startswith("bytes=")
                    or "," in range_header
                ):
                    raise ValueError()

                left, separator, right = range_header[6:].partition("-")

                if not separator:
                    raise ValueError()

                if left:
                    start = int(left)
                    end = int(right) if right else total - 1
                else:
                    suffix = int(right or "0")

                    if suffix <= 0:
                        raise ValueError()

                    start = max(0, total - suffix)
                    end = total - 1

                if start < 0 or start >= total or end < start:
                    raise ValueError()

                end = min(end, total - 1)
                status = 206

            except ValueError:
                return web.Response(
                    status=416,
                    headers={"Content-Range": f"bytes */{total}"},
                )

        length = end - start + 1

        headers = {
            "Accept-Ranges": "bytes",
            "Content-Type": record.get("mime") or "video/mp4",
            "Content-Length": str(length),
            "Cache-Control": "private, no-store",
        }

        if status == 206:
            headers["Content-Range"] = (
                f"bytes {start}-{end}/{total}"
            )

        response = web.StreamResponse(
            status=status,
            headers=headers,
        )

        await response.prepare(request)

        try:
            with path.open("rb") as file:
                file.seek(start)
                remaining = length

                while remaining:
                    chunk = await asyncio.to_thread(
                        file.read,
                        min(512 * 1024, remaining),
                    )

                    if not chunk:
                        break

                    await response.write(chunk)
                    remaining -= len(chunk)

            await response.write_eof()
            return response

        except Exception:
            log.exception("Local MEGA streaming failed")
            raise web.HTTPBadGateway(
                text="Streaming failed. Check Railway logs."
            )

    try:
        message = await mtproto.get_messages(
            record["chat_id"],
            ids=record["message_id"],
        )

        if not message or not message.media:
            raise web.HTTPNotFound(
                text="Telegram media unavailable."
            )

        total = (
            getattr(getattr(message, "file", None), "size", None)
            or record.get("size")
        )

        if not total:
            raise web.HTTPBadGateway(
                text="Could not determine media size."
            )

        start, end, status = 0, total - 1, 200
        range_header = request.headers.get("Range")

        if range_header:
            try:
                if (
                    not range_header.startswith("bytes=")
                    or "," in range_header
                ):
                    raise ValueError()

                left, separator, right = range_header[6:].partition("-")

                if not separator:
                    raise ValueError()

                if left:
                    start = int(left)
                    end = int(right) if right else total - 1
                else:
                    suffix = int(right or "0")

                    if suffix <= 0:
                        raise ValueError()

                    start = max(0, total - suffix)
                    end = total - 1

                if start < 0 or start >= total or end < start:
                    raise ValueError()

                end = min(end, total - 1)
                status = 206

            except ValueError:
                return web.Response(
                    status=416,
                    headers={"Content-Range": f"bytes */{total}"},
                )

        length = end - start + 1

        headers = {
            "Accept-Ranges": "bytes",
            "Content-Type": record.get("mime") or "video/mp4",
            "Content-Length": str(length),
            "Cache-Control": "private, no-store",
        }

        if status == 206:
            headers["Content-Range"] = (
                f"bytes {start}-{end}/{total}"
            )

        response = web.StreamResponse(
            status=status,
            headers=headers,
        )

        await response.prepare(request)

        sent = 0

        async for chunk in mtproto.iter_download(
            message,
            offset=start,
            limit=length,
            chunk_size=512 * 1024,
        ):
            if not chunk:
                continue

            chunk = chunk[:length - sent]

            await response.write(chunk)
            sent += len(chunk)

            if sent >= length:
                break

        await response.write_eof()
        return response

    except web.HTTPException:
        raise

    except Exception:
        log.exception("Telegram streaming failed")
        raise web.HTTPBadGateway(
            text="Streaming failed. Check Railway logs."
        )


async def main():
    bot = Application.builder().token(BOT_TOKEN).build()

    bot.add_handler(CommandHandler("start", start_cmd))
    bot.add_handler(CommandHandler("help", help_cmd))

    bot.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            mega_message,
        )
    )

    bot.add_handler(
        MessageHandler(
            filters.VIDEO | filters.Document.VIDEO,
            incoming_video,
        )
    )

    await bot.initialize()
    await bot.start()

    if not bot.updater:
        raise RuntimeError("Telegram polling updater unavailable")

    await bot.updater.start_polling(
        allowed_updates=Update.ALL_TYPES
    )

    log.info("Telegram Bot API polling started")

    await mtproto.start(bot_token=BOT_TOKEN)

    log.info("MTProto client connected")

    application = web.Application(
        client_max_size=2 * 1024 * 1024
    )

    application.router.add_get("/", home)
    application.router.add_get("/health", health)
    application.router.add_get("/watch/{token}", watch)
    application.router.add_get("/media/{token}", media)

    runner = web.AppRunner(application)

    await runner.setup()

    await web.TCPSite(
        runner,
        "0.0.0.0",
        PORT,
    ).start()

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