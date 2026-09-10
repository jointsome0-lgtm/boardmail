"""ClawdChat public confirmation, bounded retry and durable arrival contracts."""
from email.message import Message
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.request import BaseHandler, ProxyHandler, build_opener
from urllib.response import addinfourl

from boardmail import adapter_clawdchat as adapter
from boardmail.adapters import Batch, validate
from boardmail.config import MailError
from boardmail.store import Store


def uid(number):
    return f"00000000-0000-0000-0000-{number:012x}"


def event(number, kind="comment"):
    return {"id": uid(number + 1000), "type": kind, "post_id": uid(100),
            "comment_id": uid(number), "content": "PRIVATE NOTIFICATION PREVIEW"}


def original(number, **changes):
    return {"id": uid(number), "post_id": uid(100), "content": "Public original " + str(number),
            "author": {"id": uid(2), "name": "other-agent"}, "created_at": "2026-09-07T10:00:00Z",
            "post": {"id": uid(100), "title": "Public thread"}, "parent_id": None,
            "web_url": "https://clawdchat.cn/post/" + uid(100), **changes}


class FixtureClient:
    def __init__(self):
        self.owner = uid(1)
        self.events = []
        self.originals = {}
        self.calls = []
        self.profile = {"id": self.owner}
        self.page_error = {}

    def phase(self, seconds):
        pass

    def get(self, path, params=None, *, authenticated=False):
        self.calls.append((path, params, authenticated))
        if path == "/agents/me":
            assert authenticated
            return self.profile
        if path == "/notifications":
            assert authenticated
            offset = params["offset"]
            if offset in self.page_error:
                raise MailError(self.page_error[offset])
            return {"success": True, "items": self.events[offset:offset + params["limit"]], "total": len(self.events)}
        assert not authenticated, "Public originals must never use account credentials"
        value = self.originals[path.rsplit("/", 1)[-1]]
        if isinstance(value, Exception):
            raise value
        return value


class ClawdChatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "inbox.sqlite3")
        self.store.initialize()
        self.client = FixtureClient()

    def collect(self):
        self.store.prepare_collection()
        known, state, revision = self.store.collection_state("clawd", uid(1), "clawdchat")
        before = json.dumps(state)
        with patch.object(adapter, "Client", return_value=self.client):
            batch = adapter.collect({"account_id": uid(1)}, state, frozenset(known))
        self.assertEqual(json.dumps(state), before, "Input state is a snapshot")
        validate(batch)
        added, stale = self.store.save_collection("clawd", uid(1), "clawdchat", revision, batch)
        self.assertFalse(stale)
        self.assertNotIn("PRIVATE NOTIFICATION", json.dumps(batch.state))
        self.assertNotIn("PRIVATE NOTIFICATION", json.dumps(batch.messages))
        return batch, added

    def test_supported_kinds_use_public_originals_and_preserve_arrivals(self):
        self.client.events = [event(10), event(11, "reply"), event(12, "mention_comment"),
                              event(100, "mention_post"), event(13, "follow"), event(14)]
        self.client.originals = {uid(n): original(n) for n in (10, 11, 12, 100, 14)}
        self.client.originals[uid(11)]["parent_id"] = uid(9)
        self.client.originals[uid(100)].update(title="Mention in title", content=None)
        self.client.originals[uid(14)]["author"]["id"] = uid(1)
        batch, added = self.collect()
        self.assertEqual(added, 4)
        self.assertTrue(batch.complete)
        self.assertEqual([m["kind"] for m in batch.messages], ["reply_to_post", "reply_to_comment", "mention", "mention"])
        self.assertEqual(batch.messages[1]["parent_id"], uid(9))
        self.assertEqual(batch.messages[-1]["body"], "")
        self.store.mark("clawd", uid(10), "needs_reply")
        before = self.store.show("clawd", uid(10))
        self.assertEqual(self.collect()[1], 0)
        self.assertEqual(self.store.show("clawd", uid(10)), before)
        self.assertEqual(self.store.wait(0, 0)["next_after"], 4)

    def test_unavailable_and_bad_originals_do_not_block_siblings_or_expired_notifications(self):
        self.client.events = [event(n) for n in range(10, 17)]
        self.client.originals = {uid(n): original(n) for n in range(10, 17)}
        self.client.originals[uid(10)] = MailError("http_403")
        self.client.originals[uid(11)] = MailError("http_404")
        self.client.originals[uid(12)]["visibility"] = "private"
        self.client.originals[uid(13)]["post_id"] = uid(999)
        self.client.originals[uid(14)]["created_at"] = "bad timestamp"
        self.client.originals[uid(15)]["is_deleted"] = True
        batch, added = self.collect()
        self.assertEqual(added, 1)
        self.assertEqual(batch.unavailable, 4)
        self.assertEqual(batch.error, "invalid_response")
        self.assertEqual(batch.messages[0]["id"], uid(16))
        # Retried references survive a process/database reopen and upstream expiry.
        self.store = Store(self.store.path)
        self.client.events = []
        self.client.originals = {uid(n): original(n) for n in range(10, 17)}
        batch, added = self.collect()
        self.assertEqual(added, 6)
        self.assertTrue(batch.complete)

    def test_fresh_head_and_cyclic_backfill_progress_past_persistent_failure(self):
        self.client.events = [event(n) for n in range(10, 20)]
        self.client.originals = {uid(n): original(n) for n in range(10, 21)}
        self.client.originals[uid(10)] = MailError("network_error")
        with patch.object(adapter, "PAGE_SIZE", 2):
            self.collect()
            self.client.events.insert(0, event(20))
            batch, _ = self.collect()
            self.assertIn(uid(20), {m["id"] for m in batch.messages})
            for _ in range(8):
                self.collect()
        known = self.store.known("clawd", uid(1))
        self.assertEqual(known, {uid(n) for n in range(11, 21)})
        self.assertNotIn(uid(10), known)
        self.assertTrue(any((params or {}).get("offset", 0) >= 8 for _, params, _ in self.client.calls))

    def test_full_queue_reports_overflow_and_revisits_affected_discovery_page(self):
        self.client.events = [event(n) for n in range(10, 14)]
        self.client.originals = {uid(n): original(n) for n in range(10, 14)}
        with patch.object(adapter, "MAX_PENDING", 2):
            batch, added = self.collect()
            self.assertEqual(batch.error, "pending_overflow")
            self.assertEqual(batch.state["offset"], 0)
            self.assertLessEqual(len(batch.state["pending"]), 2)
            self.assertEqual(added, 2)
            self.assertEqual(self.collect()[1], 2)
        self.assertEqual(self.store.known("clawd", uid(1)), {uid(n) for n in range(10, 14)})

    def test_slow_retry_backlog_cannot_consume_fresh_original_budget(self):
        pending = [{"id": uid(n), "post": uid(100), "kind": "reply_to_post", "is_post": False} for n in range(10, 20)]
        self.store.prepare_collection()
        self.store.save_collection("clawd", uid(1), "clawdchat", 0, Batch(state={"pending": pending, "offset": 0}))
        self.client.events = [event(20)]
        self.client.originals = {uid(20): original(20)}
        clock, deadline = [0], [0]
        get = self.client.get
        def phase(seconds):
            deadline[0] = clock[0] + seconds
        def slow(path, params=None, **kwargs):
            if clock[0] >= deadline[0]:
                raise MailError("budget_exhausted")
            if path.startswith("/comments/") and not path.endswith(uid(20)):
                clock[0] += 5
                raise MailError("network_error")
            return get(path, params, **kwargs)
        self.client.phase = phase
        self.client.get = slow
        batch, added = self.collect()
        self.assertEqual(added, 1)
        self.assertEqual(batch.messages[0]["id"], uid(20))
        self.assertEqual(len(batch.state["pending"]), 10)

    def test_rate_limit_stops_source_and_rejected_backfill_position_resets(self):
        self.client.events = [event(n) for n in range(10, 16)]
        self.client.originals = {uid(n): original(n) for n in range(10, 16)}
        with patch.object(adapter, "PAGE_SIZE", 2):
            self.collect()
            self.client.page_error[2] = "http_429"
            self.client.calls.clear()
            batch, _ = self.collect()
            self.assertEqual(batch.error, "http_429")
            self.assertEqual(batch.state["offset"], 2)
            self.assertEqual(self.client.calls[-1][0], "/notifications")
            self.client.page_error[2] = "http_422"
            self.assertEqual(self.collect()[0].state["offset"], 0)
            self.client.page_error.clear()
            for _ in range(4):
                self.collect()
        self.assertEqual(len(self.store.known("clawd", uid(1))), 6)

    def test_wrong_token_owner_stops_before_notifications(self):
        state = {"offset": 8, "pending": [{"id": uid(10), "post": uid(100),
                                          "kind": "reply_to_post", "is_post": False}]}
        self.store.prepare_collection()
        self.store.save_collection("clawd", uid(1), "clawdchat", 0, Batch(state=state))
        self.client.profile = {"id": uid(999)}
        batch, added = self.collect()
        self.assertEqual(batch.error, "account_mismatch")
        self.assertEqual(added, 0)
        self.assertEqual([p for p, _, _ in self.client.calls], ["/agents/me"])
        self.assertEqual(batch.state, state)
        self.assertEqual(self.store.collection_state("clawd", uid(1), "clawdchat")[2], 1)
        self.assertEqual(self.store.save_collection("clawd", uid(1), "clawdchat", 1,
            Batch(state={"offset": 16, "pending": []})), (0, False))

    def test_local_setup_errors_keep_pending_state_and_planned_budget_is_partial(self):
        state = {"offset": 8, "pending": [{"id": uid(10), "post": uid(100),
                                          "kind": "reply_to_post", "is_post": False}]}
        for settings, code in [({"account_id": uid(1)}, "credentials_unavailable"),
                               ({"account_id": "not-a-uuid"}, "invalid_config")]:
            with self.subTest(code=code):
                batch = adapter.collect(settings, state, frozenset())
                self.assertEqual(batch.error, code)
                self.assertEqual(batch.state, state)
                self.assertFalse(batch.complete)
        # Exhausting a planned discovery phase is partial progress, not an outage.
        self.client.page_error[0] = "budget_exhausted"
        batch, added = self.collect()
        self.assertEqual(added, 0)
        self.assertIsNone(batch.error)
        self.assertFalse(batch.complete)

    def test_bad_reference_isolated_and_unsafe_canonical_links_use_public_api(self):
        self.client.events = [event(9), event(10)]
        del self.client.events[0]["comment_id"]
        for url in ["https://evil.invalid/post/10", "https://clawdchat.cn@evil.invalid/10",
                    "https://clawdchat.cn:444/10", "https://user@clawdchat.cn/10", "http://clawdchat.cn/10",
                    "https://clawdchat.cn/\nsecret", "https://clawdchat.cn\\@evil.invalid/10"]:
            with self.subTest(url=url):
                self.client.originals[uid(10)] = original(10, web_url=url)
                with patch.object(adapter, "Client", return_value=self.client):
                    batch = adapter.collect({}, {}, frozenset())
                validate(batch)
                self.assertEqual(batch.error, "invalid_response")
                self.assertEqual(batch.messages[0]["url"], adapter.ORIGIN + "/api/v1/comments/" + uid(10))


class ScriptedHTTPS(BaseHandler):
    """Exercise urllib's real redirect processing without sockets or secrets."""
    handler_order = 100
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def http_open(self, request):
        raise AssertionError("An HTTPS retry became an unencrypted HTTP request")

    def https_open(self, request):
        self.requests.append(request)
        status, content, location = next(self.responses)
        headers = Message()
        if location:
            headers["Location"] = location
        response = addinfourl(io.BytesIO(content), headers, request.full_url, status)
        response.msg = "Synthetic response"
        return response


class TransportTests(unittest.TestCase):
    def client(self, replies, proxy=None):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        key = Path(temp.name) / "key"
        key.write_text("synthetic-key-only\n")
        client = adapter.Client({"account_id": uid(1), "api_key_file": key})
        handler = ScriptedHTTPS(replies)
        client.opener = build_opener(ProxyHandler(proxy or {}), handler, adapter.NoRedirect())
        return client, handler

    def test_authentication_only_on_explicit_calls_and_redirect_not_followed(self):
        client, handler = self.client([(200, b'{"id":"example"}', None)] * 2 + [(302, b"", "https://evil.invalid/stolen")])
        client.get("/agents/me", authenticated=True)
        client.get("/comments/" + uid(10))
        with self.assertRaisesRegex(MailError, "^redirect_refused$"):
            client.get("/notifications", authenticated=True)
        self.assertEqual(len(handler.requests), 3)
        self.assertEqual(handler.requests[0].get_header("Authorization"), "Bearer synthetic-key-only")
        self.assertIsNone(handler.requests[1].get_header("Authorization"))
        self.assertTrue(all(r.full_url.startswith("https://clawdchat.cn/api/v1/") for r in handler.requests))

    def test_https_proxy_retry_keeps_the_original_transport(self):
        client, handler = self.client([(503, b"retry", None)] * 2 + [(200, b'{"id":"example"}', None)],
                                      proxy={"https": "http://proxy.invalid:8080"})
        self.assertEqual(client.get("/notifications", authenticated=True), {"id": "example"})
        self.assertEqual(len(handler.requests), 3)

    def test_public_original_needs_no_readable_key_file(self):
        client, handler = self.client([(200, b'{"id":"public-original"}', None)])
        client.settings['api_key_file'].unlink()
        self.assertEqual(client.get('/comments/' + uid(10)), {'id': 'public-original'})
        self.assertIsNone(handler.requests[0].get_header('Authorization'))
        with self.assertRaisesRegex(MailError, '^credentials_unavailable$'):
            client.get('/agents/me', authenticated=True)
        self.assertEqual(len(handler.requests), 1)

    def test_transient_retry_cap_rate_limit_and_response_size(self):
        client, handler = self.client([(503, b"PRIVATE ERROR", None)] * 4)
        with self.assertRaisesRegex(MailError, "^http_503$"):
            client.get("/notifications", authenticated=True)
        self.assertEqual(len(handler.requests), 3)
        client, handler = self.client([(429, b"PRIVATE RATE LIMIT", None)])
        with self.assertRaisesRegex(MailError, "^http_429$"):
            client.get("/notifications", authenticated=True)
        self.assertEqual(len(handler.requests), 1)
        client, handler = self.client([(200, b"x" * 65, None)])
        with patch.object(adapter, "MAX_RESPONSE_BYTES", 64), self.assertRaisesRegex(MailError, "^response_too_large$"):
            client.get("/comments/" + uid(10))

    def test_deadline_and_global_request_limit_stop_network_work(self):
        client, handler = self.client([])
        client.deadline = 0
        with self.assertRaisesRegex(MailError, "^budget_exhausted$"):
            client.get("/notifications", authenticated=True)
        client.phase(5)
        client.requests = adapter.MAX_REQUESTS
        with self.assertRaisesRegex(MailError, "^budget_exhausted$"):
            client.get("/notifications", authenticated=True)
        self.assertEqual(handler.requests, [])


if __name__ == "__main__":
    unittest.main()
