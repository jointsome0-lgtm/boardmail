"""ClawdChat notifications, confirmed through anonymous public originals."""
from datetime import datetime
from http.client import HTTPException
import json
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit

from boardmail import addressing, subscriptions, transport
from boardmail.adapters import Batch, Board, Originals, Replies, public_comment
from boardmail.errors import MailError, uuid

API_VERSION = 1
ORIGIN = "https://clawdchat.cn"
PAGE_SIZE = 8
MAX_PENDING = 256
MAX_REQUESTS = 40
SOURCE_SECONDS = 45
SUBSCRIPTION_REQUESTS = 10   # Reserved for subscribed threads when any exist.
SUBSCRIPTION_SECONDS = 11
COMMENT_PAGE = 20
MAX_PENDING_PARENTS = 20
MAX_SERVED = 200            # Parents a retained page may queue before it is consumed with an error.
KINDS = {"comment": "reply_to_post", "reply": "reply_to_comment",
         "mention_post": "mention", "mention_comment": "mention"}
UNAVAILABLE_ORIGINALS = ("http_403", "http_404", "http_410", "original_deleted", "thread_deleted", "original_unavailable")
# The time of a phase is over: it has none left to ask, or an answer came after it. Both end the phase alike.
OVER = ("budget_exhausted", "source_timeout")
# What the transport is told of the board.
transport.BOARDS["clawdchat"] = transport.Board(
    accept="application/json", agent="boardmail/0.2", protocol=None, key=4096, kind=None, cap=1024 * 1024,
    silence=4, budget=None, at_the_end=True, to_the_end=False)


class Client:
    """The requests of one pass or one command. fetch asks the board: the transport, or an invented board in its
    place."""
    def __init__(self, settings, *, fetch=transport.fetch):
        try:
            self.owner = uuid(settings["account_id"])
        except (KeyError, ValueError, TypeError, AttributeError):
            raise MailError("invalid_config") from None
        self.settings, self.key = settings, None
        self.end = time.monotonic() + SOURCE_SECONDS
        self.deadline = self.end
        self.requests = 0
        self.limit = MAX_REQUESTS
        self.fetch = fetch

    def phase(self, seconds):
        self.deadline = min(self.end, time.monotonic() + seconds)

    def get(self, path, params=None, *, authenticated=False):
        headers = None
        if authenticated:
            if self.key is None:
                self.key = transport.key("clawdchat", self.settings.get("api_key_file"))
            headers = {"Authorization": "Bearer " + self.key}
        url = ORIGIN + "/api/v1" + path + ("?" + urlencode(params) if params else "")
        for attempt in range(3):  # At most two retries, all inside this phase's budget.
            remaining = self.deadline - time.monotonic()
            if remaining <= 0 or self.requests >= self.limit:
                raise MailError("budget_exhausted")
            self.requests += 1
            try:
                result = self.fetch("clawdchat", url, left=remaining, headers=headers)
            except (OSError, HTTPException) as exc:
                code = transport.failure(exc)
                # A retry is for a board that was not reached and for these statuses.
                if attempt == 2 or isinstance(exc, HTTPError) and exc.code not in (408, 500, 502, 503, 504):
                    raise MailError(code) from None
            else:
                if not isinstance(result, dict) or result.get("success") is False:
                    raise MailError("invalid_response")
                return result


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
    return {"id": mid, "post": post, "kind": kind, "is_post": item["type"] == "mention_post", "types": [item["type"]]}


def _types(entry):
    """Native types behind one retained reference. A reference persisted before
    types were kept maps its kind back to the single type that produced it."""
    types = entry.get("types")
    if isinstance(types, list) and types and all(t in KINDS for t in types):
        return list(dict.fromkeys(types))
    if entry["kind"] == "mention":
        return ["mention_post" if entry["is_post"] else "mention_comment"]
    return [t for t, kind in KINDS.items() if kind == entry["kind"]]


def _addressing(owner, entry, original, context, mention):
    """``reply`` is a reply to our comment. ``comment`` is activity under a post:
    a top-level comment on our own post is direct. A nested comment with unknown
    parent ownership must remain visible even if its reply/mention notification
    arrives after the original is stored. Mentions are native or explicit
    ``@name`` in the text."""
    types = set(entry.get("types", ()))
    parent = uuid(original["parent_id"]) if original.get("parent_id") else None
    direct = "reply" in types
    if "comment" in types and not entry["is_post"]:
        post_author = context.get("author") if isinstance(context.get("author"), dict) else {}
        own_post = uuid(post_author["id"]) == owner if post_author.get("id") else None
        if own_post is not False:
            if ("parent_id" in original and original["parent_id"] is None) or (
                    parent is not None and parent == uuid(original["post_id"])):
                direct = True
    textual = addressing.mentions(mention, context.get("title") if entry["is_post"] else None, original.get("content"))
    return addressing.resolve(direct=direct, mention=bool(types & {"mention_post", "mention_comment"}) or textual)


def _fetch(client, entry):
    """One anonymous public request, checked against the reference it confirms."""
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
    body = original.get("content")
    if body is None and entry["is_post"]:
        body = ""  # Link posts may have no text body.
    message = {"id": entry["id"], "thread_id": post_id, "kind": entry["kind"],
               "parent_id": uuid(original["parent_id"]) if original.get("parent_id") else None,
               "author": _text(author["name"]), "title": _text(context.get("title", "Public reply")),
               "body": _text(body), "url": _url(original.get("web_url"), path),
               "created_at": _timestamp(original["created_at"])}
    return message, uuid(author["id"]) == client.owner, original, context


def _original(client, entry, *, include_own=False):
    message, own, _, _ = _fetch(client, entry)
    return None if own and not include_own else message


def _error(batch, exc):
    code = str(exc) if isinstance(exc, MailError) else "invalid_response"
    if code not in OVER and (batch.error is None or code == "http_429"):
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


def reply(client, mid, thread, check):
    """Replies.read of the board: the comment, and the post that it sits under is checked like the comment."""
    _, _, original, context = _fetch(client, {"id": mid, "post": thread, "is_post": False, "kind": "reply_to_comment"})
    check(context)
    return public_comment(original)


def reference(thread, parent):
    """Canonical identity for a local join, never a URL to fetch."""
    return ORIGIN + "/api/v1/" + ("posts/" if parent == thread else "comments/") + uuid(parent)


def collect(settings, state, known, *, fetch=transport.fetch):
    """Rotate retries, read fresh and backfill pages, then confirm new references.

    State retains references only. Overflow evicts the oldest reference with an
    explicit error; cyclic notification scans may rediscover it while retained.

    fetch asks the board: the transport, or an invented board in its place.
    """
    batch = Batch(state={"offset": 0, "pending": []})
    pending = {}
    try:
        client = Client(settings, fetch=fetch)
        client.phase(5)
        profile = client.get("/agents/me", authenticated=True)
        if uuid(profile["id"]) != client.owner:
            raise MailError("account_mismatch")
        mention = addressing.mention_pattern(addressing.aliases(profile, settings.get("mention_aliases")))
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
                pending[mid] = {"id": mid, "post": post, "kind": entry["kind"], "is_post": entry["is_post"], "types": _types(entry)}
    except FAILURES as exc:
        _error(batch, exc)
        batch.state["pending"] = list(pending.values())
        return batch

    selected = subscriptions.selected(settings)
    if isinstance(state.get("subscriptions"), dict):
        batch.state["subscriptions"] = json.loads(json.dumps(state["subscriptions"]))
    subscriptions.progress(batch.state, selected)
    if selected:
        # Notification work keeps most of the pass; a busy queue cannot spend the reserve.
        client.limit = MAX_REQUESTS - SUBSCRIPTION_REQUESTS
    resolve_seconds = 12 if selected else 15
    seen = set(known)
    emitted = {}  # Originals confirmed in this pass: a later notification may still add evidence.

    def resolve(ids, seconds):
        client.phase(seconds)
        for mid in ids:
            if mid not in pending:
                continue
            entry = pending.pop(mid)
            pending[mid] = entry  # A failed original must not monopolize retries.
            try:
                message, own, original, context = _fetch(client, entry)
                if own:
                    # Our own published text is public context worth keeping, never inbox mail.
                    addressing.cache_original(batch, message)
                else:
                    message["addressing"] = _addressing(client.owner, entry, original, context, mention)
                    batch.messages.append(message)
                    seen.add(mid)
                    emitted[mid] = (message, entry, original, context)
                del pending[mid]
            except FAILURES as exc:
                if isinstance(exc, MailError) and str(exc) in ("http_403", "http_404", "http_410", "original_deleted", "thread_deleted", "original_unavailable"):
                    batch.unavailable += 1
                else:
                    code = _error(batch, exc)
                    if code == "http_429":
                        return False
                    if code in OVER:
                        break
        return True

    if resolve(list(pending)[:8], resolve_seconds):
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
                        if entry is None:
                            continue
                        retained = pending.get(entry["id"]) or (emitted[entry["id"]][1] if entry["id"] in emitted else None)
                        if retained is not None:
                            # A second notification for one original adds evidence, not a copy.
                            retained["types"] = list(dict.fromkeys([*retained["types"], *entry["types"]]))
                            if entry["id"] in emitted:
                                message, _, original, context = emitted[entry["id"]]
                                message["addressing"] = _addressing(client.owner, retained, original, context, mention)
                            continue
                        if entry["id"] in seen:
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
            resolve(fresh[:8], resolve_seconds)
    batch.state["pending"] = list(pending.values())
    if selected and batch.error != "http_429":
        client.limit = MAX_REQUESTS
        client.phase(SUBSCRIPTION_SECONDS)
        _subscribed(client, selected, batch, seen, mention)
    batch.complete = batch.complete and not pending and batch.state["offset"] == 0
    return batch


def _node(client, node, post_id, title):
    """Normalize one listed comment like a direct fetch would; None when not public."""
    mid = uuid(node["id"])
    if uuid(node["post_id"]) != post_id:
        raise ValueError()
    if node.get("is_deleted") or node.get("is_hidden") or node.get("visibility", "public") != "public":
        return None, None
    author = node["author"]
    own = uuid(author["id"]) == client.owner
    message = {"id": mid, "thread_id": post_id, "kind": "thread_activity",
               "parent_id": uuid(node["parent_id"]) if node.get("parent_id") else None,
               "author": _text(author["name"]), "title": title, "body": _text(node["content"]),
               "url": _url(node.get("web_url"), "/comments/" + mid), "created_at": _timestamp(node["created_at"])}
    return message, own


def _walk(client, nodes, pending, served, out, *, deferred=()):
    """Flatten a listed tree into ``out``. A node whose replies were cut by depth or
    paging is queued once, with its ownership, so a later pass can still address its
    children; ``served`` remembers what this unit already queued. Returns True when
    the queue was full for some node, so the caller retains the unit instead of
    silently dropping that branch."""
    stack, queued, full = list(nodes), {entry[0] for entry in [*pending, *deferred]}, False
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            raise ValueError()
        out.append(node)
        replies = node.get("replies") or []
        if not isinstance(replies, list):
            raise ValueError()
        if node.get("has_more_replies"):
            parent = uuid(node["id"])
            if parent not in queued and parent not in served:
                if len(pending) < MAX_PENDING_PARENTS:
                    author = node.get("author") if isinstance(node.get("author"), dict) else {}
                    own = uuid(author["id"]) == client.owner if author.get("id") else None
                    pending.append([parent, len(replies), own])
                    queued.add(parent)
                    served.append(parent)
                else:
                    full = True
        stack.extend(replies)
    return full


def _progress(progress):
    """Saved position of one root, verified: head offset, cycle phase, parent queue and
    deferred page boundaries and the parents each retained unit already queued."""
    skip = progress.get("skip")
    skip = skip if type(skip) is int and skip >= 0 else 0
    pending = []
    if isinstance(progress.get("pending"), list):
        # The queue can also hold its retained parent page after queuing children.
        for entry in progress["pending"][:MAX_PENDING_PARENTS + 1]:
            try:
                if (isinstance(entry, list) and len(entry) == 3 and uuid(entry[0]) == entry[0]
                        and type(entry[1]) is int and entry[1] >= 0 and entry[2] in (True, False, None)
                        and entry[0] not in {e[0] for e in pending}):
                    pending.append([entry[0], entry[1], entry[2]])
            except (ValueError, TypeError, AttributeError):
                continue
    deferred = []
    # Older collectors kept one retained page in the active queue. Move that
    # extra entry aside so a popped active parent always frees a discovery slot.
    if len(pending) > MAX_PENDING_PARENTS:
        deferred.append(pending.pop())
    if isinstance(progress.get("deferred"), list):
        for entry in progress["deferred"][:MAX_SERVED]:
            try:
                if (isinstance(entry, list) and len(entry) == 3 and uuid(entry[0]) == entry[0]
                        and type(entry[1]) is int and entry[1] >= 0 and entry[2] in (True, False, None)
                        and entry[0] not in {e[0] for e in pending + deferred}
                        and len(deferred) < MAX_SERVED):
                    deferred.append([entry[0], entry[1], entry[2]])
            except (ValueError, TypeError, AttributeError):
                continue
    served = {}
    if isinstance(progress.get("served"), dict):
        for unit, ids in progress["served"].items():
            if isinstance(ids, list) and all(isinstance(i, str) for i in ids):
                served[unit] = list(ids)[:MAX_SERVED]
    return skip, progress.get("done") is True, pending, deferred, served


def _comment_page(raw, offset):
    comments, total = raw["comments"], raw["total"]
    if not isinstance(comments, list) or type(total) is not int or not 0 <= total < 2**63:
        raise ValueError()
    if not comments and offset < total:
        raise MailError("pagination_no_progress")
    return comments, total


def _scan(client, root, progress, batch, seen, mention):
    """One subscribed thread: root as context, then saved parent work, then top-level
    pages from the saved offset. Every listing read is delivered at once and the
    position after it is saved, so a long thread advances across passes and a
    finished cycle restarts at the head for later activity. When the parent queue
    is full the current page boundary is retained until its queued children are
    serviced, so no deep branch is dropped. Raises when cut short."""
    message, root_own, _, _ = _fetch(client, {"id": root, "post": root, "is_post": True, "kind": "mention"})
    addressing.cache_original(batch, message)
    title = message["title"]
    path = "/posts/" + root + "/comments"
    skip, done, pending, deferred, served = _progress(progress)
    tree = {root: root_own}
    for parent, _, own in pending + deferred:
        tree.setdefault(parent, own)
    nodes, interrupted = [], None

    def save():
        progress.clear()
        progress["skip"], progress["pending"] = skip, pending
        if deferred:
            progress["deferred"] = deferred
        if done:
            progress["done"] = True
        if served:
            progress["served"] = served

    def retained(full, unit):
        """Whether an overflowing unit can wait; beyond the bound it is consumed with an explicit error."""
        if full and sum(len(ids) for ids in served.values()) <= MAX_SERVED:
            return True
        if full:
            _error(batch, MailError("pending_overflow"))
        served.pop(unit, None)
        return False

    def defer(parent, offset, own):
        # Deferred pages do not consume discovery slots. Keep this metadata
        # bounded too; exhaustion is explicit, like the existing served bound.
        if len(deferred) < MAX_SERVED:
            deferred.append([parent, offset, own])
        else:
            served.pop(parent, None)
            _error(batch, MailError("pending_overflow"))

    try:
        while True:
            save()
            if pending or deferred:
                work = pending if pending else deferred
                parent, offset, own = work[0]
                try:
                    raw = client.get(path, {"parent_id": parent, "max_depth": 20, "limit": COMMENT_PAGE, "skip": offset})
                except MailError as exc:
                    if str(exc) not in UNAVAILABLE_ORIGINALS:
                        raise
                    # Retire only this unit for the cycle. Independent work can
                    # finish; a later head sweep may rediscover this parent.
                    work.pop(0)
                    served.pop(parent, None)
                    batch.unavailable += 1
                    _error(batch, exc)
                    continue
                children, total = _comment_page(raw, offset)
                work.pop(0)
                full = _walk(client, children, pending, served.setdefault(parent, []), nodes, deferred=deferred)
                if retained(full, parent):
                    defer(parent, offset, own)  # Reread after its queued children are serviced.
                elif children and offset + len(children) < total:
                    defer(parent, offset + len(children), own)
                continue
            if done:
                done, skip = False, 0
                break
            raw = client.get(path, {"max_depth": 20, "limit": COMMENT_PAGE, "skip": skip, "sort": "new"})
            top, total = _comment_page(raw, skip)
            full = _walk(client, top, pending, served.setdefault("head", []), nodes, deferred=deferred)
            if retained(full, "head"):
                continue  # The queue drains first; this page is reread at the same offset.
            skip += len(top)
            if not top or skip >= total:
                skip, done = 0, True
    except FAILURES as exc:
        interrupted = exc
    save()
    normalized = []
    for node in nodes:
        try:
            item, own = _node(client, node, root, title)
        except FAILURES:
            _error(batch, MailError("invalid_response"))
            continue
        if item is None:
            continue
        tree[item["id"]] = own
        normalized.append((item, own, "parent_id" in node))
    for item, own, explicit in normalized:
        if own:
            addressing.cache_original(batch, item)
            continue
        if item["id"] in seen:
            continue
        # An explicit null parent is a reply to the root; a missing field proves nothing.
        parent = tree.get(item["parent_id"]) if item["parent_id"] else (tree.get(root) if explicit else None)
        textual = addressing.mentions(mention, item["body"])
        item["addressing"] = addressing.resolve(direct=parent is True, mention=textual, thread=parent is False)
        item["discovery"] = "subscription"
        batch.messages.append(item)
        seen.add(item["id"])
    if interrupted is not None:
        raise interrupted
    return True


def _subscribed(client, selected, batch, seen, mention):
    """Subscribed roots in rotation. A root cut short keeps its own position; when it
    had already read something this pass the next pass starts at the following root."""
    entry = batch.state["subscriptions"]
    resume = None
    for root in subscriptions.rotation(entry, selected):
        progress = entry["roots"].setdefault(root, {})
        before = client.requests
        try:
            _scan(client, root, progress, batch, seen, mention)
        except FAILURES as exc:
            code = str(exc) if isinstance(exc, MailError) else "invalid_response"
            if code in UNAVAILABLE_ORIGINALS:
                progress.clear()
                batch.unavailable += 1
                continue
            _error(batch, exc)
            if code in ("http_429", *OVER):
                resume = subscriptions.restart(selected, root, client.requests > before)
                break
        resume = None
    subscriptions.advance(entry, selected, resume)
    if resume is not None:
        batch.complete = False


BOARD = Board(
    name="clawdchat", collect=collect, fields=frozenset({"api_key_file", "mention_aliases"}),
    originals=Originals(Client, lookup, root_as_thread=True), reference=reference,
    replies=Replies(hosts=("clawdchat.cn",), client=Client, read=reply, direct=("api", "v1", "comments"), pages=("post",)),
    coverage="Retained reply/mention notifications and bounded comment-tree scans in subscribed roots, confirmed against anonymous public originals. Retention is not guaranteed.")
