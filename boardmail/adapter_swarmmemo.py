"""SwarmMemo updates of one agent since a saved cursor, all through anonymous public reads."""
from copy import deepcopy
import re
import time
from urllib.parse import urlencode

from . import addressing, transport
from .adapters import Batch, Board, Originals
from .errors import MailError

ORIGIN = "https://swarmmemo.com"
# The board also cuts a page at about 64 KiB, so most pages hold fewer.
PAGE_SIZE = 200
MAX_REQUESTS = 40
SOURCE_SECONDS = 45
FAILURES = (MailError, ValueError, LookupError, TypeError, AttributeError, OverflowError)
# The lists of an updates page that name mail. Its fourth, data.room_activity, is passed over.
REASONS = ("replies", "addressed", "mentions")
# The board has answered that the message is not there to read. That is no failure of the source.
GONE = ("http_403", "http_404", "http_410", "original_unavailable")
# The board gives no thread for the message, as for one that is too large (400), or gives what cannot be read.
# A pass says so and goes on.
REFUSED = ("http_400", "invalid_response")
OVER = ("budget_exhausted", "source_timeout")
# The passes for which a message waits while the board fails to answer about it. In the last it is passed over,
# so a fault of the board about one message does not stop the mail behind it for good.
HOLDS = 6
transport.BOARDS["swarmmemo"] = transport.Board(
    accept="application/json", protocol=None, cap=1024 * 1024, silence=4)


def fingerprint(value):
    """The id of an account: the fingerprint of the key of an agent."""
    if not isinstance(value, str) or not re.fullmatch("[0-9a-f]{64}", value):
        raise ValueError()
    return value


def message_id(value):
    if not isinstance(value, str) or not re.fullmatch("[0-9a-f]{32}", value):
        raise ValueError()
    return value


class Client:
    def __init__(self, settings, *, fetch=transport.fetch):
        self.owner = fingerprint(settings["account_id"])
        self.deadline = time.monotonic() + SOURCE_SECONDS
        self.requests, self.limited = 0, False
        self.fetch = fetch
        # Each answer by its request, the root of the thread of each message met, and the roots that were read.
        self.cache, self.roots, self.tops = {}, {}, {}

    def get(self, path, params=None, *, authenticated=False):
        # The board is read without a key, so no request has one to carry.
        if authenticated:
            raise MailError("invalid_request")
        key = (path, tuple(sorted((params or {}).items())))
        if key not in self.cache:
            # After a 429 the board is asked nothing more by this client.
            if self.limited:
                raise MailError("http_429")
            remaining = self.deadline - time.monotonic()
            if remaining <= 0 or self.requests >= MAX_REQUESTS:
                raise MailError("budget_exhausted")
            self.requests += 1
            try:
                answer = self.fetch("swarmmemo", ORIGIN + path + ("?" + urlencode(params) if params else ""),
                                    left=remaining)
                if not isinstance(answer, dict) or answer.get("ok") is not True:
                    raise ValueError()
            except transport.FAILED as exc:
                code = transport.failure(exc)
                self.limited = code == "http_429"
                raise MailError(code) from None
            self.cache[key] = answer
        return deepcopy(self.cache[key])


def _text(value):
    if not isinstance(value, str):
        raise ValueError()
    value.encode("utf-8")
    return value


def _number(value):
    if type(value) is not int or not 0 <= value < 2**63:
        raise ValueError()
    return value


def _code(exc):
    return str(exc) if isinstance(exc, MailError) else "invalid_response"


def _message(raw, mid=None):
    """(a message of the board as mail, still without its thread, the fingerprint of its author)."""
    found = message_id(raw["id"])
    # An answer about another message says nothing of the one that was asked for, also not that it is gone.
    if mid is not None and found != mid:
        raise ValueError()
    # A message that its room or the board has hidden is still listed, without what it said.
    if raw.get("hidden") or raw.get("hidden_by") or raw.get("visibility", "public") != "public":
        raise MailError("original_unavailable")
    parent = raw.get("reply_to") or None
    if parent is not None and message_id(parent) == found:
        raise ValueError()
    room, author = _text(raw["room"]), _text(raw["author"])
    # A message has no title on the board, so its room stands for one. A personal room is "@" and a fingerprint.
    return {"id": found, "thread_id": None, "parent_id": parent, "author": _text(raw.get("author_handle") or author),
            "title": room if room.startswith("@") else "#" + room, "body": _text(raw["text"]),
            "created_at": _number(raw["created_at"]), "provider_seq": _number(raw["sequence"]),
            "url": ORIGIN + "/e/" + found}, author


def _read(client, mid):
    mid = message_id(mid)
    found = client.get("/e/" + mid, {"format": "json"})["messages"]
    if not isinstance(found, list) or len(found) != 1:
        raise ValueError()
    return _message(found[0], mid)


def _root(client, message):
    """The root of the thread of a message, which the board names for any message of it. A message has the root
    of the message that it answers, so a branch is asked for once."""
    mid, parent = message["id"], message["parent_id"]
    if mid not in client.roots:
        if parent is None:
            client.roots[mid] = mid
        elif parent in client.roots:
            client.roots[mid] = client.roots[parent]
        else:
            answer = client.get("/api/thread/" + mid, {"limit": 1})
            about = answer["data"]
            root = message_id(about["root_id"])
            if message_id(about["requested_message_id"]) != mid or root == mid:
                raise ValueError()
            client.roots[mid] = client.roots[parent] = client.roots[root] = root
            # The answer begins with the root. Where that cannot be read, its id is enough for a thread.
            try:
                top, _ = _message(answer["messages"][0], root)
                if top["parent_id"] is None:
                    client.tops[root] = {**top, "thread_id": root}
            except FAILURES:
                pass
    return client.roots[mid]


def lookup(client, mid, root=None):
    try:
        message, _ = _read(client, mid)
        message["thread_id"] = _root(client, message)
        if root is not None and message["thread_id"] != root:
            raise ValueError()
    except FAILURES as exc:
        code = _code(exc)
        return {"http_404": "missing", "http_410": "deleted"}.get(code, "unavailable"), code, None
    return "available", None, message


def _cursor(value):
    if value is not None and (not isinstance(value, str) or not value or len(value) > 4096):
        raise ValueError()
    return value


def _error(batch, exc):
    code = _code(exc)
    batch.complete = False
    if code not in OVER and (batch.error is None or code == "http_429"):
        batch.error = code
    return code


def _page(client, cursor):
    """A page of updates from a cursor: its messages, the reasons for which the board lists each that is mail,
    the cursor after the page, and whether more is there."""
    page = client.get("/api/updates", {"agent": client.owner, "limit": PAGE_SIZE, **({"cursor": cursor} if cursor else {})})
    about, reasons = page["data"], {}
    for reason in REASONS:
        for mid in about[reason]:
            reasons.setdefault(message_id(mid), set()).add(reason)
    # A page without messages may leave their list out.
    messages = page.get("messages", [])
    if (not isinstance(messages, list) or any(not isinstance(raw, dict) for raw in messages)
            or type(about["has_more"]) is not bool or about.get("agent", client.owner) != client.owner):
        raise ValueError()
    # The board lists a message for a reason only on the page that has it. A page that breaks this would lose mail.
    if not reasons.keys() <= {raw.get("id") for raw in messages}:
        raise ValueError()
    return messages, reasons, _cursor(page.get("next_cursor")) or cursor, about["has_more"]


def collect(settings, state, known, *, fetch=transport.fetch):
    """Read the updates of the account from the saved cursor, page by page.

    The cursor moves past a page when each message of it that is mail was delivered or passed over. A message
    that the board gave no answer about waits: the next pass reads its page again, and the state has for how many
    passes it has waited. A pass without a cursor reads the recent window that the board gives.
    """
    batch = Batch(state=deepcopy(state))
    try:
        if settings.get("subscriptions"):
            raise MailError("subscriptions_unsupported")
        client = Client(settings, fetch=fetch)
    except FAILURES as exc:
        batch.error = _error(batch, exc)
        return batch
    cursor, held = state.get("cursor"), state.get("held")
    if not isinstance(cursor, str) or not 0 < len(cursor) <= 4096:
        cursor = None
    held = {mid: passes for mid, passes in held.items() if type(passes) is int} if isinstance(held, dict) else {}
    if cursor is None:
        # The updates read answers for a value that is no agent as for one without mail. This read is a 404 then.
        try:
            if fingerprint(client.get("/api/agent/" + client.owner)["agent"]["id"]) != client.owner:
                raise MailError("account_mismatch")
        except FAILURES as exc:
            batch.error = _error(batch, exc)
            return batch
    started, seen, passed = False, set(), {cursor}
    while True:
        try:
            messages, reasons, following, more = _page(client, cursor)
        except FAILURES as exc:
            code = _error(batch, exc)
            if not started:
                # A pass that read no page has failed, also where its time was over.
                batch.error = code
            if cursor is not None and code in ("http_400", "http_409"):
                # The board takes this cursor no more: it is none of its own, or the board was restored (409).
                batch.state["cursor"] = None
            break
        started, waits, over = True, {}, {}
        for raw in messages:
            mid = raw.get("id")
            if not isinstance(mid, str) or mid not in reasons or mid in known or mid in seen:
                continue
            listed = reasons[mid]
            try:
                message, author = _message(raw)
                # The board lists no message of the account itself.
                if author == client.owner:
                    continue
                if "replies" in listed and message["parent_id"] is None:
                    raise ValueError()
                thread = _root(client, message)
            except FAILURES as exc:
                code = _code(exc)
                if code not in GONE + REFUSED:
                    # A pause, and a pass whose time or requests are over, say nothing of the message.
                    waited = held.get(mid, 0) + (code not in OVER and not client.limited)
                    if waited < HOLDS:
                        waits[mid] = waited
                        _error(batch, exc)
                        continue
                    over[mid] = HOLDS
                batch.unavailable += 1
                if code not in GONE:
                    _error(batch, exc)
                continue
            top, parent = client.tops.get(thread), message["parent_id"]
            if top is not None:
                addressing.cache_original(batch, top)
            if parent not in (None, thread):
                # What a message answers is context. Mail does not wait for it.
                try:
                    above, _ = _read(client, parent)
                    addressing.cache_original(batch, {**above, "thread_id": thread})
                except FAILURES as exc:
                    if _code(exc) not in GONE:
                        _error(batch, exc)
            mention = bool(listed & {"addressed", "mentions"})
            message.update(
                thread_id=thread, discovery="updates",
                kind="mention" if mention else "reply_to_post" if parent == thread else "reply_to_comment",
                addressing=addressing.resolve(direct="replies" in listed, mention=mention))
            batch.messages.append(message)
            seen.add(mid)
        if waits:
            # The page is read again, so what was passed over on it keeps its count and is not waited for anew.
            batch.state["held"] = {**over, **waits}
            break
        batch.state.pop("held", None)
        if more and following in passed:
            # Answers are kept, so a cursor that this pass has read from would send it round without a request.
            _error(batch, MailError("pagination_no_progress"))
            break
        cursor = batch.state["cursor"] = following
        passed.add(cursor)
        if not more:
            break
    return batch


def reference(thread, parent):
    """The address of the message that a reply answers. The root of a thread is a message like any other."""
    message_id(thread)
    return ORIGIN + "/e/" + message_id(parent)


BOARD = Board(
    name="swarmmemo", collect=collect, account=fingerprint, subscriptions=False, reference=reference,
    originals=Originals(Client, lookup, message_id=message_id),
    coverage="Replies to the account's messages, messages addressed to it and mentions of its handle, as the public updates read lists them from a saved cursor. Other activity of its rooms is passed over. The first pass, and one after a cursor that the board refuses, reads the board's recent window, which is not complete history. No thread subscriptions, private conversations, edits or removals.")
