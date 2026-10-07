"""Botnet forum inbox, with bodies confirmed through anonymous public reads."""
from copy import deepcopy
import time
from urllib.parse import quote, urlencode

from . import addressing, transport
from .adapters import Batch, Board
from .errors import MailError, identifier, uuid

API_VERSION = 1
ORIGIN = "https://botnet.com"
PAGE_SIZE = 8
MAX_PENDING = 256
MAX_REQUESTS = 40
SOURCE_SECONDS = 45
FAILURES = (MailError, ValueError, KeyError, TypeError, AttributeError, OverflowError)


class Client:
    """The requests of one pass or one command. fetch asks the board: the transport, or an invented board in its
    place."""
    def __init__(self, settings, *, fetch=transport.fetch):
        self.owner = identifier(settings["account_id"])
        self.settings, self.key = settings, None
        self.end = self.deadline = time.monotonic() + SOURCE_SECONDS
        self.requests = 0
        self.fetch = fetch
        self.cache = {}

    def phase(self, seconds):
        self.deadline = min(self.end, time.monotonic() + seconds)

    def get(self, path, params=None, *, authenticated=False):
        # Only these fixed private endpoints may receive a credential.
        if authenticated and path not in ("/me", "/inbox"):
            raise MailError("invalid_request")
        cache_key = (path, tuple(sorted((params or {}).items())))
        if not authenticated and cache_key in self.cache:
            return deepcopy(self.cache[cache_key])
        remaining = self.deadline - time.monotonic()
        if remaining <= 0 or self.requests >= MAX_REQUESTS:
            raise MailError("budget_exhausted")
        headers = None
        if authenticated:
            if self.key is None:
                self.key = transport.key("botnet", self.settings.get("api_key_file"))
            headers = {"Authorization": "Bearer " + self.key}
        self.requests += 1
        url = ORIGIN + "/api/forum" + path + ("?" + urlencode(params) if params else "")
        try:
            result = self.fetch("botnet", url, left=remaining, headers=headers)
            if not isinstance(result, dict):
                raise ValueError()
        except transport.FAILED as exc:
            raise MailError(transport.failure("botnet", exc)) from None
        if not authenticated:
            self.cache[cache_key] = result
        return deepcopy(result)


def message_id(value):
    value = identifier(value)
    if len(value) > 170 or value in (".", ".."):
        raise ValueError()
    return value


def _text(value):
    if not isinstance(value, str):
        raise ValueError()
    value.encode("utf-8")
    return value


def _number(value):
    if type(value) is not int or not 0 <= value < 2**63:
        raise ValueError()
    return value


def _visible(item):
    if item.get("is_deleted") or item.get("deleted") or item.get("status") in ("deleted", "removed"):
        raise MailError("original_deleted")
    if (item.get("is_hidden") or item.get("hidden") or item.get("visibility", "public") != "public"
            or item.get("status") in ("hidden", "private")):
        raise MailError("original_unavailable")
    if item.get("truncated") or item.get("is_truncated"):
        raise MailError("original_incomplete")


def _message(client, mid, root=None):
    mid = message_id(mid)
    raw = client.get("/topic-messages/" + quote(mid, safe=""))
    _visible(raw)
    if message_id(raw["id"]) != mid:
        raise ValueError()
    topic = uuid(raw["topicId"])
    if root is not None and topic != root:
        raise ValueError()
    parent = raw["parentMessageId"]
    parent = message_id(parent) if parent is not None else None
    if parent == mid:
        raise ValueError()
    author = raw["author"]
    owner = identifier(author["id"])
    message = {"id": mid, "thread_id": topic, "parent_id": parent,
               "author": _text(author["name"]), "title": _text(raw.get("title") or ""),
               "body": _text(raw["body"]), "created_at": _number(raw["createdAt"]) // 1000,
               "provider_seq": _number(raw["sequence"]),
               "url": ORIGIN + "/topics/" + topic + "#message-" + quote(mid, safe=":")}
    return message, owner


def _topic(client, root):
    root = uuid(root)
    raw = client.get("/topics/" + root)
    _visible(raw)
    if uuid(raw["id"]) != root:
        raise ValueError()
    return {"id": root, "thread_id": root, "parent_id": None, "author": None,
            "title": _text(raw["title"]), "body": _text(raw["description"]) if raw.get("description") is not None else "",
            "created_at": _number(raw["createdAt"]) // 1000, "url": ORIGIN + "/topics/" + root}


def lookup(client, mid, root=None):
    """Public message or topic metadata; topic membership is not a reply edge."""
    try:
        if mid == root:
            message = _topic(client, root)
        else:
            try:
                message, _ = _message(client, mid, root)
            except MailError as exc:
                if root is not None or str(exc) != "http_404":
                    raise
                try:
                    topic = uuid(mid)
                except (ValueError, TypeError, AttributeError):
                    raise exc
                message = _topic(client, topic)
    except FAILURES as exc:
        code = str(exc) if isinstance(exc, MailError) else "invalid_response"
        return {"http_404": "missing", "http_410": "deleted", "original_deleted": "deleted"}.get(code, "unavailable"), code, None
    return "available", None, message


def _reference(item):
    reason = item.get("reason")
    if reason is None:
        reason = "mention"  # Older notifications were all mentions.
    if reason not in ("mention", "reply"):
        return None
    mid = item.get("messageId")
    if mid is None:
        mid = "post:" + uuid(item["postId"]) if item.get("postId") else "thread:" + uuid(item["threadId"])
    return {"id": message_id(mid), "reasons": [reason]}


def _cursor(value):
    if value is not None and (not isinstance(value, str) or not value or len(value) > 4096):
        raise ValueError()
    return value


def _error(batch, exc):
    code = str(exc) if isinstance(exc, MailError) else "invalid_response"
    batch.complete = False
    if code != "budget_exhausted" and (batch.error is None or code == "http_429"):
        batch.error = code
    return code


def collect(settings, state, known, *, fetch=transport.fetch):
    """Read the newest page and rotate older pages; retain failed public lookups.

    Inbox cursors run backwards, unlike Botnet's separate topic activity cursors.
    Only IDs/reasons survive in pending state; notification prose is never mail.

    fetch asks the board: the transport, or an invented board in its place.
    """
    batch = Batch(state=deepcopy(state))
    try:
        if settings.get("subscriptions"):
            raise MailError("subscriptions_unsupported")
        client = Client(settings, fetch=fetch)
        client.phase(10)
        if identifier(client.get("/me", authenticated=True)["actor"]["id"]) != client.owner:
            raise MailError("account_mismatch")
        cursor = _cursor(state.get("cursor"))
        pending = {}
        for entry in state.get("pending", []):
            mid, reasons = message_id(entry["id"]), entry["reasons"]
            if not isinstance(reasons, list) or not reasons or any(r not in ("mention", "reply") for r in reasons):
                raise ValueError()
            if mid not in known:
                pending[mid] = {"id": mid, "reasons": sorted(set(reasons))}
        if len(pending) > MAX_PENDING:
            raise ValueError()
    except FAILURES as exc:
        # Until identity is verified, a spent budget is a failed preflight.
        batch.error = _error(batch, exc)
        return batch

    fresh = []
    for position in ([None, cursor] if cursor is not None else [None]):
        try:
            page = client.get("/inbox", {"limit": PAGE_SIZE, **({"cursor": position} if position else {})}, authenticated=True)
            items = page["items"]
            if not isinstance(items, list) or len(items) > PAGE_SIZE:
                raise ValueError()
            refs = []
            for item in items:
                try:
                    ref = _reference(item)
                    if ref is not None:
                        refs.append(ref)
                except FAILURES as exc:
                    _error(batch, exc)
            overflow = False
            for ref in refs:
                mid = ref["id"]
                if mid in known:
                    continue
                if mid in pending:
                    pending[mid]["reasons"] = sorted(set(pending[mid]["reasons"]) | set(ref["reasons"]))
                elif len(pending) < MAX_PENDING:
                    pending[mid] = ref
                    fresh.append(mid)
                else:
                    overflow = True
            # Harvest valid references even when the page's cursor is broken.
            following = _cursor(page["nextCursor"])
            if following is not None and following == position:
                raise MailError("pagination_no_progress")
            if overflow:
                raise MailError("pending_overflow")
            if position is not None or cursor is None:
                batch.state["cursor"] = following
        except FAILURES as exc:
            code = _error(batch, exc)
            if position is not None and code in ("http_400", "http_422", "pagination_no_progress", "invalid_response"):
                batch.state["cursor"] = None
            if code in ("http_401", "http_403", "http_429", "budget_exhausted"):
                break

    # New references get a bounded first turn; failed old references rotate to
    # the tail. Neither a busy head nor one inaccessible original owns the pass.
    fresh = fresh[:4]
    retry = [mid for mid in pending if mid not in fresh]
    stopped = batch.error in ("http_401", "http_403", "http_429")
    for seconds, order in ((12, fresh), (SOURCE_SECONDS, retry)):
        if stopped or batch.error == "http_429":
            break
        client.phase(seconds)
        for mid in order:
            entry = pending.pop(mid)
            try:
                message, author = _message(client, mid)
                if author != client.owner:
                    parent, parent_author = None, None
                    # Context failure does not discard a confirmed public body.
                    try:
                        root = _topic(client, message["thread_id"])
                        addressing.cache_original(batch, root)
                        message["title"] = message["title"] or root["title"]
                    except FAILURES as exc:
                        _error(batch, exc)
                    if message["parent_id"] is not None and batch.error != "http_429":
                        try:
                            parent, parent_author = _message(client, message["parent_id"], message["thread_id"])
                            addressing.cache_original(batch, parent)
                        except FAILURES as exc:
                            _error(batch, exc)
                    mention = "mention" in entry["reasons"]
                    direct = "reply" in entry["reasons"] or parent_author == client.owner
                    message.update(kind="mention" if mention else "reply_to_post" if parent and parent["parent_id"] is None else "reply_to_comment",
                                   addressing=addressing.resolve(direct=direct, mention=mention), discovery="inbox")
                    batch.messages.append(message)
                else:
                    addressing.cache_original(batch, message)
            except FAILURES as exc:
                code = _error(batch, exc)
                pending[mid] = entry
                if code in ("http_403", "http_404", "http_410", "original_deleted", "original_unavailable"):
                    batch.unavailable += 1
                if code in ("http_429", "budget_exhausted"):
                    break
            if batch.error == "http_429":
                break
    batch.state["pending"] = list(pending.values())
    batch.complete = batch.complete and not pending and batch.state.get("cursor") is None
    return batch


BOARD = Board(
    name="botnet", collect=collect, fields=frozenset({"api_key_file"}), subscriptions=False,
    coverage="Retained forum reply/mention notifications, confirmed against anonymous topic messages. Cyclic backfill and bounded retries do not prove complete history. No topic subscriptions or private coordination inbox.")
