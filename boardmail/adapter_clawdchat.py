"""ClawdChat notifications, confirmed through anonymous public originals."""
from datetime import datetime
from http.client import HTTPException
import json
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from boardmail.adapters import Batch
from boardmail.config import MailError, uuid

API_VERSION = 1
ORIGIN = "https://clawdchat.cn"
PAGE_SIZE = 8
MAX_PENDING = 256
MAX_REQUESTS = 40
MAX_RESPONSE_BYTES = 1024 * 1024
SOURCE_SECONDS = 45
KINDS = {"comment": "reply_to_post", "reply": "reply_to_comment",
         "mention_post": "mention", "mention_comment": "mention"}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fp.close()
        raise MailError("redirect_refused")


class Client:
    def __init__(self, settings):
        try:
            self.owner = uuid(settings["account_id"])
        except (KeyError, ValueError, TypeError, AttributeError):
            raise MailError("invalid_config") from None
        self.settings, self.key = settings, None
        self.end = time.monotonic() + SOURCE_SECONDS
        self.deadline = self.end
        self.requests = 0
        self.opener = build_opener(NoRedirect())

    def phase(self, seconds):
        self.deadline = min(self.end, time.monotonic() + seconds)

    def get(self, path, params=None, *, authenticated=False):
        headers = {"Accept": "application/json", "User-Agent": "boardmail/0.2"}
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
        for attempt in range(3):  # At most two retries, all inside this phase's budget.
            remaining = self.deadline - time.monotonic()
            if remaining <= 0 or self.requests >= MAX_REQUESTS:
                raise MailError("budget_exhausted")
            self.requests += 1
            request = Request(ORIGIN + "/api/v1" + path + ("?" + urlencode(params) if params else ""), headers=headers)
            try:
                with self.opener.open(request, timeout=min(4, remaining)) as response:
                    chunks, size = [], 0
                    while True:
                        if time.monotonic() >= self.deadline:
                            raise MailError("budget_exhausted")
                        chunk = response.read1(65536)
                        if not chunk:
                            result = json.loads(b"".join(chunks))
                            if not isinstance(result, dict) or result.get("success") is False:
                                raise MailError("invalid_response")
                            return result
                        size += len(chunk)
                        if size > MAX_RESPONSE_BYTES:
                            raise MailError("response_too_large")
                        chunks.append(chunk)
            except HTTPError as exc:
                code = exc.code
                exc.close()
                if code not in (408, 500, 502, 503, 504) or attempt == 2:
                    raise MailError("http_" + str(code)) from None
            except (URLError, OSError, HTTPException):
                if attempt == 2:
                    raise MailError("network_error") from None
        raise MailError("network_error")


def _text(value):
    if not isinstance(value, str):
        raise ValueError()
    value.encode("utf-8")
    return value


def _timestamp(value):
    parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError()
    return int(parsed.timestamp())


def _url(value, fallback):
    if isinstance(value, str) and len(value) <= 2048 and "\\" not in value and not any(ord(c) < 33 or ord(c) == 127 for c in value):
        try:
            url = urlsplit(value)
            if (url.scheme == "https" and url.hostname == "clawdchat.cn" and url.port in (None, 443)
                    and url.username is None and url.password is None):
                return value
        except ValueError:
            pass
    return ORIGIN + "/api/v1" + fallback


def _reference(item):
    kind = KINDS.get(item["type"])
    if kind is None:
        return None
    post = uuid(item["post_id"]) if item.get("post_id") else None
    mid = uuid(item["post_id"] if item["type"] == "mention_post" else item["comment_id"])
    return {"id": mid, "post": post, "kind": kind, "is_post": item["type"] == "mention_post"}


def _original(client, entry, *, include_own=False):
    path = ("/posts/" if entry["is_post"] else "/comments/") + entry["id"]
    original = client.get(path)  # Never attach credentials to public-original requests.
    if uuid(original["id"]) != entry["id"]:
        raise ValueError()
    post_id = entry["id"] if entry["is_post"] else uuid(original["post_id"])
    if entry["post"] is not None and entry["post"] != post_id:
        raise ValueError()
    context = original if entry["is_post"] else original.get("post") or {}
    if context.get("id") and uuid(context["id"]) != post_id:
        raise ValueError()
    if original.get("is_deleted"):
        raise MailError("original_deleted")
    if context.get("is_deleted"):
        raise MailError("thread_deleted")
    for obj in (original, context):
        if obj.get("is_hidden") or obj.get("visibility", "public") != "public":
            raise MailError("original_unavailable")
    author = original["author"]
    if uuid(author["id"]) == client.owner and not include_own:
        return None
    body = original.get("content")
    if body is None and entry["is_post"]:
        body = ""  # Link posts may have no text body.
    return {"id": entry["id"], "thread_id": post_id, "kind": entry["kind"],
            "parent_id": uuid(original["parent_id"]) if original.get("parent_id") else None,
            "author": _text(author["name"]), "title": _text(context.get("title", "Public reply")),
            "body": _text(body), "url": _url(original.get("web_url"), path),
            "created_at": _timestamp(original["created_at"])}


def _error(batch, exc):
    code = str(exc) if isinstance(exc, MailError) else "invalid_response"
    if code != "budget_exhausted" and (batch.error is None or code == "http_429"):
        batch.error = code
    batch.complete = False
    return code


FAILURES = (MailError, ValueError, KeyError, TypeError, AttributeError, OverflowError)


def lookup(client, mid, root=None):
    """Read a public parent, target or root without requiring account credentials."""
    try:
        entry = {"id": mid, "post": root, "is_post": mid == root, "kind": "mention"}
        try:
            message = _original(client, entry, include_own=True)
        except MailError as exc:
            if root is not None or str(exc) != "http_404": raise
            entry["is_post"] = True
            message = _original(client, entry, include_own=True)
    except FAILURES as exc:
        code = str(exc) if isinstance(exc, MailError) else "invalid_response"
        if code == "original_deleted": return "deleted", None, None
        return {"http_404": "missing", "http_410": "deleted"}.get(code, "unavailable"), code, None
    return "available", None, message


def collect(settings, state, known):
    """Rotate retries, read fresh and backfill pages, then confirm new references.

    State retains references only. Overflow evicts the oldest reference with an
    explicit error; cyclic notification scans may rediscover it while retained.
    """
    batch = Batch(state={"offset": 0, "pending": []})
    pending = {}
    try:
        client = Client(settings)
        client.phase(5)
        if uuid(client.get("/agents/me", authenticated=True)["id"]) != client.owner:
            raise MailError("account_mismatch")
    except FAILURES as exc:
        batch.error = _error(batch, exc)
        batch.state = state
        return batch
    try:
        offset = state.get("offset", 0)
        if type(offset) is not int or not 0 <= offset < 2**63:
            raise ValueError()
        batch.state["offset"] = offset
        entries = state.get("pending", [])
        if not isinstance(entries, list) or len(entries) > MAX_PENDING:
            raise ValueError()
        for entry in entries:
            mid = uuid(entry["id"])
            post = uuid(entry["post"]) if entry["post"] is not None else None
            if entry["kind"] not in KINDS.values() or type(entry["is_post"]) is not bool:
                raise ValueError()
            if mid not in known:
                pending[mid] = {"id": mid, "post": post, "kind": entry["kind"], "is_post": entry["is_post"]}
    except FAILURES as exc:
        _error(batch, exc)
        batch.state["pending"] = list(pending.values())
        return batch

    seen = set(known)

    def resolve(ids, seconds):
        client.phase(seconds)
        for mid in ids:
            if mid not in pending:
                continue
            entry = pending.pop(mid)
            pending[mid] = entry  # A failed original must not monopolize retries.
            try:
                message = _original(client, entry)
                if message is not None:
                    batch.messages.append(message)
                    seen.add(mid)
                del pending[mid]
            except FAILURES as exc:
                if isinstance(exc, MailError) and str(exc) in ("http_403", "http_404", "http_410", "original_deleted", "thread_deleted", "original_unavailable"):
                    batch.unavailable += 1
                else:
                    code = _error(batch, exc)
                    if code == "http_429":
                        return False
                    if code == "budget_exhausted":
                        break
        return True

    if resolve(list(pending)[:8], 15):
        fresh = []
        # Always inspect the head. The second request resumes an independent sweep.
        for page_offset in dict.fromkeys((0, batch.state["offset"])):
            client.phase(5)
            try:
                raw = client.get("/notifications", {"limit": PAGE_SIZE, "offset": page_offset}, authenticated=True)
                items, total = raw["items"], raw["total"]
                if not isinstance(items, list) or len(items) > PAGE_SIZE or type(total) is not int or not 0 <= total < 2**63:
                    raise ValueError()
                if not items and page_offset < total:
                    raise MailError("pagination_no_progress")
                overflow = False
                for item in items:
                    try:
                        entry = _reference(item)
                        if entry is None or entry["id"] in seen or entry["id"] in pending:
                            continue
                        if len(pending) == MAX_PENDING:
                            del pending[next(iter(pending))]
                            overflow = True
                            _error(batch, MailError("pending_overflow"))
                        pending[entry["id"]] = entry
                        fresh.append(entry["id"])
                    except FAILURES as exc:
                        _error(batch, exc)
                if page_offset == batch.state["offset"]:
                    following = page_offset + len(items)
                    batch.state["offset"] = page_offset if overflow else following if following < total else 0
            except FAILURES as exc:
                code = _error(batch, exc)
                if page_offset and code in ("http_400", "http_422", "pagination_no_progress"):
                    batch.state["offset"] = 0
                if code == "http_429":
                    break
        if batch.error != "http_429":
            resolve(fresh[:8], 15)
    batch.state["pending"] = list(pending.values())
    batch.complete = batch.complete and not pending and batch.state["offset"] == 0
    return batch
