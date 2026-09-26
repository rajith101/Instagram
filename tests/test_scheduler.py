import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request

from igscheduler.db import Store
from igscheduler.instagram import InstagramClient, InstagramError
from igscheduler.timeutil import parse, to_iso
from igscheduler.web import App, ValidationError, make_server, validate
from igscheduler.worker import publish_due


class FakeClient:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def publish(self, media_type, media_url, caption):
        self.calls.append((media_type, media_url, caption))
        if self.fail:
            raise InstagramError("boom")
        return f"ig-{len(self.calls)}"


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "test.db"))

    def tearDown(self):
        self.tmp.cleanup()


class TimeTests(unittest.TestCase):
    def test_parse_normalises_offsets_to_utc(self):
        self.assertEqual(to_iso(parse("2026-10-01T12:00:00+02:00")), "2026-10-01T10:00:00Z")
        self.assertEqual(to_iso(parse("2026-10-01T10:00:00.000Z")), "2026-10-01T10:00:00Z")


class ValidateTests(unittest.TestCase):
    def good(self, **kw):
        form = {"media_url": "https://example.com/a.jpg", "caption": "hi",
                "scheduled_utc": "2026-10-01T10:00:00.000Z"}
        form.update(kw)
        return form

    def test_valid(self):
        data = validate(self.good())
        self.assertEqual(data["media_type"], "IMAGE")
        self.assertEqual(data["scheduled_at"], "2026-10-01T10:00:00Z")

    def test_rejects_bad_input(self):
        for bad in (
            {"media_url": "ftp://x/a.jpg"},
            {"media_url": ""},
            {"media_type": "STORY"},
            {"caption": "x" * 2201},
            {"scheduled_utc": "", "scheduled_local": ""},
            {"scheduled_utc": "tomorrow"},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                validate(self.good(**bad))


class WorkerTests(StoreTestCase):
    def test_publishes_only_due_posts(self):
        due = self.store.add("IMAGE", "https://x/1.jpg", "one", "2026-01-01T00:00:00Z")
        later = self.store.add("IMAGE", "https://x/2.jpg", "two", "2030-01-01T00:00:00Z")
        client = FakeClient()

        self.assertEqual(publish_due(self.store, client, now="2026-06-01T00:00:00Z"), 1)
        self.assertEqual(client.calls, [("IMAGE", "https://x/1.jpg", "one")])
        self.assertEqual(self.store.get(due.id).status, "published")
        self.assertEqual(self.store.get(due.id).ig_media_id, "ig-1")
        self.assertEqual(self.store.get(later.id).status, "scheduled")

        # Running again does not re-publish.
        self.assertEqual(publish_due(self.store, client, now="2026-06-01T00:00:00Z"), 0)
        self.assertEqual(len(client.calls), 1)

    def test_failure_is_recorded_and_retryable(self):
        post = self.store.add("REELS", "https://x/v.mp4", "", "2026-01-01T00:00:00Z")
        publish_due(self.store, FakeClient(fail=True), now="2026-06-01T00:00:00Z")
        failed = self.store.get(post.id)
        self.assertEqual((failed.status, failed.error, failed.attempts), ("failed", "boom", 1))

        self.assertTrue(self.store.reschedule(post.id, "2026-01-01T00:00:00Z"))
        publish_due(self.store, FakeClient(), now="2026-06-01T00:00:00Z")
        done = self.store.get(post.id)
        self.assertEqual((done.status, done.error, done.attempts), ("published", None, 2))

    def test_claim_is_exclusive(self):
        post = self.store.add("IMAGE", "https://x/1.jpg", "", "2026-01-01T00:00:00Z")
        self.assertTrue(self.store.claim(post.id))
        self.assertFalse(self.store.claim(post.id))
        self.assertFalse(self.store.delete(post.id))  # can't delete while publishing
        self.assertEqual(self.store.recover_interrupted(), 1)
        self.assertEqual(self.store.get(post.id).status, "failed")


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class InstagramClientTests(unittest.TestCase):
    def make(self, responses):
        self.requests = []

        def urlopen(req, timeout=None):
            self.requests.append(req)
            resp = responses.pop(0)
            if isinstance(resp, Exception):
                raise resp
            return FakeResponse(json.dumps(resp).encode())

        return InstagramClient("123", "TOKEN", api_version="v23.0", urlopen=urlopen,
                               sleep=lambda s: None)

    def test_publish_image_flow(self):
        client = self.make([{"id": "c1"}, {"status_code": "IN_PROGRESS"},
                            {"status_code": "FINISHED"}, {"id": "m1"}])
        self.assertEqual(client.publish("IMAGE", "https://x/a.jpg", "hello #tag"), "m1")

        create, poll1, _, publish = self.requests
        self.assertEqual(create.full_url, "https://graph.facebook.com/v23.0/123/media")
        body = urllib.parse.parse_qs(create.data.decode())
        self.assertEqual(body["image_url"], ["https://x/a.jpg"])
        self.assertEqual(body["caption"], ["hello #tag"])
        self.assertEqual(body["access_token"], ["TOKEN"])
        self.assertEqual(poll1.get_method(), "GET")
        self.assertTrue(poll1.full_url.startswith("https://graph.facebook.com/v23.0/c1?"))
        self.assertEqual(publish.full_url, "https://graph.facebook.com/v23.0/123/media_publish")
        self.assertEqual(urllib.parse.parse_qs(publish.data.decode())["creation_id"], ["c1"])

    def test_reels_use_video_url(self):
        client = self.make([{"id": "c1"}, {"status_code": "FINISHED"}, {"id": "m1"}])
        client.publish("REELS", "https://x/v.mp4", "")
        body = urllib.parse.parse_qs(self.requests[0].data.decode())
        self.assertEqual(body["media_type"], ["REELS"])
        self.assertEqual(body["video_url"], ["https://x/v.mp4"])

    def test_api_error_message_is_surfaced(self):
        err = urllib.error.HTTPError(
            "u", 400, "Bad Request", {},
            io.BytesIO(json.dumps({"error": {"message": "Invalid token"}}).encode()),
        )
        client = self.make([err])
        with self.assertRaisesRegex(InstagramError, "Invalid token"):
            client.publish("IMAGE", "https://x/a.jpg", "")

    def test_container_error_status(self):
        client = self.make([{"id": "c1"}, {"status_code": "ERROR", "status": "bad aspect ratio"}])
        with self.assertRaisesRegex(InstagramError, "bad aspect ratio"):
            client.publish("IMAGE", "https://x/a.jpg", "")


class WebTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.server = make_server(App(self.store, dry_run=True), "127.0.0.1", 0)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def request(self, path, data=None, headers=None, json_body=None):
        headers = dict(headers or {})
        body = None
        if json_body is not None:
            body = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        elif data is not None:
            body = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(self.base + path, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def test_form_flow(self):
        # Form posts redirect back to the page (urllib follows the 303).
        status, _ = self.request("/posts", data={
            "media_type": "IMAGE", "media_url": "https://x/a.jpg",
            "caption": "<script>alert(1)</script>", "scheduled_utc": "2030-01-01T00:00:00Z",
        })
        self.assertEqual(status, 200)
        status, page = self.request("/")
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertNotIn("<script>alert(1)", page)
        self.assertIn("Dry-run mode", page)

    def test_invalid_form_shows_error(self):
        status, page = self.request("/posts", data={"media_url": "nope", "scheduled_utc": ""})
        self.assertEqual(status, 400)
        self.assertIn("Media URL must be", page)

    def test_json_api(self):
        status, body = self.request("/api/posts", json_body={
            "media_url": "https://x/a.jpg", "caption": "hi", "scheduled_utc": "2030-01-01T09:00:00+01:00",
        })
        self.assertEqual(status, 201)
        post = json.loads(body)
        self.assertEqual(post["scheduled_at"], "2030-01-01T08:00:00Z")

        status, body = self.request(f"/api/posts/{post['id']}/publish-now", json_body={})
        self.assertEqual(status, 200)
        self.assertEqual(self.store.get(post["id"]).status, "scheduled")

        status, body = self.request(f"/api/posts/{post['id']}/delete", json_body={})
        self.assertEqual(status, 200)
        status, body = self.request("/api/posts")
        self.assertEqual(json.loads(body), [])

    def test_cross_origin_post_refused(self):
        status, _ = self.request("/api/posts", json_body={}, headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
