# Stream-link + AniAprax automatic channel workflow

This version adds an optional Supabase sync for **new video posts in a Telegram channel**.

## What it does
- The bot's Telegram polling handler can receive channel video posts.
- For a channel video post, it registers an HTTP Range streaming URL at `PUBLIC_URL/watch/<token>`.
- It upserts the matching `public.anime` row by `telegram_chat_id` + `telegram_message_id`; if not found, it inserts one.
- Supabase fields written: `title`, `caption`, `media_type`, `telegram_file_id`, `telegram_chat_id`, `telegram_message_id`, `stream_url`.

## Important prerequisites
1. Add the Telegram bot to the source channel as an **administrator** with permission to read posts. Confirm your bot can receive channel-post updates.
2. Add these Railway variables:
   - `SUPABASE_URL=https://YOUR_PROJECT.supabase.co`
   - `SUPABASE_SERVICE_ROLE_KEY=<new server-side service role key>`
   - `SUPABASE_TABLE=anime`
3. Never put `SUPABASE_SERVICE_ROLE_KEY`, `BOT_TOKEN`, `API_HASH`, or Telethon session files in the Android app, GitHub, or chat. Store secrets only in Railway variables.
4. The Supabase table must have the fields used by this app: `title`, `caption`, `media_type`, `telegram_file_id`, `telegram_chat_id`, `telegram_message_id`, `stream_url`. Existing columns must have compatible types. The code stores `telegram_chat_id` as text; if your column is `bigint`, change `chat_id = str(msg.chat_id)` to `chat_id = int(msg.chat_id)`.
5. If `telegram_chat_id` and `telegram_message_id` are not a unique pair, this code still searches before insert, but concurrent duplicate updates are possible. A unique constraint is recommended only after checking and cleaning existing duplicates.
6. Links are held in process memory and disappear after a Railway restart/redeploy. New posts get new links when processed; old links will not automatically be restored by this change.
7. The current streamer uses a bot-token Telethon client to read and range-stream Telegram media. Test with a small video first; check Railway logs and `/health` if access fails.

## Existing deployment
Deploy this updated source to the same Railway service using your normal GitHub/CLI workflow. Keep your existing required variables (`BOT_TOKEN`, `API_ID`, `API_HASH`, `PUBLIC_URL`) and add the Supabase variables above. Start command remains `python app.py`.

## Supabase SQL checklist
Run this only if the columns are missing. Adjust `telegram_chat_id` type to match the existing schema:
```sql
alter table public.anime add column if not exists stream_url text;
alter table public.anime add column if not exists telegram_file_id text;
alter table public.anime add column if not exists telegram_chat_id text;
alter table public.anime add column if not exists telegram_message_id bigint;
alter table public.anime add column if not exists caption text;
alter table public.anime add column if not exists media_type text;
```
Do not blindly run type-changing SQL against an existing table. Inspect current column types first.
