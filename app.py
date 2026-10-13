import asyncio
import logging
import os
import secrets
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
from telethon.errors import RPCError


# --------------------------------------------------
# LOGGING
# --------------------------------------------------

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

log = logging.getLogger("telegram-video-streamer")


# --------------------------------------------------
# CONFIGURATION
# --------------------------------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
API_ID_RAW = os.getenv("API_ID", "").strip()
API_HASH = os.getenv("API_HASH", "").strip()
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
PORT = int(os.getenv("PORT", "8080"))

if not BOT_TOKEN:
    raise RuntimeError("Missing Railway variable: BOT_TOKEN")

if not API_ID_RAW.isdigit():
    raise RuntimeError(
        "API_ID must be the numeric API ID from my.telegram.org"
    )

if not API_HASH:
    raise RuntimeError("Missing Railway variable: API_HASH")

if not PUBLIC_URL.startswith("https://"):
    raise RuntimeError(
        "PUBLIC_URL must be your public Railway HTTPS domain"
    )

API_ID = int(API_ID_RAW)


# --------------------------------------------------
# TELEGRAM MTProto CLIENT
# Used to read Telegram media in chunks.
# --------------------------------------------------

telegram_client = TelegramClient(
    "railway_video_session",
    API_ID,
    API_HASH,
)


# --------------------------------------------------
# IN-MEMORY VIDEO REGISTRY
#
# Links are lost when Railway restarts the service.
# Video files themselves remain in Telegram.
# --------------------------------------------------

videos = {}


# --------------------------------------------------
# HTML VIDEO PLAYER
# --------------------------------------------------

PLAYER_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">

<title>Telegram Video Streamer</title>

<style>
* {
    box-sizing: border-box;
}

body {
    margin: 0;
    min-height: 100vh;
    padding: 18px;
    display: grid;
    place-items: center;
    background: #0b1020;
    color: #f3f5ff;
    font-family: system-ui, sans-serif;
}

main {
    width: min(900px, 100%);
    padding: 20px;
    background: #151e32;
    border: 1px solid #293752;
    border-radius: 18px;
}

h1 {
    font-size: 1.4rem;
    margin-top: 0;
}

p {
    color: #b8c5dc;
    line-height: 1.6;
}

video {
    display: block;
    width: 100%;
    max-height: 75vh;
    margin-top: 18px;
    background: #000;
    border-radius: 12px;
}

.small {
    font-size: 0.85rem;
}
</style>
</head>

<body>
<main>
    <h1>Telegram Video Streamer</h1>

    <p>
        Play your video directly in the browser.
        Playback depends on Telegram media access,
        video format, and hosting limits.
    </p>

    <video id="player" controls playsinline preload="metadata"></video>

    <p class="small">
        Keep your streaming link private.
        Anyone with the link may be able to access the video.
    </p>
</main>

<script>
const token = location.pathname.split("/").pop();

document.getElementById("player").src =
    "/media/" + encodeURIComponent(token);
</script>
</body>
</html>
"""


# --------------------------------------------------
# TELEGRAM COMMAND HANDLER
# --------------------------------------------------

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message

    if not message:
        return

    await message.reply_text(
        "Welcome to Telegram Video Streamer!\n\n"
        "Send me a video or a video file and I will "
        "try to generate a browser streaming link.\n\n"
        "Commands:\n"
        "/start - Start the bot\n"
        "/help - Show help"
    )

    log.info(
        "Handled /start for chat %s",
        update.effective_chat.id if update.effective_chat else "unknown",
    )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message

    if message:
        await message.reply_text(
            "How to use this bot:\n\n"
            "1. Send a video to this chat.\n"
            "2. Wait for the streaming link.\n"
            "3. Open the link in your browser.\n\n"
            "Large videos may take time to start. "
            "Some video formats may not play in every browser."
        )


# --------------------------------------------------
# VIDEO MESSAGE HANDLER
# --------------------------------------------------

async def video_received(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message

    if not message:
        return

    file_id = None
    mime_type = "video/mp4"
    file_name = None
    file_size = None

    if message.video:
        media = message.video

        file_id = media.file_id
        mime_type = media.mime_type or "video/mp4"
        file_name = media.file_name
        file_size = media.file_size

    elif message.document:
        media = message.document

        mime_type = media.mime_type or ""

        if not mime_type.startswith("video/"):
            await message.reply_text(
                "Please send a video or a document "
                "with a video MIME type."
            )
            return

        file_id = media.file_id
        file_name = media.file_name
        file_size = media.file_size

    if not file_id:
        await message.reply_text(
            "I could not identify the video. Please try again."
        )
        return

    # Acknowledge receipt before preparing the link.
    status = await message.reply_text(
        "Video received. Preparing your streaming link..."
    )

    try:
        # Find the Telegram message using the MTProto client.
        #
        # The message is stored in the bot's private chat
        # with the user. For private bot chats, the chat ID
        # and message ID are used to retrieve the media.
        #
        # The media is not downloaded here.
        record_token = secrets.token_urlsafe(24)

        videos[record_token] = {
            "chat_id": message.chat_id,
            "message_id": message.message_id,
            "file_id": file_id,
            "mime": mime_type,
            "name": file_name,
            "size": file_size,
        }

        link = f"{PUBLIC_URL}/watch/{record_token}"

        size_text = (
            f"{file_size / (1024 * 1024):.1f} MB"
            if file_size
            else "Unknown size"
        )

        await status.edit_text(
            "Your streaming link is ready!\n\n"
            f"{link}\n\n"
            f"File size: {size_text}\n\n"
            "Open the link in your browser.\n"
            "Keep the link private."
        )

        log.info(
            "Registered video: chat_id=%s message_id=%s size=%s",
            message.chat_id,
            message.message_id,
            file_size,
        )

    except Exception:
        log.exception("Could not register video")

        with suppress(Exception):
            await status.edit_text(
                "I received your video but could not prepare "
                "the link. Please check the Railway logs and try again."
            )


# --------------------------------------------------
# HTTP ROUTES
# --------------------------------------------------

async def home(request):
    return web.Response(
        text="Telegram Video Streamer is running.",
        content_type="text/plain",
    )


async def health(request):
    return web.json_response(
        {
            "status": "ok",
            "telegram_connected": telegram_client.is_connected(),
        }
    )


async def watch(request):
    token = request.match_info["token"]

    if token not in videos:
        raise web.HTTPNotFound(
            text="Link not found. Send the video again to generate a new link."
        )

    return web.Response(
        text=PLAYER_HTML,
        content_type="text/html",
    )


# --------------------------------------------------
# HTTP RANGE PARSER
# --------------------------------------------------

def parse_range_header(header, total):
    """
    Returns (start, end) inclusive.
    Raises ValueError for an invalid range.
    """

    if not header:
        return 0, total - 1

    if not header.startswith("bytes="):
        raise ValueError("Unsupported Range header")

    spec = header[6:].strip()

    if "," in spec:
        raise ValueError("Multiple ranges are not supported")

    left, sep, right = spec.partition("-")

    if not sep:
        raise ValueError("Malformed Range header")

    if left:
        start = int(left)
        end = int(right) if right else total - 1
    else:
        suffix_length = int(right or "0")

        if suffix_length <= 0:
            raise ValueError("Invalid suffix range")

        start = max(0, total - suffix_length)
        end = total - 1

    if start < 0 or start >= total or end < start:
        raise ValueError("Range is outside the file")

    end = min(end, total - 1)

    return start, end


# --------------------------------------------------
# STREAM MEDIA
# --------------------------------------------------

async def media(request):
    token = request.match_info["token"]
    record = videos.get(token)

    if not record:
        raise web.HTTPNotFound(
            text="Link expired or unknown. Send the video again."
        )

    if not telegram_client.is_connected():
        raise web.HTTPServiceUnavailable(
            text="Telegram media client is not connected."
        )

    try:
        msg = await telegram_client.get_messages(
            record["chat_id"],
            ids=record["message_id"],
        )

        if not msg or not msg.media:
            raise web.HTTPNotFound(
                text="Telegram media is unavailable."
            )

        total = getattr(
            getattr(msg, "file", None),
            "size",
            None,
        )

        if not total:
            total = record.get("size")

        if not total:
            raise web.HTTPBadGateway(
                text="Could not determine video size."
            )

        range_header = request.headers.get("Range")

        try:
            start, end = parse_range_header(
                range_header,
                total,
            )
        except (ValueError, TypeError):
            return web.Response(
                status=416,
                headers={
                    "Content-Range": f"bytes */{total}",
                    "Accept-Ranges": "bytes",
                },
                text="Requested byte range is not satisfiable.",
            )

        length = end - start + 1
        status_code = 206 if range_header else 200

        headers = {
            "Accept-Ranges": "bytes",
            "Content-Type": record.get("mime") or "video/mp4",
            "Content-Length": str(length),
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        }

        if status_code == 206:
            headers["Content-Range"] = (
                f"bytes {start}-{end}/{total}"
            )

        response = web.StreamResponse(
            status=status_code,
            headers=headers,
        )

        await response.prepare(request)

        # Stream data in chunks rather than downloading
        # the entire file to disk or memory.
        #
        # Telegram/Telethon media access can fail for some
        # messages or file types; such failures are logged.

        sent = 0
        chunk_size = 512 * 1024

        iterator = telegram_client.iter_download(
            msg,
            offset=start,
            limit=length,
            chunk_size=chunk_size,
        )

        async for chunk in iterator:
            if not chunk:
                continue

            remaining = length - sent

            if remaining <= 0:
                break

            if len(chunk) > remaining:
                chunk = chunk[:remaining]

            await response.write(chunk)
            sent += len(chunk)

            if sent >= length:
                break

        if sent != length:
            log.warning(
                "Incomplete stream: sent %s of %s bytes",
                sent,
                length,
            )

        await response.write_eof()
        return response

    except web.HTTPException:
        raise

    except (RPCError, OSError, ValueError):
        log.exception("Telegram media streaming failed")

        raise web.HTTPBadGateway(
            text="Telegram could not provide this media."
        )

    except asyncio.CancelledError:
        raise

    except Exception:
        log.exception("Unexpected streaming error")

        raise web.HTTPBadGateway(
            text="Streaming failed. Check the Railway logs."
        )


# --------------------------------------------------
# STARTUP AND SHUTDOWN
# --------------------------------------------------

async def start_telegram(application):
    """
    Start the Telethon MTProto client.

    The python-telegram-bot Application separately handles
    Bot API updates through polling.
    """

    await telegram_client.start(
        bot_token=BOT_TOKEN,
    )

    log.info("Telegram MTProto client connected")


async def stop_telegram(application):
    if telegram_client.is_connected():
        await telegram_client.disconnect()

    log.info("Telegram MTProto client disconnected")


# --------------------------------------------------
# MAIN
# --------------------------------------------------

async def main():
    # Bot API client for commands and incoming video messages.
    bot_app = Application.builder().token(BOT_TOKEN).build()

    bot_app.add_handler(
        CommandHandler("start", start_command)
    )

    bot_app.add_handler(
        CommandHandler("help", help_command)
    )

    bot_app.add_handler(
        MessageHandler(
            filters.VIDEO | filters.Document.VIDEO,
            video_received,
        )
    )

    # Start Bot API application and polling.
    await bot_app.initialize()
    await bot_app.start()

    if bot_app.updater is None:
        raise RuntimeError("Telegram polling updater is unavailable")

    await bot_app.updater.start_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )

    log.info("Telegram Bot API polling started")

    # Start the separate MTProto media client.
    await start_telegram(bot_app)

    # Start HTTP server.
    web_app = web.Application(client_max_size=2 * 1024 * 1024)

    web_app.router.add_get("/", home)
    web_app.router.add_get("/health", health)
    web_app.router.add_get("/watch/{token}", watch)
    web_app.router.add_get("/media/{token}", media)

    runner = web.AppRunner(web_app)
    await runner.setup()

    site = web.TCPSite(
        runner,
        host="0.0.0.0",
        port=PORT,
    )

    await site.start()

    log.info("HTTP server listening on port %s", PORT)

    try:
        # Keep the process alive.
        await asyncio.Event().wait()

    finally:
        log.info("Shutting down services")

        await runner.cleanup()

        if bot_app.updater and bot_app.updater.running:
            await bot_app.updater.stop()

        await bot_app.stop()
        await bot_app.shutdown()

        await stop_telegram(bot_app)


if __name__ == "__main__":
    asyncio.run(main())
