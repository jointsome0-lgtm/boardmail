"""Explicit thread subscriptions across all six built-in adapters. Every board response is invented.

Subscribed roots are supplied by the core as ``settings["subscriptions"]``. Other-author
activity in those threads arrives as ``kind: thread_activity``; addressing follows only the
ancestry each provider actually shows. Empty subscriptions must not change a single request.
"""
from copy import deepcopy
import io
from itertools import groupby
import json
import re
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError, URLError
from uuid import UUID

from boardmail import adapter_clawdchat as clawd
from boardmail import adapter_fourclaw as fourclaw
from boardmail import adapter_fruitflies as fruit
from boardmail import addressing, commands, providers, subscriptions
from boardmail.adapters import Batch, collect_all, validate
from boardmail.config import MailError
from boardmail.store import Store
from examples.fixtures import FixtureBoard, named, original, settings, status, uid
from kit import Clock, fixed, mark, new_inbox
from test_clawdchat import Board as ClawdChat, event as clawd_event, key_file, original as clawd_original
from test_fourclaw import THREAD, page as claw_page, post as claw_post, thread as claw_thread, threads as claw_threads
from test_fruitflies import feed as fly_feed, post as fly_post

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

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        # The client waits between two requests, and a pass has its time. Here a wait only moves the clock.
        self.enterContext(fixed(Clock(1790000000)))

    def client(self, threads, subscribed, **extra):
        cfg = {**settings(self.temp.name)["postingboard"], "threads": [uid(n) for n in threads], "subscriptions": [uid(n) for n in subscribed], **extra}
        client = FixtureBoard("postingboard", cfg)
        client.comments[uid(302)] += [named(323, 302, reply_to=313, body="answering your reply"),
                                      named(324, 302, reply_to=315), named(325, 302, reply_to=999)]
        return client

    def collect(self, client, state=None, known=()):
        batch = providers.collect("postingboard", client.settings, deepcopy(state or {}), set(known), fetch=client)
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


class ThreadBoard(FixtureBoard):
    """The invented Colony or Moltbook with public threads: the identity of the account, no notifications, and
    thread pages shaped like the saved live responses (Colony: items/total/has_more/page; Moltbook: cursor tree).

    clock is the clock of the test. fits is how many public requests the time of a pass has room for: the answer
    to the last of them comes as that time ends, so the client sends no further one. None: an answer takes no
    time. per_page is the most comments that the board gives on a page."""

    def __init__(self, source, cfg, clock=None):
        super().__init__(source, cfg)
        self.posts, self.comments, self.failures = {}, {}, {}
        self.colony = source == "the-colony"
        self.clock, self.fits, self.requests = clock, None, 0

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
        self.requests += 1
        if self.requests == self.fits:
            self.clock.advance(providers.SOURCE_SECONDS)
        match = re.fullmatch(r"/posts/([0-9a-f-]{36})(/comments)?", path)
        root = match.group(1)
        if root in self.failures:
            raise self.failures[root]
        if root not in self.posts:
            raise not_found()
        if not match.group(2):
            return deepcopy(self.posts[root]) if self.colony else {"success": True, "post": deepcopy(self.posts[root])}
        items, limit = self.comments.get(root, []), self.limit(params)
        if self.colony:
            assert params["sort"] == "oldest", "Colony requires oldest, not Moltbook's old"
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
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        # A pass has its time. The clock stands still until a board moves it.
        self.clock = Clock(1790000000)
        self.enterContext(fixed(self.clock))

    def client(self, source, roots, **extra):
        cfg = {**settings(self.temp.name)[source], "subscriptions": [uid(n) for n in roots], "mention_aliases": ["@sample"], **extra}
        return ThreadBoard(source, cfg, self.clock)

    def collect(self, client, state=None, known=()):
        batch = providers.collect(client.source, client.settings, deepcopy(state or {}), set(known), fetch=client)
        shape(self, batch)
        return batch

    def others(self, client, root, numbers, parent=None):
        """Comments of another account under a root, as the board keeps them."""
        return [{**original(n, root, colony=client.colony), "parent_id": uid(parent) if parent else None} for n in numbers]

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
            with self.subTest(source=source):
                client = self.client(source, [root])
                client.per_page = 3
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
                    self.assertEqual([p["sort"] for p in pages], ["oldest"] * 3)
                else:
                    self.assertEqual([p["sort"] for p in pages], ["old"] * 3)
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
        client.failures[uid(400)] = status(429)  # The board tells the client to slow down before it gave anything.
        client.calls.clear()
        cut = self.collect(client, batch.state, known=set(by_id(batch)))
        self.assertFalse(cut.complete)
        self.assertEqual(cut.error, "http_429")
        self.assertEqual(cut.state["subscriptions"]["next"], uid(400), "Resume at the interrupted root")
        self.assertEqual([c[0] for c in client.calls if c[0].startswith("/posts/")], ["/posts/" + uid(400)])

    def test_slow_root_that_spends_the_budget_cannot_starve_another_root(self):
        for source in ('the-colony', 'moltbook'):
            with self.subTest(source=source):
                client = self.client(source, [400, 401])
                self.thread(client, 401)
                real = client.get

                def slow(path, params=None, **kwargs):
                    if path == '/posts/' + uid(400):
                        # The root has not answered when the time of the pass is over, and the transport says so.
                        self.clock.advance(providers.SOURCE_SECONDS + 1)
                        raise MailError('source_timeout')
                    return real(path, params, **kwargs)

                client.get = slow
                first = self.collect(client)
                second = self.collect(client, first.state)
                self.assertFalse(first.complete)
                self.assertEqual(first.state['subscriptions']['next'], uid(401))
                self.assertIn(uid(412), by_id(second), 'The slow root must give another root a turn')

    def test_cut_off_pagination_still_delivers_what_was_read(self):
        client = self.client("the-colony", [400])
        self.thread(client, 400)
        client.per_page, client.fits = 3, 2  # The time of the pass is over after the root and the first page of three.
        batch = self.collect(client)
        self.assertFalse(batch.complete)
        self.assertIsNone(batch.error)
        self.assertEqual(set(by_id(batch)), {uid(411), uid(413)}, "First page delivered; own comment 412 is context")
        self.assertEqual([p["page"] for path, p, _ in client.calls if path.endswith("/comments")], [1])
        client.fits = None
        second = self.collect(client, batch.state, known=set(by_id(batch)))
        self.assertEqual(set(by_id(second)), {uid(n) for n in (414, 415, 416)})
        self.assertEqual(by_id(second)[uid(414)]["addressing"], "thread", "A page-one parent's ownership was retained")
        self.assertEqual(batch.state["subscriptions"]["roots"][uid(400)]["page"], 2, "The cut pass saved its next page")
        self.assertEqual(batch.state["subscriptions"]["next"], uid(400), "A single root simply resumes")
        self.assertNotIn("page", second.state["subscriptions"]["roots"][uid(400)], "A finished cycle restarts at the head")

    def test_colony_page_cap_keeps_progress_until_the_real_end(self):
        client = self.client("the-colony", [400])
        self.thread(client, 400)
        # The board gives one comment on a page, and a pass reads 100 pages of a thread at most. So 201 comments
        # take three passes.
        client.per_page, client.comments[uid(400)] = 1, self.others(client, 400, range(1000, 1201))
        state, known, got = {}, set(), []
        for number in (1, 2, 3):
            batch = self.collect(client, state, known)
            got.extend(m["id"] for m in batch.messages)
            known.update(got)
            state = json.loads(json.dumps(batch.state))  # A resumed collector sees saved state.
            progress = state["subscriptions"]["roots"][uid(400)]
            self.assertEqual(batch.complete, number == 3)
            self.assertEqual(progress.get("page"), 100*number+1 if number < 3 else None)
        replay = self.collect(client, state, known)
        self.assertEqual(got, [uid(n) for n in range(1000, 1201)])
        self.assertEqual(replay.messages, [])
        self.assertEqual([p["page"] for path, p, _ in client.calls if path.endswith("/comments")],
                         [*range(1, 202), *range(1, 101)])

    def test_colony_invalid_continuation_never_claims_a_finished_scan(self):
        for values, error in (({"page": 1}, "pagination_no_progress"),
                              ({"items": [], "has_more": True}, "pagination_no_progress"),
                              ({"page": True}, "invalid_response"),
                              ({"has_more": "false"}, "invalid_response")):
            with self.subTest(values=values):
                client = self.client("the-colony", [400])
                self.thread(client, 400)
                real = client.get
                def malformed(path, params=None, **kw):
                    response = real(path, params, **kw)
                    return {**response, **values} if path.endswith("/comments") else response
                client.get = malformed
                state = {"subscriptions": {"next": None, "roots": {uid(400): {"page": 2}}}}
                batch = self.collect(client, state)
                self.assertEqual(batch.error, error)
                self.assertFalse(batch.complete)
                self.assertEqual(batch.messages, [], "No data from an unvalidated page enters the inbox")
                if error == "pagination_no_progress":
                    self.assertNotIn("page", batch.state["subscriptions"]["roots"][uid(400)])
                    client.get = real
                    self.assertEqual(len(self.collect(client, batch.state).messages), 5)

    def test_colony_overlapping_pages_and_restart_preserve_read_and_replied(self):
        client = self.client("the-colony", [400])
        self.thread(client, 400)
        client.comments[uid(400)].insert(3, deepcopy(client.comments[uid(400)][0]))
        client.per_page = 3
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "mail.sqlite3"
            store = new_inbox(database, {client.source: client.settings})
            commands.execute(store, "subscribe", sources={client.source: client.settings}, source=client.source, thread=uid(400))
            def collect(db):
                client.requests, client.fits = 0, 2  # The time of a pass is over after the root and one page.
                return collect_all(db, {client.source: client.settings}, fetch=client)
            self.assertEqual(collect(store)["added"], 2)
            mark(store, client.source, uid(411), "read")
            mark(store, client.source, uid(411), "replied", ref="https://thecolony.ai/posts/" + uid(400) + "#comment-" + uid(999))
            marked = store.show(client.source, uid(411))
            self.assertEqual(collect(Store(database))["added"], 2)
            self.assertEqual(collect(Store(database))["added"], 1)
            self.assertEqual(collect(Store(database))["added"], 0)
            self.assertEqual(Store(database).show(client.source, uid(411)), marked)

    def passes(self, client, roots, limit, count):
        """Repeated passes whose time has room for the same number of public requests; every message once."""
        state, known, got, nexts = {}, set(), {}, []
        for _ in range(count):
            client.requests, client.fits = 0, limit
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
            with self.subTest(source=source):
                client = self.client(source, [root, root + 1])
                client.per_page = 3
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
                client.requests, client.fits = 0, 2
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
            with self.subTest(source=source):
                client = self.client(source, [400])
                self.thread(client, 400)
                # 400 more comments of others: as many as the ownership that a root keeps.
                if client.colony:
                    # Pages of 100. When the last one is read, the root and the seven comments have left the map.
                    client.comments[uid(400)] += self.others(client, 400, range(1000, 1400))
                else:
                    # One page with a tree under its first comment. The seven comments leave the map before the
                    # first of them is looked at.
                    client.comments[uid(400)].insert(0, {**self.others(client, 400, [999])[0],
                                                         'replies': self.others(client, 400, range(1000, 1400), parent=999)})
                batch = self.collect(client)
                got = by_id(batch)
                self.assertEqual(len(batch.state['subscriptions']['roots'][uid(400)]['owners']), subscriptions.MAX_OWNERS)
                self.assertNotIn(uid(412), got)
                self.assertNotIn(uid(417), got)
                self.assertEqual(got[uid(411)]['addressing'], 'thread', 'The root was verified in this very pass')
                if client.colony:
                    self.assertEqual(got[uid(1399)]['addressing'], 'thread', 'And it still is on the last page')

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
        # 400 comments of others, as many as the ownership that a root keeps, and then two late replies.
        client.comments[uid(400)] += [*self.others(client, 400, range(1000, 1400)), *self.others(client, 400, [1400], parent=411),
                                      *self.others(client, 400, [1401], parent=412), *self.others(client, 400, [1402], parent=1399)]
        batch = self.collect(client)
        got = by_id(batch)
        self.assertEqual(got[uid(413)]["addressing"], "direct", "Parent 412 was fetched on the same page")
        self.assertIsNone(got[uid(1400)]["addressing"], "Parent 411 left the bounded map: unknown, never invented")
        self.assertIsNone(got[uid(1401)]["addressing"], "So did our own comment 412")
        self.assertEqual(got[uid(1402)]["addressing"], "thread", "Parent 1399 is still in it")
        self.assertEqual(len(batch.state["subscriptions"]["roots"][uid(400)]["owners"]), subscriptions.MAX_OWNERS)


class ClawdThreads(ClawdChat):
    """Adds the public comment listing with depth cut-offs and parent pages.

    page is the most comments that the board gives on one page, however many a request asks for. None: as many
    as the request asks for."""

    def __init__(self):
        super().__init__()
        self.threads, self.children, self.page = {}, {}, None

    def most(self, limit):
        return min(limit, self.page or limit)

    def get(self, path, params, *, authenticated):
        match = re.fullmatch(r"/posts/([0-9a-f-]{36})/comments", path)
        if not match:
            return super().get(path, params, authenticated=authenticated)
        assert not authenticated
        root = match.group(1)
        if root not in self.threads:
            return 404
        nodes = self.children[params["parent_id"]] if params.get("parent_id") else self.threads[root]
        skip = params.get("skip", 0)
        selected = nodes[skip:skip + self.most(params["limit"])]
        return {"success": True, "comments": selected, "total": len(nodes), "comment_count": 0,
                "returned_count": len(selected), "max_depth": params.get("max_depth", 2), "parent_id": params.get("parent_id")}


class DepthLimitedClawd(ClawdThreads):
    """One finite tree; every response expands exactly the requested relative depth.

    Pagination selects only this page's roots. Counts use the entire underlying
    tree, including descendants hidden by the depth boundary. No shallow reply is
    omitted, and no saved provider progress is injected into the collector.
    """

    def __init__(self):
        super().__init__()
        self.originals[uid(100)] = clawd_original(100, title="Finite deep tree")
        self.forest, self.nodes, self.edges = [], {}, {}
        next_id = 1000

        def chain(parent, length):
            nonlocal next_id
            first = None
            for _ in range(length):
                mid = uid(next_id)
                next_id += 1
                self.nodes[mid] = clawd_original(next_id - 1, parent_id=parent)
                self.edges[mid] = []
                if parent is None:
                    self.forest.append(mid)
                else:
                    self.edges[parent].append(mid)
                first = first or mid
                parent = mid
            return first, parent

        # Twenty depth-20 cut parents fill the ordinary queue. Their parent
        # listings each expose two chains through depth 40. Those cut nodes still
        # have descendants through depth 61, so the finite frontier can need more
        # work before any queued unit reaches a leaf.
        for _ in range(20):
            _, parent = chain(None, 20)
            chain(parent, 41)
            chain(parent, 41)
        self.originals[uid(100)]["comment_count"] = len(self.nodes)

    def get(self, path, params, *, authenticated):
        if not path.endswith("/comments"):
            return super().get(path, params, authenticated=authenticated)
        assert not authenticated
        assert path == "/posts/" + uid(100) + "/comments"
        depth, skip, limit = params["max_depth"], params["skip"], self.most(params["limit"])
        assert depth == 20 and skip >= 0 and limit > 0
        parent = params.get("parent_id")
        roots = self.edges[parent] if parent is not None else self.forest

        def expand(mid, remaining):
            children = self.edges[mid]
            replies = [expand(child, remaining - 1) for child in children] if remaining > 1 else []
            return {**self.nodes[mid], "replies": replies, "reply_count": len(children),
                    "has_more_replies": len(replies) < len(children)}

        selected = [expand(mid, depth) for mid in roots[skip:skip + limit]]

        def count(nodes):
            return sum(1 + count(node["replies"]) for node in nodes)

        return {"success": True, "comments": selected, "total": len(roots),
                "comment_count": len(self.nodes), "returned_count": count(selected),
                "max_depth": depth, "parent_id": parent}


class DefensiveClawd(DepthLimitedClawd):
    """Small finite trees with the same depth/count contract, plus explicit faults.

    Keys select one head or direct-child page, not an entire root. A fault that is a number is the HTTP status
    that the board answers that page with. Empty faults deliberately contradict that page's remaining total;
    ordinary responses still come from the complete edge map and expand all shallow replies.
    """

    def __init__(self):
        ClawdThreads.__init__(self)
        self.originals[uid(100)] = clawd_original(100, title="Defensive finite tree")
        self.forest, self.nodes, self.edges, self.faults = [], {}, {}, {}

    def chain(self, first, length, parent=None):
        for number in range(first, first + length):
            mid = uid(number)
            self.nodes[mid] = clawd_original(number, parent_id=parent)
            self.edges[mid] = []
            if parent is None:
                self.forest.append(mid)
            else:
                self.edges[parent].append(mid)
            parent = mid
        self.originals[uid(100)]["comment_count"] = len(self.nodes)
        return parent

    def get(self, path, params, *, authenticated):
        raw = super().get(path, params, authenticated=authenticated)
        if not path.endswith("/comments"):
            return raw
        fault = self.faults.get((params.get("parent_id"), params["skip"]))
        return fault if type(fault) is int else {**raw, **(fault or {})}


def node(n, parent=None, author=2, replies=(), more=False, **changes):
    return {**clawd_original(n, parent_id=uid(parent) if parent else None, author={"id": uid(author), "name": "agent-" + str(author)}, **changes),
            "replies": list(replies), "reply_count": len(replies) + (1 if more else 0), "has_more_replies": more}


def seconds(board):
    """The seconds that each phase of a pass had for a request, in the order of the phases. The clock of the pass
    must stand still."""
    return [left for left, _ in groupby(asked.left for asked in board.asked)]


def listings(board):
    """The query of each request for a page of comments."""
    return [params for path, params, _ in board.calls if path.endswith("/comments")]


class ClawdChatSubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.key = key_file(self)

    def collect(self, board, subscribed, state=None, known=(), step=0):
        """One pass over an invented board. With a step the clock moves by that many seconds each time the pass
        looks at it, so the eleven seconds that the pass has for subscribed threads are over after a few requests:
        after two of them at a step of 4, after three at a step of 3, and after ten at a step of 1."""
        cfg = {"account_id": uid(1), "api_key_file": self.key, "subscriptions": [uid(n) for n in subscribed],
               "mention_aliases": ["sample"]}
        before = json.dumps(state or {})
        with fixed(Clock(1790000000, step)):
            batch = clawd.collect(cfg, state or {}, frozenset(known), fetch=board)
        self.assertEqual(json.dumps(state or {}), before, "Input state is a snapshot")
        shape(self, batch)
        return batch

    def thread(self, board):
        board.originals[uid(100)] = clawd_original(100, title="Thread", author={"id": uid(2), "name": "host"})
        deep = node(16, parent=15)
        board.threads[uid(100)] = [node(11), node(12, author=1, replies=[node(13, parent=12)]),
                                   node(14, parent=11), node(15, more=True), node(17, content="@sample look here")]
        board.children[uid(15)] = [deep]

    def test_listed_tree_deep_children_and_shown_ownership(self):
        board = ClawdThreads()
        self.thread(board)
        batch = self.collect(board, [100])
        self.assertIsNone(batch.error)
        got = by_id(batch)
        self.assertEqual({int(UUID(k)): (m["kind"], m["addressing"]) for k, m in got.items()},
                         {11: ("thread_activity", "thread"), 13: ("thread_activity", "direct"), 14: ("thread_activity", "thread"),
                          15: ("thread_activity", "thread"), 16: ("thread_activity", "thread"), 17: ("thread_activity", "mention")})
        self.assertEqual({o["id"] for o in batch.originals}, {uid(100), uid(12)})
        listing = listings(board)
        self.assertEqual(listing[0]["max_depth"], 20)
        self.assertEqual(listing[1]["parent_id"], uid(15), "Cut replies are fetched by parent page")
        self.assertEqual(batch.state["subscriptions"]["roots"][uid(100)], {"skip": 0, "pending": []})
        self.assertTrue(batch.complete)
        replay = self.collect(board, [100], batch.state, known=set(got))
        self.assertEqual(replay.messages, [])

    def test_busy_notifications_leave_time_and_requests_for_subscriptions(self):
        board = ClawdThreads()
        self.thread(board)
        board.events = [clawd_event(n) for n in range(200, 260)]
        for n in range(200, 260):
            board.originals[uid(n)] = clawd_original(n)
        state = {"offset": 0, "pending": [{"id": uid(n), "post": uid(100), "kind": "reply_to_post", "is_post": False, "types": ["comment"]} for n in range(300, 340)]}
        for n in range(300, 340):
            board.originals[uid(n)] = clawd_original(n)
        batch = self.collect(board, [100], state)
        self.assertEqual(seconds(board), [5, 12, 5, 12, 11], "Identity, shorter notification phases, then the reserve")
        self.assertLessEqual(len(board.asked), clawd.MAX_REQUESTS)
        self.assertTrue(listings(board), "Subscription still ran")
        self.assertIn(uid(11), by_id(batch))
        self.assertTrue(batch.state["pending"], "Notification backlog is retained for the next pass")
        idle = ClawdThreads()
        idle.events, idle.originals = list(board.events), dict(board.originals)
        without = self.collect(idle, [], deepcopy(state))
        self.assertEqual(seconds(idle), [5, 15, 5, 15], "Without subscriptions the pass is unchanged")
        self.assertNotIn("subscriptions", without.state)
        self.assertEqual(listings(idle), [])
        # Originals that the board does not give take all thirty requests that notification work has. The ten
        # that are kept for subscribed threads are still there: the root, its head page and one parent page.
        down = ClawdThreads()
        self.thread(down)
        down.events = list(board.events)
        for n in (*range(200, 260), *range(300, 340)):
            down.originals[uid(n)] = URLError("The board is not reached")
        batch = self.collect(down, [100], state)
        self.assertEqual(batch.error, "network_error")
        root = "/posts/" + uid(100)
        self.assertEqual([path for path, _, _ in down.calls[30:]], [root, root + "/comments", root + "/comments"])
        self.assertTrue({uid(11), uid(16)} <= set(by_id(batch)))

    def test_cut_off_scan_resumes_with_parent_ownership(self):
        board = ClawdThreads()
        self.thread(board)
        # The root and one listing page fit in the time; the parent page does not.
        batch = self.collect(board, [100], step=4)
        self.assertFalse(batch.complete)
        self.assertIsNone(batch.error, "A spent budget is incomplete progress, not a failed operation")
        self.assertIn(uid(11), by_id(batch))
        self.assertNotIn(uid(16), by_id(batch))
        progress = batch.state["subscriptions"]["roots"][uid(100)]
        self.assertEqual(progress["pending"], [[uid(15), 0, False]])
        again = ClawdThreads()
        self.thread(again)
        second = self.collect(again, [100], batch.state, known=set(by_id(batch)))
        self.assertEqual(set(by_id(second)), {uid(16)})
        self.assertEqual(by_id(second)[uid(16)]["addressing"], "thread")
        self.assertEqual(second.state["subscriptions"]["roots"][uid(100)]["pending"], [])

    def passes(self, board, subscribed, count, step):
        state, known, got, nexts = {}, set(), {}, []
        for _ in range(count):
            board.calls.clear()
            batch = self.collect(board, subscribed, state, known, step=step)
            for message in batch.messages:
                self.assertNotIn(message["id"], got, "No message is delivered twice")
                got[message["id"]] = message
            known |= set(got)
            state = batch.state
            nexts.append(state["subscriptions"]["next"])
        return got, state, nexts

    def test_repeated_identical_budgets_reach_deep_children_and_another_root(self):
        board = ClawdThreads()
        self.thread(board)
        board.originals[uid(101)] = clawd_original(101, title="Healthy", author={"id": uid(3), "name": "other"})
        board.threads[uid(101)] = [node(21, author=3, post_id=uid(101))]
        board.page = 3
        # The root and one listing fit in the time of each pass.
        got, state, nexts = self.passes(board, [100, 101], count=5, step=4)
        self.assertEqual({int(UUID(k)): (m["kind"], m["addressing"]) for k, m in got.items()},
                         {11: ("thread_activity", "thread"), 13: ("thread_activity", "direct"), 14: ("thread_activity", "thread"),
                          15: ("thread_activity", "thread"), 16: ("thread_activity", "thread"), 17: ("thread_activity", "mention"),
                          21: ("thread_activity", "thread")})
        self.assertEqual(nexts[:3], [uid(101), uid(100), uid(101)], "The spent root waits a turn; the untouched one keeps it")
        self.assertEqual(state["subscriptions"]["roots"][uid(100)], {"skip": 0, "pending": []})
        self.assertEqual(listings(board), [{"parent_id": uid(15), "max_depth": 20, "limit": 20, "skip": 0}],
                         "The final pass drained the saved parent before any head page")

    def test_missing_parent_field_is_not_a_top_level_reply(self):
        board = ClawdThreads()
        board.originals[uid(100)] = clawd_original(100, title="Ours", author={"id": uid(1), "name": "me"})
        bare = node(11)
        del bare["parent_id"]
        board.threads[uid(100)] = [bare, node(12)]
        got = by_id(self.collect(board, [100]))
        self.assertEqual((got[uid(11)]["addressing"], got[uid(12)]["addressing"]), (None, "direct"))

    def test_saved_parent_work_is_drained_before_the_head_is_rescanned(self):
        board = ClawdThreads()
        self.thread(board)
        state = {"subscriptions": {"next": None, "roots": {uid(100): {"skip": 0, "done": True, "pending": [[uid(15), 0, False]]}}}}
        batch = self.collect(board, [100], state)
        self.assertEqual(set(by_id(batch)), {uid(16)})
        self.assertEqual([p.get("parent_id") for p in listings(board)], [uid(15)])
        self.assertTrue(batch.complete)
        self.assertEqual(batch.state["subscriptions"]["roots"][uid(100)], {"skip": 0, "pending": []})

    def test_nested_queue_overflow_retains_the_parent_page_between_passes(self):
        board = ClawdThreads()
        board.originals[uid(100)] = clawd_original(100, title='Thread', author={'id': uid(2), 'name': 'host'})
        # The page of comment 11 shows 21 replies that have more below them: one more than the queue of twenty takes.
        cut = range(20, 41)
        board.threads[uid(100)] = [node(11, more=True)]
        board.children = {uid(11): [node(12, parent=11, replies=[node(n, parent=12, more=True) for n in cut])],
                          **{uid(n): [node(n + 100, parent=n)] for n in cut}}
        state, known, batches = {}, set(), []
        for _ in range(3):
            # A pass has the time for its root and nine pages.
            batch = self.collect(board, [100], state, known, step=1)
            known.update(by_id(batch))
            state = batch.state
            batches.append(batch)
        self.assertEqual([batch.complete for batch in batches], [False, False, True],
                         'The unserved branch still belongs to this scan')
        self.assertEqual(batches[1].state['subscriptions']['roots'][uid(100)]['deferred'], [[uid(11), 0, False]],
                         'The page waits for the replies that it queued')
        self.assertEqual(known, {uid(n) for n in (11, 12, *cut, *(n + 100 for n in cut))})
        parents = [params.get('parent_id') for params in listings(board)]
        self.assertEqual((parents.count(None), parents.count(uid(11))), (1, 2),
                         'Saved work completes before another head scan')

    def test_depth_limited_finite_tree_drains_from_empty_state_at_default_bounds(self):
        self.assert_finite_tree_drains(DepthLimitedClawd())

    def test_depth_limited_tree_preserves_parent_pagination_between_passes(self):
        board = DepthLimitedClawd()
        board.page = 1
        self.assert_finite_tree_drains(board)
        self.assertTrue(any(params.get("parent_id") and params["skip"] == 1 for params in listings(board)))

    def test_deferred_parent_keeps_ownership_and_next_offset_across_restart(self):
        board = DepthLimitedClawd()
        parent = board.forest[0]
        for _ in range(19):
            parent = board.edges[parent][0]
        board.nodes[parent]["author"] = {"id": uid(1), "name": "reader"}
        second_child = board.edges[parent][1]
        state, known, retained = {}, set(), False
        board.page = 1
        for _ in range(8):
            batch = self.collect(board, [100], state, known, step=4)
            self.assertIsNone(batch.error)
            known.update(by_id(batch))
            state = json.loads(json.dumps(batch.state))
            pages = state["subscriptions"]["roots"][uid(100)].get("deferred", [])
            retained = retained or [parent, 1, True] in pages
            if second_child in by_id(batch):
                self.assertEqual(by_id(batch)[second_child]["addressing"], "direct")
                break
        self.assertTrue(retained, "The next direct-child page retains its parent's ownership")
        self.assertIn(second_child, known)

    def assert_finite_tree_drains(self, board):
        self.assertEqual(len(board.nodes), 2040)
        state, known, completed = {}, set(), False
        for _ in range(12):
            board.asked.clear()
            batch = self.collect(board, [100], state, known)
            self.assertIsNone(batch.error)
            self.assertLessEqual(len(board.asked), clawd.MAX_REQUESTS)
            delivered = set(by_id(batch))
            self.assertFalse(delivered & known, "Saved arrivals are not delivered twice")
            known.update(delivered)
            state = json.loads(json.dumps(batch.state))
            progress = state["subscriptions"]["roots"][uid(100)]
            self.assertLessEqual(len(progress["pending"]), clawd.MAX_PENDING_PARENTS)
            self.assertLessEqual(len(progress.get("deferred", [])), clawd.MAX_SERVED)
            queued = [entry[0] for entry in progress["pending"] + progress.get("deferred", [])]
            self.assertEqual(len(queued), len(set(queued)), "Work units are not duplicated across queues")
            if batch.complete:
                completed = True
                break
        self.assertTrue(completed, "The finite depth-respecting tree must finish under identical budgets")
        self.assertEqual(known, set(board.nodes), "Every finite branch must be collected")
        self.assertEqual(state["subscriptions"]["roots"][uid(100)], {"skip": 0, "pending": []})

    def wide(self, cut, ours=None):
        """A board whose thread 100 shows comment 11 with these replies on its head page. Below each reply is one
        more comment, on a page of its own. Its number is that of the reply plus 1000."""
        board = ClawdThreads()
        board.originals[uid(100)] = clawd_original(100, title="Thread", author={"id": uid(2), "name": "host"})
        board.threads[uid(100)] = [node(11, replies=[node(n, parent=11, author=1 if n == ours else 2, more=True) for n in cut])]
        for n in cut:
            board.children[uid(n)] = [node(n + 1000, parent=n)]
        return board

    def test_full_parent_queue_retains_the_page_until_its_children_are_serviced(self):
        # One reply more than the queue of twenty takes. Reply 30 is ours.
        cut = range(20, 41)
        board = self.wide(cut, ours=30)
        batch = self.collect(board, [100])
        self.assertIsNone(batch.error)
        self.assertTrue(batch.complete)
        got = by_id(batch)
        self.assertEqual({int(UUID(k)): m["addressing"] for k, m in got.items()},
                         {11: "thread", **{n: "thread" for n in cut if n != 30},
                          **{n + 1000: "direct" if n == 30 else "thread" for n in cut}})
        listing = [(p.get("parent_id"), p["skip"]) for p in listings(board)]
        self.assertEqual(listing, [(None, 0), *[(uid(n), 0) for n in reversed(cut[1:])], (None, 0), (uid(20), 0)],
                         "The head page is reread at the same offset until every cut parent was queued")
        self.assertEqual(batch.state["subscriptions"]["roots"][uid(100)], {"skip": 0, "pending": []})
        # A page may queue 200 parents and is then read once more. With 221 it is over that bound.
        cut = range(200, 421)
        board = self.wide(cut)
        state, known, errors = {}, set(), []
        for _ in range(7):
            bounded = self.collect(board, [100], state, known)
            known.update(by_id(bounded))
            state = bounded.state
            errors.append(bounded.error)
        self.assertEqual(errors, [None] * 5 + ["pending_overflow", None],
                         "Beyond the bound the page is consumed with an explicit error")
        self.assertEqual(known, {uid(n) for n in (11, *cut, *(n + 1000 for n in cut[1:]))},
                         "Only the branch beyond the bound is missing, and the error says so")
        self.assertTrue(bounded.complete)

    def test_interrupted_overflow_state_is_resumed_without_duplicate_parents(self):
        board = ClawdThreads()
        board.originals[uid(100)] = clawd_original(100, title="Thread", author={"id": uid(2), "name": "host"})
        board.threads[uid(100)] = [node(11, more=True), node(12, more=True)]
        board.children[uid(11)], board.children[uid(12)] = [node(31, parent=11)], [node(32, parent=12)]
        # The head page was read and parent 11 queued when the pass ended.
        state = {"subscriptions": {"next": None, "roots": {uid(100): {"skip": 0, "pending": [[uid(11), 0, False]], "served": {"head": [uid(11)]}}}}}
        batch = self.collect(board, [100], state)
        self.assertEqual(set(by_id(batch)), {uid(11), uid(12), uid(31), uid(32)})
        self.assertEqual([p.get("parent_id") for p in listings(board)], [uid(11), None, uid(12)])
        self.assertEqual(batch.state["subscriptions"]["roots"][uid(100)], {"skip": 0, "pending": []})

    def test_missing_root_and_rotation_over_several_roots(self):
        board = ClawdThreads()
        self.thread(board)
        batch = self.collect(board, [100, 101])
        self.assertEqual(batch.unavailable, 1)
        self.assertIn(uid(11), by_id(batch))
        self.assertEqual(batch.state["subscriptions"]["next"], uid(100))

    def test_unavailable_child_retires_only_its_unit_and_retries_next_cycle(self):
        for status in (403, 404, 410):
            with self.subTest(status=status):
                board = DefensiveClawd()
                board.chain(101, 21)
                board.chain(201, 21)
                board.chain(301, 1)
                board.faults[(uid(220), 0)] = status
                board.page = 2
                batch = self.collect(board, [100])
                self.assertEqual(set(by_id(batch)), set(board.nodes) - {uid(221)},
                                 "The independent parent and later head page still drain")
                self.assertEqual((batch.error, batch.complete, batch.unavailable), ("http_" + str(status), False, 1))
                self.assertIn(uid(100), {item["id"] for item in batch.originals})
                self.assertEqual(batch.state["subscriptions"]["roots"][uid(100)], {"skip": 0, "pending": []})
                board.faults.clear()
                retry = self.collect(board, [100], json.loads(json.dumps(batch.state)), set(by_id(batch)))
                self.assertEqual(set(by_id(retry)), {uid(221)})
                self.assertEqual((retry.error, retry.complete, retry.unavailable), (None, True, 0))

    def test_child_unavailability_keeps_healthy_root_offset_and_sibling_checkpoint(self):
        board = DefensiveClawd()
        board.chain(101, 21)
        board.chain(201, 21)
        board.chain(301, 1)
        board.faults[(uid(220), 0)] = 404
        board.page = 2
        # The time is over after the root, the first head page and the page that is not there.
        batch = self.collect(board, [100], step=3)
        progress = batch.state["subscriptions"]["roots"][uid(100)]
        self.assertEqual(progress, {"skip": 2, "pending": [[uid(120), 0, False]]})
        self.assertEqual((batch.error, batch.complete, batch.unavailable), ("http_404", False, 1))
        board.calls.clear()
        resumed = self.collect(board, [100], json.loads(json.dumps(batch.state)), set(by_id(batch)))
        self.assertEqual(set(by_id(resumed)), {uid(121), uid(301)})
        self.assertEqual([p.get("parent_id") for p in listings(board)],
                         [uid(120), None], "The failed parent is not retried ahead of healthy saved work")
        self.assertTrue(resumed.complete)

    def test_unavailable_deferred_child_page_does_not_block_later_head(self):
        board = DefensiveClawd()
        parent = board.chain(101, 20)
        board.chain(121, 1, parent)
        board.chain(122, 1, parent)
        board.chain(301, 1)
        board.faults[(parent, 1)] = 410
        board.page = 1
        batch = self.collect(board, [100])
        self.assertEqual(set(by_id(batch)), set(board.nodes) - {uid(122)})
        self.assertEqual((batch.error, batch.complete, batch.unavailable), ("http_410", False, 1))
        listing = [(p.get("parent_id"), p["skip"]) for p in listings(board)]
        self.assertEqual(listing, [(None, 0), (parent, 0), (parent, 1), (None, 1)])

    def test_unavailable_root_still_resets_only_that_root(self):
        for status in (403, 404, 410):
            with self.subTest(status=status):
                board = DefensiveClawd()
                board.chain(11, 1)
                board.originals[uid(101)] = status
                state = {"subscriptions": {"next": uid(101), "roots": {
                    uid(101): {"skip": 7, "pending": [[uid(12), 0, False]]}}}}
                batch = self.collect(board, [101, 100], state)
                self.assertEqual(set(by_id(batch)), {uid(11)})
                self.assertEqual(batch.state["subscriptions"]["roots"][uid(101)], {})
                self.assertEqual((batch.error, batch.complete, batch.unavailable), (None, True, 1))

    def test_empty_nonterminal_head_retains_offset_and_resumes_without_duplicates(self):
        board = DefensiveClawd()
        for number in (11, 12, 13):
            board.chain(number, 1)
        board.faults[(None, 1)] = {"comments": [], "returned_count": 0}
        board.page = 1
        batch = self.collect(board, [100])
        self.assertEqual(set(by_id(batch)), {uid(11)})
        self.assertEqual((batch.error, batch.complete), ("pagination_no_progress", False))
        self.assertEqual(batch.state["subscriptions"]["roots"][uid(100)], {"skip": 1, "pending": []})
        board.faults.clear()
        retry = self.collect(board, [100], json.loads(json.dumps(batch.state)), set(by_id(batch)))
        self.assertEqual(set(by_id(retry)), {uid(12), uid(13)})
        self.assertEqual((retry.error, retry.complete), (None, True))

    def test_empty_nonterminal_parent_retains_active_or_deferred_position_and_ownership(self):
        for offset in (0, 1):
            with self.subTest(offset=offset):
                board = DefensiveClawd()
                parent = board.chain(101, 20)
                board.nodes[parent]["author"] = {"id": uid(1), "name": "me"}
                board.chain(121, 1, parent)
                board.chain(122, 1, parent)
                board.chain(301, 1)
                board.faults[(parent, offset)] = {"comments": [], "returned_count": 0}
                board.page = 1
                batch = self.collect(board, [100])
                self.assertEqual((batch.error, batch.complete), ("pagination_no_progress", False))
                progress = batch.state["subscriptions"]["roots"][uid(100)]
                self.assertEqual(progress["skip"], 1)
                self.assertEqual(progress["pending"] + progress.get("deferred", []), [[parent, offset, True]])
                self.assertEqual(bool(progress.get("deferred")), offset == 1)
                board.faults.clear()
                retry = self.collect(board, [100], json.loads(json.dumps(batch.state)), set(by_id(batch)))
                self.assertEqual(set(by_id(retry)), {uid(121), uid(122), uid(301)} - set(by_id(batch)))
                self.assertEqual(by_id(retry)[uid(122)]["addressing"], "direct")
                self.assertEqual((retry.error, retry.complete), (None, True))

    def test_empty_terminal_pages_finish_when_counts_shrink(self):
        board = DefensiveClawd()
        empty = self.collect(board, [100])
        self.assertEqual((empty.error, empty.complete, empty.messages), (None, True, []))
        for parent_page in (False, True):
            with self.subTest(parent_page=parent_page):
                board = DefensiveClawd()
                if parent_page:
                    parent = board.chain(101, 20)
                    board.chain(121, 1, parent)
                    board.chain(122, 1, parent)
                else:
                    board.chain(121, 1)
                    board.chain(122, 1)
                board.page = 1
                # The time is over before the page that would show comment 122.
                first = self.collect(board, [100], step=3 if parent_page else 4)
                self.assertFalse(first.complete)
                self.assertIsNone(first.error)
                if parent_page:
                    board.edges[parent].remove(uid(122))
                else:
                    board.forest.remove(uid(122))
                del board.nodes[uid(122)], board.edges[uid(122)]
                board.originals[uid(100)]["comment_count"] = len(board.nodes)
                board.calls.clear()
                final = self.collect(board, [100], json.loads(json.dumps(first.state)), set(by_id(first)))
                self.assertEqual((final.error, final.complete, final.messages), (None, True, []))
                listing = listings(board)
                self.assertEqual(listing[0]["skip"], 1, "The checkpoint is tested against a now-terminal empty page")
                self.assertEqual(listing[0].get("parent_id"), parent if parent_page else None)

    def test_parent_page_totals_use_the_same_validation_as_head_pages(self):
        for parent_page in (False, True):
            for total in (-1, True, 2**63, "1"):
                with self.subTest(parent_page=parent_page, total=total):
                    board = DefensiveClawd()
                    parent = board.chain(101, 21)
                    key = (uid(120), 0) if parent_page else (None, 0)
                    board.faults[key] = {"total": total}
                    batch = self.collect(board, [100])
                    self.assertEqual((batch.error, batch.complete), ("invalid_response", False))
                    progress = batch.state["subscriptions"]["roots"][uid(100)]
                    if parent_page:
                        self.assertEqual(progress["pending"], [[uid(120), 0, False]])
                    else:
                        self.assertEqual(progress, {"skip": 0, "pending": []})


class FourclawSubscriptionTests(unittest.TestCase):
    OTHER = "00000000-0000-4000-8000-000000000002"

    def collect(self, pages, subscribed, state=None, known=frozenset(), **cfg):
        board = claw_threads(pages)
        batch = fourclaw.collect(dict(account_id="Reader", watched_threads=[THREAD], subscriptions=subscribed, **cfg),
                                 state or {}, known, fetch=board)
        shape(self, batch)
        return batch, [claw_thread(asked) for asked in board.asked]

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
        board = claw_threads(pages)
        first = fourclaw.collect(dict(account_id="Reader", watched_threads=threads, subscriptions=[extra]),
                                 {"next_thread": 98}, frozenset(), fetch=board)
        shape(self, first)
        self.assertIsNone(first.error, "101 runtime roots do not violate the configured cap of 100")
        self.assertEqual([m["body"] for m in first.messages], ["subscribed activity"])
        self.assertEqual(first.state, {"next_thread": 1})
        second = fourclaw.collect(dict(account_id="Reader", watched_threads=threads, subscriptions=[]), first.state,
                                  frozenset(), fetch=board)
        self.assertEqual(second.state, {"next_thread": 5})

    def test_invalid_subscription_value_is_a_config_error(self):
        pages = {THREAD: claw_page(replies=[claw_post("Other", "@Reader hi")])}
        batch, fetched = self.collect(pages, ["not-a-uuid"])
        self.assertEqual((batch.error, fetched), ("invalid_config", []))

    def test_subscription_only_setup_without_watched_threads(self):
        pages = {self.OTHER: claw_page(replies=[claw_post("Other", "untagged reply")])}
        for cfg in ({"account_id": "Reader", "subscriptions": [self.OTHER]},
                    {"account_id": "Reader", "watched_threads": [], "subscriptions": [self.OTHER]}):
            with self.subTest(cfg=cfg):
                board = claw_threads(pages)
                batch = fourclaw.collect(cfg, {}, frozenset(), fetch=board)
                shape(self, batch)
                self.assertIsNone(batch.error)
                self.assertEqual([m["kind"] for m in batch.messages], ["thread_activity"])
                self.assertEqual([claw_thread(asked) for asked in board.asked], [self.OTHER])
                self.assertEqual(batch.state, {"next_thread": 0})
        for cfg in ({"account_id": "Reader"}, {"account_id": "Reader", "watched_threads": [], "subscriptions": []},
                    {"account_id": "Reader", "watched_threads": "x", "subscriptions": [self.OTHER]},
                    {"account_id": "Reader", "watched_threads": ["bad"], "subscriptions": [self.OTHER]},
                    {"account_id": "Reader", "subscriptions": [self.OTHER, 5]},
                    {"account_id": "Reader", "watched_threads": [f"00000000-0000-4000-8000-{n:012d}" for n in range(1, 102)]}):
            with self.subTest(cfg=cfg):
                board = claw_threads(pages)
                batch = fourclaw.collect(cfg, {}, frozenset(), fetch=board)
                self.assertEqual((batch.error, board.asked), ("invalid_config", []))


class FruitfliesSubscriptionTests(unittest.TestCase):
    ROOT, OTHER = str(UUID(int=50)), str(UUID(int=60))

    def collect(self, pages, subscribed, state=None, known=frozenset()):
        board = fly_feed(*pages)
        batch = fruit.collect({"account_id": "alice", "subscriptions": subscribed}, state or {}, known, fetch=board)
        shape(self, batch)
        return batch, len(board.asked)

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
        # Two full pages of children: six members and 200 more are over what the memory of a thread holds.
        crowd = [[fly_post(n, parent=50, kind="answer") for n in range(first, first + 100)] for first in (1000, 1100)]
        third, _ = self.collect([own, *crowd], [self.ROOT], second.state, known=set(got) | set(by_id(second)))
        self.assertEqual(len(by_id(third)), 200)
        kept = third.state["subscriptions"]["roots"][self.ROOT]["members"]
        self.assertEqual(len(kept), 200)
        self.assertIn(self.ROOT, kept)

    def test_unseen_root_author_and_parent_stay_unknown(self):
        newest = [fly_post(6, "child", author="carol", parent=50, kind="answer")]
        batch, _ = self.collect([[], newest, []], [self.ROOT])
        self.assertEqual([(m["kind"], m["addressing"]) for m in batch.messages], [("thread_activity", None)])
        empty, _ = self.collect([[], newest, []], [])
        self.assertEqual((empty.messages, "subscriptions" in empty.state), ([], False))

    def test_newest_members_survive_older_history_before_a_later_child_arrives(self):
        def dated(n, minute, parent=50, author="other"):
            # minute: of the day. A later one is a newer post, whatever the number of the post.
            return {**fly_post(n, parent=parent, author=author, kind="answer" if parent else "post"),
                    "created_at": "2026-09-07T{:02d}:{:02d}:00Z".format(*divmod(minute, 60))}

        # The memory of a thread holds the root and 199 members. The recent members come first and have the
        # smallest numbers; only their dates say that they are the newest.
        recent = [dated(n, 1000 + n) for n in range(399, 299, -1)]
        old = [dated(n, n - 400) for n in range(498, 399, -1)]
        first, requests = self.collect([[], recent, [dated(50, 0, parent=None), *old]], [self.ROOT])
        self.assertEqual(requests, 3)
        self.assertEqual(set(by_id(first)), {str(UUID(int=n)) for n in range(300, 499)})
        # A restart sees only older historical members, not the recent parents.
        second, requests = self.collect([[], [], [dated(n, n - 400) for n in range(598, 498, -1)]],
                                        [self.ROOT], json.loads(json.dumps(first.state)), set(by_id(first)))
        self.assertEqual(requests, 3)
        members = second.state["subscriptions"]["roots"][self.ROOT]["members"]
        self.assertEqual(set(members), {str(UUID(int=n)) for n in (50, *range(300, 400), *range(500, 599))},
                         "The root, the 100 recent members and the 99 newest of the others")
        # The recent parent is absent from every current page. Retained ancestry
        # must recognize its child; older history must not have displaced it.
        third, requests = self.collect([[], [dated(700, 1420, parent=300), dated(701, 1421)], []],
                                       [self.ROOT], json.loads(json.dumps(second.state)),
                                       set(by_id(first)) | set(by_id(second)))
        self.assertEqual(requests, 3)
        self.assertEqual({m["id"]: (m["thread_id"], m["addressing"]) for m in third.messages},
                         {str(UUID(int=n)): (self.ROOT, "thread") for n in (700, 701)})


if __name__ == "__main__":
    unittest.main()
