"""SwarmMemo delivery, cursors and what a pass does where the board fails, without a real agent."""
from contextlib import nullcontext
import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import URLError
from urllib.parse import urlsplit

from boardmail import adapter_swarmmemo as adapter, commands
from boardmail.config import MailError, load
from examples.fixtures import FakeBoard, status
from kit import Clock, fixed, mark, new_inbox


def fingerprint(n):
    return format(n, "064x")


def mid(n):
    return format(n, "032x")


ME, WRITER = fingerprint(1), fingerprint(2)
E = "/e/"
THREAD = "/api/thread/"
AGENT = "/api/agent/"


def updates_from(cursor=None):
    """The request for a page of updates, as calls of a Board has it."""
    return "/api/updates", {"agent": ME, "limit": "200", **({"cursor": cursor} if cursor else {})}


def late(clock, seconds):
    """What a board has for a request whose answer takes this many seconds to come. The clock moves by them, and
    where the request had no more time than that, the transport calls the answer late."""
    def given(asked):
        clock.advance(seconds)
        return MailError("source_timeout") if seconds >= asked.left else None
    return given


class Board(FakeBoard):
    """An invented SwarmMemo at the transport seam: the public messages by id, and for each the reasons for which
    the board lists it for the account. A message that has none is activity of a room.

    A page of updates holds the messages after its cursor in the order of their sequence, per_page of them at
    most. A cursor is of one generation of the board, and the board refuses one of another with 409. A read
    without a cursor begins window messages before the newest.

    A message that was hidden is given as before, without what it said.

    errors has what the board answers instead for a path, and page_errors for the updates read from a cursor.
    A number is the HTTP status that it answers with, an exception is how the request fails, a dict is the
    answer, and a function of the request gives one of these, or None for the usual answer.

    calls has each request in short: its path and its query. A request that carries a header of its own, as a
    key would be, fails the test."""
    def __init__(self):
        super().__init__(self.answer)
        self.posts, self.reasons, self.errors, self.page_errors, self.calls = {}, {}, {}, {}, []
        self.per_page, self.window, self.generation, self.current = 200, 1000, "first", ME

    def post(self, n, *reasons, by=WRITER, reply_to=None, **changes):
        """A message of this number. It answers the message of the number reply_to, and the board lists it for
        the account for these reasons."""
        signed = {} if by == "anonymous" else {"author_handle": "reader" if by == ME else "writer"}
        self.posts[mid(n)] = {"type": "message", "visibility": "public", "id": mid(n), "sequence": n,
                              "room": "lobby", "page": "main", "text": f"Public message {n}", "kind": "note",
                              "author": by, **signed, "created_at": 1790000000 + n, "hidden": False,
                              **({"reply_to": mid(reply_to)} if reply_to else {}), **changes}
        if reasons:
            self.reasons[mid(n)] = set(reasons)

    def given(self, post):
        return post | {"type": "tombstone", "text": "", "hidden_by": "room"} if post["hidden"] else post

    def cursor(self, n):
        """The cursor of a reader that has read up to the message of this number."""
        return f"{self.generation}:{n}"

    def answer(self, asked):
        assert asked.board == "swarmmemo" and asked.body is None and asked.headers == {}, asked
        assert asked.url.startswith(adapter.ORIGIN + "/"), asked.url
        path, params = urlsplit(asked.url).path, asked.params
        self.calls.append((path, params))
        instead = self.page_errors.get(params.get("cursor")) if path == "/api/updates" else self.errors.get(path)
        if callable(instead):
            instead = instead(asked)
        if instead is not None:
            return status(instead) if type(instead) is int else instead
        if path == "/api/updates":
            return self.updates(params)
        if path.startswith(AGENT):
            assert not params, params
            return {"ok": True, "agent": {"id": ME, "handle": "reader"}} if path == AGENT + ME else status(404)
        post = self.posts.get(path.rsplit("/", 1)[-1])
        if post is None:
            return status(404)
        if path.startswith(E):
            assert params == {"format": "json"}, params
            return self.envelope([self.given(post)], post["sequence"], has_more=False)
        assert path.startswith(THREAD) and params == {"limit": "1"}, asked.url
        root = post
        while "reply_to" in root:
            root = self.posts[root["reply_to"]]
        return self.envelope([self.given(root)], root["sequence"], has_more=root is not post,
                             requested_message_id=post["id"], root_id=root["id"], room=root["room"])

    def envelope(self, messages, read_to, **data):
        # The board leaves the list of a page without messages out.
        return {"ok": True, "generation": self.generation, **({"messages": messages} if messages else {}),
                "next_cursor": self.cursor(read_to), "data": data}

    def updates(self, params):
        assert params["agent"] == ME and params["limit"] == "200", params
        stream = sorted((post for post in self.posts.values() if post["author"] != ME),
                        key=lambda post: post["sequence"])
        if "cursor" in params:
            generation, _, read_to = params["cursor"].partition(":")
            if generation != self.generation:
                return status(409)
            read_to = int(read_to)
        else:
            read_to = stream[-self.window]["sequence"] - 1 if len(stream) > self.window else 0
        newer = [post for post in stream if post["sequence"] > read_to]
        page = newer[:self.per_page]
        listed = {reason: [post["id"] for post in page if reason in self.reasons.get(post["id"], ())]
                  for reason in ("replies", "addressed", "mentions")}
        return self.envelope([self.given(post) for post in page], page[-1]["sequence"] if page else read_to,
                             agent=self.current, scope="agent", has_more=len(newer) > len(page), **listed, wakeups=[],
                             room_activity=[post["id"] for post in page if post["id"] not in self.reasons])


class SwarmMemoTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name)
        config = self.path / "config.json"
        config.write_text(json.dumps({"database": "mail.sqlite3", "sources": {"swarmmemo": {"account_id": ME}}}))
        self.settings = load(config)["sources"]["swarmmemo"]
        self.asks = {"sources": {"swarmmemo": self.settings}}
        self.new("mail")

    def new(self, name):
        """A new inbox and a board that has what the account wrote: a post of its own, and under the post of
        another an answer of its own."""
        self.store, self.board = new_inbox(self.path / (name + ".sqlite3")), Board()
        self.board.post(1, by=ME)
        self.board.post(2)
        self.board.post(4, by=ME, reply_to=2)

    def collect(self, clock=None):
        """One pass of the collect command over the board: its exit code, how many messages it added, its
        errors, and of the source its status, how many originals were unavailable and whether more waits. With a
        clock the time stands still for the pass unless the board moves it."""
        with fixed(clock) if clock else nullcontext():
            result, code = commands.execute(self.store, "collect", **self.asks, fetch=self.board)
        health, = result["sources"]
        return (code, result["added"], [found["error"] for found in result["errors"]], health["status"],
                health["unavailable"], health["backlog_pending"])

    def arrived(self):
        return self.store.known("swarmmemo", ME)

    def context(self, id):
        return commands.execute(self.store, "context", source="swarmmemo", id=id, **self.asks, fetch=self.board)

    def test_a_reply_an_addressed_message_and_a_mention_arrive_once_and_activity_of_a_room_does_not(self):
        self.board.post(10, "replies", reply_to=1)
        self.board.post(11, "replies", reply_to=4)
        self.board.post(12, "addressed")
        self.board.post(13, "mentions", reply_to=2)
        self.board.post(14, "replies", "mentions", reply_to=1)
        self.board.post(15, "replies", by="anonymous", reply_to=1)
        self.board.post(16, reply_to=2)  # Activity of the room.
        # The account has a room of its own, which the board names with its fingerprint.
        self.board.post(5, by=ME, room="@" + ME)
        self.board.post(17, "replies", reply_to=5, room="@" + ME)
        self.assertEqual(self.collect(), (0, 7, [], "ok", 0, False))
        shown = {n: self.store.show("swarmmemo", mid(n)) for n in (10, 11, 12, 13, 14, 15, 17)}
        self.assertEqual(self.arrived(), {message["id"] for message in shown.values()})
        self.assertEqual({n: (message["thread_id"], message["parent_id"], message["kind"], message["addressing"])
                          for n, message in shown.items()}, {
            # The thread is the root that the board gives, also for an answer deep in it.
            10: (mid(1), mid(1), "reply_to_post", "direct"),
            11: (mid(2), mid(4), "reply_to_comment", "direct"),
            12: (mid(12), None, "mention", "mention"),
            13: (mid(2), mid(2), "mention", "mention"),
            14: (mid(1), mid(1), "mention", "direct+mention"),
            15: (mid(1), mid(1), "reply_to_post", "direct"),
            17: (mid(5), mid(5), "reply_to_post", "direct")})
        first = shown[10]
        self.assertEqual(
            (first["title"], first["author"], first["body"], first["url"], first["created_at"], first["provider_seq"]),
            ("#lobby", "writer", "Public message 10", "https://swarmmemo.com/e/" + mid(10), 1790000010, 10))
        self.assertEqual((shown[15]["author"], shown[17]["title"]), ("anonymous", "@" + ME))
        # A pass without a cursor asks for the agent first. Then the board was asked once for each branch, and for
        # the one message that is neither mail nor a root.
        self.assertEqual(self.board.calls, [
            (AGENT + ME, {}), updates_from(), (THREAD + mid(10), {"limit": "1"}), (THREAD + mid(11), {"limit": "1"}),
            (E + mid(4), {"format": "json"}), (THREAD + mid(17), {"limit": "1"})])
        # A page of mail shows the root of a thread and what a message answers without a request.
        listed, _ = commands.execute(self.store, "list")
        brief = {message["id"]: message["brief"] for message in listed["messages"]}
        root, parent = brief[mid(11)]["root"], brief[mid(11)]["parent"]
        self.assertEqual((root["status"], root["body"], parent["status"], parent["body"]),
                         ("cached", "Public message 2", "cached", "Public message 4"))
        self.assertEqual((brief[mid(10)]["root"]["body"], brief[mid(10)]["parent"]["status"]),
                         ("Public message 1", "same_as_root"))
        self.assertEqual((brief[mid(12)]["root"]["status"], brief[mid(12)]["parent"]["status"]),
                         ("current_message", "none"))
        # The next pass reads on from where this one ended.
        del self.board.calls[:]
        self.assertEqual(self.collect(), (0, 0, [], "ok", 0, False))
        self.assertEqual(self.board.calls, [updates_from(self.board.cursor(17))])
        self.board.post(18, "replies", reply_to=4)
        self.assertEqual(self.collect(), (0, 1, [], "ok", 0, False))
        self.assertEqual(self.store.show("swarmmemo", mid(18))["thread_id"], mid(2))

    def test_a_pass_reads_page_after_page_and_the_next_goes_on_where_its_requests_ended(self):
        # 120 messages of a room, two to a page, and four answers to the account among them.
        replies = (10, 50, 90, 129)
        for n in range(10, 130):
            self.board.post(n, *(("replies",) if n in replies else ()), reply_to=1 if n in replies else None)
        self.board.per_page = 2
        # A pass has forty requests: the agent, 38 pages and the thread of the first answer.
        self.assertEqual(self.collect(), (0, 2, [], "ok", 0, True))
        self.assertEqual((len(self.board.asked), self.arrived()), (40, {mid(10), mid(50)}))
        del self.board.asked[:], self.board.calls[:]
        self.assertEqual(self.collect(), (0, 2, [], "ok", 0, False))
        self.assertEqual(self.board.calls[0], updates_from(self.board.cursor(84)))
        # 23 pages to the end, and the thread of the first answer that this pass meets.
        self.assertEqual((len(self.board.asked), self.arrived()), (24, {mid(n) for n in replies}))

    def test_a_cursor_that_the_board_refuses_is_given_up_and_nothing_arrives_twice(self):
        self.board.post(10, "replies", reply_to=1)
        self.assertEqual(self.collect(), (0, 1, [], "ok", 0, False))
        # The board was restored from a copy: it takes no cursor of before.
        self.board.generation = "restored"
        self.board.post(11, "replies", reply_to=1)
        self.assertEqual(self.collect(), (1, 0, ["http_409"], "error", 0, True))
        del self.board.calls[:]
        self.assertEqual(self.collect(), (0, 1, [], "ok", 0, False))
        self.assertEqual(self.board.calls[:2], [(AGENT + ME, {}), updates_from()])
        # A cursor that the board calls none of its own.
        self.board.page_errors[self.board.cursor(11)] = 400
        self.board.post(12, "replies", reply_to=1)
        self.assertEqual(self.collect(), (1, 0, ["http_400"], "error", 0, True))
        self.assertEqual(self.collect(), (0, 1, [], "ok", 0, False))
        self.assertEqual(self.arrived(), {mid(10), mid(11), mid(12)})

    def test_a_value_that_is_no_agent_of_the_board_is_an_error_and_not_an_empty_inbox(self):
        # The updates read answers for it as for an agent without mail. The record of the agent does not.
        self.board.errors[AGENT + ME] = 404
        self.assertEqual(self.collect(), (1, 0, ["http_404"], "error", 0, True))
        self.assertEqual(self.board.calls, [(AGENT + ME, {})])
        # A board that fails says nothing of the agent.
        self.board.errors[AGENT + ME] = 503
        self.assertEqual(self.collect(), (1, 0, ["http_503"], "error", 0, True))
        # An agent that has changed its key is read by the old fingerprint, and its record names the new one.
        self.board.errors[AGENT + ME] = {"ok": True, "agent": {"id": WRITER}}
        del self.board.calls[:]
        self.board.post(10, "replies", reply_to=1)
        self.assertEqual(self.collect(), (0, 1, [], "ok", 0, False))
        # With a cursor the agent is known, and the board is not asked about it again.
        del self.board.calls[:]
        self.assertEqual(self.collect(), (0, 0, [], "ok", 0, False))
        self.assertEqual(self.board.calls, [updates_from(self.board.cursor(10))])
        del self.board.errors[AGENT + ME]

    def test_a_message_of_the_agent_under_its_new_key_is_no_mail(self):
        # The board names the agent by its new fingerprint after a key change, and its new messages carry that one.
        self.board.post(10, "replies", reply_to=1)
        self.assertEqual(self.collect(), (0, 1, [], "ok", 0, False))
        self.board.current = fingerprint(3)
        self.board.post(11, "replies", by=fingerprint(3), reply_to=10)
        self.board.post(12, "replies", reply_to=11)
        self.assertEqual(self.collect(), (0, 1, [], "ok", 0, False))
        self.assertEqual(self.arrived(), {mid(10), mid(12)})

    def test_a_page_with_only_messages_of_the_agent_is_passed_and_the_page_after_it_is_read(self):
        # The board leaves out what the configured fingerprint wrote, so the first page is given as it is.
        for name, by in (("configured key", ME), ("new key", fingerprint(3))):
            with self.subTest(author=name):
                self.new(name)
                self.board.post(10, "replies", reply_to=1)
                self.assertEqual(self.collect(), (0, 1, [], "ok", 0, False))
                self.board.current = fingerprint(3)
                self.board.post(11, "replies", by=by, reply_to=10)
                self.board.post(12, "replies", reply_to=11)
                self.board.page_errors[self.board.cursor(10)] = self.board.envelope(
                    [self.board.posts[mid(11)]], 11, agent=fingerprint(3), scope="agent", has_more=True,
                    replies=[mid(11)], addressed=[], mentions=[], wakeups=[], room_activity=[])
                del self.board.calls[:]
                self.assertEqual(self.collect(), (0, 1, [], "ok", 0, False))
                self.assertEqual([call for call in self.board.calls if call[0] == "/api/updates"],
                                 [updates_from(self.board.cursor(10)), updates_from(self.board.cursor(11))])
                self.assertEqual(self.arrived(), {mid(10), mid(12)})
                # The next pass reads on after the second page and finds nothing new.
                del self.board.calls[:]
                self.assertEqual(self.collect(), (0, 0, [], "ok", 0, False))
                self.assertEqual(self.board.calls, [updates_from(self.board.cursor(12))])

    def test_a_board_whose_cursor_stands_still_is_not_read_in_a_circle(self):
        self.board.post(10, "replies", reply_to=1)
        self.board.per_page = 1
        # The page after the first names its own cursor as the next one and says that more is there.
        still = self.board.cursor(2)
        self.board.page_errors[still] = {"ok": True, "generation": "first", "next_cursor": still, "data": {
            "has_more": True, "replies": [], "addressed": [], "mentions": [], "room_activity": []}}
        self.assertEqual(self.collect(), (1, 0, ["pagination_no_progress"], "error", 0, True))
        self.assertEqual(len(self.board.asked), 3)
        del self.board.page_errors[still]
        self.assertEqual(self.collect(), (0, 1, [], "ok", 0, False))

    def test_a_message_that_the_board_gives_no_answer_about_waits_and_the_rest_of_its_page_arrives(self):
        clock = Clock(1790000000)
        for name, failure, gave, came in (
                ("broken", 503, (1, 3, ["http_503"], "error", 0, True), (10, 12, 13)),
                ("not reached", URLError("The board is not reached"), (1, 3, ["network_error"], "error", 0, True),
                 (10, 12, 13)),
                # After a pause the board is asked nothing more, so what needs another answer waits as well.
                ("busy", 429, (1, 2, ["http_429"], "error", 0, True), (10, 12)),
                # So it is where the time of the pass is over, and that is no failure of the source.
                ("late", late(clock, 46), (0, 2, [], "ok", 0, True), (10, 12))):
            with self.subTest(failure=name):
                self.new(name)
                self.board.post(3, by=ME)
                self.board.post(10, "replies", reply_to=1)
                self.board.post(11, "replies", reply_to=4)
                self.board.post(12, "addressed")
                self.board.post(13, "replies", reply_to=3)
                self.board.errors[THREAD + mid(11)] = failure
                self.assertEqual(self.collect(clock), gave)
                self.assertEqual(self.arrived(), {mid(n) for n in came})
                # The board answers again. The page is read from where the pass before began.
                del self.board.errors[THREAD + mid(11)], self.board.calls[:]
                self.assertEqual(self.collect(clock), (0, 4 - len(came), [], "ok", 0, False))
                self.assertEqual(self.board.calls[1], updates_from())
                self.assertEqual(self.arrived(), {mid(n) for n in (10, 11, 12, 13)})

    def test_a_message_that_the_board_keeps_failing_about_is_passed_over_in_the_sixth_pass(self):
        self.board.post(10, "replies", reply_to=1)
        self.board.post(11, "replies", reply_to=4)
        self.board.post(12, "addressed")
        # A pause of the board is no answer about a message, however often it comes.
        self.board.errors[THREAD + mid(11)] = 429
        self.assertEqual(self.collect(), (1, 2, ["http_429"], "error", 0, True))
        for _ in range(7):
            self.assertEqual(self.collect(), (1, 0, ["http_429"], "error", 0, True))
        self.board.errors[THREAD + mid(11)] = 503
        for _ in range(5):
            self.assertEqual(self.collect(), (1, 0, ["http_503"], "error", 0, True))
        self.assertEqual(self.collect(), (1, 0, ["http_503"], "error", 1, True))
        self.assertEqual(self.arrived(), {mid(10), mid(12)})
        self.assertEqual(self.collect(), (0, 0, [], "ok", 0, False))

    def test_a_message_that_was_passed_over_is_not_waited_for_anew_while_its_page_is_read_again(self):
        self.board.post(10, "replies", reply_to=4)
        self.board.errors[THREAD + mid(10)] = 503
        for _ in range(3):
            self.assertEqual(self.collect(), (1, 0, ["http_503"], "error", 0, True))
        self.board.post(11, "replies", reply_to=6)
        self.board.errors[THREAD + mid(11)] = 503
        for _ in range(2):
            self.assertEqual(self.collect(), (1, 0, ["http_503"], "error", 0, True))
        # Message 10 is passed over in its sixth pass, and in each pass after it for which message 11 keeps the page.
        for _ in range(2):
            self.assertEqual(self.collect(), (1, 0, ["http_503"], "error", 1, True))
        del self.board.errors[THREAD + mid(10)]
        self.assertEqual(self.collect(), (1, 1, ["http_503"], "error", 0, True))
        self.assertEqual(self.arrived(), {mid(10)})

    def test_a_board_that_asks_for_a_pause_is_asked_nothing_more_in_that_pass(self):
        self.board.post(3, by=ME)
        self.board.post(10, "replies", reply_to=4)
        self.board.post(11, "addressed")
        self.board.post(12, "replies", reply_to=3)
        # The pause comes for what message 10 answers. That is context, so the message arrives without it.
        self.board.errors[E + mid(4)] = 429
        self.assertEqual(self.collect(), (1, 2, ["http_429"], "error", 0, True))
        self.assertEqual((self.board.calls[-1], self.arrived()), ((E + mid(4), {"format": "json"}), {mid(10), mid(11)}))
        del self.board.errors[E + mid(4)]
        self.assertEqual(self.collect(), (0, 1, [], "ok", 0, False))
        self.assertEqual(self.arrived(), {mid(10), mid(11), mid(12)})

    def test_a_message_that_the_board_does_not_give_is_passed_over_and_holds_nothing(self):
        for name, answer, errors in (("missing", 404, []), ("removed", 410, []), ("refused", 403, []),
                                     # The board reads no thread that is too large for it.
                                     ("too large", 400, ["http_400"]),
                                     ("unreadable", {"ok": True, "data": {}}, ["invalid_response"])):
            with self.subTest(answer=name):
                self.new(name)
                self.board.post(10, "replies", reply_to=1)
                self.board.post(11, "replies", reply_to=4)
                self.board.post(12, "addressed")
                self.board.errors[THREAD + mid(11)] = answer
                self.assertEqual(self.collect(),
                                 (1, 2, errors, "error", 1, True) if errors else (0, 2, [], "ok", 1, False))
                self.assertEqual(self.arrived(), {mid(10), mid(12)})
                # The next pass does not ask for it again.
                del self.board.calls[:]
                self.assertEqual(self.collect(), (0, 0, [], "ok", 0, False))
                self.assertEqual(self.board.calls, [updates_from(self.board.cursor(12))])
        # A message that was hidden: the board lists it without what it said.
        self.board.post(13, "replies", reply_to=1, hidden=True)
        self.assertEqual(self.collect(), (0, 0, [], "ok", 1, False))

    def test_an_answer_under_a_root_that_was_hidden_arrives_with_the_id_of_its_thread(self):
        self.board.posts[mid(2)]["hidden"] = True
        self.board.post(10, "replies", reply_to=4)
        self.assertEqual(self.collect(), (0, 1, [], "ok", 0, False))
        listed, _ = commands.execute(self.store, "list")
        answer, = listed["messages"]
        self.assertEqual((answer["thread_id"], answer["brief"]["root"], answer["brief"]["parent"]["status"]),
                         (mid(2), {"id": mid(2), "status": "not_available_locally"}, "cached"))

    def test_context_and_expand_read_the_board_as_it_is_now(self):
        self.board.post(10, "replies", reply_to=4)
        self.collect()
        del self.board.calls[:]
        result, code = self.context(mid(10))
        self.assertEqual((code, result["target"]["remote_status"], result["root"]["id"], result["root"]["status"],
                          result["parent"]["id"], result["parent"]["status"], result["parent"]["message"]["body"]),
                         (0, "available", mid(2), "available", mid(4), "available", "Public message 4"))
        # A root answers no message, and one that the inbox does not have is read from the board.
        result, code = self.context(mid(2))
        self.assertEqual((code, result["target"]["origin"], result["parent"]["status"]), (0, "remote", "none"))
        result, code = commands.execute(self.store, "expand", source="swarmmemo", thread=mid(2), through=1,
                                        **self.asks, fetch=self.board)
        self.assertEqual((code, result["complete"], result["root"]["id"], result["root"]["origin"]),
                         (0, True, mid(2), "remote"))
        self.assertEqual([(item["id"], item["parent"]["id"], item["parent"]["status"]) for item in result["items"]],
                         [(mid(10), mid(4), "available")])
        # A message that was hidden since, and one that the board never had.
        self.board.posts[mid(4)]["hidden"] = True
        result, code = self.context(mid(10))
        self.assertEqual((code, result["parent"]["status"], result["parent"]["error"]),
                         (1, "unavailable", "original_unavailable"))
        result, code = self.context(mid(999))
        self.assertEqual((code, result["target"]["status"], result["target"]["error"]), (1, "missing", "http_404"))
        with self.assertRaisesRegex(MailError, "^invalid_message_id$"):
            self.context("no-id-of-the-board")
        self.assertNotIn("/api/updates", [path for path, _ in self.board.calls])

    def test_an_answer_to_a_recorded_reply_shows_the_exchange_before_it(self):
        self.board.post(10, "replies", reply_to=1)
        self.collect()
        # The account answers message 10 with message 20 and records where its answer is.
        self.board.post(20, by=ME, reply_to=10)
        mark(self.store, "swarmmemo", mid(10), "replied", ref="https://swarmmemo.com/e/" + mid(20))
        self.board.post(21, "replies", reply_to=20)
        self.collect()
        listed, _ = commands.execute(self.store, "list", after=1)
        answer, = listed["messages"]
        before = answer["brief"]["previous_exchange"]
        self.assertEqual((answer["id"], before["status"], [message["id"] for message in before["messages"]]),
                         (mid(21), "linked", [mid(10)]))

    def test_the_board_takes_no_subscription(self):
        with self.assertRaisesRegex(MailError, "^subscriptions_unsupported$"):
            commands.execute(self.store, "subscribe", **self.asks, source="swarmmemo", thread=mid(1))


if __name__ == "__main__":
    unittest.main()
