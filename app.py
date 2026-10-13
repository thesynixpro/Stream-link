import asyncio
import logging
import os
import secrets
from aiohttp import web
from telethon import TelegramClient, events
from telethon.errors import RPCError

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("telegram-video-streamer")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
API_ID_RAW = os.getenv("API_ID", "").strip()
API_HASH = os.getenv("API_HASH", "").strip()
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
PORT = int(os.getenv("PORT", "8080"))

if not BOT_TOKEN:
    raise RuntimeError("Missing required Railway Variable: BOT_TOKEN")
if not API_ID_RAW.isdigit():
    raise RuntimeError("API_ID must be the numeric App api_id from my.telegram.org")
if not API_HASH:
    raise RuntimeError("Missing required Railway Variable: API_HASH")
if not PUBLIC_URL.startswith("https://"):
    raise RuntimeError("PUBLIC_URL must be your Railway public HTTPS domain, e.g. https://your-service.up.railway.app")

API_ID = int(API_ID_RAW)
client = TelegramClient("railway_bot_session", API_ID, API_HASH)
# In-memory links: a restart clears generated links. The actual video remains in Telegram.
videos = {}

HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Telegram Video Stream</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#0b1220;color:#eef2ff;font:16px system-ui,sans-serif;display:grid;min-height:100vh;place-items:center;padding:18px}
main{width:min(900px,100%);background:#131d30;border:1px solid #263550;border-radius:20px;padding:20px;box-shadow:0 20px 60px #0005}
h1{font-size:1.35rem;margin:0 0 8px}p{color:#aab8d0}video{width:100%;max-height:75vh;background:#000;border-radius:12px;margin-top:14px}
.small{font-size:.85rem;color:#93a4bf}
</style></head><body><main><h1>Telegram Video Stream</h1><p>Playback streams from Telegram. Playback availability depends on Telegram access and hosting limits.</p>
<video controls playsinline preload="metadata" src="/media/VIDEO_TOKEN"></video>
<p class="small">Keep this link private. Anyone who has the link may be able to view the video.</p></main>
<script>document.querySelector('video').src='/media/'+location.pathname.split('/').pop();</script>
</body></html>"""

async def start_client(app):
    await client.start(bot_token=BOT_TOKEN)
    log.info("Telegram MTProto bot client connected")
    app["telegram_client"] = client

    @client.on(events.NewMessage(incoming=True))
    async def on_video(event):
        msg = event.message
        if not msg or not msg.media:
            return
        try:
            if not getattr(msg, "video", None) and not getattr(msg, "document", None):
                return
            # Only accept video MIME types or Telegram's video attribute.
            mime = getattr(getattr(msg, "file", None), "mime_type", None) or ""
            if not getattr(msg, "video", None) and not mime.startswith("video/"):
                return
            token = secrets.token_urlsafe(24)
            videos[token] = {"chat_id": event.chat_id, "message_id": msg.id,
                             "size": getattr(getattr(msg, "file", None), "size", None),
                             "name": getattr(getattr(msg, "file", None), "name", None),
                             "mime": mime or "video/mp4"}
            link = f"{PUBLIC_URL}/watch/{token}"
            await event.reply(f"Your streaming link:\n{link}\n\nKeep this link private.")
            log.info("Stored video message for a generated link")
        except Exception:
            log.exception("Could not process incoming video")

async def close_client(app):
    if client.is_connected():
        await client.disconnect()

def find_video(token):
    return videos.get(token)

async def home(request):
    return web.Response(text="Telegram Video Streamer is running.", content_type="text/plain")

async def watch(request):
    token = request.match_info["token"]
    if token not in videos:
        raise web.HTTPNotFound(text="Link not found. Send the video to the bot again after a restart.")
    page = HTML.replace("VIDEO_TOKEN", token)
    return web.Response(text=page, content_type="text/html")

async def media(request):
    token = request.match_info["token"]
    record = find_video(token)
    if not record:
        raise web.HTTPNotFound(text="Link not found. Send the video to the bot again after a restart.")

    try:
        msg = await client.get_messages(record["chat_id"], ids=record["message_id"])
        if not msg or not msg.media:
            raise web.HTTPNotFound(text="Telegram message/media is unavailable.")
        total = getattr(getattr(msg, "file", None), "size", None)
        if not total:
            raise web.HTTPBadRequest(text="Could not determine media size.")

        start, end = 0, total - 1
        status = 200
        range_header = request.headers.get("Range")
        if range_header:
            if not range_header.startswith("bytes=") or "," in range_header:
                raise web.HTTPRequestRangeNotSatisfiable()
            spec = range_header[6:].strip()
            left, sep, right = spec.partition("-")
            if not sep:
                raise web.HTTPRequestRangeNotSatisfiable()
            if left:
                start = int(left)
                end = int(right) if right else total - 1
            else:
                suffix = int(right or "0")
                if suffix <= 0:
                    raise web.HTTPRequestRangeNotSatisfiable()
                start = max(0, total - suffix)
            if start >= total or end < start:
                raise web.HTTPRequestRangeNotSatisfiable()
            end = min(end, total - 1)
            status = 206

        length = end - start + 1
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Type": record.get("mime") or "video/mp4",
            "Content-Length": str(length),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        }
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{total}"
        response = web.StreamResponse(status=status, headers=headers)
        await response.prepare(request)

        # Telethon reads in chunks; do not store the entire video on disk/RAM.
        # Telegram/Telethon may impose alignment or access restrictions for some media.
        iterator = client.iter_download(msg, offset=start, limit=length, chunk_size=512 * 1024)
        sent = 0
        async for chunk in iterator:
            if not chunk:
                continue
            remaining = length - sent
            chunk = chunk[:remaining]
            await response.write(chunk)
            sent += len(chunk)
            if sent >= length:
                break
        await response.write_eof()
        return response
    except web.HTTPException:
        raise
    except (ValueError, RPCError) as exc:
        log.exception("Telegram media streaming failed")
        raise web.HTTPBadGateway(text=f"Telegram could not provide this media: {type(exc).__name__}")
    except (ConnectionError, asyncio.CancelledError):
        raise
    except Exception as exc:
        log.exception("Unexpected streaming error")
        raise web.HTTPBadGateway(text=f"Streaming failed: {type(exc).__name__}")

async def main():
    app = web.Application(client_max_size=2 * 1024 * 1024)
    app.router.add_get("/", home)
    app.router.add_get("/watch/{token}", watch)
    app.router.add_get("/media/{token}", media)
    app.on_startup.append(start_client)
    app.on_cleanup.append(close_client)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("HTTP server listening on port %s", PORT)
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
