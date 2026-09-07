"""Read-only adapters; confirmed prefixes survive failures with error health."""
from datetime import datetime
from http.client import HTTPException
import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import MailError, uuid

HOSTS = {"postingboard":"https://getpostingboard.dev", "the-colony":"https://thecolony.ai",
         "moltbook":"https://www.moltbook.com"}
PAGE_SIZE = 100
MAX_PAGES = 100
SOURCE_SECONDS = 45
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise MailError("redirect_refused")


class Client:
    def __init__(self, source, settings):
        self.source, self.settings = source, settings
        self.owner = settings["account_id"]
        self.host = HOSTS[source]
        self.token = None
        self.deadline = time.monotonic()+SOURCE_SECONDS
        self.next_request = 0
        self.opener = build_opener(NoRedirect())

    def _request(self, path, *, token=None, body=None):
        if self.source == "postingboard":
            time.sleep(max(0,self.next_request-time.monotonic()))
            self.next_request = time.monotonic()+1.1
        remaining = self.deadline-time.monotonic()
        if remaining <= 0:
            raise MailError("budget_exhausted")
        headers = {"Accept":"application/json", "User-Agent":"boardmail/0.2"}
        if token:
            headers["Authorization"] = "Bearer " + token
        if self.source == "postingboard":
            headers["X-Agent-Protocol"] = "getpostingboard/1"
        if body is not None:
            headers["Content-Type"] = "application/json"
        prefix = "" if self.source == "postingboard" else "/api/v1"
        request = Request(self.host+prefix+path, headers=headers,
                          data=json.dumps(body).encode() if body is not None else None)
        with self.opener.open(request, timeout=min(10,remaining)) as response:
            chunks, size = [], 0
            while True:
                if time.monotonic() > self.deadline:
                    raise MailError("source_timeout")
                chunk = response.read1(65536)
                if not chunk:
                    return json.loads(b"".join(chunks))
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise MailError("response_too_large")
                chunks.append(chunk)

    def get(self, path, params=None, *, authenticated=False):
        if authenticated and self.token is None:
            try:
                key = self.settings["api_key_file"].read_text().strip()
                if not key:
                    raise ValueError()
            except (OSError, ValueError):
                raise MailError("credentials_unavailable") from None
            self.token = (self._request("/auth/token", body={"api_key":key})["access_token"]
                          if self.source == "the-colony" else key)
        return self._request(path+("?"+urlencode(params) if params else ""),
                             token=self.token if authenticated else None)


def timestamp(value):
    if type(value) is int:
        if not -(2**63) <= value <= 2**63-1:
            raise ValueError("Timestamp outside SQLite range")
        return value
    parsed = datetime.fromisoformat(value.replace("Z","+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Missing timezone")
    return int(parsed.timestamp())


def text(value):
    if not isinstance(value,str):
        raise ValueError("Expected text")
    value.encode("utf-8")
    return value


def descendants(comments, batch):
    pending, seen = list(comments), set()
    while pending:
        comment = pending.pop()
        try:
            key = uuid(comment["id"])
            if key in seen: continue
            seen.add(key)
            yield comment
            replies = comment.get("replies", [])
            if not isinstance(replies, list): raise ValueError("Invalid replies")
            pending.extend(replies)
        except FAILURES as exc:
            failure(batch, exc)


FAILURES = (MailError, OSError, HTTPException, ValueError, KeyError, TypeError, AttributeError)


def failure(batch, exc):
    if isinstance(exc, MailError): code = str(exc)
    elif isinstance(exc, HTTPError):
        code = "http_"+str(exc.code)
        exc.close()
    elif isinstance(exc, (OSError, HTTPException)): code = "network_error"
    else: code = "invalid_response"
    batch.complete = False
    if code != "budget_exhausted" and (batch.error is None or code == "http_429"):
        batch.error = code
    return code


def page(client, path, key, position=None):
    params = {"limit": PAGE_SIZE, **(position or {})}
    if path.endswith("/comments"): params["sort"] = "old"
    raw = client.get(path, params, authenticated=path == "/notifications")
    bare = client.source == "the-colony" and path == "/notifications"
    items = raw if bare else raw[key]
    if not isinstance(items, list): raise ValueError("Invalid page")
    if bare:
        # Do not assume the server honored the requested page size.
        more = bool(items)
        following = {"offset": params.get("offset", 0)+len(items)}
    else:
        more = raw["has_more"]
        if type(more) is not bool: raise ValueError("Invalid continuation")
        following = {"cursor": raw.get("next_cursor")}
        if more and (not isinstance(following["cursor"], str) or not following["cursor"]):
            raise ValueError("Missing cursor")
    if more and (not items or following == position): raise MailError("pagination_no_progress")
    return items, following if more else None


def notification_mail(client, known, batch):
    colony = client.source == "the-colony"
    state = batch.state
    pending = state.setdefault("pending", {})
    for key, entry in list(pending.items()):
        entry["ids"] = {mid: kind for mid, kind in entry["ids"].items() if mid not in known}
        if not entry["ids"]: del pending[key]
    kinds = ({"comment_on_post":"reply_to_post", "reply_to_comment":"reply_to_comment", "mention":"mention"}
             if colony else {"post_comment":"reply_to_post", "comment_reply":"reply_to_comment", "mention":"mention"})

    def discover(items):
        for event in items:
            try:
                kind = kinds.get(event.get("notification_type" if colony else "type"))
                if kind is None: continue
                raw_post = event.get("post_id" if colony else "relatedPostId")
                if raw_post is None: continue  # Outside addressable post/thread mail.
                post_id = uuid(raw_post)
                raw_comment = event.get("comment_id" if colony else "relatedCommentId")
                mid = uuid(raw_comment) if raw_comment else post_id
                if mid in known: continue
                key = mid if colony else post_id
                entry = pending.setdefault(key, {"post": post_id, "ids": {}, "cursor": None})
                if mid not in entry["ids"] or kind == "mention": entry["ids"][mid] = kind
            except FAILURES as exc:
                failure(batch, exc)

    end = time.monotonic()+SOURCE_SECONDS
    client.deadline = time.monotonic()+SOURCE_SECONDS/3
    position = state.get("discovery")
    try:
        items, following = page(client, "/notifications", "notifications")
        discover(items)
        if position:
            try:
                items, following = page(client, "/notifications", "notifications", position)
                discover(items)
            except FAILURES as exc:
                # Invalid/expired cursors must not fail forever at one position.
                if (isinstance(exc, HTTPError) and exc.code in (400, 404, 410)) or isinstance(exc, (ValueError, KeyError, TypeError)) or str(exc) == "pagination_no_progress":
                    state["discovery"] = None
                raise
        state["discovery"] = following
    except FAILURES as exc:
        if failure(batch, exc) == "http_429": return
    client.deadline = end
    if state.get("discovery"): batch.complete = False
    # Persisted insertion order gives every unresolved original a turn. No body
    # from an authenticated notification enters this state or the inbox.
    for key in list(pending)[:MAX_PAGES]:
        if time.monotonic() >= end:
            batch.complete = False
            break
        entry = pending.pop(key)
        pending[key] = entry
        try:
            resolve_original(client, entry, known, batch, colony)
        except FAILURES as exc:
            if failure(batch, exc) == "http_429": break
        if not entry["ids"]: del pending[key]
        elif entry.get("cursor"): batch.complete = False
    if len(pending) > MAX_PAGES: batch.complete = False


def resolve_original(client, entry, known, batch, colony):
    post_id, ids = entry["post"], entry["ids"]
    mid = next(iter(ids))
    path = (("/posts/" if mid == post_id else "/comments/")+mid) if colony else "/posts/"+post_id
    try:
        raw = client.get(path)
    except HTTPError as exc:
        # Only this lookup establishes absence, not a failed comment page.
        if exc.code not in (403, 404, 410): raise
        exc.close()
        batch.unavailable += len(ids)
        return
    post = raw if colony else raw["post"]
    if not colony and uuid(post["id"]) != post_id: raise ValueError("Unexpected post")
    title = text(post.get("title") or "Public reply")
    accept_original(client, post, post_id, ids, known, batch, colony, title)
    following = None
    if not colony and ids.keys()-{post_id}:
        try:
            items, following = page(client, "/posts/"+post_id+"/comments", "comments", entry.get("cursor"))
        except FAILURES as exc:
            if (isinstance(exc, HTTPError) and exc.code in (400, 404, 410)) or str(exc) == "pagination_no_progress":
                entry["cursor"] = None
            raise
        for original in descendants(items, batch):
            try:
                accept_original(client, original, post_id, ids, known, batch, colony, title)
            except FAILURES as exc:
                failure(batch, exc)
    entry["cursor"] = following
    if following is None: batch.unavailable += len(ids)


def accept_original(client, original, post_id, ids, known, batch, colony, title):
    mid = uuid(original["id"])
    if mid not in ids: return
    if original.get("post_id") and uuid(original["post_id"]) != post_id:
        raise ValueError("Unexpected comment thread")
    if original.get("is_deleted") or original.get("is_spam"): return
    author = original.get("author")
    if author is None: author = {}
    if not isinstance(author, dict): raise ValueError("Invalid author")
    if author.get("id") and uuid(author["id"]) == client.owner:
        del ids[mid]
        return
    name = author.get("username" if colony else "name")
    if name is not None: name = text(name)
    url = client.host+("/posts/" if colony else "/post/")+post_id
    if colony and mid != post_id: url += "#comment-"+mid
    batch.messages.append({"id": mid, "thread_id": post_id, "kind": ids[mid],
        "parent_id": uuid(original["parent_id"]) if original.get("parent_id") else None,
        "author": name, "title": title, "body": text(original["body" if colony else "content"]),
        "url": url, "created_at": timestamp(original["created_at"])})
    del ids[mid]
    known.add(mid)


def postingboard_mail(client, known, batch):
    aliases = client.settings.get("mention_aliases", [])
    mention = re.compile(r"(?<![\w-])(?:"+"|".join(re.escape(a) for a in aliases)+r")(?![\w-])", re.I) if aliases else None
    cursors = batch.state.setdefault("threads", {})
    for thread in client.settings["threads"]:
        path = "/v1/posts/"+thread
        end = time.monotonic()+SOURCE_SECONDS
        first = None
        # A busy fresh page cannot consume the backfill's two-thirds budget.
        client.deadline = time.monotonic()+SOURCE_SECONDS/3
        try:
            first = client.get(path, {"limit": 30}, authenticated=True)
            postingboard_items(client, first, thread, mention, known, batch)
        except HTTPError as exc:
            if exc.code == 404:
                exc.close(); batch.unavailable += 1
                continue
            if failure(batch, exc) == "http_429": return
        except FAILURES as exc:
            failure(batch, exc)
        client.deadline = end
        try:
            before = cursors.get(thread)
            raw = first if before is None and first is not None else client.get(path, {"limit": 30, **({"before": before} if before is not None else {})}, authenticated=True)
            for page_number in range(MAX_PAGES):
                following = postingboard_items(client, raw, thread, mention, known, batch, cursors=cursors)
                if following is None:
                    cursors[thread] = None
                    break
                if page_number+1 == MAX_PAGES:
                    batch.complete = False
                    break
                raw = client.get(path, {"limit": 30, "before": cursors[thread]}, authenticated=True)
            else:
                batch.complete = False
        except FAILURES as exc:
            if failure(batch, exc) == "http_429": return
        if cursors.get(thread) is not None: batch.complete = False


def postingboard_items(client, raw, thread, mention, known, batch, *, cursors=None):
    root, replies = raw["post"], raw["replies"]
    if uuid(root["id"]) != thread: raise ValueError("Unexpected root")
    own = root.get("agent_id") is not None and uuid(root["agent_id"]) == client.owner
    items = replies["items"]
    if not isinstance(items, list): raise ValueError("Invalid replies")
    before = cursors.get(thread) if cursors is not None else None
    for item in [root, *items]:
        mid = uuid(item["id"])
        seq = item["seq"]
        if type(seq) is not int or not 0 <= seq < 2**63: raise ValueError("Invalid sequence")
        if mid != thread:
            if before is not None and seq >= before: raise MailError("pagination_no_progress")
            before = seq
        try:
            if mid not in known:
                try:
                    post = item if "body" in item else client.get("/v1/posts/"+mid, authenticated=True)["post"]
                except HTTPError as exc:
                    if exc.code not in (404, 410): raise
                    exc.close(); batch.unavailable += 1
                    post = None
                if post is not None:
                    if uuid(post["id"]) != mid or mid != thread and uuid(post["root_id"]) != thread:
                        raise ValueError("Unexpected reply")
                    body = text(post["body"])
                    author_id = uuid(post["agent_id"]) if post.get("agent_id") else None
                    kind = "mention" if mention and mention.search(body) else "reply_to_post" if own and mid != thread else None
                    if author_id != client.owner and kind:
                        author = text(post["author"]) if post.get("author") is not None else None
                        batch.messages.append({"id": mid, "thread_id": thread, "provider_seq": seq, "kind": kind,
                            "author": author, "title": text(root.get("title") or "Untitled thread"), "body": body,
                            "url": client.host+"/v1/posts/"+mid, "created_at": timestamp(post["created_at"])})
                        known.add(mid)
        except FAILURES as exc:
            code = failure(batch, exc)
            if code in ("http_429", "budget_exhausted"): raise
            # Cyclic scans retry this item. Its failure cannot freeze siblings.
        if cursors is not None and mid != thread: cursors[thread] = seq
    following = replies["next_before"]
    if following is not None and (type(following) is not int or not items or following != before):
        raise MailError("pagination_no_progress")
    return following


def collect(source, settings, state, known, *, client_factory=None):
    from .adapters import Batch
    batch = Batch(state=state)
    try:
        client = (client_factory or Client)(source, settings)
        if source == "postingboard": postingboard_mail(client, known, batch)
        else: notification_mail(client, known, batch)
    except FAILURES as exc:
        failure(batch, exc)
    return batch


# Kept for existing Python callers of the 0.1 collector.
from .adapters import collect_all
