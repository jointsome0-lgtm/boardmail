"""Botnet delivery, recovery and credential boundaries, without a real account."""
from copy import deepcopy
from email.message import Message
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError
from urllib.parse import parse_qs, unquote, urlsplit
from urllib.request import BaseHandler, ProxyHandler, build_opener
from urllib.response import addinfourl

from boardmail import adapter_botnet as adapter, commands
from boardmail.adapters import Batch, validate
from boardmail.config import MailError, load
from boardmail.store import Store


def uid(n):
    return f"00000000-0000-0000-0000-{n:012x}"


OWNER = "participant-" + uid(1)
TOPIC = uid(200)
OPENER = "thread:" + uid(100)


def mid(n):
    return "post:" + uid(n)


def original(n, **changes):
    return {"id": mid(n), "topicId": TOPIC, "parentMessageId": OPENER,
            "body": "Public message " + str(n), "title": None,
            "createdAt": 1790593200000, "sequence": n, "status": None,
            "author": {"id": "participant-" + uid(2), "name": "Other"}, **changes}


class FixtureClient:
    def __init__(self):
        self.owner = OWNER
        self.profile = {"actor": {"id": OWNER}}
        self.events, self.calls = [], []
        self.page_errors = {}
        self.topic_error = None
        self.bad_cursor = False
        self.originals = {OPENER: original(100, id=OPENER, parentMessageId=None,
                                         author={"id": OWNER, "name": "Owner"})}

    def add(self, n, reason="reply", **changes):
        self.events.append({"id": n, "threadId": uid(100), "postId": uid(n),
                            "reason": reason, "preview": "PRIVATE PREVIEW", "readAt": 1})
        self.originals[mid(n)] = original(n, **changes)

    def phase(self, seconds):
        pass

    def get(self, path, params=None, *, authenticated=False):
        self.calls.append((path, params, authenticated))
        if path == "/me":
            assert authenticated
            return self.profile
        if path == "/inbox":
            assert authenticated
            cursor = params.get("cursor")
            if cursor in self.page_errors:
                raise MailError(self.page_errors[cursor])
            ceiling = int(cursor.split(":")[1]) if cursor else 10000
            items = sorted((e for e in self.events if e["id"] < ceiling), key=lambda e: -e["id"])
            page = items[:params["limit"]]
            return {"items": page, "nextCursor": 123 if self.bad_cursor else
                    "older:" + str(page[-1]["id"]) if len(items) > len(page) else None}
        assert not authenticated, "Public-original requests carried credentials"
        if path == "/topics/" + TOPIC:
            if self.topic_error:
                raise MailError(self.topic_error)
            return {"id": TOPIC, "title": "Topic, not legacy thread", "description": "Public topic",
                    "createdAt": 1790593200000}
        value = self.originals.get(unquote(path.rsplit("/", 1)[-1]), MailError("http_404"))
        if isinstance(value, Exception):
            raise value
        return deepcopy(value)


class BotnetTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name)
        cfg = self.path / "config.json"
        cfg.write_text(json.dumps({"database": "mail.sqlite3", "sources": {
            "botnet": {"account_id": OWNER, "api_key_file": "missing.key"}}}))
        self.settings = load(cfg)["sources"]["botnet"]
        self.store = Store(self.path / "mail.sqlite3")
        self.store.initialize()
        self.client = FixtureClient()

    def collect(self):
        self.store.prepare_collection()
        known, state, revision = self.store.collection_state("botnet", OWNER, "botnet")
        before = deepcopy(state)
        with patch.object(adapter, "Client", return_value=self.client):
            batch = adapter.collect(self.settings, state, frozenset(known))
        self.assertEqual(state, before)
        validate(batch)
        added, stale = self.store.save_collection("botnet", OWNER, "botnet", revision, batch)
        self.assertFalse(stale)
        self.assertNotIn("PRIVATE PREVIEW", json.dumps(batch.__dict__))
        return batch, added

    def test_native_and_legacy_notifications_deliver_once_with_original_relationships(self):
        self.client.add(10)
        self.client.add(11, reason="mention", parentMessageId=mid(10))
        self.client.events[-1]["messageId"] = mid(11)
        self.client.add(12, author={"id": OWNER, "name": "Owner"})
        batch, added = self.collect()
        self.assertEqual(added, 2)
        self.assertTrue(batch.complete)
        message = self.store.show("botnet", mid(10))
        self.assertEqual((message["thread_id"], message["parent_id"], message["kind"], message["addressing"]),
                         (TOPIC, OPENER, "reply_to_post", "direct"))
        self.assertEqual(message["created_at"], 1790593200)
        self.assertEqual(self.store.show("botnet", mid(11))["addressing"], "mention")
        self.store.mark("botnet", mid(10), "read")
        saved = self.store.show("botnet", mid(10))
        self.assertEqual(self.collect()[1], 0)
        self.assertEqual(self.store.show("botnet", mid(10)), saved)
        self.assertEqual(self.store.status()["counts"]["latest_arrival"], 2)
        # A topic outage must not hide a readable parent or collapse two native
        # reasons. A null reason is a legacy mention, like an absent reason.
        self.client.add(13, reason=None)
        self.client.add(14, reason="reply", parentMessageId=mid(999))
        self.client.events.append({**self.client.events[-1], "reason": "mention"})
        self.client.topic_error = "http_503"
        batch, added = self.collect()
        self.assertEqual((added, batch.error), (2, "http_503"))
        for n in (13, 14):
            self.assertEqual(self.store.show("botnet", mid(n))["addressing"], "direct+mention")
        with self.assertRaisesRegex(MailError, "^subscriptions_unsupported$"):
            self.store.set_subscription("botnet", TOPIC, True, self.settings)

    def test_new_head_backfill_and_failed_reference_survive_reopen_and_upstream_expiry(self):
        for n in range(10, 16):
            self.client.add(n)
        self.client.originals[mid(14)] = MailError("network_error")
        with patch.object(adapter, "PAGE_SIZE", 2):
            self.collect()
            self.store = Store(self.store.path)
            self.client.add(16)  # Arrived ahead of the saved backwards cursor.
            for _ in range(3):
                self.collect()
        self.assertEqual(self.store.known("botnet", OWNER), {mid(n) for n in range(10, 17) if n != 14})
        self.client.events.clear()  # The failed reference must outlive notifications.
        self.client.originals[mid(14)] = original(14)
        batch, added = self.collect()
        self.assertEqual(added, 1)
        self.assertTrue(batch.complete)
        self.assertEqual(batch.state["pending"], [])

    def test_overflow_and_bad_cursor_are_explicit_and_recover_without_dropping_pending(self):
        for n in range(10, 14):
            self.client.add(n)
        self.client.originals[mid(13)] = MailError("http_404")
        with patch.object(adapter, "MAX_PENDING", 2):
            batch, added = self.collect()
            self.assertEqual((batch.error, added, batch.unavailable), ("pending_overflow", 1, 1))
            self.assertIn(mid(13), {x["id"] for x in batch.state["pending"]})
            self.assertIsNone(batch.state.get("cursor"))
            self.collect()
            self.collect()
        self.assertEqual(self.store.known("botnet", OWNER), {mid(10), mid(11), mid(12)})
        for n in range(20, 24):
            self.client.add(n)
        with patch.object(adapter, "PAGE_SIZE", 2):
            self.collect()
            cursor = self.store.collection_state("botnet", OWNER, "botnet")[1]["cursor"]
            self.client.page_errors[cursor] = "http_422"
            batch, _ = self.collect()
            self.assertEqual(batch.error, "http_422")
            self.assertIsNone(batch.state["cursor"])
            self.assertIn(mid(13), {x["id"] for x in batch.state["pending"]})

        # A malformed item on an older page reports failure but cannot pin the
        # scan there forever. Valid refs survive even a malformed page cursor.
        self.client.page_errors.clear()
        self.client.events.append({"id": 15, "reason": "mention", "threadId": None})
        with patch.object(adapter, "PAGE_SIZE", 2):
            self.collect()  # Head: 23, 22.
            self.collect()  # Older page: 21, 20.
            self.client.calls.clear()
            batch, _ = self.collect()  # Older page: malformed 15, valid 13.
            self.assertIn(("/inbox", {"limit": 2, "cursor": "older:20"}, True), self.client.calls)
            self.assertEqual((batch.error, batch.state["cursor"]), ("invalid_response", "older:13"))
            self.collect()
            batch, _ = self.collect()
        self.assertIsNone(batch.state["cursor"])
        self.client.add(30)
        self.client.bad_cursor = True
        batch, added = self.collect()
        self.assertEqual((batch.error, added), ("invalid_response", 1))
        self.assertIn(mid(30), self.store.known("botnet", OWNER))

    def test_identity_and_public_original_failures_never_save_unconfirmed_text(self):
        self.client.add(10)
        self.client.profile = {"actor": {"id": "other-account"}}
        batch, added = self.collect()
        self.assertEqual((batch.error, added, batch.state), ("account_mismatch", 0, {}))
        self.assertEqual([p for p, _, _ in self.client.calls], ["/me"])
        self.client.profile = {"actor": {"id": OWNER}}
        self.client.originals[mid(10)] = original(999, body="WRONG BODY")
        batch, added = self.collect()
        self.assertEqual((batch.error, added), ("invalid_response", 0))
        self.assertEqual(batch.state["pending"], [{"id": mid(10), "reasons": ["reply"]}])
        self.client.page_errors[None] = "http_429"
        self.client.calls.clear()
        batch, _ = self.collect()
        self.assertEqual(batch.error, "http_429")
        self.assertEqual([p for p, _, _ in self.client.calls], ["/me", "/inbox"])

    def test_context_keeps_topic_separate_from_parent_and_rejects_cross_topic_parent(self):
        self.client.add(10)
        self.collect()
        self.client.calls.clear()
        before = self.store.path.read_bytes()
        with patch.object(adapter, "Client", return_value=self.client):
            result, code = commands.execute(self.store, "context", source="botnet", id=mid(10),
                                            sources={"botnet": self.settings})
            self.assertEqual((code, result["root"]["id"], result["parent"]["id"]), (0, TOPIC, OPENER))
            opener, code = commands.execute(self.store, "context", source="botnet", id=OPENER,
                                            sources={"botnet": self.settings})
            self.assertEqual((code, opener["parent"]["status"]), (0, "none"))
            self.client.originals[OPENER]["topicId"] = uid(999)
            result, code = commands.execute(self.store, "context", source="botnet", id=mid(10),
                                            sources={"botnet": self.settings})
            self.assertEqual((code, result["parent"]["status"]), (1, "unavailable"))
        self.assertTrue(all(not auth for _, _, auth in self.client.calls))
        self.assertEqual(self.store.path.read_bytes(), before)

    def pending_backlog(self):
        for number in range(10, 70):
            self.client.add(number)
        return {"pending": [{"id": mid(number), "reasons": ["reply"]} for number in range(10, 70)]}

    def collect_with_real_client(self, state, *, monotonic=lambda: 0, before_response=None):
        self.store.prepare_collection()
        self.store.save_collection("botnet", OWNER, "botnet", 0, Batch(state=state))
        self.store.save("botnet", OWNER, [], now=1)
        with patch.object(adapter.time, "monotonic", side_effect=monotonic):
            client = adapter.Client(self.settings)
            client.key = "synthetic-key-only"

            def response(request, timeout):
                url = urlsplit(request.full_url)
                self.assertEqual((url.scheme, url.netloc), ("https", "botnet.com"))
                self.assertTrue(url.path.startswith("/api/forum/"))
                path = url.path.removeprefix("/api/forum")
                params = {key: values[0] for key, values in parse_qs(url.query).items()}
                if "limit" in params:
                    params["limit"] = int(params["limit"])
                value = self.client.get(path, params, authenticated=request.get_header("Authorization") is not None)
                if before_response is not None:
                    before_response(path)
                return io.BytesIO(json.dumps(value).encode())

            # Exercise production cache and budgets with a controlled clock and offline transport.
            with patch.object(client.opener, "open", side_effect=response), patch.object(adapter, "Client", return_value=client):
                result, code = commands.execute(self.store, "collect", sources={"botnet": self.settings})
        return client, result, code

    def test_real_request_cap_saves_healthy_partial_progress(self):
        state = self.pending_backlog()
        client, result, code = self.collect_with_real_client(state)
        known, saved, _ = self.store.collection_state("botnet", OWNER, "botnet")
        pending = {entry["id"] for entry in saved["pending"]}
        self.assertEqual((client.requests, result["added"], len(pending)), (40, 36, 24))
        self.assertEqual(known | pending, {entry["id"] for entry in state["pending"]})
        self.assertFalse(known & pending)
        self.assertEqual((code, result["failed"], result["errors"]), (0, False, []))
        health = result["sources"][0]
        self.assertEqual((health["status"], health["error"], health["backlog_pending"]), ("ok", None, True))
        self.assertGreater(health["last_ok"], 1)
        self.assertEqual(commands.execute(self.store, "status", require_fresh=True)[1], 0)

    def test_real_cap_does_not_hide_an_earlier_network_failure(self):
        state = self.pending_backlog()
        self.client.originals[mid(10)] = URLError("Synthetic transport failure")
        client, result, code = self.collect_with_real_client(state)
        self.assertEqual((client.requests, code), (40, 1))
        self.assertGreater(result["added"], 0)
        self.assertEqual(result["errors"][0]["error"], "network_error")
        health = result["sources"][0]
        self.assertEqual((health["status"], health["last_ok"], health["backlog_pending"]), ("error", 1, True))
        self.assertIn(mid(10), {entry["id"] for entry in self.store.collection_state("botnet", OWNER, "botnet")[1]["pending"]})

    def test_real_time_budget_after_identity_is_healthy_partial_progress(self):
        state, clock = self.pending_backlog(), [0]
        def expire(path):
            if path == "/inbox":
                clock[0] = 46
        client, result, code = self.collect_with_real_client(state, monotonic=lambda: clock[0], before_response=expire)
        self.assertEqual((client.requests, result["added"], code, result["errors"]), (2, 0, 0, []))
        self.assertCountEqual(self.store.collection_state("botnet", OWNER, "botnet")[1]["pending"], state["pending"])
        health = result["sources"][0]
        self.assertEqual((health["status"], health["backlog_pending"]), ("ok", True))
        self.assertGreater(health["last_ok"], 1)

    def test_identity_preflight_budget_failure_remains_an_error(self):
        state, clock = self.pending_backlog(), [0]
        def expire(path):
            self.assertEqual(path, "/me")
            clock[0] = 11
        client, result, code = self.collect_with_real_client(state, monotonic=lambda: clock[0], before_response=expire)
        self.assertEqual((client.requests, result["added"], code), (1, 0, 1))
        self.assertEqual(result["errors"][0]["error"], "budget_exhausted")
        self.assertEqual(self.store.collection_state("botnet", OWNER, "botnet")[1], state)
        health = result["sources"][0]
        self.assertEqual((health["status"], health["last_ok"], health["backlog_pending"]), ("error", 1, True))

    def test_http_keeps_credentials_on_fixed_private_endpoints_and_stops_redirects(self):
        class HTTPS(BaseHandler):
            handler_order = 100
            requests = []
            def https_open(inner, request):
                inner.requests.append(request)
                headers = Message()
                redirect = len(inner.requests) == 3
                if redirect:
                    headers["Location"] = "https://evil.invalid/token"
                response = addinfourl(io.BytesIO(b'{}'), headers, request.full_url, 302 if redirect else 200)
                response.msg = "Synthetic response"
                return response
        key = self.path / "test.key"
        key.write_text("synthetic-key-only\n")
        client = adapter.Client({**self.settings, "api_key_file": key})
        handler = HTTPS()
        client.opener = build_opener(ProxyHandler({}), handler, adapter.NoRedirect())
        client.get("/me", authenticated=True)
        key.unlink()
        client.get("/topic-messages/" + mid(10))
        with self.assertRaisesRegex(MailError, "^redirect_refused$"):
            client.get("/inbox", authenticated=True)
        self.assertEqual(len(handler.requests), 3)
        self.assertEqual(handler.requests[0].get_header("Authorization"), "Bearer synthetic-key-only")
        self.assertIsNone(handler.requests[1].get_header("Authorization"))
        self.assertTrue(all(r.full_url.startswith("https://botnet.com/api/forum/") for r in handler.requests))
        client.deadline = 0
        with self.assertRaisesRegex(MailError, "^budget_exhausted$"):
            client.get("/inbox", authenticated=True)
        self.assertEqual(len(handler.requests), 3)
