"""Minimal client for publishing through the Instagram Graph API.

Publishing is a two-step process:
  1. Create a media container:  POST /{ig-user-id}/media
  2. Wait until the container's status_code is FINISHED, then
     publish it:               POST /{ig-user-id}/media_publish

Only Instagram Business or Creator accounts can publish through the API, and the
media must be reachable at a public URL (Instagram downloads it from there).
"""

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional

log = logging.getLogger(__name__)

DEFAULT_API_VERSION = "v23.0"
DEFAULT_BASE_URL = "https://graph.facebook.com"


class InstagramError(Exception):
    pass


class InstagramClient:
    def __init__(
        self,
        user_id: str,
        access_token: str,
        api_version: str = DEFAULT_API_VERSION,
        base_url: str = DEFAULT_BASE_URL,
        poll_interval: float = 5.0,
        poll_timeout: float = 300.0,
        urlopen: Callable = urllib.request.urlopen,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.user_id = user_id
        self.access_token = access_token
        self.base = f"{base_url.rstrip('/')}/{api_version}"
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout
        self._urlopen = urlopen
        self._sleep = sleep

    def _request(self, method: str, path: str, params: dict) -> dict:
        params = {**params, "access_token": self.access_token}
        encoded = urllib.parse.urlencode(params)
        url = f"{self.base}/{path}"
        if method == "GET":
            req = urllib.request.Request(f"{url}?{encoded}", method="GET")
        else:
            req = urllib.request.Request(url, data=encoded.encode(), method=method)
        try:
            with self._urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            raise InstagramError(_error_message(exc)) from None
        except urllib.error.URLError as exc:
            raise InstagramError(f"Network error: {exc.reason}") from None

    def create_container(self, media_type: str, media_url: str, caption: str) -> str:
        params = {"caption": caption}
        if media_type == "IMAGE":
            params["image_url"] = media_url
        elif media_type == "REELS":
            params["media_type"] = "REELS"
            params["video_url"] = media_url
        else:
            raise InstagramError(f"Unsupported media type: {media_type}")
        data = self._request("POST", f"{self.user_id}/media", params)
        if "id" not in data:
            raise InstagramError(f"Unexpected response creating container: {data}")
        return data["id"]

    def wait_until_ready(self, container_id: str) -> None:
        deadline = time.monotonic() + self.poll_timeout
        while True:
            data = self._request("GET", container_id, {"fields": "status_code,status"})
            code = data.get("status_code")
            if code in ("FINISHED", "PUBLISHED"):
                return
            if code in ("ERROR", "EXPIRED"):
                detail = data.get("status") or code
                raise InstagramError(f"Instagram could not process the media: {detail}")
            if time.monotonic() >= deadline:
                raise InstagramError("Timed out waiting for Instagram to process the media")
            self._sleep(self.poll_interval)

    def publish_container(self, container_id: str) -> str:
        data = self._request(
            "POST", f"{self.user_id}/media_publish", {"creation_id": container_id}
        )
        if "id" not in data:
            raise InstagramError(f"Unexpected response publishing: {data}")
        return data["id"]

    def publish(self, media_type: str, media_url: str, caption: str) -> str:
        """Publish a post and return the new Instagram media id."""
        container_id = self.create_container(media_type, media_url, caption)
        self.wait_until_ready(container_id)
        return self.publish_container(container_id)


class DryRunClient:
    """Stands in for InstagramClient when no credentials are configured.
    Nothing is sent to Instagram; posts are marked published with a fake id."""

    dry_run = True

    def __init__(self):
        self._count = 0

    def publish(self, media_type: str, media_url: str, caption: str) -> str:
        self._count += 1
        log.info("[dry run] would publish %s %s", media_type, media_url)
        return f"dry-run-{int(time.time())}-{self._count}"


def _error_message(exc: urllib.error.HTTPError) -> str:
    body: Optional[str] = None
    try:
        body = exc.read().decode(errors="replace")
        err = json.loads(body).get("error", {})
        if err.get("message"):
            msg = err["message"]
            if err.get("error_user_msg"):
                msg += f" ({err['error_user_msg']})"
            return f"Instagram API error {exc.code}: {msg}"
    except (ValueError, AttributeError):
        pass
    return f"Instagram API error {exc.code}: {body or exc.reason}"
