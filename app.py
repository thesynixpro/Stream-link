import asyncio, logging, os, re, secrets, tempfile
from pathlib import Path
from urllib.parse import urlparse
from contextlib import suppress
from aiohttp import web
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from telethon import TelegramClient
from mega import Mega

logging.basicConfig(level=os.getenv('LOG_LEVEL','INFO').upper(), format='%(asctime)s %(levelname)s %(name)s: %(message)s')
log=logging.getLogger('mega-streamer')
BOT_TOKEN=os.getenv('BOT_TOKEN','').strip()
API_ID_RAW=os.getenv('API_ID','').strip()
API_HASH=os.getenv('API_HASH','').strip()
PUBLIC_URL=os.getenv('PUBLIC_URL','').strip().rstrip('/')
PORT=int(os.getenv('PORT','8080'))
MEGA_EMAIL=os.getenv('MEGA_EMAIL','').strip()
MEGA_PASSWORD=os.getenv('MEGA_PASSWORD','')
MAX_TRANSFER_GB=float(os.getenv('MAX_TRANSFER_GB','2.0'))
MAX_BYTES=int(MAX_TRANSFER_GB*1024**3)
if not BOT_TOKEN: raise RuntimeError('Missing Railway variable: BOT_TOKEN')
if not API_ID_RAW.isdigit(): raise RuntimeError('API_ID must be numeric (from my.telegram.org)')
if not API_HASH: raise RuntimeError('Missing Railway variable: API_HASH')
if not PUBLIC_URL.startswith('https://'): raise RuntimeError('PUBLIC_URL must be your Railway HTTPS domain')
API_ID=int(API_ID_RAW)
mtproto=TelegramClient('railway_mega_session',API_ID,API_HASH)
videos={}
transfer_lock=asyncio.Lock()
STREAM_DIR=Path(os.getenv('STREAM_DIR','/tmp/mega-streams'))
STREAM_DIR.mkdir(parents=True, exist_ok=True)
PLAYER='''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Telegram Video</title><style>body{margin:0;background:#0b1220;color:#eef2ff;font:16px system-ui;padding:16px}main{max-width:900px;margin:auto;background:#151e32;padding:20px;border-radius:16px}video{width:100%;max-height:75vh;background:#000}p{color:#bac6da}</style></head><body><main><h2>Telegram Video Stream</h2><p>Keep this link private.</p><video id="v" controls playsinline preload="metadata"></video><script>document.getElementById('v').src='/media/'+location.pathname.split('/').pop()</script></main></body></html>'''

def mega_link(s):
    try:
        p=urlparse(s.strip())
        return p.scheme in ('http','https') and (p.hostname or '').lower() in ('mega.nz','www.mega.nz','mega.co.nz','www.mega.co.nz')
    except Exception: return False

def download_mega(url, outdir):
    # Run this under Python 3.10 (runtime.txt); legacy mega.py dependencies
    # are not compatible with Python 3.12.
    m=Mega()
    account=m.login(MEGA_EMAIL,MEGA_PASSWORD) if MEGA_EMAIL and MEGA_PASSWORD else m.login()
    # mega.py's public-link API uses download_url for file links. Folder link
    # handling varies by package version; errors are caught and sent to the user.
    account.download_url(url, dest_path=outdir)

def safe_name(n):
    return re.sub(r'[^A-Za-z0-9._() \-\[\]]+','_',Path(n or 'video').name).strip(' .')[:160] or 'video'

def register_mega_file(path):
    token=secrets.token_urlsafe(24)
    size=Path(path).stat().st_size
    suffix=Path(path).suffix.lower()
    mime={'.mp4':'video/mp4','.m4v':'video/mp4','.mkv':'video/x-matroska','.mov':'video/quicktime','.webm':'video/webm','.avi':'video/x-msvideo','.mpeg':'video/mpeg','.mpg':'video/mpeg'}.get(suffix,'application/octet-stream')
    videos[token]={'local_path':str(path),'mime':mime,'name':Path(path).name,'size':size}
    return f'{PUBLIC_URL}/watch/{token}'

async def register_uploaded(chat_id,sent,path):
    token=secrets.token_urlsafe(24); size=Path(path).stat().st_size
    videos[token]={'chat_id':chat_id,'message_id':sent.id,'mime':'video/mp4','name':Path(path).name,'size':size}
    return f'{PUBLIC_URL}/watch/{token}'

async def start_cmd(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if update.effective_message:
        await update.effective_message.reply_text('Welcome! Send a public MEGA file link. Videos are downloaded to temporary Railway storage, uploaded to Telegram, then a streaming link is returned. Large transfers depend on Railway disk/network and Telegram limits. Use /help.')

async def help_cmd(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if update.effective_message:
        await update.effective_message.reply_text('Send a public mega.nz/file/... link. Files are temporarily downloaded to Railway before upload. Folder-link support depends on the MEGA package/link format. Only transfer content you own or have permission to copy.')

async def mega_message(update:Update, context:ContextTypes.DEFAULT_TYPE):
    msg=update.effective_message
    if not msg or not msg.text: return
    match=re.search(r'https?://(?:www\.)?(?:mega\.nz|mega\.co\.nz)/\S+',msg.text.strip(),re.I)
    if not match: return
    url=match.group(0).rstrip(').,;')
    if not mega_link(url): return
    status=await msg.reply_text('MEGA link received. Preparing video for browser streaming on the server...')
    async with transfer_lock:
        try:
            # Keep the downloaded file on Railway so the browser can stream it.
            # The bot does NOT upload or send the video back to Telegram.
            workdir=Path(tempfile.mkdtemp(prefix='mega-',dir=str(STREAM_DIR)))
            await asyncio.to_thread(download_mega,url,str(workdir))
            candidates=[p for p in workdir.rglob('*') if p.is_file() and p.suffix.lower() in {'.mp4','.mkv','.mov','.webm','.avi','.m4v','.mpeg','.mpg'}]
            candidates=[p for p in candidates if p.stat().st_size<=MAX_BYTES]
            if not candidates:
                await status.edit_text(f'No supported video found, or file exceeds configured {MAX_TRANSFER_GB:g} GB limit. Check the public link and Railway disk capacity.')
                return
            links=[]
            for p in candidates:
                token=secrets.token_urlsafe(24)
                size=p.stat().st_size
                suffix=p.suffix.lower()
                mime={'.mp4':'video/mp4','.m4v':'video/mp4','.mkv':'video/x-matroska','.mov':'video/quicktime','.webm':'video/webm','.avi':'video/x-msvideo','.mpeg':'video/mpeg','.mpg':'video/mpeg'}.get(suffix,'application/octet-stream')
                videos[token]={'local_path':str(p),'mime':mime,'name':p.name,'size':size}
                links.append(f'{safe_name(p.name)} ({size/1024**2:.1f} MB):\n{PUBLIC_URL}/watch/{token}')
            await status.edit_text('Ready. I will only send streaming links; the video file is not sent to Telegram.\n\n'+'\n\n'.join(links))
        except Exception as e:
            log.exception('MEGA stream preparation failed')
            with suppress(Exception):
                await status.edit_text(f'MEGA link failed ({type(e).__name__}): {str(e)[:200]}\nCheck Railway logs, public-link access, and disk capacity.')

async def incoming_video(update:Update, context:ContextTypes.DEFAULT_TYPE):
    msg=update.effective_message
    if not msg: return
    media=msg.video or msg.document
    if not media: return
    mime=getattr(media,'mime_type','') or ''
    if msg.document and not mime.startswith('video/'): return
    status=await msg.reply_text('Video received. Preparing streaming link...')
    try:
        original=await mtproto.get_messages(msg.chat_id,ids=msg.message_id)
        if not original or not original.media: raise RuntimeError('MTProto cannot access this message')
        token=secrets.token_urlsafe(24)
        size=getattr(getattr(original,'file',None),'size',None) or getattr(media,'file_size',None)
        videos[token]={'chat_id':msg.chat_id,'message_id':msg.message_id,'mime':mime or 'video/mp4','size':size}
        await status.edit_text(f'Streaming link:\n{PUBLIC_URL}/watch/{token}')
    except Exception:
        log.exception('Incoming video registration failed')
        await status.edit_text('Could not prepare the link. Check Railway logs.')

async def home(req): return web.Response(text='Telegram Video Streamer is running.')
async def health(req): return web.json_response({'status':'ok','mtproto_connected':mtproto.is_connected(),'links':len(videos)})
async def watch(req):
    if req.match_info['token'] not in videos: raise web.HTTPNotFound(text='Link missing or service restarted; send video again.')
    return web.Response(text=PLAYER,content_type='text/html')

async def media(req):
    token=req.match_info['token']; rec=videos.get(token)
    if not rec: raise web.HTTPNotFound(text='Link missing or service restarted.')
    # MEGA imports are streamed from the file stored on Railway, not from Telegram.
    local_path=rec.get('local_path')
    if local_path:
        path=Path(local_path)
        if not path.is_file(): raise web.HTTPNotFound(text='Stream file is no longer available.')
        total=path.stat().st_size
        start,end,status=0,total-1,200; rh=req.headers.get('Range')
        if rh:
            try:
                if not rh.startswith('bytes=') or ',' in rh: raise ValueError()
                left,sep,right=rh[6:].partition('-')
                if not sep: raise ValueError()
                if left: start=int(left); end=int(right) if right else total-1
                else:
                    suffix=int(right or '0')
                    if suffix<=0: raise ValueError()
                    start=max(0,total-suffix); end=total-1
                if start<0 or start>=total or end<start: raise ValueError()
                end=min(end,total-1); status=206
            except ValueError: return web.Response(status=416,headers={'Content-Range':f'bytes */{total}'})
        length=end-start+1
        headers={'Accept-Ranges':'bytes','Content-Type':rec.get('mime') or 'video/mp4','Content-Length':str(length),'Cache-Control':'private, no-store'}
        if status==206: headers['Content-Range']=f'bytes {start}-{end}/{total}'
        resp=web.StreamResponse(status=status,headers=headers); await resp.prepare(req)
        try:
            with path.open('rb') as f:
                f.seek(start); remaining=length
                while remaining:
                    chunk=await asyncio.to_thread(f.read,min(512*1024,remaining))
                    if not chunk: break
                    await resp.write(chunk); remaining-=len(chunk)
            await resp.write_eof(); return resp
        except Exception:
            log.exception('Local MEGA streaming failed')
            raise web.HTTPBadGateway(text='Streaming failed; check Railway logs.')
    try:
        msg=await mtproto.get_messages(rec['chat_id'],ids=rec['message_id'])
        if not msg or not msg.media: raise web.HTTPNotFound(text='Telegram media unavailable.')
        total=getattr(getattr(msg,'file',None),'size',None) or rec.get('size')
        if not total: raise web.HTTPBadGateway(text='Could not determine media size.')
        start,end,status=0,total-1,200; rh=req.headers.get('Range')
        if rh:
            try:
                if not rh.startswith('bytes=') or ',' in rh: raise ValueError()
                left,sep,right=rh[6:].partition('-')
                if not sep: raise ValueError()
                if left: start=int(left); end=int(right) if right else total-1
                else:
                    suffix=int(right or '0')
                    if suffix<=0: raise ValueError()
                    start=max(0,total-suffix); end=total-1
                if start<0 or start>=total or end<start: raise ValueError()
                end=min(end,total-1); status=206
            except ValueError: return web.Response(status=416,headers={'Content-Range':f'bytes */{total}'})
        length=end-start+1
        headers={'Accept-Ranges':'bytes','Content-Type':rec.get('mime') or 'video/mp4','Content-Length':str(length),'Cache-Control':'private, no-store'}
        if status==206: headers['Content-Range']=f'bytes {start}-{end}/{total}'
        resp=web.StreamResponse(status=status,headers=headers); await resp.prepare(req); sent=0
        async for chunk in mtproto.iter_download(msg,offset=start,limit=length,chunk_size=512*1024):
            if not chunk: continue
            chunk=chunk[:length-sent]; await resp.write(chunk); sent+=len(chunk)
            if sent>=length: break
        await resp.write_eof(); return resp
    except web.HTTPException: raise
    except Exception:
        log.exception('Streaming failed')
        raise web.HTTPBadGateway(text='Streaming failed; check Railway logs.')

async def main():
    bot=Application.builder().token(BOT_TOKEN).build()
    bot.add_handler(CommandHandler('start',start_cmd)); bot.add_handler(CommandHandler('help',help_cmd))
    bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,mega_message))
    bot.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO,incoming_video))
    await bot.initialize(); await bot.start()
    if not bot.updater: raise RuntimeError('Telegram polling updater unavailable')
    await bot.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    log.info('Telegram Bot API polling started')
    await mtproto.start(bot_token=BOT_TOKEN); log.info('MTProto client connected')
    wa=web.Application(client_max_size=2*1024*1024)
    wa.router.add_get('/',home); wa.router.add_get('/health',health); wa.router.add_get('/watch/{token}',watch); wa.router.add_get('/media/{token}',media)
    runner=web.AppRunner(wa); await runner.setup(); await web.TCPSite(runner,'0.0.0.0',PORT).start()
    log.info('HTTP server listening on port %s',PORT)
    try: await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        if bot.updater and bot.updater.running: await bot.updater.stop()
        await bot.stop(); await bot.shutdown()
        if mtproto.is_connected(): await mtproto.disconnect()

if __name__=='__main__': asyncio.run(main())
