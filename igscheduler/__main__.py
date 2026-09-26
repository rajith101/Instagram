"""Command-line entry point: python -m igscheduler [serve|publish-due]"""

import argparse
import logging
import os
import sys

from .db import Store
from .instagram import DEFAULT_API_VERSION, DEFAULT_BASE_URL, DryRunClient, InstagramClient
from .web import App, make_server
from .worker import Worker, publish_due

log = logging.getLogger("igscheduler")


def load_dotenv(path: str = ".env") -> None:
    """Load KEY=VALUE lines from a .env file without overriding the real environment."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def make_client():
    user_id = os.environ.get("IG_USER_ID", "").strip()
    token = os.environ.get("IG_ACCESS_TOKEN", "").strip()
    if not user_id or not token:
        log.warning("IG_USER_ID / IG_ACCESS_TOKEN not set - running in DRY-RUN mode")
        return DryRunClient()
    return InstagramClient(
        user_id,
        token,
        api_version=os.environ.get("IG_API_VERSION", DEFAULT_API_VERSION),
        base_url=os.environ.get("IG_API_BASE", DEFAULT_BASE_URL),
    )


def main(argv=None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="igscheduler", description="Schedule Instagram posts.")
    parser.add_argument("--db", default=os.environ.get("IGS_DB", "posts.db"),
                        help="SQLite database file (default: posts.db)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run the web UI and background publisher (default)")
    serve.add_argument("--host", default=os.environ.get("IGS_HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("IGS_PORT", "8000")))
    serve.add_argument("--interval", type=float, default=30.0,
                       help="seconds between checks for due posts (default: 30)")

    sub.add_parser("publish-due", help="publish due posts once and exit (for cron)")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    store = Store(args.db)
    client = make_client()
    dry_run = isinstance(client, DryRunClient)

    if args.command == "publish-due":
        count = publish_due(store, client)
        log.info("Published %d post(s)", count)
        return 0

    host = getattr(args, "host", "127.0.0.1")
    port = getattr(args, "port", 8000)
    interval = getattr(args, "interval", 30.0)

    recovered = store.recover_interrupted()
    if recovered:
        log.warning("%d post(s) were interrupted mid-publish and marked failed", recovered)

    worker = Worker(store, client, interval=interval)
    worker.start()
    server = make_server(App(store, worker, dry_run=dry_run), host, port)
    log.info("Open http://%s:%d in your browser", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        worker.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
