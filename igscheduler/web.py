"""Tiny web UI + JSON API built on the standard library's http.server."""

import html
import json
import logging
import re
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from .db import MEDIA_TYPES, Store
from .timeutil import now_iso, parse, to_iso

log = logging.getLogger(__name__)

MAX_CAPTION = 2200  # Instagram's caption limit
MAX_BODY = 64 * 1024


class ValidationError(ValueError):
    pass


def validate(form: dict) -> dict:
    media_type = (form.get("media_type") or "IMAGE").strip().upper()
    if media_type not in MEDIA_TYPES:
        raise ValidationError("Media type must be IMAGE or REELS.")

    media_url = (form.get("media_url") or "").strip()
    parsed = urllib.parse.urlparse(media_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValidationError("Media URL must be a public http(s) link to the image or video.")

    caption = (form.get("caption") or "").replace("\r\n", "\n")
    if len(caption) > MAX_CAPTION:
        raise ValidationError(f"Caption is {len(caption)} characters; the limit is {MAX_CAPTION}.")

    # The browser sends scheduled_utc (converted from the user's local time by JS).
    # Without JS we fall back to scheduled_local, read as the server's local time.
    raw_time = (form.get("scheduled_utc") or form.get("scheduled_local") or "").strip()
    if not raw_time:
        raise ValidationError("Pick a date and time to publish.")
    try:
        scheduled_at = to_iso(parse(raw_time))
    except ValueError:
        raise ValidationError(f"Could not understand the time {raw_time!r}.") from None

    return {
        "media_type": media_type,
        "media_url": media_url,
        "caption": caption,
        "scheduled_at": scheduled_at,
    }


class App:
    def __init__(self, store: Store, worker=None, dry_run: bool = False):
        self.store = store
        self.worker = worker
        self.dry_run = dry_run

    def wake_worker(self) -> None:
        if self.worker is not None:
            self.worker.wake()


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "igscheduler"

        def log_message(self, fmt, *args):
            log.debug("%s - " + fmt, self.address_string(), *args)

        # -- routing -------------------------------------------------------
        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            if path == "/":
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                msg = query.get("msg", [None])[0]
                self._send_html(render_page(app, notice=msg))
            elif path == "/api/posts":
                self._send_json([p.to_dict() for p in app.store.list()])
            elif path == "/healthz":
                self._send_json({"ok": True})
            else:
                self._send_error(HTTPStatus.NOT_FOUND, "Not found")

        def do_POST(self):
            if not self._same_origin():
                return self._send_error(HTTPStatus.FORBIDDEN, "Cross-origin request refused")
            path = urllib.parse.urlparse(self.path).path
            is_api = path.startswith("/api/")
            if is_api:
                path = path[len("/api"):]
            try:
                form = self._read_body(is_api)
            except ValueError as exc:
                return self._send_error(HTTPStatus.BAD_REQUEST, str(exc))

            if path == "/posts":
                return self._create(form, is_api)
            m = re.fullmatch(r"/posts/(\d+)/(delete|publish-now)", path)
            if not m:
                return self._send_error(HTTPStatus.NOT_FOUND, "Not found")
            post_id, action = int(m.group(1)), m.group(2)
            if action == "delete":
                ok = app.store.delete(post_id)
                msg = "Post deleted." if ok else "That post can't be deleted right now."
            else:
                ok = app.store.reschedule(post_id, now_iso())
                if ok:
                    app.wake_worker()
                msg = "Publishing now…" if ok else "That post can't be published right now."
            if is_api:
                status = HTTPStatus.OK if ok else HTTPStatus.CONFLICT
                return self._send_json({"ok": ok, "message": msg}, status)
            self._redirect(msg)

        def _create(self, form: dict, is_api: bool):
            try:
                data = validate(form)
            except ValidationError as exc:
                if is_api:
                    return self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return self._send_html(
                    render_page(app, error=str(exc), form=form), HTTPStatus.BAD_REQUEST
                )
            post = app.store.add(**data)
            app.wake_worker()
            if is_api:
                return self._send_json(post.to_dict(), HTTPStatus.CREATED)
            self._redirect("Post scheduled.")

        # -- helpers -------------------------------------------------------
        def _same_origin(self) -> bool:
            origin = self.headers.get("Origin")
            if not origin or origin == "null":
                return origin is None
            return urllib.parse.urlparse(origin).netloc == self.headers.get("Host")

        def _read_body(self, is_json: bool) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ValueError("Request too large")
            raw = self.rfile.read(length).decode("utf-8") if length else ""
            if is_json:
                if not raw:
                    return {}
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError("Expected a JSON object")
                return {k: str(v) if v is not None else "" for k, v in data.items()}
            return {k: v[0] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}

        def _send(self, body: bytes, content_type: str, status=HTTPStatus.OK):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, text: str, status=HTTPStatus.OK):
            self._send(text.encode(), "text/html; charset=utf-8", status)

        def _send_json(self, data, status=HTTPStatus.OK):
            self._send(json.dumps(data).encode(), "application/json", status)

        def _send_error(self, status, message: str):
            self._send_json({"error": message}, status)

        def _redirect(self, msg: str):
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/?" + urllib.parse.urlencode({"msg": msg}))
            self.send_header("Content-Length", "0")
            self.end_headers()

    return Handler


def make_server(app: App, host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(app))


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

def _e(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _time(iso: Optional[str]) -> str:
    if not iso:
        return ""
    return f'<time datetime="{_e(iso)}">{_e(iso.replace("T", " ").replace("Z", " UTC"))}</time>'


def _post_card(post) -> str:
    if post.media_type == "IMAGE":
        thumb = f'<img src="{_e(post.media_url)}" alt="" loading="lazy" referrerpolicy="no-referrer">'
    else:
        thumb = '<div class="reel">▶ Reel</div>'

    when = {
        "scheduled": f"Scheduled for {_time(post.scheduled_at)}",
        "publishing": "Publishing…",
        "published": f"Published {_time(post.published_at)}",
        "failed": f"Failed (was due {_time(post.scheduled_at)})",
    }[post.status]

    actions = []
    if post.status in ("scheduled", "failed"):
        label = "Retry now" if post.status == "failed" else "Publish now"
        actions.append(
            f'<form method="post" action="/posts/{post.id}/publish-now">'
            f'<button type="submit">{label}</button></form>'
        )
    if post.status != "publishing":
        actions.append(
            f'<form method="post" action="/posts/{post.id}/delete" '
            f'onsubmit="return confirm(\'Delete this post from the schedule?\')">'
            f'<button type="submit" class="secondary">Delete</button></form>'
        )

    error = f'<p class="error-text">{_e(post.error)}</p>' if post.error else ""
    media_id = (
        f'<p class="meta">Instagram media id: <code>{_e(post.ig_media_id)}</code></p>'
        if post.ig_media_id else ""
    )
    caption = _e(post.caption) or '<span class="meta">(no caption)</span>'
    return f"""
    <li class="post">
      <a class="thumb" href="{_e(post.media_url)}" target="_blank" rel="noopener noreferrer">{thumb}</a>
      <div class="body">
        <div class="row"><span class="badge {post.status}">{post.status}</span>
          <span class="meta">{post.media_type.title()} · {when}</span></div>
        <p class="caption">{caption}</p>
        {error}{media_id}
        <div class="actions">{''.join(actions)}</div>
      </div>
    </li>"""


def render_page(app: App, notice: Optional[str] = None, error: Optional[str] = None,
                form: Optional[dict] = None) -> str:
    form = form or {}
    posts = app.store.list()
    upcoming = [p for p in posts if p.status != "published"]
    done = [p for p in posts if p.status == "published"]

    def section(title, items, empty):
        body = "".join(_post_card(p) for p in items) or f'<li class="empty">{empty}</li>'
        return f'<section><h2>{title} <span class="count">{len(items)}</span></h2><ul class="posts">{body}</ul></section>'

    banner = ""
    if app.dry_run:
        banner = ('<div class="banner">Dry-run mode: no Instagram credentials are set, so '
                  'posts are only simulated. See the README to connect your account.</div>')
    flash = ""
    if error:
        flash = f'<div class="flash error">{_e(error)}</div>'
    elif notice:
        flash = f'<div class="flash">{_e(notice)}</div>'

    media_type = (form.get("media_type") or "IMAGE").upper()
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Post Scheduler</title>
<style>{CSS}</style>
</head>
<body>
<main>
  <header><h1>Instagram Post Scheduler</h1></header>
  {banner}{flash}
  <section class="card">
    <h2>Schedule a post</h2>
    <form method="post" action="/posts" id="new-post">
      <label>Type
        <select name="media_type">
          <option value="IMAGE"{' selected' if media_type == 'IMAGE' else ''}>Image</option>
          <option value="REELS"{' selected' if media_type == 'REELS' else ''}>Reel (video)</option>
        </select>
      </label>
      <label>Media URL
        <input type="url" name="media_url" required placeholder="https://example.com/photo.jpg"
               value="{_e(form.get('media_url'))}">
        <small>Must be publicly reachable. Instagram fetches the file from this link (JPEG for images, MP4 for reels).</small>
      </label>
      <label>Caption
        <textarea name="caption" rows="5" maxlength="{MAX_CAPTION}" id="caption">{_e(form.get('caption'))}</textarea>
        <small><span id="caption-count">0</span> / {MAX_CAPTION}</small>
      </label>
      <label>Publish at
        <input type="datetime-local" name="scheduled_local" id="scheduled_local" required
               value="{_e(form.get('scheduled_local'))}">
        <small id="tz-hint">Your local time.</small>
      </label>
      <input type="hidden" name="scheduled_utc" id="scheduled_utc">
      <button type="submit">Schedule post</button>
    </form>
  </section>
  {section("Queue", upcoming, "Nothing scheduled yet.")}
  {section("Published", done, "Nothing published yet.")}
</main>
<script>{JS}</script>
</body>
</html>"""


CSS = """
:root { --bg:#f6f6f8; --card:#fff; --text:#1c1c21; --muted:#6b6b76; --border:#e2e2e8;
  --accent:#d6246e; --accent-text:#fff; --ok:#1f8a4c; --warn:#b26a00; --bad:#c62828; --info:#3056d3; }
@media (prefers-color-scheme: dark) { :root { --bg:#121216; --card:#1c1c22; --text:#ececf1;
  --muted:#9a9aa6; --border:#2e2e37; --accent:#ff4f93; --ok:#4cc27f; --warn:#e0a13a; --bad:#ff6b6b; --info:#7c9bff; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text);
  font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }
main { max-width:760px; margin:0 auto; padding:24px 16px 64px; }
h1 { font-size:1.5rem; margin:0 0 16px; }
h2 { font-size:1.1rem; margin:24px 0 12px; }
.card h2 { margin-top:0; }
.card, .post { background:var(--card); border:1px solid var(--border); border-radius:12px; }
.card { padding:20px; }
label { display:block; font-weight:600; margin-bottom:14px; }
input, select, textarea { display:block; width:100%; margin-top:6px; padding:9px 10px; font:inherit;
  color:inherit; background:var(--bg); border:1px solid var(--border); border-radius:8px; }
small { display:block; font-weight:400; color:var(--muted); margin-top:4px; }
button { font:inherit; font-weight:600; border:0; border-radius:8px; padding:9px 16px; cursor:pointer;
  background:var(--accent); color:var(--accent-text); }
button.secondary { background:transparent; color:var(--muted); border:1px solid var(--border); }
.posts { list-style:none; padding:0; margin:0; display:grid; gap:12px; }
.post { display:flex; gap:14px; padding:12px; }
.thumb { flex:0 0 96px; height:96px; border-radius:8px; overflow:hidden; background:var(--bg);
  display:flex; align-items:center; justify-content:center; color:var(--muted); text-decoration:none; }
.thumb img { width:100%; height:100%; object-fit:cover; }
.body { flex:1; min-width:0; }
.row { display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
.caption { margin:6px 0; white-space:pre-wrap; overflow-wrap:anywhere; max-height:7.5em; overflow:hidden; }
.meta { color:var(--muted); font-size:.9em; }
.badge { font-size:.75em; font-weight:700; text-transform:uppercase; letter-spacing:.04em;
  padding:2px 8px; border-radius:999px; border:1px solid currentColor; }
.badge.scheduled { color:var(--info); } .badge.publishing { color:var(--warn); }
.badge.published { color:var(--ok); } .badge.failed { color:var(--bad); }
.error-text { color:var(--bad); margin:6px 0; overflow-wrap:anywhere; }
.actions { display:flex; gap:8px; margin-top:8px; }
.actions button { padding:6px 12px; font-size:.9em; }
.empty { color:var(--muted); padding:12px 0; }
.count { color:var(--muted); font-weight:400; }
.flash, .banner { padding:10px 14px; border-radius:8px; margin-bottom:16px; border:1px solid var(--border); background:var(--card); }
.flash.error { border-color:var(--bad); color:var(--bad); }
.banner { border-color:var(--warn); }
code { overflow-wrap:anywhere; }
@media (max-width:480px) { .thumb { flex-basis:64px; height:64px; } }
"""

JS = """
(function () {
  // Show stored UTC times in the viewer's local time zone.
  document.querySelectorAll('time[datetime]').forEach(function (el) {
    var d = new Date(el.getAttribute('datetime'));
    if (!isNaN(d)) el.textContent = d.toLocaleString(undefined, {dateStyle: 'medium', timeStyle: 'short'});
  });

  var local = document.getElementById('scheduled_local');
  var utc = document.getElementById('scheduled_utc');
  var pad = function (n) { return String(n).padStart(2, '0'); };
  if (!local.value) {
    var d = new Date(Date.now() + 60 * 60 * 1000);
    local.value = d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) +
      'T' + pad(d.getHours()) + ':' + pad(d.getMinutes());
  }
  try {
    document.getElementById('tz-hint').textContent =
      'Your local time (' + Intl.DateTimeFormat().resolvedOptions().timeZone + ').';
  } catch (e) {}
  document.getElementById('new-post').addEventListener('submit', function () {
    var d = new Date(local.value);
    utc.value = isNaN(d) ? '' : d.toISOString();
  });

  var caption = document.getElementById('caption');
  var count = document.getElementById('caption-count');
  var update = function () { count.textContent = caption.value.length; };
  caption.addEventListener('input', update);
  update();

  // Refresh while something is in flight so the status updates on its own.
  if (document.querySelector('.badge.publishing')) setTimeout(function () { location.reload(); }, 5000);
})();
"""
