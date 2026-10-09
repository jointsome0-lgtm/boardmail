"""ClawdChat public confirmation, bounded retry and durable arrival contracts."""
import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import URLError
from urllib.parse import urlsplit

from boardmail import adapter_clawdchat as adapter, commands
from boardmail.adapters import validate
from boardmail.config import MailError
from boardmail.store import Store
from examples.fixtures import FakeBoard, status
from kit import Clock, failing, fixed, mark, new_inbox, one_pass

KEY = "synthetic-key-only"


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


def waits(number):
    """The entry of a reference that waits in the state of a pass: to a comment of this number, under the post of
    the tests, that the account was told of as a comment on that post."""
    return {"id": uid(number), "post": uid(100), "kind": "reply_to_post", "is_post": False, "types": ["comment"]}


def key_file(test):
    """The file of an invented key, in a folder that is gone when the test ends."""
    folder = tempfile.TemporaryDirectory()
    test.addCleanup(folder.cleanup)
    path = Path(folder.name) / "key"
    path.write_text(KEY + "\n")
    return path


class Board(FakeBoard):
    """An invented ClawdChat at the transport seam: the profile of the account, what the account is notified of,
    and the public originals by id. An original that it does not have is a 404.

    What the board has for a request can also be a number, which is the HTTP status that it answers with, an
    exception, which is how the request fails, or a function of the request that gives one of these. page_error
    has such a thing for the page of notifications at an offset, and down has one for every request.

    calls has each request in short: its path below the API, its query with numbers as numbers, and whether it
    carried the key. A request that carries the key where it must not, or none where it must, fails the test."""
    def __init__(self):
        super().__init__(self.answer)
        self.profile = {"id": uid(1)}
        self.events, self.originals, self.page_error, self.calls = [], {}, {}, []
        self.down = None

    def answer(self, asked):
        assert asked.board == "clawdchat" and asked.body is None, asked
        assert asked.url.startswith(adapter.ORIGIN + "/api/v1/"), asked.url
        private = "Authorization" in asked.headers
        assert asked.headers == ({"Authorization": "Bearer " + KEY} if private else {}), asked.headers
        path = urlsplit(asked.url).path.removeprefix("/api/v1")
        params = {name: int(value) if value.isdigit() else value for name, value in asked.params.items()}
        self.calls.append((path, params, private))
        answer = self.get(path, params, authenticated=private) if self.down is None else self.down
        if callable(answer):
            answer = answer(asked)
        return status(answer) if type(answer) is int else answer

    def get(self, path, params, *, authenticated):
        if path == "/agents/me":
            assert authenticated
            return self.profile
        if path == "/notifications":
            assert authenticated
            offset = params["offset"]
            if offset in self.page_error:
                return self.page_error[offset]
            return {"success": True, "items": self.events[offset:offset + params["limit"]], "total": len(self.events)}
        assert not authenticated, "Public originals must never use account credentials"
        return self.originals.get(path.rsplit("/", 1)[-1], 404)


class ClawdChatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = new_inbox(Path(self.temp.name) / "inbox.sqlite3")
        self.board = Board()
        self.settings = {"account_id": uid(1), "api_key_file": key_file(self), "adapter": "clawdchat"}

    def collect(self):
        batch, added = one_pass(self.store, "clawd", self.settings, self.board)
        self.assertNotIn("PRIVATE NOTIFICATION", json.dumps(batch.state))
        self.assertNotIn("PRIVATE NOTIFICATION", json.dumps(batch.messages))
        return batch, added

    def alone(self, state, known=()):
        """A pass that has no inbox file: it gets its state and what the passes before it found from the test."""
        before = json.dumps(state)
        batch = adapter.collect(self.settings, state, frozenset(known), fetch=self.board)
        self.assertEqual(json.dumps(state), before, "Input state is a snapshot")
        validate(batch)
        self.assertNotIn("PRIVATE NOTIFICATION", json.dumps([batch.state, batch.messages]))
        return batch

    def test_nested_comment_stays_visible_when_target_signal_arrives_next_pass(self):
        for number, later_type in ((70, "reply"), (71, "mention_comment")):
            with self.subTest(later_type=later_type):
                self.board.events = [event(number)]
                self.board.originals[uid(number)] = original(number, parent_id=uid(9))
                before = self.store.status()["counts"]["latest_arrival"]
                _, added = self.collect()
                self.assertEqual(added, 1)
                page = self.store.page(before, scope="addressed")
                self.assertEqual([m["id"] for m in page["messages"]], [uid(number)])
                self.assertIsNone(page["messages"][0]["addressing"])
                self.assertEqual(page["thread_activity"], [])
                checkpoint = page["next_after"]
                mark(self.store, "clawd", uid(number), "read")
                stored = self.store.show("clawd", uid(number))

                later = {**event(number, later_type), "id": uid(number + 2000)}
                self.board.events.insert(0, later)
                calls = len(self.board.calls)
                _, added = self.collect()
                self.assertEqual(added, 0)
                self.assertEqual(self.store.show("clawd", uid(number)), stored)
                self.assertEqual(self.store.page(checkpoint, scope="addressed")["scanned"], 0)
                self.assertFalse(any(path.startswith("/comments/") for path, _, _ in self.board.calls[calls:]))

    def test_supported_kinds_use_public_originals_and_preserve_arrivals(self):
        self.board.events = [event(10), event(11, "reply"), event(12, "mention_comment"),
                             event(100, "mention_post"), event(13, "follow"), event(14)]
        self.board.originals = {uid(n): original(n) for n in (10, 11, 12, 100, 14)}
        self.board.originals[uid(11)]["parent_id"] = uid(9)
        self.board.originals[uid(100)].update(title="Mention in title", content=None)
        self.board.originals[uid(14)]["author"]["id"] = uid(1)
        batch, added = self.collect()
        self.assertEqual(added, 4)
        self.assertTrue(batch.complete)
        self.assertEqual([m["kind"] for m in batch.messages], ["reply_to_post", "reply_to_comment", "mention", "mention"])
        self.assertEqual(batch.messages[1]["parent_id"], uid(9))
        self.assertEqual(batch.messages[-1]["body"], "")
        mark(self.store, "clawd", uid(10), "needs_reply")
        before = self.store.show("clawd", uid(10))
        self.assertEqual(self.collect()[1], 0)
        self.assertEqual(self.store.show("clawd", uid(10)), before)
        self.assertEqual(self.store.wait(0, 0)["next_after"], 4)

    def test_unavailable_and_bad_originals_do_not_block_siblings_or_expired_notifications(self):
        self.board.events = [event(n) for n in range(10, 17)]
        self.board.originals = {uid(n): original(n) for n in range(10, 17)}
        self.board.originals[uid(10)] = 403
        self.board.originals[uid(11)] = 404
        self.board.originals[uid(12)]["visibility"] = "private"
        self.board.originals[uid(13)]["post_id"] = uid(999)
        self.board.originals[uid(14)]["created_at"] = "bad timestamp"
        self.board.originals[uid(15)]["is_deleted"] = True
        batch, added = self.collect()
        self.assertEqual(added, 1)
        self.assertEqual(batch.unavailable, 4)
        self.assertEqual(batch.error, "invalid_response")
        self.assertEqual(batch.messages[0]["id"], uid(16))
        # Retried references survive a process/database reopen and upstream expiry.
        self.store = Store(self.store.path)
        self.board.events = []
        self.board.originals = {uid(n): original(n) for n in range(10, 17)}
        batch, added = self.collect()
        self.assertEqual(added, 6)
        self.assertTrue(batch.complete)

    def test_an_original_that_is_gone_is_unavailable_and_neither_an_error_nor_backlog_of_the_source(self):
        asked_for = "/comments/" + uid(11)
        under_deleted = {"id": uid(100), "title": "Public thread", "is_deleted": True}
        for name, gone in (("refused", 403), ("missing", 404), ("removed", 410),
                           ("deleted", original(11, is_deleted=True)), ("hidden", original(11, is_hidden=True)),
                           ("private", original(11, visibility="private")),
                           ("under a deleted post", original(11, post=under_deleted))):
            with self.subTest(gone=name):
                self.store, self.board = new_inbox(Path(self.temp.name) / (name + ".sqlite3")), Board()
                self.board.events = [event(10), event(11)]
                self.board.originals = {uid(10): original(10), uid(11): gone}
                for added in (1, 0, 0):
                    del self.board.calls[:]
                    batch, got = self.collect()
                    self.assertEqual((got, batch.error, batch.unavailable), (added, None, 1))
                    # Its reference waits and remembers the answer. The original is asked for on each pass, and
                    # each pass has finished: no more mail is on its way, so no page names the source.
                    self.assertIn(asked_for, [path for path, _, _ in self.board.calls])
                    self.assertEqual(batch.state["pending"], self.gone([11]))
                    self.assertTrue(batch.complete)
                    self.assertEqual(commands.execute(self.store, "list")[0]["sources"], [])
                self.board.originals[uid(11)] = original(11)
                batch, got = self.collect()
                self.assertEqual((got, batch.unavailable, batch.complete, batch.state["pending"]), (1, 0, True, []))
                self.assertEqual(self.store.show("clawd", uid(11))["body"], "Public original 11")

    def gone(self, numbers):
        """The queue of a state: a reference for each of these numbers, to a comment that the board does not have.
        Each remembers that a pass before was answered so."""
        return [{**waits(n), "gone": True} for n in numbers]

    def asked_for(self):
        """The comments that the board was asked for since its calls were last cleared, in the order of asking."""
        return [path.rsplit("/", 1)[-1] for path, _, _ in self.board.calls if path.startswith("/comments/")]

    def test_a_queue_of_gone_references_is_no_backlog_also_where_a_pass_does_not_ask_for_each(self):
        batch = self.alone({"pending": self.gone(range(1000, 1020)), "offset": 0})
        # A pass asks for the eight references at the head of the queue, and they go to its end. The pass has
        # finished all the same: each of the twelve others remembers that it is gone.
        self.assertEqual((batch.unavailable, batch.error, batch.complete), (8, None, True))
        self.assertEqual(self.asked_for(), [uid(n) for n in range(1000, 1008)])
        self.assertEqual(batch.state["pending"], self.gone((*range(1008, 1020), *range(1000, 1008))))
        # A comment that is there again is delivered when the turn of its reference comes.
        self.board.originals[uid(1009)] = original(1009)
        batch = self.alone(batch.state)
        self.assertEqual(([message["id"] for message in batch.messages], batch.error, batch.complete),
                         ([uid(1009)], None, True))
        self.assertEqual(len(batch.state["pending"]), 19)
        # A reference that no pass has asked for is backlog: its comment may be there.
        del self.board.calls[:]
        batch = self.alone({"pending": [*self.gone(range(1000, 1019)), waits(1019)], "offset": 0})
        self.assertNotIn(uid(1019), self.asked_for())
        self.assertEqual((batch.unavailable, batch.error, batch.complete), (8, None, False))

    def test_a_reference_that_was_gone_and_fails_in_another_way_is_backlog_until_it_is_gone_again(self):
        for name, failure, error in (("not reached", URLError("The board is not reached"), "network_error"),
                                     ("of another post", original(1002, post_id=uid(999)), "invalid_response"),
                                     # Another comment that is gone says nothing of the one that was asked for.
                                     ("another, deleted", original(999, is_deleted=True), "invalid_response"),
                                     ("another, hidden", original(999, is_hidden=True), "invalid_response")):
            with self.subTest(failure=name):
                self.board = Board()
                self.board.originals[uid(1002)] = failure
                batch = self.alone({"pending": self.gone(range(1000, 1020)), "offset": 0})
                self.assertEqual((batch.error, batch.complete), (error, False))
                self.assertEqual([entry for entry in batch.state["pending"] if "gone" not in entry], [waits(1002)])
                # The board answers as before. Until the reference is asked for again, it is not known to be
                # gone: the next pass asks for eight others.
                del self.board.originals[uid(1002)]
                del self.board.calls[:]
                batch = self.alone(batch.state)
                self.assertNotIn(uid(1002), self.asked_for())
                self.assertEqual((batch.error, batch.complete), (None, False))
                del self.board.calls[:]
                batch = self.alone(batch.state)
                self.assertIn(uid(1002), self.asked_for())
                self.assertEqual((batch.error, batch.complete), (None, True))
                self.assertCountEqual(batch.state["pending"], self.gone(range(1000, 1020)))

    def test_a_gone_reference_that_gets_no_answer_in_the_time_of_its_phase_stays_gone(self):
        state = {"pending": self.gone((30, 31, 32)), "offset": 0}
        for code in ("budget_exhausted", "source_timeout"):
            with self.subTest(code=code):
                # The second request of the pass, after the profile, is the first for a reference.
                batch = adapter.collect(self.settings, state, frozenset(), fetch=failing(self.board, 2, MailError(code)))
                self.assertEqual((batch.error, batch.unavailable, batch.complete), (None, 0, True))
                self.assertEqual(batch.state["pending"], self.gone((31, 32, 30)))
        # Only true says that a reference is gone. Anything else in its place is read as no such answer.
        for value in (False, 1, "true", None):
            with self.subTest(value=value):
                state = {"pending": [{**waits(30), "gone": value}], "offset": 0}
                batch = adapter.collect(self.settings, state, frozenset(),
                                        fetch=failing(self.board, 2, MailError("budget_exhausted")))
                self.assertEqual((batch.error, batch.complete), (None, False))
                self.assertEqual(batch.state["pending"], [waits(30)])

    def test_fresh_head_and_cyclic_backfill_progress_past_persistent_failure(self):
        # Five pages of notifications. The original of the first one is never reached.
        self.board.events = [event(n) for n in range(10, 50)]
        self.board.originals = {uid(n): original(n) for n in range(10, 51)}
        self.board.originals[uid(10)] = URLError("The board is not reached")
        self.collect()
        self.board.events.insert(0, event(50))
        batch, _ = self.collect()
        self.assertIn(uid(50), {m["id"] for m in batch.messages})
        for _ in range(6):
            self.collect()
        known = self.store.known("clawd", uid(1))
        self.assertEqual(known, {uid(n) for n in range(11, 51)})
        self.assertNotIn(uid(10), known)
        offsets = [params["offset"] for path, params, _ in self.board.calls if path == "/notifications"]
        self.assertEqual(offsets, [0, 0, 8, 0, 16, 0, 24, 0, 32, 0, 40, 0, 0, 8],
                         "Each pass reads the head and one page of the sweep, which starts again at its end")

    def test_full_queue_reports_overflow_and_revisits_affected_discovery_page(self):
        # 256 references wait for originals that the board no longer has. The queue holds no more.
        waiting = [{"id": uid(n), "post": uid(100), "kind": "reply_to_post", "is_post": False} for n in range(1000, 1256)]
        self.board.events = [event(n) for n in range(10, 34)]
        self.board.originals = {uid(n): original(n) for n in range(10, 34)}
        batch = self.alone({"pending": waiting, "offset": 8})
        self.assertEqual((batch.error, len(batch.messages), batch.unavailable), ("pending_overflow", 8, 8))
        self.assertEqual(batch.state["offset"], 8, "The page at which a reference was lost is read again")
        held = [entry["id"] for entry in batch.state["pending"]]
        # The eight that were tried first went to the end of the queue. The sixteen after them were the oldest
        # when the two pages came, and each new reference took the place of one.
        self.assertEqual(held, [uid(n) for n in (*range(1024, 1256), *range(1000, 1008), *range(18, 26))])
        found = {message["id"] for message in batch.messages}
        self.assertEqual(found, {uid(n) for n in range(10, 18)})
        batch = self.alone(batch.state, found)
        self.assertEqual((batch.error, batch.messages, batch.state["offset"]), (None, [], 16), "Nothing is lost now, so the sweep moves on")
        self.assertEqual(len(batch.state["pending"]), 248)

    def test_slow_retry_backlog_cannot_consume_fresh_original_budget(self):
        pending = [{"id": uid(n), "post": uid(100), "kind": "reply_to_post", "is_post": False} for n in range(10, 20)]
        self.board.events = [event(20)]
        clock = Clock(1790000000)

        def silent(asked):
            """No answer comes: the socket stays silent for as long as the transport lets it."""
            clock.advance(min(4, asked.left))
            return TimeoutError()

        self.board.originals = {uid(20): original(20), **{uid(n): silent for n in range(10, 20)}}
        with fixed(clock):
            batch = self.alone({"pending": pending, "offset": 0})
        self.assertEqual([message["id"] for message in batch.messages], [uid(20)])
        self.assertEqual(len(batch.state["pending"]), 10)
        # The fifteen seconds of the old references are four tries: three for the first of them, one for the next.
        self.assertEqual([path for path, _, _ in self.board.calls],
                         ["/agents/me", *["/comments/" + uid(10)] * 3, "/comments/" + uid(11), "/notifications",
                          "/comments/" + uid(20)])

    def test_rate_limit_stops_source_and_rejected_backfill_position_resets(self):
        self.board.events = [event(n) for n in range(10, 34)]
        self.board.originals = {uid(n): original(n) for n in range(10, 34)}
        self.collect()
        self.board.page_error[8] = 429
        self.board.calls.clear()
        batch, _ = self.collect()
        self.assertEqual(batch.error, "http_429")
        self.assertEqual(batch.state["offset"], 8)
        self.assertEqual([path for path, _, _ in self.board.calls], ["/agents/me", "/notifications", "/notifications"])
        self.board.page_error[8] = 422
        self.assertEqual(self.collect()[0].state["offset"], 0)
        self.board.page_error.clear()
        for _ in range(4):
            self.collect()
        self.assertEqual(len(self.store.known("clawd", uid(1))), 24)

    def test_wrong_token_owner_stops_before_notifications(self):
        # A pass leaves a reference to an original that the board does not have.
        self.board.events = [event(10)]
        self.collect()
        _, state, revision = self.store.collection_state("clawd", uid(1), "clawdchat")
        self.assertEqual([entry["id"] for entry in state["pending"]], [uid(10)])
        self.board.profile = {"id": uid(999)}
        self.board.calls.clear()
        batch, added = self.collect()
        self.assertEqual(batch.error, "account_mismatch")
        self.assertEqual(added, 0)
        self.assertEqual([p for p, _, _ in self.board.calls], ["/agents/me"])
        self.assertEqual(batch.state, state)
        # The pass took no revision, so a pass that started before it and ends after it is not late.
        self.assertEqual(self.store.collection_state("clawd", uid(1), "clawdchat")[2], revision)

    def test_local_setup_errors_keep_pending_state_and_planned_budget_is_partial(self):
        state = {"offset": 8, "pending": [{"id": uid(10), "post": uid(100),
                                          "kind": "reply_to_post", "is_post": False}]}
        for settings, code in [({"account_id": uid(1)}, "credentials_unavailable"),
                               ({"account_id": "not-a-uuid"}, "invalid_config")]:
            with self.subTest(code=code):
                batch = adapter.collect(settings, state, frozenset(), fetch=self.board)
                self.assertEqual(batch.error, code)
                self.assertEqual(batch.state, state)
                self.assertFalse(batch.complete)
                self.assertEqual(self.board.asked, [])
        # A planned discovery phase whose time is over is partial progress, not an outage: where it has none left
        # to ask, and where an answer comes after it.
        for over in ("budget_exhausted", "source_timeout"):
            with self.subTest(over=over):
                self.board.page_error[0] = MailError(over)
                batch, added = self.collect()
                self.assertEqual(added, 0)
                self.assertIsNone(batch.error)
                self.assertFalse(batch.complete)

    def test_bad_reference_isolated_and_unsafe_canonical_links_use_public_api(self):
        self.board.events = [event(9), event(10)]
        del self.board.events[0]["comment_id"]
        for url in ["https://evil.invalid/post/10", "https://clawdchat.cn@evil.invalid/10",
                    "https://clawdchat.cn:444/10", "https://user@clawdchat.cn/10", "http://clawdchat.cn/10",
                    "https://clawdchat.cn/\nsecret", "https://clawdchat.cn\\@evil.invalid/10"]:
            with self.subTest(url=url):
                self.board.originals[uid(10)] = original(10, web_url=url)
                batch = adapter.collect(self.settings, {}, frozenset(), fetch=self.board)
                validate(batch)
                self.assertEqual(batch.error, "invalid_response")
                self.assertEqual(batch.messages[0]["url"], adapter.ORIGIN + "/api/v1/comments/" + uid(10))


class RequestTests(unittest.TestCase):
    """What the client of ClawdChat asks the board: with which key, and how often. These passes have no inbox
    file. A pass gets its state from the test."""
    def setUp(self):
        self.board = Board()
        self.settings = {"account_id": uid(1), "api_key_file": key_file(self)}

    def collect(self, state=None, board=None):
        batch = adapter.collect(self.settings, state or {}, frozenset(), fetch=board or self.board)
        validate(batch)
        return batch

    def test_the_key_goes_with_the_profile_and_the_notifications_and_with_no_other_request(self):
        self.board.events = [event(10), event(100, "mention_post")]
        self.board.originals = {uid(10): original(10), uid(100): original(100, title="A post")}
        profile = self.board.profile

        def once(asked):
            self.settings["api_key_file"].unlink()  # The key is read once for a pass. The notifications need it too.
            return profile

        self.board.profile = once
        self.assertEqual(len(self.collect().messages), 2)
        private = {"Authorization": "Bearer " + KEY}
        self.assertEqual([(asked.board, asked.url, asked.headers) for asked in self.board.asked], [
            ("clawdchat", "https://clawdchat.cn/api/v1/agents/me", private),
            ("clawdchat", "https://clawdchat.cn/api/v1/notifications?limit=8&offset=0", private),
            ("clawdchat", "https://clawdchat.cn/api/v1/comments/" + uid(10), {}),
            ("clawdchat", "https://clawdchat.cn/api/v1/posts/" + uid(100), {})])

    def test_a_key_that_the_board_does_not_take_is_no_credentials(self):
        for text in ("", "two words", "synthetic-k\u00e9y", "k" * 4097):
            with self.subTest(text=text[:20]):
                self.settings["api_key_file"].write_text(text + "\n", encoding="utf-8")
                batch = self.collect()
                self.assertEqual((batch.error, batch.complete), ("credentials_unavailable", False))
                self.assertEqual(self.board.asked, [])

    def test_a_board_that_is_down_is_asked_three_times_and_one_that_refuses_once(self):
        for answer, code, times in [(408, "http_408", 3), (500, "http_500", 3), (502, "http_502", 3),
                                    (503, "http_503", 3), (504, "http_504", 3),
                                    (URLError("The board is not reached"), "network_error", 3),
                                    (400, "http_400", 1), (401, "http_401", 1), (403, "http_403", 1),
                                    (404, "http_404", 1), (422, "http_422", 1), (429, "http_429", 1),
                                    # What the transport says of a redirect and of an answer over the size cap.
                                    (MailError("redirect_refused"), "redirect_refused", 1),
                                    (MailError("response_too_large"), "response_too_large", 1)]:
            with self.subTest(answer=answer):
                board = Board()
                board.profile = answer
                batch = self.collect(board=board)
                self.assertEqual((batch.error, len(board.asked)), (code, times))
        # A try that fails is not the end of the request: the third one is answered, and the pass goes on.
        answers = iter([503, URLError("The board is not reached"), {"id": uid(1)}])
        self.board.profile = lambda asked: next(answers)
        batch = self.collect()
        self.assertEqual((batch.error, batch.complete), (None, True))
        self.assertEqual([path for path, _, _ in self.board.calls], ["/agents/me"] * 3 + ["/notifications"])

    def test_an_answer_that_says_no_success_is_not_read(self):
        self.board.profile = {"success": False, "id": uid(1)}
        batch = self.collect()
        self.assertEqual((batch.error, len(self.board.asked)), ("invalid_response", 1))
        # The same holds for a public original: it is no mail, and its reference waits.
        self.board.profile = {"id": uid(1)}
        self.board.events = [event(10), event(11)]
        self.board.originals = {uid(10): {**original(10), "success": False}, uid(11): original(11)}
        batch = self.collect()
        self.assertEqual((batch.error, [message["id"] for message in batch.messages]), ("invalid_response", [uid(11)]))
        self.assertEqual([entry["id"] for entry in batch.state["pending"]], [uid(10)])

    def test_a_pass_asks_the_board_forty_times_and_no_more(self):
        # No original is reached, and each one is tried three times: the eight that waited, then the new ones.
        pending = [{"id": uid(n), "post": uid(100), "kind": "reply_to_post", "is_post": False} for n in range(30, 38)]
        self.board.events = [event(n) for n in range(10, 18)]
        self.board.originals = {uid(n): URLError("The board is not reached") for n in (*range(10, 18), *range(30, 38))}
        batch = self.collect({"pending": pending, "offset": 0})
        self.assertEqual((batch.error, batch.messages, batch.complete), ("network_error", [], False))
        self.assertEqual(len(self.board.asked), 40)
        # One request for the profile, 24 for the eight that waited, one for the notifications, and 14 are left
        # for the new references: four of them in full and two tries of the fifth.
        self.assertEqual([path for path, _, _ in self.board.calls[26:]],
                         [path for n in range(10, 15) for path in ["/comments/" + uid(n)] * 3][:14])
        self.assertEqual(len(batch.state["pending"]), 16)


if __name__ == "__main__":
    unittest.main()
