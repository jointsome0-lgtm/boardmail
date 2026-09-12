"""Collection-time addressing evidence and the already-fetched original cache.

Every provider response is invented. ``kind`` must stay exactly as before;
``addressing`` is derived only from what the collector actually saw.
"""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import UUID

from boardmail import addressing, config, providers
from boardmail import adapter_clawdchat as clawd
from boardmail import adapter_fourclaw as fourclaw
from boardmail import adapter_fruitflies as fruit
from boardmail.adapters import Batch, validate
from boardmail.config import MailError
from examples.fixtures import FixtureClient, named, original, settings, uid
from test_fourclaw import THREAD, page as claw_page, post as claw_post
from test_fruitflies import post as fly_post

PREVIEW = "PRIVATE NOTIFICATION PREVIEW"


def with_profile(client, **fields):
    """Add board-verified profile names to the identity response already fetched."""
    get = client.get
    def wrapped(path, params=None, **kw):
        result = get(path, params, **kw)
        if path in ("/agents/me", "/v1/me"):
            (result["agent"] if "agent" in result else result).update(fields)
        return result
    client.get = wrapped


def by_id(batch):
    return {m["id"]: m for m in batch.messages}


def originals(batch):
    return {o["id"]: o for o in batch.originals}


def assert_clean(test, batch):
    validate(batch)
    for item in batch.originals:
        test.assertEqual(set(item), set(addressing.ORIGINAL_FIELDS))
    dump = json.dumps([batch.messages, batch.state, batch.originals])
    test.assertNotIn(PREVIEW, dump)
    test.assertNotIn("preview", dump)
    for message in batch.messages:
        test.assertIn(message.get("addressing"), (None, *addressing.VALUES))


class NotificationBoardTests(unittest.TestCase):
    """The Colony and Moltbook: native types plus the public original's parent."""

    def event(self, client, n, kind, *, comment=True, post=None):
        colony = client.source == "the-colony"
        root = post or (101 if colony else 201)
        item = {"id": uid(n + 1000), "notification_type" if colony else "type": kind,
                "post_id" if colony else "relatedPostId": uid(root), "content": PREVIEW}
        if comment: item["comment_id" if colony else "relatedCommentId"] = uid(n)
        client.events.append(item)

    def collect(self, client, state=None, known=None):
        cfg = client.settings
        batch = providers.collect(client.source, cfg, deepcopy(state or {}), set(known or ()), client_factory=lambda *_: client)
        assert_clean(self, batch)
        return batch

    def test_colony_native_types_parents_and_overlaps(self):
        client = FixtureClient("the-colony", {**settings()["the-colony"], "mention_aliases": ["@Sample"]})
        with_profile(client, username="colony-name")
        client.events.clear()
        comments = {111: dict(), 113: dict(parent_id=uid(111)), 114: dict(parent_id=uid(110)),
                    115: dict(), 116: dict(parent_id=uid(111), body="@colony-name please look"),
                    117: dict(parent_id=uid(111)), 118: dict(parent_id=uid(111), body="cc @sample and @Sample-two"),
                    112: dict()}
        client.comments = [{**original(n, 101, colony=True), **changes} for n, changes in comments.items()]
        for n, kinds in ((111, ["comment_on_post"]), (112, ["mention"]), (113, ["comment_on_post"]),
                         (114, ["reply_to_comment"]), (115, ["comment_on_post", "mention"]),
                         (116, ["comment_on_post"]), (117, ["comment_on_post", "reply_to_comment"]),
                         (118, ["comment_on_post"])):
            for kind in kinds: self.event(client, n, kind)
        batch = self.collect(client)
        self.assertFalse(batch.error)
        got = by_id(batch)
        self.assertEqual({n: got[uid(n)]["addressing"] for n in comments}, {
            111: "direct", 112: "mention", 113: "thread", 114: "direct", 115: "direct+mention",
            116: "mention", 117: "direct", 118: "mention"})
        # Legacy kinds are untouched: a mention notification still overrides.
        self.assertEqual({n: got[uid(n)]["kind"] for n in (111, 112, 114, 115, 117)},
                         {111: "reply_to_post", 112: "mention", 114: "reply_to_comment", 115: "mention", 117: "reply_to_post"})
        self.assertEqual(got[uid(118)]["body"], "cc @sample and @Sample-two")

    def test_colony_overlap_across_collections_and_legacy_pending_state(self):
        client = FixtureClient("the-colony", settings()["the-colony"])
        client.events.clear(); client.comments.clear()
        self.event(client, 121, "comment_on_post")
        first = self.collect(client)
        self.assertEqual(first.messages, [])
        self.assertEqual(first.state["pending"][uid(121)]["types"], {uid(121): ["comment_on_post"]})
        # The original appears together with a second, retained notification.
        client.comments.append({**original(121, 101, colony=True), "parent_id": uid(110)})
        self.event(client, 121, "reply_to_comment")
        second = self.collect(client, first.state)
        self.assertEqual(by_id(second)[uid(121)]["addressing"], "direct")
        self.assertNotIn(uid(121), second.state["pending"])
        # A reference persisted before types were recorded still resolves from its one native type.
        client.comments.append({**original(122, 101, colony=True), "parent_id": uid(110)})
        legacy = {"pending": {uid(122): {"post": uid(101), "ids": {uid(122): "reply_to_comment"}, "cursor": None}}}
        third = self.collect(client, legacy)
        self.assertEqual(by_id(third)[uid(122)]["addressing"], "direct")
        legacy = {"pending": {uid(122): {"post": uid(101), "ids": {uid(122): "reply_to_post"}, "cursor": None}}}
        self.assertEqual(by_id(self.collect(client, legacy))[uid(122)]["addressing"], "thread")

    def test_moltbook_tree_shows_own_parent_and_caches_already_fetched_context(self):
        client = FixtureClient("moltbook", settings()["moltbook"])
        client.events.clear()
        own_comment = original(250, 201, 2)
        own_comment["replies"] = [{**original(213, 201), "parent_id": uid(250)}]
        top = original(211, 201)
        top["replies"] = [{**original(212, 201), "parent_id": uid(211)},
                          {**original(214, 201), "parent_id": uid(211), "content": "@Sample-Writer-Two reads this"}]
        client.comments = [top, own_comment]
        with_profile(client, name="sample-writer-two")
        for n, kinds in ((211, ["post_comment"]), (212, ["post_comment"]), (213, ["post_comment"]),
                         (214, ["post_comment"]), (250, ["post_comment"])):
            for kind in kinds: self.event(client, n, kind)
        client.events.append({"id": uid(1999), "type": "mention", "relatedPostId": uid(201), "content": PREVIEW})
        batch = self.collect(client)
        self.assertFalse(batch.error)
        got = by_id(batch)
        self.assertEqual({n: got[uid(n)]["addressing"] for n in (211, 212, 213, 214)},
                         {211: "direct", 212: "thread", 213: "direct", 214: "mention"})
        self.assertEqual(got[uid(214)]["kind"], "reply_to_post")
        self.assertNotIn(uid(250), got, "Our own comment is context, not mail")
        self.assertNotIn(uid(201), got, "A mention notification on our own post is not mail")
        cached = originals(batch)
        self.assertEqual(set(cached), {uid(201), uid(250), uid(211)}, "root, own comment and the accepted replies' parent")
        self.assertEqual(cached[uid(201)]["title"], "Example discussion")
        self.assertEqual(cached[uid(211)]["body"], "A synthetic public reply.")
        self.assertEqual(cached[uid(250)]["parent_id"], None)
        self.assertEqual(cached[uid(250)]["url"], client.host + "/post/" + uid(201) + "#comment-" + uid(250))
        self.assertLessEqual(len(client.calls), 5, "No extra requests were spent on the cache")

    def test_moltbook_foreign_post_mention_and_deleted_root_are_not_cached(self):
        client = FixtureClient("moltbook", settings()["moltbook"])
        client.events.clear()
        client.root = {**original(301, 301), "title": "Their thread"}
        client.comments = []
        client.events.append({"id": uid(1901), "type": "mention", "relatedPostId": uid(301), "content": PREVIEW})
        batch = self.collect(client)
        self.assertEqual(by_id(batch)[uid(301)]["addressing"], "mention")
        self.assertEqual(set(originals(batch)), {uid(301)}, "A fetched public root is kept once")
        client.root["is_deleted"] = True
        batch = self.collect(client)
        self.assertEqual(batch.messages, [])
        self.assertEqual(originals(batch), {})


class PostingboardTests(unittest.TestCase):
    def collect(self, client, state=None, known=None):
        batch = providers.collect("postingboard", client.settings, deepcopy(state or {}), set(known or ()), client_factory=lambda *_: client)
        assert_clean(self, batch)
        return batch

    def test_watched_thread_targets_and_flat_replies(self):
        cfg = {**settings()["postingboard"], "threads": [uid(301)]}
        client = FixtureClient("postingboard", cfg)
        with_profile(client, handle="sample")
        client.comments[uid(301)] += [named(316, 301, reply_to=301), named(318, 301, 3), named(317, 301, reply_to=318),
                                      named(319, 301, reply_to=311), named(320, 301, reply_to=999),
                                      named(321, 301, body="@sample-agent check this"), named(322, 301, body="@sample here")]
        batch = self.collect(client)
        self.assertFalse(batch.error)
        got = by_id(batch)
        self.assertEqual({n: got[uid(n)]["addressing"] for n in (311, 312, 316, 317, 319, 320, 321, 322)}, {
            311: "thread", 312: "thread", 316: "direct", 317: "direct", 319: "thread", 320: None,
            321: "mention", 322: "mention"})
        self.assertEqual({n: got[uid(n)]["kind"] for n in (311, 316, 321, 322)},
                         {311: "reply_to_post", 316: "reply_to_post", 321: "mention", 322: "reply_to_post"})
        self.assertEqual(got[uid(317)]["parent_id"], uid(318))
        self.assertEqual(set(originals(batch)), {uid(301), uid(318)}, "The root and our own reply, never foreign replies")
        self.assertEqual(originals(batch)[uid(301)]["body"], "A synthetic named-board reply.")

    def test_foreign_thread_mentions_summaries_and_own_posts(self):
        cfg = {**settings()["postingboard"], "threads": [uid(302)]}
        client = FixtureClient("postingboard", cfg)
        client.summaries = {uid(313)}
        batch = self.collect(client)
        got = by_id(batch)
        self.assertEqual(set(got), {uid(314)})
        self.assertEqual((got[uid(314)]["addressing"], got[uid(314)]["kind"]), ("mention", "mention"))
        # Our own summarized reply was hydrated in full before it became cached context.
        self.assertEqual(set(originals(batch)), {uid(302), uid(313)})
        self.assertEqual(originals(batch)[uid(313)]["body"], "A synthetic named-board reply.")

    def test_inbox_reasons_search_hits_and_explicit_text(self):
        cfg = {**settings()["postingboard"], "threads": [], "inbox": True, "alias_search": ["meliora"]}
        client = FixtureClient("postingboard", cfg)
        posts = {uid(500): named(500, 500), uid(501): named(501, 500, body="@sample-agent please confirm."),
                 uid(502): named(502, 500, reply_to=501, body="A direct reply to your comment."),
                 uid(503): named(503, 500, reply_to=501, body="@sample-agent, and a direct reply."),
                 uid(504): named(504, 500, body="Activity in your thread."),
                 uid(505): named(505, 500, body="Activity in your thread naming @sample-agent."),
                 uid(601): named(601, 600, body="Thanks Meliora, the summary helped."),
                 uid(602): named(602, 600, body="Thanks @meliora, the summary helped."),
                 uid(603): named(603, 600, 3, body="Own post mentioning meliora.")}
        client.others = posts
        client.inbox = [(1, posts[uid(501)], ["mention"]), (2, posts[uid(502)], ["direct_reply"]),
                        (3, posts[uid(503)], ["direct_reply", "mention"]), (4, posts[uid(504)], ["reply_to_your_thread"]),
                        (5, posts[uid(505)], ["reply_to_your_thread"])]
        client.search["meliora"] = [posts[uid(601)], posts[uid(602)], posts[uid(603)]]
        batch = self.collect(client)
        self.assertFalse(batch.error)
        got = by_id(batch)
        self.assertEqual({n: got[uid(n)]["addressing"] for n in (501, 502, 503, 504, 505, 601, 602)}, {
            501: "mention", 502: "direct", 503: "direct+mention", 504: "thread", 505: "mention",
            601: None, 602: "mention"})
        self.assertEqual(got[uid(601)]["discovery"], "search:meliora")
        self.assertEqual(got[uid(504)]["kind"], "reply_to_post")
        self.assertEqual(set(originals(batch)), {uid(603)}, "Only the fully fetched own post; previews are never kept")
        self.assertEqual(originals(batch)[uid(603)]["body"], "Own post mentioning meliora.")


class ClawdChatClient:
    def __init__(self):
        self.owner = uid(1)
        self.events, self.originals, self.calls = [], {}, []
        self.profile = {"id": self.owner, "name": "clawd-name"}

    def phase(self, seconds):
        pass

    def get(self, path, params=None, *, authenticated=False):
        self.calls.append((path, params, authenticated))
        if path == "/agents/me":
            return self.profile
        if path == "/notifications":
            offset = params["offset"]
            return {"success": True, "items": self.events[offset:offset + params["limit"]], "total": len(self.events)}
        assert not authenticated
        value = self.originals[path.rsplit("/", 1)[-1]]
        if isinstance(value, Exception): raise value
        return value


def clawd_event(number, kind="comment"):
    return {"id": uid(number + 1000), "type": kind, "post_id": uid(100),
            "comment_id": uid(number), "content": PREVIEW}


def clawd_original(number, **changes):
    return {"id": uid(number), "post_id": uid(100), "content": "Public original " + str(number),
            "author": {"id": uid(2), "name": "other-agent"}, "created_at": "2026-09-07T10:00:00Z",
            "post": {"id": uid(100), "title": "Public thread"}, "parent_id": None,
            "web_url": "https://clawdchat.cn/post/" + uid(100), **changes}


class ClawdChatTests(unittest.TestCase):
    def setUp(self):
        self.client = ClawdChatClient()

    def collect(self, state=None, known=(), cfg=None):
        with patch.object(clawd, "Client", return_value=self.client):
            batch = clawd.collect(cfg or {"account_id": uid(1)}, deepcopy(state or {}), frozenset(known))
        assert_clean(self, batch)
        return batch

    def test_comment_is_checked_against_parent_and_post_ownership(self):
        c = self.client
        c.events = [clawd_event(10), clawd_event(11), clawd_event(12, "reply"), clawd_event(13), clawd_event(13, "reply"),
                    clawd_event(14), clawd_event(14, "mention_comment"), clawd_event(15, "mention_comment"), clawd_event(15),
                    {"id": uid(1100), "type": "mention_post", "post_id": uid(100), "content": PREVIEW}, clawd_event(16)]
        c.originals = {uid(n): clawd_original(n) for n in range(10, 17)}
        c.originals[uid(100)] = clawd_original(100, title="Their post", content="@clawd-name is named here")
        c.originals[uid(11)]["parent_id"] = uid(9)
        c.originals[uid(12)]["parent_id"] = uid(9)
        c.originals[uid(15)]["parent_id"] = uid(9)
        c.originals[uid(16)].update(parent_id=uid(9), content="@Clawd-Name could you check?")
        with patch.object(clawd, "PAGE_SIZE", 20):
            batch = self.collect()
        self.assertFalse(batch.error)
        got = by_id(batch)
        self.assertEqual({n: got[uid(n)]["addressing"] for n in (10, 11, 12, 13, 14, 15, 100, 16)}, {
            10: "direct", 11: "thread", 12: "direct", 13: "direct", 14: "direct+mention", 15: "mention",
            100: "mention", 16: "mention"})
        self.assertEqual({n: got[uid(n)]["kind"] for n in (10, 12, 13, 14, 15, 100)},
                         {10: "reply_to_post", 12: "reply_to_comment", 13: "reply_to_post", 14: "reply_to_post",
                          15: "mention", 100: "mention"})
        self.assertTrue(batch.complete)
        self.assertEqual(batch.state["pending"], [])
        self.assertEqual(originals(batch), {})

    def test_foreign_post_context_own_comment_and_partial_names(self):
        c = self.client
        c.events = [clawd_event(17), clawd_event(18), clawd_event(19, "mention_comment")]
        c.originals = {uid(n): clawd_original(n) for n in (17, 18, 19)}
        c.originals[uid(17)]["post"] = {"id": uid(100), "title": "Their post", "author": {"id": uid(2)}}
        c.originals[uid(18)]["author"] = {"id": uid(1), "name": "us"}
        c.originals[uid(19)].update(content="@clawd-name-two is someone else")
        batch = self.collect()
        got = by_id(batch)
        self.assertEqual({n: got[uid(n)]["addressing"] for n in (17, 19)}, {17: None, 19: "mention"})
        self.assertNotIn(uid(18), got)
        self.assertEqual(set(originals(batch)), {uid(18)}, "Only our own fetched comment is cached")
        self.assertEqual(originals(batch)[uid(18)]["author"], "us")

    def test_overlap_after_same_pass_resolution_is_still_merged(self):
        c = self.client
        c.events = [clawd_event(40, "reply")]
        c.originals = {uid(40): MailError("network_error")}
        first = self.collect()
        self.assertEqual(first.state["pending"][0]["types"], ["reply"])
        # The retry succeeds before discovery reads the second notification.
        c.originals = {uid(40): clawd_original(40, parent_id=uid(9))}
        c.events.insert(0, clawd_event(40, "mention_comment"))
        second = self.collect(first.state)
        self.assertEqual(by_id(second)[uid(40)]["addressing"], "direct+mention")
        self.assertEqual(len(second.messages), 1)

    def test_overlap_arriving_later_and_legacy_pending_entries(self):
        c = self.client
        c.events = [clawd_event(20)]
        c.originals = {uid(20): MailError("network_error")}
        first = self.collect()
        self.assertEqual(first.messages, [])
        self.assertEqual(first.state["pending"][0]["types"], ["comment"])
        c.events.insert(0, clawd_event(20, "reply"))
        second = self.collect(first.state)
        self.assertEqual(second.messages, [])
        self.assertEqual(second.state["pending"][0]["types"], ["comment", "reply"])
        c.originals = {uid(20): clawd_original(20, parent_id=uid(9))}
        third = self.collect(second.state)
        self.assertEqual(by_id(third)[uid(20)]["addressing"], "direct")
        self.assertEqual(by_id(third)[uid(20)]["kind"], "reply_to_post", "The first retained kind is preserved")
        legacy = {"offset": 0, "pending": [
            {"id": uid(21), "post": uid(100), "kind": "reply_to_comment", "is_post": False},
            {"id": uid(22), "post": uid(100), "kind": "reply_to_post", "is_post": False},
            {"id": uid(23), "post": uid(100), "kind": "mention", "is_post": False}]}
        c.events = []
        c.originals = {uid(21): clawd_original(21, parent_id=uid(9)), uid(22): clawd_original(22, parent_id=uid(9)),
                       uid(23): clawd_original(23)}
        fourth = self.collect(legacy)
        self.assertEqual({n: by_id(fourth)[uid(n)]["addressing"] for n in (21, 22, 23)},
                         {21: "direct", 22: "thread", 23: "mention"})

    def test_configured_aliases_add_textual_mentions(self):
        c = self.client
        c.events = [clawd_event(30), clawd_event(31)]
        c.originals = {uid(30): clawd_original(30, parent_id=uid(9), content="ping @Helper"),
                       uid(31): clawd_original(31, content="ping @Helper too")}
        batch = self.collect(cfg={"account_id": uid(1), "mention_aliases": ["@helper"]})
        self.assertEqual({n: by_id(batch)[uid(n)]["addressing"] for n in (30, 31)}, {30: "mention", 31: "direct+mention"})


class FourclawTests(unittest.TestCase):
    def collect(self, html, **extra):
        with patch.object(fourclaw, "_fetch", return_value=html):
            batch = fourclaw.collect(dict(account_id="Reader", watched_threads=[THREAD], **extra), {}, frozenset())
        assert_clean(self, batch)
        return batch

    def test_own_thread_is_never_direct_and_public_context_is_kept(self):
        ids = [f"10000000-0000-4000-8000-{n:012d}" for n in range(1, 5)]
        html = claw_page("Reader", [claw_post("Other", "First reply"), claw_post("Other", "@Reader second reply"),
                                    claw_post("Reader", "Our own reply"), claw_post("Another", "Third reply")], ids)
        batch = self.collect(html)
        self.assertEqual([(m["id"], m["addressing"], m["kind"]) for m in batch.messages],
                         [(ids[0], "thread", "reply_to_post"), (ids[1], "mention", "reply_to_post"), (ids[3], "thread", "reply_to_post")])
        self.assertEqual(set(originals(batch)), {THREAD, ids[2]})
        self.assertEqual(originals(batch)[THREAD]["body"], "Opening")
        self.assertEqual(originals(batch)[ids[2]]["author"], "Reader")

    def test_foreign_thread_keeps_only_mentions_and_the_root(self):
        html = claw_page("Other", [claw_post("Other", "@Reader hello"), claw_post("Other", "unrelated")])
        batch = self.collect(html)
        self.assertEqual([(m["addressing"], m["kind"]) for m in batch.messages], [("mention", "mention")])
        self.assertEqual(set(originals(batch)), {THREAD})


class FruitfliesTests(unittest.TestCase):
    def collect(self, pages, cfg=None):
        with patch.object(fruit, "_fetch", side_effect=pages):
            batch = fruit.collect(cfg or {"account_id": "alice"}, {}, frozenset())
        assert_clean(self, batch)
        return batch

    def test_verified_parent_and_explicit_handle_combine(self):
        own = [fly_post(1, "our question", author="alice", kind="question"), fly_post(2, "our answer", author="alice", kind="answer")]
        rows = [fly_post(3, "@alice hello"), fly_post(4, parent=1, kind="answer"), fly_post(5, "@ALICE thanks", parent=2, kind="answer"),
                fly_post(6, "@ally as configured", parent=1, kind="answer"), fly_post(7, "@ally alone"), fly_post(8, "plain")]
        batch = self.collect([own, rows, []], {"account_id": "alice", "mention_aliases": ["Ally"]})
        self.assertEqual([(int(UUID(m["id"])), m["addressing"], m["kind"]) for m in batch.messages],
                         [(3, "mention", "mention"), (4, "direct", "reply_to_post"), (5, "direct+mention", "reply_to_comment"),
                          (6, "direct+mention", "reply_to_post"), (7, "mention", "mention")])
        cached = originals(batch)
        self.assertEqual(set(cached), {fly_post(1)["id"], fly_post(2)["id"]})
        self.assertEqual(cached[fly_post(1)["id"]]["body"], "our question")


class ConfigTests(unittest.TestCase):
    def load(self, adapter, **extra):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            base = {"adapter": adapter, "account_id": uid(1) if adapter in config.LEGACY_ADAPTERS or adapter == "clawdchat" else "reader"}
            if adapter in config.LEGACY_ADAPTERS or adapter == "clawdchat": base["api_key_file"] = "unused.key"
            if adapter == "postingboard": base["inbox"] = True
            if adapter == "fourclaw": base["watched_threads"] = [THREAD]
            path.write_text(json.dumps({"database": "mail.sqlite3", "sources": {"alias": {**base, **extra}}}))
            return config.load(path)["sources"]["alias"]

    def test_mention_aliases_are_validated_the_same_way_for_every_builtin(self):
        for adapter in config.SOURCE_FIELDS:
            with self.subTest(adapter=adapter):
                loaded = self.load(adapter, mention_aliases=[" @Name ", "@Name", "other"])
                self.assertEqual(loaded["mention_aliases"], ["@Name", "other"])
                for bad in ("@Name", ["", "x"], ["x" * 101], [1]):
                    with self.assertRaisesRegex(MailError, "^invalid_config$"):
                        self.load(adapter, mention_aliases=bad)
        self.assertEqual(self.load("postingboard")["mention_aliases"], [])
        self.assertNotIn("mention_aliases", self.load("fourclaw"), "The account-name default stays with the adapter")


class CacheTests(unittest.TestCase):
    def test_originals_are_bounded_normalized_and_deduplicated(self):
        batch = Batch()
        item = {"id": uid(1), "thread_id": uid(1), "title": "T", "body": "B", "url": "https://example.invalid/1",
                "created_at": 1, "kind": "mention", "provider_seq": 5}
        self.assertTrue(addressing.cache_original(batch, item))
        self.assertFalse(addressing.cache_original(batch, item), "Duplicates are ignored")
        self.assertEqual(batch.originals, [{"id": uid(1), "thread_id": uid(1), "parent_id": None, "author": None,
                                                           "title": "T", "body": "B", "url": "https://example.invalid/1", "created_at": 1}])
        with patch.object(addressing, "MAX_ORIGINALS", 3):
            for n in range(2, 6):
                addressing.cache_original(batch, {**item, "id": uid(n)})
        self.assertEqual(len(batch.originals), 3)

    def test_alias_rules(self):
        names = addressing.aliases({"username": "Colony-Name", "name": "@colony-name", "id": uid(1)}, ["@Extra", " ", 5, "x" * 101])
        self.assertEqual(names, ["Colony-Name", "Extra"])
        pattern = addressing.mention_pattern(names)
        self.assertTrue(addressing.mentions(pattern, "hi @colony-name!"))
        self.assertTrue(addressing.mentions(pattern, None, "(@EXTRA)"))
        self.assertFalse(addressing.mentions(pattern, "colony-name without the sign"))
        self.assertFalse(addressing.mentions(pattern, "@colony-name-two is someone else"))
        self.assertFalse(addressing.mentions(pattern, "mail@colony-name.example"))
        self.assertIsNone(addressing.mention_pattern([]))
        self.assertFalse(addressing.mentions(None, "@anyone"))
        self.assertEqual(addressing.resolve(direct=True, mention=True, thread=True), "direct+mention")
        self.assertEqual(addressing.resolve(mention=True, thread=True), "mention")
        self.assertIsNone(addressing.resolve())


if __name__ == "__main__":
    unittest.main()
