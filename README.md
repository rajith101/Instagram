# Instagram Post Scheduler

A small self-hosted app for scheduling Instagram posts. You queue up posts in a
web page and a background worker publishes them at the right time through the
official **Instagram Graph API**.

- Pure Python 3.9+ standard library: nothing to `pip install`
- Image posts and Reels
- Queue view with Publish now, Retry, and Delete
- Stores posts in a local SQLite file (`posts.db`)
- **Dry-run mode** when no credentials are set, so you can try it out safely

## Quick start

```bash
python3 -m igscheduler serve
```

Open <http://127.0.0.1:8000>, then add a post with a public image URL, a caption and a
time. Without credentials the app runs in dry-run mode: posts are marked
"published" but nothing is sent to Instagram.

## Connecting your Instagram account

The Instagram API only lets you publish to **Business** or **Creator** accounts.
Personal accounts can't use it. You'll need:

1. An Instagram Business/Creator account. You can switch in the Instagram app under
   *Settings → Account type and tools*.
2. A Meta developer app (<https://developers.facebook.com/apps>) with the
   Instagram product added.
3. An access token with the `instagram_business_basic` and
   `instagram_business_content_publish` permissions (or `instagram_basic` +
   `instagram_content_publish` if you log in through a Facebook Page), plus your
   Instagram user ID. Generate both in the app dashboard, and use a
   **long-lived** token (it lasts about 60 days).

Then create a `.env` file next to this README:

```bash
cp .env.example .env
# edit .env:
IG_USER_ID=1784xxxxxxxxxxxx
IG_ACCESS_TOKEN=IGAA...
```

Restart the app. The dry-run banner goes away once real credentials are loaded.

> If you use Instagram Login (tokens starting with `IGAA`), also set
> `IG_API_BASE=https://graph.instagram.com`. Tokens issued through Facebook Login
> use the default `https://graph.facebook.com`.

## Things to know

- **Media must be at a public URL.** Instagram downloads the file itself, so a
  file on your laptop won't work. Any public host (S3, Cloudinary, your
  website, etc.) is fine. Images must be JPEG; Reels must be MP4/MOV.
- **The app must be running** at the scheduled time. Leave it running on a
  computer or small server that stays on. Alternatively, run
  `python3 -m igscheduler publish-due` from cron every few minutes.
- Times are entered in your browser's local time zone and stored in UTC.
- A post scheduled in the past is published right away.
- Instagram limits accounts to about 50 API-published posts per 24 hours.
- If the app is stopped in the middle of publishing, that post is marked
  **failed** rather than retried automatically, to avoid double-posting. Check
  Instagram, then hit *Retry now* if needed.
- The web UI has **no login**. It listens on `127.0.0.1` only by default. Don't
  expose it to the internet without putting authentication in front of it.

## Configuration

| Setting | Env var | Default |
|---|---|---|
| Instagram user ID | `IG_USER_ID` | *(dry run)* |
| Access token | `IG_ACCESS_TOKEN` | *(dry run)* |
| Graph API version | `IG_API_VERSION` | `v23.0` |
| Graph API host | `IG_API_BASE` | `https://graph.facebook.com` |
| Database file | `IGS_DB` / `--db` | `posts.db` |
| Host / port | `IGS_HOST`, `IGS_PORT` / `--host`, `--port` | `127.0.0.1`, `8000` |
| Check interval | `--interval` | 30 seconds |

## JSON API

```bash
# schedule
curl -X POST localhost:8000/api/posts -H 'Content-Type: application/json' -d '{
  "media_type": "IMAGE",
  "media_url": "https://example.com/photo.jpg",
  "caption": "Hello! #instagram",
  "scheduled_utc": "2026-10-01T09:00:00+01:00"
}'

curl localhost:8000/api/posts                        # list
curl -X POST localhost:8000/api/posts/1/publish-now  # publish / retry now
curl -X POST localhost:8000/api/posts/1/delete       # delete
```

## Development

```bash
python3 -m unittest discover -s tests -v
```

Code layout:

- `igscheduler/db.py`: SQLite storage and post status transitions
- `igscheduler/instagram.py`: Graph API client (create container → wait → publish)
- `igscheduler/worker.py`: background publisher loop
- `igscheduler/web.py`: web UI and JSON API
- `igscheduler/__main__.py`: CLI (`serve`, `publish-due`)
