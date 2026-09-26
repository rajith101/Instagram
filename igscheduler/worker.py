"""Background worker that publishes posts once they are due."""

import logging
import threading
from typing import Optional

from .db import Store
from .timeutil import now_iso

log = logging.getLogger(__name__)


def publish_post(store: Store, client, post_id: int) -> bool:
    """Claim and publish a single scheduled post. Returns True on success."""
    if not store.claim(post_id):
        return False
    post = store.get(post_id)
    try:
        media_id = client.publish(post.media_type, post.media_url, post.caption)
    except Exception as exc:  # noqa: BLE001 - any failure should be recorded on the post
        log.warning("Post %s failed: %s", post_id, exc)
        store.mark_failed(post_id, str(exc))
        return False
    store.mark_published(post_id, media_id)
    log.info("Post %s published as %s", post_id, media_id)
    return True


def publish_due(store: Store, client, now: Optional[str] = None) -> int:
    """Publish every post whose scheduled time has passed. Returns the number published."""
    published = 0
    for post in store.due(now or now_iso()):
        if publish_post(store, client, post.id):
            published += 1
    return published


class Worker(threading.Thread):
    def __init__(self, store: Store, client, interval: float = 30.0):
        super().__init__(name="publisher", daemon=True)
        self.store = store
        self.client = client
        self.interval = interval
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                publish_due(self.store, self.client)
            except Exception:  # noqa: BLE001 - keep the worker alive
                log.exception("Unexpected error in publisher loop")
            self._wake_event.wait(self.interval)
            self._wake_event.clear()

    def wake(self) -> None:
        """Check for due posts right away instead of waiting for the next tick."""
        self._wake_event.set()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
