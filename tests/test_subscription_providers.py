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
    """The common validator will accept thread_activity; until then check every other rule."""
    copy = deepcopy(batch)
    for message in copy.messages:
        if message["kind"] == "thread_activity":
            message["kind"] = "mention"
    validate(copy)
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
        self.assertEqual(got[uid(314)]["discovery"], "thread")
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
                self.assertNotIn("page", batch.state["subscriptions"]["roots"][uid(root)])
                replay = self.collect(client, batch.state, known=set(got))
                self.assertEqual(replay.messages, [])
                self.assertEqual(replay.state["subscriptions"]["roots"], {uid(root): {}})

    def test_notification_progress_survives_and_unsubscribe_prunes(self):
        client = self.client("moltbook", [500])
        self.thread(client, 500)
        state = {"discovery": {"cursor": "keep"}, "pending": {}, "subscriptions": {"next": None, "roots": {uid(500): {}, uid(777): {}}}}
        batch = self.collect(client, state)
        self.assertEqual(batch.state["subscriptions"]["roots"], {uid(500): {}}, "A dropped root loses only its own entry")
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
        self.assertEqual(by_id(second)[uid(414)]["addressing"], "thread", "Rereading from the head restores a page-one parent")


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
        self.assertEqual(members, {self.ROOT: False, str(UUID(int=12)): True, str(UUID(int=13)): False,
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


class HelperTests(unittest.TestCase):
    def test_rotation_is_stable_when_roots_change(self):
        roots = [uid(3), uid(1), uid(2)]
        entry = {"next": uid(2), "roots": {}}
        self.assertEqual(subscriptions.rotation(entry, roots), [uid(2), uid(3), uid(1)])
        self.assertEqual(subscriptions.rotation(entry, [uid(1), uid(3)]), [uid(3), uid(1)], "A removed cursor root cannot block")
        subscriptions.advance(entry, roots, None)
        self.assertEqual(entry["next"], uid(1))
        with self.assertRaisesRegex(MailError, "invalid_config"):
            subscriptions.selected({"subscriptions": "x"})


if __name__ == "__main__":
    unittest.main()
