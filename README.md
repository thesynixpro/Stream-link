# Telegram MEGA Video Streamer (Railway)

## Features
- /start and /help
- MEGA public file links and folder links
- Downloads videos to temporary Railway disk, then uploads via Telethon/MTProto
- Generates browser links with HTTP Range support

## Railway variables
Required: BOT_TOKEN, API_ID, API_HASH, PUBLIC_URL. Optional: MEGA_EMAIL, MEGA_PASSWORD, MAX_TRANSFER_GB, LOG_LEVEL.
Start command: `python app.py`.

## Important limitations
- This downloads to temporary disk first; it is not zero-disk streaming.
- 500 MB–2 GB transfers depend on Railway disk/network quotas, MEGA access, Telegram limits, and library compatibility. Test with a small public file first.
- Public folder download API differs between mega.py releases; check logs if a folder fails.
- Links are stored in memory and disappear after restart/redeploy.
- Never commit tokens, API hashes, MEGA passwords, or Telethon session files.
- Only transfer content you own or have permission to copy.
