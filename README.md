# Telegram Video Streamer — Railway starter

This is a single-process starter: one Telethon bot client receives videos and the same process serves the browser player and media ranges. It avoids running two separate polling clients with the same bot token.

## Security first
A bot token and API hash were visible in chat screenshots/messages. Treat the bot token as compromised: open @BotFather, revoke the old token, and create a new one. Do not paste secrets into source code or commit them to GitHub. Keep all secrets in Railway Variables.

## Railway setup (phone-friendly)
1. Upload these files to a private GitHub repository.
2. In Railway, create a project from that repository.
3. In the service's Variables tab, add:
   - `BOT_TOKEN` = the newly generated token from BotFather
   - `API_ID` = numeric App api_id from my.telegram.org
   - `API_HASH` = App api_hash from my.telegram.org
   - `PUBLIC_URL` = the exact HTTPS public domain Railway assigns to this service, without a trailing slash
   Railway normally provides `PORT`; the app also defaults to 8080 if not provided.
4. Ensure the Start Command is `python app.py` (or use the included Procfile).
5. Generate a public domain in Railway Networking, set it as `PUBLIC_URL`, then redeploy.
6. Open the domain. It should show “Telegram Video Streamer is running.”
7. Send a small test video directly to the bot. It should reply with a `/watch/...` link.

## Important limitations
- This is a starter, not a guarantee that every Telegram bot account can retrieve every message through MTProto. Test with a small video first.
- Generated links are stored in memory and will stop working after a service restart/redeploy; send the video again after a restart.
- The bot must receive the video in a chat it can access. Protected content, deleted messages, inaccessible channels, and some media types may not work.
- Large-video playback depends on Telegram access rules, Railway bandwidth/usage limits, and Telegram/Telethon support for the particular file. Streaming avoids saving the entire video on the Railway filesystem, but viewers still consume outbound bandwidth.
- Do not use this to distribute copyrighted videos without permission.
