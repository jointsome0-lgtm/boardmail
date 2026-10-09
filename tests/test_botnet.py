"""Botnet delivery, recovery and credential boundaries, without a real account."""
from contextlib import nullcontext
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import URLError
from urllib.parse import quote, unquote, urlsplit

from boardmail import adapter_botnet as adapter, commands
from boardmail.adapters import validate
from boardmail.config import MailError, load
from boardmail.store import Store
from examples.fixtures import FakeBoard, status
from kit import Clock, failing, fixed, mark, new_inbox, one_pass

KEY = "synthetic-key-only"


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


def key_file(test):
    """The file of an invented key, in a folder that is gone when the test ends."""
    folder = tempfile.TemporaryDirectory()
    test.addCleanup(folder.cleanup)
    path = Path(folder.name) / "key"
    path.write_text(KEY + "\n")
    return path


def slow(clock, seconds, answer=None):
    """What a board has when its answer takes this many seconds to come. The clock moves by them, and where the
    request had no more time than that, the transport calls the answer late."""
    def given(asked):
        clock.advance(seconds)
        return MailError("source_timeout") if seconds >= asked.left else answer
    return given


class Board(FakeBoard):
    """An invented Botnet at the transport seam: the profile of the account, its inbox, one topic and the public
    messages by id. A message that it does not have is a 404.

    What the board has for a request can also be a number, which is the HTTP status that it answers with, an
    exception, which is how the request fails, or a function of the request that gives one of these. page_errors
    has such a thing for the page of the inbox at a cursor, and topic_error for the topic.

    calls has each request in short: its path below the API, its query with numbers as numbers, and whether it
    carried the key. A request that carries the key where it must not, or none where it must, fails the test."""
    def __init__(self):
        super().__init__(self.answer)
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

    def answer(self, asked):
        assert asked.board == "botnet" and asked.body is None, asked
        assert asked.url.startswith(adapter.ORIGIN + "/api/forum/"), asked.url
        private = "Authorization" in asked.headers
        assert asked.headers == ({"Authorization": "Bearer " + KEY} if private else {}), asked.headers
        path = urlsplit(asked.url).path.removeprefix("/api/forum")
        params = {name: int(value) if value.isdigit() else value for name, value in asked.params.items()}
        self.calls.append((path, params, private))
        answer = self.get(path, params, authenticated=private)
        if callable(answer):
            answer = answer(asked)
        return status(answer) if type(answer) is int else answer

    def get(self, path, params, *, authenticated):
        if path == "/me":
            assert authenticated
            return self.profile
        if path == "/inbox":
            assert authenticated
            cursor = params.get("cursor")
            if cursor in self.page_errors:
                return self.page_errors[cursor]
            ceiling = int(cursor.split(":")[1]) if cursor else 10000
            items = sorted((e for e in self.events if e["id"] < ceiling), key=lambda e: -e["id"])
            page = items[:params["limit"]]
            return {"items": page, "nextCursor": 123 if self.bad_cursor else
                    "older:" + str(page[-1]["id"]) if len(items) > len(page) else None}
        assert not authenticated, "Public-original requests carried credentials"
        if path == "/topics/" + TOPIC:
            if self.topic_error:
                return self.topic_error
            return {"id": TOPIC, "title": "Topic, not legacy thread", "description": "Public topic",
                    "createdAt": 1790593200000}
        return self.originals.get(unquote(path.rsplit("/", 1)[-1]), 404)


class BotnetTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name)
        cfg = self.path / "config.json"
        cfg.write_text(json.dumps({"database": "mail.sqlite3", "sources": {
            "botnet": {"account_id": OWNER, "api_key_file": "example.key"}}}))
        self.key = self.path / "example.key"
        self.key.write_text(KEY + "\n")
        self.settings = load(cfg)["sources"]["botnet"]
        self.store = new_inbox(self.path / "mail.sqlite3")
        self.board = Board()

    def collect(self):
        batch, added = one_pass(self.store, "botnet", self.settings, self.board)
        self.assertNotIn("PRIVATE PREVIEW", json.dumps(batch.__dict__))
        return batch, added

    def test_native_and_legacy_notifications_deliver_once_with_original_relationships(self):
        self.board.add(10)
        self.board.add(11, reason="mention", parentMessageId=mid(10))
        self.board.events[-1]["messageId"] = mid(11)
        self.board.add(12, author={"id": OWNER, "name": "Owner"})
        batch, added = self.collect()
        self.assertEqual(added, 2)
        self.assertTrue(batch.complete)
        message = self.store.show("botnet", mid(10))
        self.assertEqual((message["thread_id"], message["parent_id"], message["kind"], message["addressing"]),
                         (TOPIC, OPENER, "reply_to_post", "direct"))
        self.assertEqual(message["created_at"], 1790593200)
        self.assertEqual(self.store.show("botnet", mid(11))["addressing"], "mention")
        mark(self.store, "botnet", mid(10), "read")
        saved = self.store.show("botnet", mid(10))
        self.assertEqual(self.collect()[1], 0)
        self.assertEqual(self.store.show("botnet", mid(10)), saved)
        self.assertEqual(self.store.status()["counts"]["latest_arrival"], 2)
        # A topic outage must not hide a readable parent or collapse two native
        # reasons. A null reason is a legacy mention, like an absent reason.
        self.board.add(13, reason=None)
        self.board.add(14, reason="reply", parentMessageId=mid(999))
        self.board.events.append({**self.board.events[-1], "reason": "mention"})
        self.board.topic_error = 503
        batch, added = self.collect()
        self.assertEqual((added, batch.error), (2, "http_503"))
        for n in (13, 14):
            self.assertEqual(self.store.show("botnet", mid(n))["addressing"], "direct+mention")
        with self.assertRaisesRegex(MailError, "^subscriptions_unsupported$"):
            commands.execute(self.store, "subscribe", sources={"botnet": self.settings}, source="botnet", thread=TOPIC)

    def test_new_head_backfill_and_failed_reference_survive_reopen_and_upstream_expiry(self):
        for n in range(10, 30):
            self.board.add(n)
        self.board.originals[mid(14)] = URLError("The board is not reached")
        self.collect()
        self.store = Store(self.store.path)
        self.board.add(30)  # Arrived ahead of the saved backwards cursor.
        for _ in range(3):
            self.collect()
        self.assertEqual(self.store.known("botnet", OWNER), {mid(n) for n in range(10, 31) if n != 14})
        cursors = [params.get("cursor") for path, params, _ in self.board.calls if path == "/inbox"]
        self.assertEqual(cursors, [None, None, "older:22", None, "older:14", None],
                         "Each pass reads the head, and one older page until the sweep is at its end")
        self.board.events.clear()  # The failed reference must outlive notifications.
        self.board.originals[mid(14)] = original(14)
        batch, added = self.collect()
        self.assertEqual(added, 1)
        self.assertTrue(batch.complete)
        self.assertEqual(batch.state["pending"], [])

    def test_identity_and_public_original_failures_never_save_unconfirmed_text(self):
        self.board.add(10)
        self.board.profile = {"actor": {"id": "other-account"}}
        batch, added = self.collect()
        self.assertEqual((batch.error, added, batch.state), ("account_mismatch", 0, {}))
        self.assertEqual([p for p, _, _ in self.board.calls], ["/me"])
        self.board.profile = {"actor": {"id": OWNER}}
        self.board.originals[mid(10)] = original(999, body="WRONG BODY")
        batch, added = self.collect()
        self.assertEqual((batch.error, added), ("invalid_response", 0))
        self.assertEqual(batch.state["pending"], [{"id": mid(10), "reasons": ["reply"]}])
        self.board.page_errors[None] = 429
        self.board.calls.clear()
        batch, _ = self.collect()
        self.assertEqual(batch.error, "http_429")
        self.assertEqual([p for p, _, _ in self.board.calls], ["/me", "/inbox"])

    def test_context_keeps_topic_separate_from_parent_and_rejects_cross_topic_parent(self):
        self.board.add(10)
        self.collect()
        self.board.calls.clear()
        self.key.unlink()  # What is public is read without the key of the account.
        before = self.store.path.read_bytes()
        asks = {"sources": {"botnet": self.settings}, "fetch": self.board}
        result, code = commands.execute(self.store, "context", source="botnet", id=mid(10), **asks)
        self.assertEqual((code, result["root"]["id"], result["parent"]["id"]), (0, TOPIC, OPENER))
        opener, code = commands.execute(self.store, "context", source="botnet", id=OPENER, **asks)
        self.assertEqual((code, opener["parent"]["status"]), (0, "none"))
        self.board.originals[OPENER]["topicId"] = uid(999)
        result, code = commands.execute(self.store, "context", source="botnet", id=mid(10), **asks)
        self.assertEqual((code, result["parent"]["status"]), (1, "unavailable"))
        self.assertEqual(len(self.board.calls), 8)
        self.assertTrue(all(not auth for _, _, auth in self.board.calls))
        self.assertEqual(self.store.path.read_bytes(), before)

    def pending_backlog(self):
        """Sixty messages that the account was told of in passes that had no time for an original. The state that
        these passes left: sixty references that wait. The last of the passes was healthy at self.healthy."""
        for number in range(10, 70):
            self.board.add(number)
        ready, clock = self.board.originals, Clock(1)
        self.board.originals = dict.fromkeys(ready, slow(clock, 46))
        for _ in range(8):  # A pass reads the head of the inbox and one older page of it: eight references a page.
            result, code = self.collect_command(clock)
            self.assertEqual((code, result["added"], result["errors"]), (0, 0, []))
        self.board.originals, self.healthy = ready, result["sources"][0]["last_ok"]
        self.board.asked.clear()
        state = self.store.collection_state("botnet", OWNER, "botnet")[1]
        self.assertCountEqual([entry["id"] for entry in state["pending"]], [mid(number) for number in range(10, 70)])
        return state

    def collect_command(self, clock=None):
        """One pass of the collect command: what it gave and its exit code. With a clock the time stands still
        for the pass unless the board moves it."""
        with fixed(clock) if clock else nullcontext():
            return commands.execute(self.store, "collect", sources={"botnet": self.settings}, fetch=self.board)

    def test_an_original_that_is_gone_is_unavailable_and_no_error_of_the_source(self):
        asked_for = "/topic-messages/" + quote(mid(12), safe="")
        for name, gone in (("deleted", original(12, is_deleted=True)), ("hidden", original(12, is_hidden=True)),
                           ("refused", 403), ("missing", 404), ("removed", 410)):
            with self.subTest(gone=name):
                self.store, self.board = new_inbox(self.path / (name + ".sqlite3")), Board()
                for number in (10, 11, 12):
                    self.board.add(number)
                self.board.originals[mid(12)] = gone
                for added in (2, 0):
                    self.board.calls.clear()
                    result, code = self.collect_command()
                    self.assertEqual((code, result["added"], result["failed"], result["errors"]), (0, added, False, []))
                    health = result["sources"][0]
                    self.assertEqual((health["status"], health["error"], health["unavailable"]), ("ok", None, 1))
                    self.assertIsNotNone(health["last_ok"])
                    # Its reference waits, so the original is asked for on each pass.
                    self.assertTrue(health["backlog_pending"])
                    self.assertIn(asked_for, [path for path, _, _ in self.board.calls])
                self.board.originals[mid(12)] = original(12)
                result, code = self.collect_command()
                health = result["sources"][0]
                self.assertEqual((code, result["added"], health["unavailable"], health["backlog_pending"]),
                                 (0, 1, 0, False))
                self.assertEqual(self.store.show("botnet", mid(12))["body"], "Public message 12")

    def test_an_original_that_fails_in_another_way_is_an_error_of_the_source(self):
        for name, failing, error in (("busy", 429, "http_429"), ("broken", 500, "http_500"),
                                     ("another", original(999), "invalid_response"),
                                     ("cut", original(12, truncated=True), "original_incomplete")):
            with self.subTest(failing=name):
                self.store, self.board = new_inbox(self.path / (name + ".sqlite3")), Board()
                self.board.add(12)
                self.board.originals[mid(12)] = failing
                result, code = self.collect_command()
                self.assertEqual((code, result["added"], [found["error"] for found in result["errors"]]),
                                 (1, 0, [error]))
                health = result["sources"][0]
                self.assertEqual((health["status"], health["error"], health["unavailable"], health["last_ok"]),
                                 ("error", error, 0, None))

    def test_an_inbox_that_refuses_the_key_is_an_error_of_the_source(self):
        self.board.add(10)
        for status in (401, 403):
            self.board.page_errors[None] = status
            result, code = self.collect_command()
            self.assertEqual((code, result["added"]), (1, 0))
            self.assertEqual([(found["error"], found["next_action"]) for found in result["errors"]],
                             [("http_" + str(status), "check_config_and_credentials")])

    def test_real_request_cap_saves_healthy_partial_progress(self):
        state = self.pending_backlog()
        result, code = self.collect_command()
        known, saved, _ = self.store.collection_state("botnet", OWNER, "botnet")
        pending = {entry["id"] for entry in saved["pending"]}
        self.assertEqual((len(self.board.asked), result["added"], len(pending)), (40, 36, 24))
        self.assertEqual(known | pending, {entry["id"] for entry in state["pending"]})
        self.assertFalse(known & pending)
        self.assertEqual((code, result["failed"], result["errors"]), (0, False, []))
        health = result["sources"][0]
        self.assertEqual((health["status"], health["error"], health["backlog_pending"]), ("ok", None, True))
        self.assertGreater(health["last_ok"], self.healthy)
        self.assertEqual(commands.execute(self.store, "status", require_fresh=True)[1], 0)

    def test_real_cap_does_not_hide_an_earlier_network_failure(self):
        state = self.pending_backlog()
        first = state["pending"][0]["id"]  # The reference that the pass tries first.
        self.board.originals[first] = URLError("Synthetic transport failure")
        result, code = self.collect_command()
        self.assertEqual((len(self.board.asked), code), (40, 1))
        self.assertGreater(result["added"], 0)
        self.assertEqual(result["errors"][0]["error"], "network_error")
        health = result["sources"][0]
        self.assertEqual((health["status"], health["last_ok"], health["backlog_pending"]), ("error", self.healthy, True))
        self.assertIn(first, {entry["id"] for entry in self.store.collection_state("botnet", OWNER, "botnet")[1]["pending"]})

    def test_real_time_budget_after_identity_is_healthy_partial_progress(self):
        state, clock = self.pending_backlog(), Clock(1790000000)
        # The inbox takes longer than the 45 seconds of the pass.
        self.board.page_errors[None] = slow(clock, 46)
        result, code = self.collect_command(clock)
        self.assertEqual((len(self.board.asked), result["added"], code, result["errors"]), (2, 0, 0, []))
        self.assertCountEqual(self.store.collection_state("botnet", OWNER, "botnet")[1]["pending"], state["pending"])
        health = result["sources"][0]
        self.assertEqual((health["status"], health["backlog_pending"]), ("ok", True))
        self.assertGreater(health["last_ok"], self.healthy)

    def test_identity_preflight_budget_failure_remains_an_error(self):
        state, clock = self.pending_backlog(), Clock(1790000000)
        # The profile takes longer than the ten seconds that it has.
        self.board.profile = slow(clock, 11, self.board.profile)
        result, code = self.collect_command(clock)
        self.assertEqual((len(self.board.asked), result["added"], code), (1, 0, 1))
        self.assertEqual(result["errors"][0]["error"], "source_timeout")
        self.assertEqual(self.store.collection_state("botnet", OWNER, "botnet")[1], state)
        health = result["sources"][0]
        self.assertEqual((health["status"], health["last_ok"], health["backlog_pending"]), ("error", self.healthy, True))


class PassTests(unittest.TestCase):
    """Passes that have no inbox file. A pass gets the state that the pass before it gave, and it knows what the
    passes before it found."""
    def setUp(self):
        self.board = Board()
        self.settings = {"account_id": OWNER, "api_key_file": key_file(self)}
        self.state, self.known = {}, set()

    def collect(self):
        before = deepcopy(self.state)
        batch = adapter.collect(self.settings, self.state, frozenset(self.known), fetch=self.board)
        self.assertEqual(self.state, before)
        validate(batch)
        self.assertNotIn("PRIVATE PREVIEW", json.dumps(batch.__dict__))
        self.state = batch.state
        self.known |= {message["id"] for message in batch.messages}
        return batch

    def test_the_key_goes_with_the_profile_and_the_inbox_and_with_no_other_request(self):
        self.board.add(10)
        profile = self.board.profile

        def once(asked):
            self.settings["api_key_file"].unlink()  # The key is read once for a pass. The inbox needs it too.
            return profile

        self.board.profile = once
        self.assertEqual(len(self.collect().messages), 1)
        private, forum = {"Authorization": "Bearer " + KEY}, "https://botnet.com/api/forum"
        self.assertEqual([(asked.board, asked.url, asked.headers) for asked in self.board.asked], [
            ("botnet", forum + "/me", private),
            ("botnet", forum + "/inbox?limit=8", private),
            ("botnet", forum + "/topic-messages/post%3A" + uid(10), {}),
            ("botnet", forum + "/topics/" + TOPIC, {}),
            ("botnet", forum + "/topic-messages/thread%3A" + uid(100), {})])

    def test_a_key_that_the_board_does_not_take_is_no_credentials(self):
        self.board.add(10)
        for text in ("", "two words", "synthetic-k\u00e9y", "k" * 4097):
            with self.subTest(text=text[:20]):
                self.settings["api_key_file"].write_text(text + "\n", encoding="utf-8")
                batch = self.collect()
                self.assertEqual((batch.error, batch.messages, batch.complete), ("credentials_unavailable", [], False))
                self.assertEqual(self.board.asked, [])

    def test_overflow_is_explicit_and_recovers_without_dropping_pending(self):
        # 255 references wait for messages that the board does not show. The queue has room for one more.
        waiting = [{"id": mid(n), "reasons": ["reply"]} for n in range(1000, 1255)]
        self.state = {"pending": waiting}
        for n in range(10, 13):
            self.board.add(n)
        batch = self.collect()
        # The newest notification got the last place. Its message, the topic and the opener are three requests,
        # so 35 of the forty are left for the references that waited.
        self.assertEqual((batch.error, self.known, batch.unavailable), ("pending_overflow", {mid(12)}, 35))
        self.assertIsNone(batch.state.get("cursor"), "The page at which a reference found no place is read again")
        self.assertEqual(len(self.board.asked), 40)
        batch = self.collect()
        self.assertEqual((batch.error, len(batch.messages)), ("pending_overflow", 1))
        batch = self.collect()
        # The references that waited are still not there. That is no error of the source.
        self.assertEqual((batch.error, len(batch.messages), batch.complete), (None, 1, False))
        self.assertEqual(self.known, {mid(10), mid(11), mid(12)})
        self.assertEqual({entry["id"] for entry in batch.state["pending"]}, {entry["id"] for entry in waiting})

    def test_a_late_answer_ends_a_phase_as_a_phase_without_time_does(self):
        """The transport calls an answer that comes after its time source_timeout, on every board. The pass goes
        on from it as from a phase that has no time left to ask, which is budget_exhausted: the same mail, and the
        same state for the next pass. Only a pass whose identity check failed says which of the two it was."""
        # Notifications on two pages of the inbox, and three references that wait from passes before.
        for n in range(10, 22):
            self.board.add(n)
        for n in range(30, 33):
            self.board.originals[mid(n)] = original(n)
        self.state = {"cursor": "older:14", "pending": [{"id": mid(n), "reasons": ["reply"]} for n in range(30, 33)]}
        healthy = adapter.collect(self.settings, self.state, frozenset(), fetch=self.board)
        self.assertEqual((healthy.error, healthy.complete, len(healthy.messages)), (None, True, 15))
        asked = [path.split("/")[1] for path, _, _ in self.board.calls]
        self.assertEqual((asked[:3], set(asked[3:])), (["me", "inbox", "inbox"], {"topic-messages", "topics"}))
        for number in range(1, len(asked) + 1):
            with self.subTest(request=number, path=self.board.calls[number - 1][0]):
                spent, late = (vars(adapter.collect(self.settings, self.state, frozenset(),
                                                    fetch=failing(self.board, number, MailError(code))))
                               for code in ("budget_exhausted", "source_timeout"))
                self.assertEqual((spent.pop("error"), late.pop("error")),
                                 ("budget_exhausted", "source_timeout") if number == 1 else (None, None))
                self.assertEqual(late, spent)
                self.assertFalse(late["complete"])
        # The phase ends at that answer. The first of the references that waited fails, in the last phase of the
        # pass: nothing is asked after it, it goes to the end of the queue, and the others keep their place.
        paths = [path for path, _, _ in self.board.calls[:len(asked)]]
        first = paths.index("/topic-messages/" + quote(mid(30), safe="")) + 1
        del self.board.calls[:]
        late = adapter.collect(self.settings, self.state, frozenset(),
                               fetch=failing(self.board, first, MailError("source_timeout")))
        self.assertEqual([path for path, _, _ in self.board.calls], paths[:first - 1])
        self.assertEqual([entry["id"] for entry in late.state["pending"]],
                         [mid(n) for n in (31, 32, 17, 16, 15, 14, 13, 12, 11, 10, 30)])

    def test_a_rejected_or_broken_cursor_is_explicit_and_keeps_what_was_found(self):
        for n in (*range(10, 17), *range(18, 30)):
            self.board.add(n)
        self.board.originals[mid(25)] = 404
        self.assertEqual(self.collect().state["cursor"], "older:22")
        self.board.page_errors["older:22"] = 422
        batch = self.collect()
        self.assertEqual(batch.error, "http_422")
        self.assertIsNone(batch.state["cursor"])
        self.assertIn(mid(25), {x["id"] for x in batch.state["pending"]})

        # A malformed item on an older page reports failure but cannot pin the
        # scan there forever. Valid refs survive even a malformed page cursor.
        self.board.page_errors.clear()
        self.board.events.append({"id": 17, "reason": "mention", "threadId": None})
        self.collect()  # Head: 29 to 22.
        self.board.calls.clear()
        batch = self.collect()  # Older page: 21 to 18, the malformed 17, 16 to 14.
        self.assertIn(("/inbox", {"limit": 8, "cursor": "older:22"}, True), self.board.calls)
        self.assertEqual((batch.error, batch.state["cursor"]), ("invalid_response", "older:14"))
        batch = self.collect()  # Older page: 13 to 10, the end.
        self.assertIsNone(batch.state["cursor"])
        self.assertEqual(self.known, {mid(n) for n in (*range(10, 17), *range(18, 30)) if n != 25})
        self.board.add(30)
        self.board.bad_cursor = True
        batch = self.collect()
        self.assertEqual((batch.error, len(batch.messages)), ("invalid_response", 1))
        self.assertIn(mid(30), self.known)


class ExpandTests(unittest.TestCase):
    """The expand command at the invented board. The collect command filled the inbox from the same board, so the
    test writes nothing into the inbox file itself."""
    def test_expand_asks_the_board_once_for_each_original_and_without_the_key(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        store, board = Store(Path(folder.name) / "mail.sqlite3"), Board()
        sources = {"botnet": {"adapter": "botnet", "account_id": OWNER, "api_key_file": key_file(self)}}
        asks = {"source": "botnet", "thread": TOPIC, "through": 2, "sources": sources, "fetch": board}
        commands.execute(store, "init", sources=sources)
        board.add(10)
        board.add(11, parentMessageId=mid(10))
        self.assertEqual(commands.execute(store, "collect", sources=sources, fetch=board)[0]["added"], 2)
        sources["botnet"]["api_key_file"].unlink()  # What is public is read without the key of the account.
        board.calls.clear()
        result, code = commands.execute(store, "expand", **asks)
        self.assertEqual((code, result["complete"], result["root"]["id"], result["root"]["origin"]),
                         (0, True, TOPIC, "remote"))
        self.assertEqual([(item["id"], item["parent"]["id"], item["parent"]["status"]) for item in result["items"]],
                         [(mid(10), OPENER, "available"), (mid(11), mid(10), "available")])
        # The topic is the root and no parent. Message 10 is asked once, though it is also the parent of 11.
        asked = [("/topics/" + TOPIC, {}, False),
                 *[("/topic-messages/" + quote(name, safe=""), {}, False) for name in (mid(10), OPENER, mid(11))]]
        self.assertEqual(board.calls, asked)
        # An opener that is in another topic now is no parent, and a message that the board no longer has is
        # missing.
        board.originals[OPENER]["topicId"] = uid(999)
        board.originals[mid(11)] = 404
        result, code = commands.execute(store, "expand", **asks)
        first, second = result["items"]
        self.assertEqual((code, result["complete"], first["parent"]["status"], first["parent"]["error"]),
                         (1, False, "unavailable", "invalid_response"))
        self.assertEqual((second["target"]["remote_status"], second["target"]["error"], second["parent"]["status"]),
                         ("missing", "http_404", "available"))
        self.assertEqual(board.calls, asked * 2)
        result, code = commands.execute(store, "expand", **asks, local=True)
        self.assertEqual((result["fetched"], len(board.calls)), (False, 8), "A local read asks the board nothing")


if __name__ == "__main__":
    unittest.main()
