"""Explicit thread subscriptions across all six built-in adapters. Every board response is invented.

Subscribed roots are supplied by the core as ``settings["subscriptions"]``. Other-author
activity in those threads arrives as ``kind: thread_activity``; addressing follows only the
ancestry each provider actually shows. Empty subscriptions must not change a single request.
"""
from copy import deepcopy
import io
import json
import re
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from uuid import UUID

from boardmail import adapter_clawdchat as clawd
from boardmail import adapter_fourclaw as fourclaw
from boardmail import adapter_fruitflies as fruit
from boardmail import addressing, providers, subscriptions
from boardmail.adapters import Batch, validate
from boardmail.config import MailError
from examples.fixtures import FixtureClient, named, original, settings, uid
from test_clawdchat import FixtureClient as ClawdChatClient, event as clawd_event, original as clawd_original
from test_fourclaw import THREAD, page as claw_page, post as claw_post
from test_fruitflies import post as fly_post

PREVIEW = "PRIVATE NOTIFICATION PREVIEW"


def shape(test, batch):
    """Validate actual provider output, without replacing subscription kinds."""
    validate(batch)
    for item in batch.originals:
        test.assertEqual(set(item), set(addressing.ORIGINAL_FIELDS))
    dump = json.dumps([batch.messages, batch.state, batch.originals])
    test.assertNotIn(PREVIEW, dump)
    for message in batch.messages:
        test.assertIn(message.get("addressing"), (None, *addressing.VALUES))
        if message["kind"] == "thread_activity":
            test.assertEqual(message["discovery"], "subscription")


def by_id(batch):
    return {m["id"]: m for m in batch.messages}


def not_found():
    return HTTPError("https://example.invalid", 404, "absent", {}, io.BytesIO())


class PostingboardSubscriptionTests(unittest.TestCase):
    """Root 301 is ours, root 302 belongs to another account (examples.fixtures)."""

    def client(self, threads, subscribed, **extra):
        cfg = {**settings()["postingboard"], "threads": [uid(n) for n in threads], "subscriptions": [uid(n) for n in subscribed], **extra}
        client = FixtureClient("postingboard", cfg)
        client.comments[uid(302)] += [named(323, 302, reply_to=313, body="answering your reply"),
                                      named(324, 302, reply_to=315), named(325, 302, reply_to=999)]
        return client

    def collect(self, client, state=None, known=()):
        batch = providers.collect("postingboard", client.settings, deepcopy(state or {}), set(known), client_factory=lambda *_: client)
        shape(self, batch)
        return batch

    def test_subscribed_foreign_root_delivers_activity_with_shown_ancestry(self):
        client = self.client([301], [302])
        batch = self.collect(client)
        self.assertFalse(batch.error)
        got = by_id(batch)
        foreign = {n: (got[uid(n)]["kind"], got[uid(n)]["addressing"]) for n in (314, 315, 323, 324, 325)}
        self.assertEqual(foreign, {314: ("mention", "mention"), 315: ("thread_activity", "thread"),
                                   323: ("thread_activity", "direct"), 324: ("thread_activity", "thread"),
                                   325: ("thread_activity", None)})
        self.assertEqual(got[uid(323)]["discovery"], "subscription")
        self.assertEqual(got[uid(314)]["discovery"], "subscription", "This root was discovered only by subscribing")
        self.assertNotIn(uid(313), got, "Our own reply is context, never mail")
        self.assertIn(uid(313), {o["id"] for o in batch.originals})
        self.assertIn(uid(302), {o["id"] for o in batch.originals})
        # Our own root keeps its established kinds.
        self.assertEqual({got[uid(n)]["kind"] for n in (311, 312)}, {"reply_to_post"})
        replay = self.collect(client, batch.state, known=set(got))
        self.assertEqual(replay.messages, [])

    def test_without_subscription_only_mentions_and_no_extra_requests(self):
        plain = self.client([302], [])
        first = self.collect(plain)
        self.assertEqual(set(by_id(first)), {uid(314)})
        self.assertNotIn("subscriptions", first.state)
        again = self.client([302], [302])
        second = self.collect(again)
        self.assertEqual([c[0] for c in plain.calls], [c[0] for c in again.calls], "Subscribing a configured root adds no request")
        self.assertEqual(set(by_id(second)), {uid(314), uid(315), uid(323), uid(324), uid(325)})
        self.assertEqual(by_id(second)[uid(314)]["discovery"], "thread", "Configured discovery remains independent")

    def test_unsubscribe_prunes_only_subscription_progress(self):
        client = self.client([301], [302])
        first = self.collect(client)
        self.assertEqual(set(first.state["threads"]), {uid(301), uid(302)})
        self.assertEqual(set(first.state["subscriptions"]["roots"]), {uid(302)})
        dropped = self.client([301], [])
        second = self.collect(dropped, first.state, known=set(by_id(first)))
        self.assertEqual(set(second.state["threads"]), {uid(301)})
        self.assertNotIn("subscriptions", second.state)
        self.assertEqual([c[0] for c in dropped.calls if uid(302) in c[0]], [])

    def test_subscription_only_setup_without_configured_threads(self):
        for threads in (None, []):
            with self.subTest(threads=threads):
                client = self.client([], [302])
                if threads is None:
                    del client.settings["threads"]
                batch = self.collect(client)
                self.assertFalse(batch.error)
                self.assertEqual(set(by_id(batch)), {uid(314), uid(315), uid(323), uid(324), uid(325)})
                self.assertEqual(set(batch.state["threads"]), {uid(302)})

    def test_missing_root_is_unavailable_and_later_roots_still_run(self):
        client = self.client([301], [999, 302])
        batch = self.collect(client)
        self.assertEqual(batch.unavailable, 1)
        self.assertIn(uid(315), by_id(batch))


class ThreadClient:
    """Invented Colony/Moltbook API: identity, empty notifications and public thread pages
    shaped like the saved live responses (Colony: items/total/has_more/page; Moltbook: cursor tree)."""

    def __init__(self, source, cfg):
        self.source, self.settings, self.owner = source, cfg, cfg["account_id"]
        self.host = "https://" + source + ".example.invalid"
        self.calls, self.posts, self.comments, self.failures = [], {}, {}, {}
        self.colony = source == "the-colony"
        self.limit, self.requests = None, 0  # Public requests allowed per pass, when bounded.

    def get(self, path, params=None, *, authenticated=False):
        params = dict(params or {})
        self.calls.append((path, params, authenticated))
        if path in ("/agents/me",):
            assert authenticated
            return {"agent": {"id": self.owner}} if not self.colony else {"id": self.owner}
        if path == "/notifications":
            assert authenticated
            return [] if self.colony else {"notifications": [], "has_more": False, "next_cursor": "0"}
        assert not authenticated, "Public thread reads must be anonymous"
        if self.limit is not None and self.requests >= self.limit:
            raise MailError("budget_exhausted")
        self.requests += 1
        match = re.fullmatch(r"/posts/([0-9a-f-]{36})(/comments)?", path)
        root = match.group(1)
        if root in self.failures:
            raise self.failures[root]
        if root not in self.posts:
            raise not_found()
        if not match.group(2):
            return deepcopy(self.posts[root]) if self.colony else {"success": True, "post": deepcopy(self.posts[root])}
        items, limit = self.comments.get(root, []), params["limit"]
        if self.colony:
            page = params.get("page", 1)
            start = (page - 1) * limit
            return {"items": deepcopy(items[start:start + limit]), "total": len(items),
                    "has_more": start + limit < len(items), "page": page}
        if not str(params.get("cursor", "0")).isdigit():
            raise HTTPError("https://example.invalid", 400, "bad cursor", {}, io.BytesIO())
        start = int(params.get("cursor", "0"))
        return {"comments": deepcopy(items[start:start + limit]), "has_more": start + limit < len(items),
                "next_cursor": str(start + limit), "count": min(limit, max(0, len(items) - start))}


class NotificationBoardSubscriptionTests(unittest.TestCase):
    def client(self, source, roots, **extra):
        cfg = {**settings()[source], "subscriptions": [uid(n) for n in roots], "mention_aliases": ["@sample"], **extra}
        return ThreadClient(source, cfg)

    def collect(self, client, state=None, known=()):
        batch = providers.collect(client.source, client.settings, deepcopy(state or {}), set(known), client_factory=lambda *_: client)
        shape(self, batch)
        return batch

    def thread(self, client, root, author=10):
        colony = client.colony
        me = int(UUID(client.owner))
        client.posts[uid(root)] = {**original(root, root, author, colony=colony), "title": "Public thread"}
        c = lambda n, parent=None, who=10, body="A synthetic public reply.": {
            **original(n, root, who, colony=colony, body=body), "parent_id": uid(parent) if parent else None}
        client.comments[uid(root)] = [c(root + 11), c(root + 12, who=me), c(root + 13, parent=root + 12),
                                      c(root + 14, parent=root + 11), c(root + 15, parent=999),
                                      c(root + 16, body="@sample please look"), c(root + 17, who=me, parent=root + 11)]

    def test_colony_and_moltbook_paginate_from_the_head_and_address_by_shown_parents(self):
        for source, root in (("the-colony", 400), ("moltbook", 500)):
            with self.subTest(source=source), patch.object(providers, "PAGE_SIZE", 3):
                client = self.client(source, [root])
                self.thread(client, root)
                batch = self.collect(client)
                self.assertFalse(batch.error)
                got = by_id(batch)
                self.assertEqual({n - root: (got[uid(n)]["kind"], got[uid(n)]["addressing"]) for n in sorted(int(UUID(k)) for k in got)},
                                 {11: ("thread_activity", "thread"), 13: ("thread_activity", "direct"),
                                  14: ("thread_activity", "thread"), 15: ("thread_activity", None),
                                  16: ("thread_activity", "mention")})
                self.assertEqual(got[uid(root + 13)]["thread_id"], uid(root))
                self.assertEqual({o["id"] for o in batch.originals}, {uid(root), uid(root + 12), uid(root + 17)},
                                 "Root and our own comments are context")
                pages = [p for path, p, _ in client.calls if path.endswith("/comments")]
                self.assertEqual(len(pages), 3, "Seven comments in pages of three")
                if source == "the-colony":
                    self.assertEqual([p["page"] for p in pages], [1, 2, 3])
                progress = batch.state["subscriptions"]["roots"][uid(root)]
                self.assertEqual(set(progress), {"owners"}, "A finished cycle keeps only fetched ownership")
                self.assertEqual({int(UUID(k)) - root: v for k, v in progress["owners"].items()},
                                 {0: False, 11: False, 12: True, 13: False, 14: False, 15: False, 16: False, 17: True})
                replay = self.collect(client, batch.state, known=set(got))
                self.assertEqual(replay.messages, [])
                self.assertEqual(set(replay.state["subscriptions"]["roots"]), {uid(root)})

    def test_notification_progress_survives_and_unsubscribe_prunes(self):
        client = self.client("moltbook", [500])
        self.thread(client, 500)
        state = {"discovery": {"cursor": "keep"}, "pending": {}, "subscriptions": {"next": None, "roots": {uid(500): {}, uid(777): {}}}}
        batch = self.collect(client, state)
        self.assertEqual(set(batch.state["subscriptions"]["roots"]), {uid(500)}, "A dropped root loses only its own entry")
        self.assertIn("discovery", batch.state)
        gone = self.client("moltbook", [])
        self.thread(gone, 500)
        second = self.collect(gone, batch.state, known=set(by_id(batch)))
        self.assertNotIn("subscriptions", second.state)
        self.assertEqual([c[0] for c in gone.calls], ["/agents/me", "/notifications"], "Empty subscriptions add no request")

    def test_unavailable_and_failing_roots_do_not_starve_healthy_ones(self):
        client = self.client("the-colony", [400, 401, 402, 403])
        self.thread(client, 402)
        client.failures[uid(401)] = HTTPError("https://example.invalid", 503, "busy", {}, io.BytesIO())
        batch = self.collect(client)
        self.assertEqual(batch.unavailable, 2, "Two roots absent")
        self.assertEqual(batch.error, "http_503")
        self.assertIn(uid(413), by_id(batch))
        self.assertEqual(batch.state["subscriptions"]["next"], uid(400), "A finished pass restarts the rotation")
        client.failures[uid(400)] = MailError("budget_exhausted")
        client.calls.clear()
        cut = self.collect(client, batch.state, known=set(by_id(batch)))
        self.assertFalse(cut.complete)
        self.assertEqual(cut.state["subscriptions"]["next"], uid(400), "Resume at the interrupted root")
        self.assertEqual([c[0] for c in client.calls if c[0].startswith("/posts/")], ["/posts/" + uid(400)])

    def test_slow_root_that_spends_the_budget_cannot_starve_another_root(self):
        for source in ('the-colony', 'moltbook'):
            with self.subTest(source=source):
                client = self.client(source, [400, 401])
                self.thread(client, 401)
                clock = [0]
                real = client.get

                def slow(path, params=None, **kwargs):
                    if path == '/posts/' + uid(400):
                        clock[0] += providers.SOURCE_SECONDS
                        raise MailError('budget_exhausted')
                    return real(path, params, **kwargs)

                client.get = slow
                with patch.object(providers.time, 'monotonic', side_effect=lambda: clock[0]):
                    first = self.collect(client)
                    second = self.collect(client, first.state)
                self.assertFalse(first.complete)
                self.assertIn(uid(412), by_id(second), 'The slow root must give another root a turn')

    def test_cut_off_pagination_still_delivers_what_was_read(self):
        client = self.client("the-colony", [400])
        self.thread(client, 400)
        real = client.get
        def flaky(path, params=None, **kw):
            if path.endswith("/comments") and params.get("page") == 2:
                raise MailError("budget_exhausted")
            return real(path, params, **kw)
        client.get = flaky
        with patch.object(providers, "PAGE_SIZE", 3):
            batch = self.collect(client)
        self.assertFalse(batch.complete)
        self.assertEqual(set(by_id(batch)), {uid(411), uid(413)}, "First page delivered; own comment 412 is context")
        client.get = real
        with patch.object(providers, "PAGE_SIZE", 3):
            second = self.collect(client, batch.state, known=set(by_id(batch)))
        self.assertEqual(set(by_id(second)), {uid(n) for n in (414, 415, 416)})
        self.assertEqual(by_id(second)[uid(414)]["addressing"], "thread", "A page-one parent's ownership was retained")
        self.assertEqual(batch.state["subscriptions"]["roots"][uid(400)]["page"], 2, "The cut pass saved its next page")
        self.assertEqual(batch.state["subscriptions"]["next"], uid(400), "A single root simply resumes")
        self.assertNotIn("page", second.state["subscriptions"]["roots"][uid(400)], "A finished cycle restarts at the head")

    def passes(self, client, roots, limit, count):
        """Repeated passes under one identical public-request budget; every message once."""
        state, known, got, nexts = {}, set(), {}, []
        for _ in range(count):
            client.requests, client.limit = 0, limit
            batch = self.collect(client, state, known)
            for message in batch.messages:
                self.assertNotIn(message["id"], got, "No message is delivered twice")
                got[message["id"]] = message
            known |= set(got)
            state = batch.state
            nexts.append(state["subscriptions"]["next"])
        return got, state, nexts

    def test_repeated_identical_budgets_advance_a_long_thread_and_a_healthy_root(self):
        for source, root in (("the-colony", 400), ("moltbook", 500)):
            with self.subTest(source=source), patch.object(providers, "PAGE_SIZE", 3):
                client = self.client(source, [root, root + 1])
                self.thread(client, root)  # Seven comments: three pages of three.
                client.posts[uid(root + 1)] = {**original(root + 1, root + 1, 20, colony=client.colony), "title": "Healthy"}
                client.comments[uid(root + 1)] = [{**original(root + 21, root + 1, 20, colony=client.colony), "parent_id": None}]
                # Root plus one page fit in a pass; the long thread alone would need four.
                got, state, nexts = self.passes(client, [root, root + 1], limit=2, count=5)
                self.assertEqual({int(UUID(k)) - root: (m["kind"], m["addressing"]) for k, m in got.items()},
                                 {11: ("thread_activity", "thread"), 13: ("thread_activity", "direct"),
                                  14: ("thread_activity", "thread"), 15: ("thread_activity", None),
                                  16: ("thread_activity", "mention"), 21: ("thread_activity", "thread")})
                self.assertEqual(nexts[:3], [uid(root + 1), uid(root), uid(root + 1)],
                                 "A root that spent the pass waits a turn; one that read nothing keeps it")
                progress = state["subscriptions"]["roots"][uid(root)]
                self.assertNotIn("page", progress)
                self.assertNotIn("cursor", progress)
                self.assertEqual(progress["owners"][uid(root + 11)], False, "Page-one ownership survived the passes")
                # Later activity is found by the next cycle from the head.
                client.comments[uid(root)].append({**original(root + 18, root, 30, colony=client.colony), "parent_id": uid(root + 12)})
                client.requests, client.limit = 0, 2
                fresh = self.collect(client, state, set(got))
                self.assertEqual([(m["id"], m["addressing"]) for m in fresh.messages], [], "Page one first; the new comment is on page three")
                for _ in range(8):  # Both roots alternate; page three comes around on the fifth pass.
                    client.requests = 0
                    fresh = self.collect(client, fresh.state, set(got))
                    if fresh.messages: break
                self.assertEqual([(m["id"], m["addressing"]) for m in fresh.messages], [(uid(root + 18), "direct")],
                                 "A later reply to our retained comment is direct")

    def test_ownership_cache_eviction_never_delivers_our_comments_or_forgets_the_current_root(self):
        for source in ('the-colony', 'moltbook'):
            with self.subTest(source=source), patch.object(subscriptions, 'MAX_OWNERS', 2):
                client = self.client(source, [400])
                self.thread(client, 400)
                batch = self.collect(client)
                got = by_id(batch)
                self.assertNotIn(uid(412), got)
                self.assertNotIn(uid(417), got)
                self.assertEqual(got[uid(411)]['addressing'], 'thread', 'The root was verified in this very pass')

    def test_missing_parent_field_is_not_a_top_level_reply(self):
        for source, root in (("the-colony", 400), ("moltbook", 500)):
            with self.subTest(source=source):
                client = self.client(source, [root])
                me = int(UUID(client.owner))
                client.posts[uid(root)] = {**original(root, root, me, colony=client.colony), "title": "Our thread"}
                bare = original(root + 11, root, 10, colony=client.colony)
                del bare["parent_id"]
                client.comments[uid(root)] = [bare, {**original(root + 12, root, 10, colony=client.colony), "parent_id": None}]
                got = by_id(self.collect(client))
                self.assertEqual((got[uid(root + 11)]["addressing"], got[uid(root + 12)]["addressing"]), (None, "direct"),
                                 "Only an explicit null parent is a reply to our root")

    def test_moltbook_rejected_saved_cursor_restarts_at_the_head(self):
        client = self.client("moltbook", [500])
        self.thread(client, 500)
        state = {"subscriptions": {"next": None, "roots": {uid(500): {"cursor": "stale", "pages": 1}}}}
        batch = self.collect(client, state)
        self.assertEqual(batch.error, "http_400")
        self.assertNotIn("cursor", batch.state["subscriptions"]["roots"][uid(500)], "The rejected position is dropped")
        second = self.collect(client, batch.state)
        self.assertEqual(len(second.messages), 5)
        self.assertIsNone(second.error)

    def test_bounded_ownership_map_leaves_older_parents_unknown(self):
        client = self.client("the-colony", [400])
        self.thread(client, 400)
        with patch.object(subscriptions, "MAX_OWNERS", 3), patch.object(providers, "PAGE_SIZE", 3):
            batch = self.collect(client)
        got = by_id(batch)
        self.assertEqual(got[uid(413)]["addressing"], "direct", "Parent 412 was fetched on the same page")
        self.assertIsNone(got[uid(414)]["addressing"], "Parent 411 left the bounded map: unknown, never invented")
        self.assertEqual(len(batch.state["subscriptions"]["roots"][uid(400)]["owners"]), 3)


class ClawdThreadClient(ClawdChatClient):
    """Adds the public comment listing with depth cut-offs and parent pages, plus the request cap."""

    def __init__(self):
        super().__init__()
        self.threads, self.children, self.limit, self.requests, self.phases = {}, {}, clawd.MAX_REQUESTS, 0, []

    def phase(self, seconds):
        self.phases.append(seconds)

    def get(self, path, params=None, *, authenticated=False):
        if self.requests >= self.limit:
            raise MailError("budget_exhausted")
        self.requests += 1
        match = re.fullmatch(r"/posts/([0-9a-f-]{36})/comments", path)
        if not match:
            if path.startswith("/posts/") and path.rsplit("/", 1)[-1] not in self.originals:
                raise MailError("http_404")
            return super().get(path, params, authenticated=authenticated)
        self.calls.append((path, params, authenticated))
        assert not authenticated
        root, params = match.group(1), dict(params or {})
        if root not in self.threads:
            raise MailError("http_404")
        nodes = self.children[params["parent_id"]] if params.get("parent_id") else self.threads[root]
        skip, limit = params.get("skip", 0), params["limit"]
        selected = deepcopy(nodes[skip:skip + limit])
        return {"success": True, "comments": selected, "total": len(nodes), "comment_count": 0,
                "returned_count": len(selected), "max_depth": params.get("max_depth", 2), "parent_id": params.get("parent_id")}


def node(n, parent=None, author=2, replies=(), more=False, **changes):
    return {**clawd_original(n, parent_id=uid(parent) if parent else None, author={"id": uid(author), "name": "agent-" + str(author)}, **changes),
            "replies": list(replies), "reply_count": len(replies) + (1 if more else 0), "has_more_replies": more}


class ClawdChatSubscriptionTests(unittest.TestCase):
    def collect(self, client, subscribed, state=None, known=(), **extra):
        cfg = {"account_id": uid(1), "subscriptions": [uid(n) for n in subscribed], "mention_aliases": ["sample"], **extra}
        before = json.dumps(state or {})
        with patch.object(clawd, "Client", return_value=client):
            batch = clawd.collect(cfg, state or {}, frozenset(known))
        self.assertEqual(json.dumps(state or {}), before, "Input state is a snapshot")
        shape(self, batch)
        return batch

    def thread(self, client):
        client.originals[uid(100)] = clawd_original(100, title="Thread", author={"id": uid(2), "name": "host"})
        deep = node(16, parent=15)
        client.threads[uid(100)] = [node(11), node(12, author=1, replies=[node(13, parent=12)]),
                                    node(14, parent=11), node(15, more=True), node(17, content="@sample look here")]
        client.children[uid(15)] = [deep]

    def test_listed_tree_deep_children_and_shown_ownership(self):
        client = ClawdThreadClient()
        self.thread(client)
        batch = self.collect(client, [100])
        self.assertIsNone(batch.error)
        got = by_id(batch)
        self.assertEqual({int(UUID(k)): (m["kind"], m["addressing"]) for k, m in got.items()},
                         {11: ("thread_activity", "thread"), 13: ("thread_activity", "direct"), 14: ("thread_activity", "thread"),
                          15: ("thread_activity", "thread"), 16: ("thread_activity", "thread"), 17: ("thread_activity", "mention")})
        self.assertEqual({o["id"] for o in batch.originals}, {uid(100), uid(12)})
        listing = [p for path, p, _ in client.calls if path.endswith("/comments")]
        self.assertEqual(listing[0]["max_depth"], 20)
        self.assertEqual(listing[1]["parent_id"], uid(15), "Cut replies are fetched by parent page")
        self.assertEqual(batch.state["subscriptions"]["roots"][uid(100)], {"skip": 0, "pending": []})
        self.assertTrue(batch.complete)
        replay = self.collect(client, [100], batch.state, known=set(got))
        self.assertEqual(replay.messages, [])

    def test_busy_notifications_leave_time_and_requests_for_subscriptions(self):
        client = ClawdThreadClient()
        self.thread(client)
        client.events = [clawd_event(n) for n in range(200, 260)]
        for n in range(200, 260):
            client.originals[uid(n)] = clawd_original(n)
        state = {"offset": 0, "pending": [{"id": uid(n), "post": uid(100), "kind": "reply_to_post", "is_post": False, "types": ["comment"]} for n in range(300, 340)]}
        for n in range(300, 340):
            client.originals[uid(n)] = clawd_original(n)
        batch = self.collect(client, [100], state)
        self.assertEqual(client.phases, [5, 12, 5, 12, 11], "Identity, shorter notification phases, then the reserve")
        self.assertLessEqual(client.requests, clawd.MAX_REQUESTS)
        self.assertTrue(any(path.endswith("/comments") for path, _, _ in client.calls), "Subscription still ran")
        self.assertIn(uid(11), by_id(batch))
        self.assertTrue(batch.state["pending"], "Notification backlog is retained for the next pass")
        idle = ClawdThreadClient()
        idle.events, idle.originals = list(client.events), dict(client.originals)
        without = self.collect(idle, [], deepcopy(state))
        self.assertEqual(idle.phases, [5, 15, 5, 15], "Without subscriptions the pass is unchanged")
        self.assertNotIn("subscriptions", without.state)
        self.assertFalse(any(path.endswith("/comments") for path, _, _ in idle.calls))

    def test_cut_off_scan_resumes_with_parent_ownership(self):
        client = ClawdThreadClient()
        self.thread(client)
        # Identity, notifications, root and one listing page fit; the parent page does not.
        with patch.object(clawd, "MAX_REQUESTS", 4), patch.object(clawd, "SUBSCRIPTION_REQUESTS", 1):
            batch = self.collect(client, [100])
        self.assertFalse(batch.complete)
        self.assertIsNone(batch.error, "A spent budget is incomplete progress, not a failed operation")
        self.assertIn(uid(11), by_id(batch))
        self.assertNotIn(uid(16), by_id(batch))
        progress = batch.state["subscriptions"]["roots"][uid(100)]
        self.assertEqual(progress["pending"], [[uid(15), 0, False]])
        again = ClawdThreadClient()
        self.thread(again)
        second = self.collect(again, [100], batch.state, known=set(by_id(batch)))
        self.assertEqual(set(by_id(second)), {uid(16)})
        self.assertEqual(by_id(second)[uid(16)]["addressing"], "thread")
        self.assertEqual(second.state["subscriptions"]["roots"][uid(100)]["pending"], [])

    def passes(self, client, subscribed, count, requests, reserve):
        state, known, got, nexts = {}, set(), {}, []
        for _ in range(count):
            client.requests = 0
            client.calls.clear()
            with patch.object(clawd, "MAX_REQUESTS", requests), patch.object(clawd, "SUBSCRIPTION_REQUESTS", reserve):
                batch = self.collect(client, subscribed, state, known)
            for message in batch.messages:
                self.assertNotIn(message["id"], got, "No message is delivered twice")
                got[message["id"]] = message
            known |= set(got)
            state = batch.state
            nexts.append(state["subscriptions"]["next"])
        return got, state, nexts

    def test_repeated_identical_budgets_reach_deep_children_and_another_root(self):
        client = ClawdThreadClient()
        self.thread(client)
        client.originals[uid(101)] = clawd_original(101, title="Healthy", author={"id": uid(3), "name": "other"})
        client.threads[uid(101)] = [node(21, author=3, post_id=uid(101))]
        # Identity and notifications take two requests; the root and one listing fit per pass.
        with patch.object(clawd, "COMMENT_PAGE", 3):
            got, state, nexts = self.passes(client, [100, 101], count=5, requests=4, reserve=1)
        self.assertEqual({int(UUID(k)): (m["kind"], m["addressing"]) for k, m in got.items()},
                         {11: ("thread_activity", "thread"), 13: ("thread_activity", "direct"), 14: ("thread_activity", "thread"),
                          15: ("thread_activity", "thread"), 16: ("thread_activity", "thread"), 17: ("thread_activity", "mention"),
                          21: ("thread_activity", "thread")})
        self.assertEqual(nexts[:3], [uid(101), uid(100), uid(101)], "The spent root waits a turn; the untouched one keeps it")
        self.assertEqual(state["subscriptions"]["roots"][uid(100)], {"skip": 0, "pending": []})
        listing = [p for path, p, _ in client.calls if path.endswith("/comments")]
        self.assertEqual(listing, [{"parent_id": uid(15), "max_depth": 20, "limit": 3, "skip": 0}],
                         "The final pass drained the saved parent before any head page")

    def test_missing_parent_field_is_not_a_top_level_reply(self):
        client = ClawdThreadClient()
        client.originals[uid(100)] = clawd_original(100, title="Ours", author={"id": uid(1), "name": "me"})
        bare = node(11)
        del bare["parent_id"]
        client.threads[uid(100)] = [bare, node(12)]
        got = by_id(self.collect(client, [100]))
        self.assertEqual((got[uid(11)]["addressing"], got[uid(12)]["addressing"]), (None, "direct"))

    def test_saved_parent_work_is_drained_before_the_head_is_rescanned(self):
        client = ClawdThreadClient()
        self.thread(client)
        state = {"subscriptions": {"next": None, "roots": {uid(100): {"skip": 0, "done": True, "pending": [[uid(15), 0, False]]}}}}
        batch = self.collect(client, [100], state)
        self.assertEqual(set(by_id(batch)), {uid(16)})
        self.assertEqual([p.get("parent_id") for path, p, _ in client.calls if path.endswith("/comments")], [uid(15)])
        self.assertTrue(batch.complete)
        self.assertEqual(batch.state["subscriptions"]["roots"][uid(100)], {"skip": 0, "pending": []})

    def test_nested_queue_overflow_retains_the_parent_page_between_passes(self):
        state, known, batches, heads = {}, set(), [], 0
        with patch.object(clawd, 'MAX_REQUESTS', 4), patch.object(clawd, 'SUBSCRIPTION_REQUESTS', 1), \
                patch.object(clawd, 'MAX_PENDING_PARENTS', 1):
            for _ in range(5):
                client = ClawdThreadClient()
                client.originals[uid(100)] = clawd_original(100, title='Thread', author={'id': uid(2), 'name': 'host'})
                client.threads[uid(100)] = [node(11, more=True)]
                client.children = {uid(11): [node(12, parent=11, more=True), node(13, parent=11, more=True)],
                                   uid(12): [node(14, parent=12)], uid(13): [node(15, parent=13)]}
                batch = self.collect(client, [100], state, known)
                known.update(by_id(batch))
                state = batch.state
                batches.append(batch)
                heads += sum(path.endswith('/comments') and not params.get('parent_id') for path, params, _ in client.calls)
        self.assertFalse(batches[2].complete, 'The unserved branch still belongs to this scan')
        self.assertEqual(known, {uid(n) for n in (11, 12, 13, 14, 15)})
        self.assertTrue(batches[-1].complete)
        self.assertEqual(heads, 1, 'Saved work completes before another head scan')

    def test_full_parent_queue_retains_the_page_until_its_children_are_serviced(self):
        client = ClawdThreadClient()
        client.originals[uid(100)] = clawd_original(100, title="Thread", author={"id": uid(2), "name": "host"})
        client.threads[uid(100)] = [node(11, more=True), node(12, author=1, more=True), node(13, more=True)]
        for parent in (11, 12, 13):
            client.children[uid(parent)] = [node(parent + 20, parent=parent)]
        with patch.object(clawd, "MAX_PENDING_PARENTS", 1):
            batch = self.collect(client, [100])
        self.assertIsNone(batch.error)
        self.assertTrue(batch.complete)
        got = by_id(batch)
        self.assertEqual({int(UUID(k)): m["addressing"] for k, m in got.items()},
                         {11: "thread", 13: "thread", 31: "thread", 32: "direct", 33: "thread"})
        listing = [(p.get("parent_id"), p["skip"]) for path, p, _ in client.calls if path.endswith("/comments")]
        self.assertEqual(listing, [(None, 0), (uid(13), 0), (None, 0), (uid(12), 0), (None, 0), (uid(11), 0)],
                         "The head page is reread at the same offset until every cut parent was queued")
        self.assertEqual(batch.state["subscriptions"]["roots"][uid(100)], {"skip": 0, "pending": []})
        with patch.object(clawd, "MAX_PENDING_PARENTS", 1), patch.object(clawd, "MAX_SERVED", 1):
            bounded = self.collect(client, [100])
        self.assertEqual(bounded.error, "pending_overflow", "Beyond the bound the page is consumed with an explicit error")
        self.assertEqual(set(by_id(bounded)), {uid(11), uid(13), uid(33), uid(32)}, "Only the branch beyond the bound is missing, and the error says so")

    def test_interrupted_overflow_state_is_resumed_without_duplicate_parents(self):
        client = ClawdThreadClient()
        client.originals[uid(100)] = clawd_original(100, title="Thread", author={"id": uid(2), "name": "host"})
        client.threads[uid(100)] = [node(11, more=True), node(12, more=True)]
        client.children[uid(11)], client.children[uid(12)] = [node(31, parent=11)], [node(32, parent=12)]
        # The head page was read and parent 11 queued when the pass ended.
        state = {"subscriptions": {"next": None, "roots": {uid(100): {"skip": 0, "pending": [[uid(11), 0, False]], "served": {"head": [uid(11)]}}}}}
        with patch.object(clawd, "MAX_PENDING_PARENTS", 1):
            batch = self.collect(client, [100], state)
        self.assertEqual(set(by_id(batch)), {uid(11), uid(12), uid(31), uid(32)})
        self.assertEqual([p.get("parent_id") for path, p, _ in client.calls if path.endswith("/comments")], [uid(11), None, uid(12)])
        self.assertEqual(batch.state["subscriptions"]["roots"][uid(100)], {"skip": 0, "pending": []})

    def test_missing_root_and_rotation_over_several_roots(self):
        client = ClawdThreadClient()
        self.thread(client)
        batch = self.collect(client, [100, 101])
        self.assertEqual(batch.unavailable, 1)
        self.assertIn(uid(11), by_id(batch))
        self.assertEqual(batch.state["subscriptions"]["next"], uid(100))


class FourclawSubscriptionTests(unittest.TestCase):
    OTHER = "00000000-0000-4000-8000-000000000002"

    def collect(self, pages, subscribed, state=None, known=frozenset(), **cfg):
        def fetch(thread):
            return pages[thread]
        with patch.object(fourclaw, "_fetch", side_effect=fetch) as fetched:
            batch = fourclaw.collect(dict(account_id="Reader", watched_threads=[THREAD], subscriptions=subscribed, **cfg), state or {}, known)
        shape(self, batch)
        return batch, [c.args[0] for c in fetched.call_args_list]

    def test_subscribed_page_delivers_untagged_replies_with_unknown_recipient(self):
        pages = {THREAD: claw_page(replies=[claw_post("Other", "@Reader hi"), claw_post("Other", "not for you")]),
                 self.OTHER: claw_page(replies=[claw_post("Other", "untagged reply"), claw_post("Another", "@Reader hey"),
                                                claw_post("Reader", "our own"), claw_post("Other", "second untagged")],
                                       ids=[f"20000000-0000-4000-8000-{n:012d}" for n in range(4)])}
        batch, fetched = self.collect(pages, [self.OTHER])
        self.assertEqual(sorted(fetched), sorted([THREAD, self.OTHER]))
        watched = [m for m in batch.messages if m["thread_id"] == THREAD]
        self.assertEqual([m["kind"] for m in watched], ["mention"], "An unsubscribed watched foreign page keeps mentions only")
        subscribed = [m for m in batch.messages if m["thread_id"] == self.OTHER]
        self.assertTrue(all(m['discovery'] == 'subscription' for m in subscribed))
        self.assertEqual([(m["kind"], m["addressing"], m["body"]) for m in subscribed],
                         [("thread_activity", None, "untagged reply"), ("mention", "mention", "@Reader hey"),
                          ("thread_activity", None, "second untagged")])
        self.assertTrue(all(m["parent_id"] is None for m in subscribed), "The page shows no reply targets")
        self.assertIn(self.OTHER, {o["id"] for o in batch.originals})
        replay, _ = self.collect(pages, [self.OTHER], batch.state, known={m["id"] for m in batch.messages})
        self.assertEqual(replay.messages, [])

    def test_union_rotation_keeps_config_cap_and_advances(self):
        threads = [f"00000000-0000-4000-8000-{n:012d}" for n in range(1, 101)]
        extra = "30000000-0000-4000-8000-000000000001"
        pages = {t: claw_page(replies=[claw_post("Other", "quiet")]) for t in [*threads, extra]}
        pages[extra] = claw_page(replies=[claw_post("Other", "subscribed activity")])
        def fetch(thread):
            return pages[thread]
        with patch.object(fourclaw, "_fetch", side_effect=fetch):
            first = fourclaw.collect(dict(account_id="Reader", watched_threads=threads, subscriptions=[extra]), {"next_thread": 98}, frozenset())
            shape(self, first)
            self.assertIsNone(first.error, "101 runtime roots do not violate the configured cap of 100")
            self.assertEqual([m["body"] for m in first.messages], ["subscribed activity"])
            self.assertEqual(first.state, {"next_thread": 1})
            second = fourclaw.collect(dict(account_id="Reader", watched_threads=threads, subscriptions=[]), first.state, frozenset())
            self.assertEqual(second.state, {"next_thread": 5})

    def test_invalid_subscription_value_is_a_config_error(self):
        pages = {THREAD: claw_page(replies=[claw_post("Other", "@Reader hi")])}
        batch, fetched = self.collect(pages, ["not-a-uuid"])
        self.assertEqual((batch.error, fetched), ("invalid_config", []))

    def test_subscription_only_setup_without_watched_threads(self):
        pages = {self.OTHER: claw_page(replies=[claw_post("Other", "untagged reply")])}
        def fetch(thread):
            return pages[thread]
        for cfg in ({"account_id": "Reader", "subscriptions": [self.OTHER]},
                    {"account_id": "Reader", "watched_threads": [], "subscriptions": [self.OTHER]}):
            with self.subTest(cfg=cfg), patch.object(fourclaw, "_fetch", side_effect=fetch) as fetched:
                batch = fourclaw.collect(cfg, {}, frozenset())
                shape(self, batch)
                self.assertIsNone(batch.error)
                self.assertEqual([m["kind"] for m in batch.messages], ["thread_activity"])
                self.assertEqual([c.args[0] for c in fetched.call_args_list], [self.OTHER])
                self.assertEqual(batch.state, {"next_thread": 0})
        for cfg in ({"account_id": "Reader"}, {"account_id": "Reader", "watched_threads": [], "subscriptions": []},
                    {"account_id": "Reader", "watched_threads": "x", "subscriptions": [self.OTHER]},
                    {"account_id": "Reader", "watched_threads": ["bad"], "subscriptions": [self.OTHER]},
                    {"account_id": "Reader", "subscriptions": [self.OTHER, 5]},
                    {"account_id": "Reader", "watched_threads": [f"00000000-0000-4000-8000-{n:012d}" for n in range(1, 102)]}):
            with self.subTest(cfg=cfg), patch.object(fourclaw, "_fetch", side_effect=fetch) as fetched:
                batch = fourclaw.collect(cfg, {}, frozenset())
                self.assertEqual((batch.error, fetched.call_args_list), ("invalid_config", []))


class FruitfliesSubscriptionTests(unittest.TestCase):
    ROOT, OTHER = str(UUID(int=50)), str(UUID(int=60))

    def collect(self, pages, subscribed, state=None, known=frozenset()):
        with patch.object(fruit, "_fetch", side_effect=pages) as fetch:
            batch = fruit.collect({"account_id": "alice", "subscriptions": subscribed}, state or {}, known)
        shape(self, batch)
        return batch, len(fetch.call_args_list)

    def test_newest_first_ancestry_own_parents_and_bounded_memory(self):
        own = [fly_post(1, author="alice", kind="question"), fly_post(12, author="alice", parent=50, kind="answer")]
        newest = [fly_post(7, "grandchild", author="bob", parent=6, kind="answer"), fly_post(13, "to us", author="bob", parent=12, kind="answer"),
                  fly_post(6, "child", author="carol", parent=50, kind="answer"), fly_post(9, "elsewhere", parent=777, kind="answer"),
                  fly_post(61, "other root child", author="dan", parent=60, kind="answer")]
        history = [fly_post(50, "the root", author="root-author"), fly_post(60, "another root", author="eve")]
        batch, requests = self.collect([own, newest, history], [self.ROOT])
        self.assertEqual(requests, 3, "Subscriptions add no feed request")
        got = by_id(batch)
        self.assertEqual({int(UUID(k)): (m["kind"], m["addressing"], m["thread_id"]) for k, m in got.items()},
                         {6: ("thread_activity", "thread", self.ROOT), 7: ("thread_activity", "thread", self.ROOT),
                          13: ("reply_to_comment", "direct", str(UUID(int=12)))})
        members = batch.state["subscriptions"]["roots"][self.ROOT]["members"]
        self.assertEqual({mid: value[0] for mid, value in members.items()},
                         {self.ROOT: False, str(UUID(int=12)): True, str(UUID(int=13)): False,
                                   str(UUID(int=6)): False, str(UUID(int=7)): False})
        self.assertNotIn(str(UUID(int=61)), members, "Another root's activity is not a member")
        # Next pass: only a new descendant is visible, its ancestry comes from memory.
        later = [fly_post(10, "later", author="bob", parent=7, kind="answer")]
        second, _ = self.collect([own, later, []], [self.ROOT], batch.state, known=set(got))
        self.assertEqual({k: (m["kind"], m["addressing"]) for k, m in by_id(second).items()},
                         {str(UUID(int=10)): ("thread_activity", "thread")})
        with patch.object(fruit, "MAX_MEMBERS", 3):
            third, _ = self.collect([own, [fly_post(n, parent=50, kind="answer") for n in range(20, 26)], []], [self.ROOT], second.state, known=set(got) | set(by_id(second)))
        kept = third.state["subscriptions"]["roots"][self.ROOT]["members"]
        self.assertEqual(len(kept), 3)
        self.assertIn(self.ROOT, kept)

    def test_unseen_root_author_and_parent_stay_unknown(self):
        newest = [fly_post(6, "child", author="carol", parent=50, kind="answer")]
        batch, _ = self.collect([[], newest, []], [self.ROOT])
        self.assertEqual([(m["kind"], m["addressing"]) for m in batch.messages], [("thread_activity", None)])
        empty, _ = self.collect([[], newest, []], [])
        self.assertEqual((empty.messages, "subscriptions" in empty.state), ([], False))

    def test_newest_members_survive_older_history_before_a_later_child_arrives(self):
        def dated(n, minute, parent=50, author="other"):
            return {**fly_post(n, parent=parent, author=author, kind="answer" if parent else "post"),
                    "created_at": f"2026-09-07T10:{minute:02d}:00Z"}

        with patch.object(fruit, "MAX_MEMBERS", 4):
            first, requests = self.collect([[], [dated(n, n) for n in range(16, 11, -1)],
                                            [dated(50, 0, parent=None)]], [self.ROOT])
            self.assertEqual(requests, 3)
            self.assertEqual(set(by_id(first)), {str(UUID(int=n)) for n in range(12, 17)})
            # A restart sees only older historical members, not the recent parents.
            second, requests = self.collect([[], [], [dated(n, n) for n in range(11, 8, -1)]],
                                            [self.ROOT], json.loads(json.dumps(first.state)), set(by_id(first)))
            self.assertEqual(requests, 3)
            # The recent parent is absent from every current page. Retained ancestry
            # must recognize its child; older history must not have displaced it.
            third, requests = self.collect([[], [dated(100, 20, parent=16), dated(101, 21)], []],
                                           [self.ROOT], json.loads(json.dumps(second.state)),
                                           set(by_id(first)) | set(by_id(second)))
            self.assertEqual(requests, 3)
        self.assertEqual({m["id"]: (m["thread_id"], m["addressing"]) for m in third.messages},
                         {str(UUID(int=n)): (self.ROOT, "thread") for n in (100, 101)})


if __name__ == "__main__":
    unittest.main()
