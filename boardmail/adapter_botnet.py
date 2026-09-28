"""Botnet forum inbox, with bodies confirmed through anonymous public reads."""
from copy import deepcopy
from http.client import HTTPException
import json
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import addressing
from .adapters import Batch
from .config import MailError, identifier, uuid

API_VERSION = 1
ORIGIN = "https://botnet.com"
PAGE_SIZE = 8
MAX_PENDING = 256
MAX_REQUESTS = 40
MAX_RESPONSE_BYTES = 1024 * 1024
SOURCE_SECONDS = 45
FAILURES = (MailError, ValueError, KeyError, TypeError, AttributeError, OverflowError)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fp.close()
        raise MailError("redirect_refused")


class Client:
    def __init__(self, settings):
        self.owner = identifier(settings["account_id"])
        self.settings, self.key = settings, None
        self.end = self.deadline = time.monotonic() + SOURCE_SECONDS
        self.requests = 0
        self.opener = build_opener(NoRedirect())
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
        headers = {"Accept": "application/json", "User-Agent": "boardmail"}
        if authenticated:
            if self.key is None:
                try:
                    with Path(self.settings.get("api_key_file")).open() as stream:
                        key = stream.read(4097).strip()
                    if not key or len(key) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in key):
                        raise ValueError()
                    self.key = key
                except (OSError, UnicodeError, ValueError, TypeError):
                    raise MailError("credentials_unavailable") from None
            headers["Authorization"] = "Bearer " + self.key
        self.requests += 1
        request = Request(ORIGIN + "/api/forum" + path + ("?" + urlencode(params) if params else ""), headers=headers)
        try:
            with self.opener.open(request, timeout=min(4, remaining)) as response:
                chunks, size = [], 0
                while True:
                    if time.monotonic() >= self.deadline:
                        raise MailError("budget_exhausted")
                    chunk = response.read1(65536)
                    if not chunk:
                        result = json.loads(b"".join(chunks))
                        if not isinstance(result, dict):
                            raise ValueError()
                        if not authenticated:
                            self.cache[cache_key] = result
                        return deepcopy(result)
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise MailError("response_too_large")
                    chunks.append(chunk)
        except HTTPError as exc:
            code = exc.code
            exc.close()
            raise MailError("http_" + str(code)) from None
        except (URLError, OSError, HTTPException):
            raise MailError("network_error") from None
        except (ValueError, UnicodeError):
            raise MailError("invalid_response") from None


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
    reason = item.get("reason", "mention")  # Older notifications were all mentions.
    if reason not in ("mention", "reply"):
        return None
    mid = item.get("messageId")
    if mid is None:
        mid = "post:" + uuid(item["postId"]) if item.get("postId") else "thread:" + uuid(item["threadId"])
    return {"id": message_id(mid), "reason": reason}


def _cursor(value):
    if value is not None and (not isinstance(value, str) or not value or len(value) > 4096):
        raise ValueError()
    return value


def _error(batch, exc):
    code = str(exc) if isinstance(exc, MailError) else "invalid_response"
    batch.complete = False
    if batch.error is None or code == "http_429":
        batch.error = code
    return code


def collect(settings, state, known):
    """Read the newest page and rotate older pages; retain failed public lookups.

    Inbox cursors run backwards, unlike Botnet's separate topic activity cursors.
    Only IDs/reasons survive in pending state; notification prose is never mail.
    """
    batch = Batch(state=deepcopy(state))
    try:
        if settings.get("subscriptions"):
            raise MailError("subscriptions_unsupported")
        client = Client(settings)
        client.phase(10)
        if identifier(client.get("/me", authenticated=True)["actor"]["id"]) != client.owner:
            raise MailError("account_mismatch")
        cursor = _cursor(state.get("cursor"))
        pending = {entry["id"]: {"id": message_id(entry["id"]), "reason": entry["reason"]}
                   for entry in state.get("pending", []) if entry["id"] not in known}
        if len(pending) > MAX_PENDING or any(e["reason"] not in ("mention", "reply") for e in pending.values()):
            raise ValueError()
    except FAILURES as exc:
        _error(batch, exc)
        return batch

    fresh = []
    for position in ([None, cursor] if cursor is not None else [None]):
        try:
            page = client.get("/inbox", {"limit": PAGE_SIZE, **({"cursor": position} if position else {})}, authenticated=True)
            items, following = page["items"], _cursor(page["nextCursor"])
            if not isinstance(items, list) or len(items) > PAGE_SIZE:
                raise ValueError()
            if following is not None and following == position:
                raise MailError("pagination_no_progress")
            refs, valid_page = [], True
            for item in items:
                try:
                    ref = _reference(item)
                    if ref is not None:
                        refs.append(ref)
                except FAILURES as exc:
                    _error(batch, exc)
                    valid_page = False
            overflow = False
            for ref in refs:
                mid = ref["id"]
                if mid in known:
                    continue
                if mid in pending:
                    if ref["reason"] == "mention":
                        pending[mid] = ref
                elif len(pending) < MAX_PENDING:
                    pending[mid] = ref
                    fresh.append(mid)
                else:
                    overflow = True
            if overflow:
                raise MailError("pending_overflow")
            if valid_page and (position is not None or cursor is None):
                batch.state["cursor"] = following
        except FAILURES as exc:
            code = _error(batch, exc)
            if position is not None and code in ("http_400", "http_422", "pagination_no_progress"):
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
                        if message["parent_id"] is not None:
                            parent, parent_author = _message(client, message["parent_id"], message["thread_id"])
                            addressing.cache_original(batch, parent)
                    except FAILURES as exc:
                        _error(batch, exc)
                    mention = entry["reason"] == "mention"
                    direct = entry["reason"] == "reply" or parent_author == client.owner
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
