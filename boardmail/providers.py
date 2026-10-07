"""Read-only adapters; confirmed prefixes survive failures with error health."""
import base64
import binascii
from datetime import datetime
from functools import partial
import hashlib
import hmac
from http.client import HTTPException
import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode

from . import addressing, subscriptions, transport
from .adapters import Batch, Board
from .adapters import collect_all  # Kept for existing Python callers of the 0.1 collector.
from .errors import MailError, uuid

HOSTS = {"postingboard":"https://getpostingboard.dev", "the-colony":"https://thecolony.ai",
         "moltbook":"https://www.moltbook.com"}
PAGE_SIZE = 100
MAX_PAGES = 100
SOURCE_SECONDS = 45
MAX_AUTH_ERROR_BYTES = 4096
COLONY_AUTH_CODES = frozenset({
    "AUTH_2FA_REQUIRED", "AUTH_2FA_INVALID", "AUTH_INVALID_TOKEN",
    "AUTH_TOKEN_REVOKED", "AUTH_PENDING_ACTIVATION", "AUTH_IP_DENIED", "AUTH_AGENT_ONLY",
})


# The three boards of this module call a failed request alike. Where no client is at hand to say which board
# is meant, this one answers for the three.
ANY = "moltbook"


def colony_auth_error(exc):
    """Read only a bounded, allowlisted reason; never retain provider prose."""
    try:
        raw = exc.read(MAX_AUTH_ERROR_BYTES + 1)
        if len(raw) > MAX_AUTH_ERROR_BYTES:
            return None
        data = json.loads(raw)
        detail = data.get("detail") if isinstance(data, dict) else None
        code = detail.get("code") if isinstance(detail, dict) else None
        if isinstance(code, str) and code in COLONY_AUTH_CODES:
            return code.lower()
    except (OSError, HTTPException, ValueError, TypeError, AttributeError):
        pass
    finally:
        exc.close()
    return None


def colony_totp(secret_file):
    """Generate one SHA-1, 30-second, six-digit TOTP without storing its value."""
    try:
        with secret_file.open(encoding="ascii") as stream:
            raw = stream.read(129)
    except OSError:
        raise MailError("credentials_unavailable") from None
    except UnicodeError:
        raise MailError("invalid_totp_secret") from None
    if len(raw) > 128:
        raise MailError("invalid_totp_secret")
    secret = raw.strip()
    if not secret:
        raise MailError("credentials_unavailable")
    try:
        key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
        if not key:
            raise ValueError()
    except (binascii.Error, ValueError):
        raise MailError("invalid_totp_secret") from None
    counter = (int(time.time()) // 30).to_bytes(8, "big")
    digest = hmac.new(key, counter, hashlib.sha1).digest()
    offset = digest[-1] & 15
    number = int.from_bytes(digest[offset:offset + 4], "big") & 0x7fffffff
    return f"{number % 1_000_000:06d}"


class Client:
    """The client of Postingboard, Colony or Moltbook. fetch asks the board: the transport, or an invented board
    in its place."""
    def __init__(self, source, settings, *, fetch=transport.fetch):
        self.source, self.settings = source, settings
        self.owner = settings["account_id"]
        self.host = HOSTS[source]
        self.token = None
        self.deadline = time.monotonic()+SOURCE_SECONDS
        self.next_request = 0
        self.fetch = fetch

    def _request(self, path, *, token=None, body=None):
        if self.source == "postingboard":
            time.sleep(max(0,self.next_request-time.monotonic()))
            self.next_request = time.monotonic()+1.1
        remaining = self.deadline-time.monotonic()
        if remaining <= 0:
            raise MailError("budget_exhausted")
        prefix = "" if self.source == "postingboard" else "/api/v1"
        try:
            return self.fetch(self.source, self.host+prefix+path, left=remaining,
                              headers={"Authorization": "Bearer " + token} if token else None, body=body)
        except HTTPError as exc:
            if self.source == "the-colony" and (token or path == "/auth/token") and exc.code in (400, 401, 403):
                code = colony_auth_error(exc)
                if code:
                    raise MailError(code) from None
            raise

    def get(self, path, params=None, *, authenticated=False):
        if authenticated and self.token is None:
            key = transport.key(self.source, self.settings["api_key_file"])
            if self.source == "the-colony":
                body = {"api_key": key}
                if "totp_secret_file" in self.settings:
                    body["totp_code"] = colony_totp(self.settings["totp_secret_file"])
                self.token = self._request("/auth/token", body=body)["access_token"]
            else:
                self.token = key
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


def error_code(exc):
    return transport.failure(ANY, exc)


def failure(batch, exc):
    code = error_code(exc)
    batch.complete = False
    if code != "budget_exhausted" and (batch.error is None or code == "http_429"):
        batch.error = code
    return code


def page(client, path, key, position=None):
    colony_comments = client.source == "the-colony" and path.endswith("/comments")
    params = {"limit": PAGE_SIZE, **(position or {})}
    if colony_comments:
        number = params.get("page", 1)
        if type(number) is not int or number < 1: raise ValueError("Invalid page number")
        params = {"limit": PAGE_SIZE, "page": number, "sort": "oldest"}
    elif path.endswith("/comments"):
        params["sort"] = "old"
    raw = client.get(path, params, authenticated=path == "/notifications")
    bare = client.source == "the-colony" and path == "/notifications"
    items = raw if bare else raw["items" if colony_comments else key]
    if not isinstance(items, list): raise ValueError("Invalid page")
    if bare:
        # Do not assume the server honored the requested page size.
        more = bool(items)
        following = {"offset": params.get("offset", 0)+len(items)}
    else:
        more = raw["has_more"]
        if type(more) is not bool: raise ValueError("Invalid continuation")
        if colony_comments:
            echoed = raw["page"]
            if type(echoed) is not int or echoed < 1: raise ValueError("Invalid page number")
            if echoed != number: raise MailError("pagination_no_progress")
            following = {"page": number+1}
        else:
            following = {"cursor": raw.get("next_cursor")}
            if more and (not isinstance(following["cursor"], str) or not following["cursor"]):
                raise ValueError("Missing cursor")
    if more and (not items or following == position): raise MailError("pagination_no_progress")
    return items, following if more else None


NATIVE_KINDS = {
    "the-colony": {"comment_on_post": "reply_to_post", "reply_to_comment": "reply_to_comment", "mention": "mention"},
    "moltbook": {"post_comment": "reply_to_post", "comment_reply": "reply_to_comment", "mention": "mention"},
}


def native_types(entry, mid, kinds):
    """Every native notification type retained for one original.

    A reference persisted by an earlier collector recorded only its kind; that
    kind came from exactly one native type, so it is restored, not guessed.
    """
    types = (entry.get("types") or {}).get(mid) if isinstance(entry.get("types"), dict) else None
    if isinstance(types, list) and types and all(t in kinds for t in types):
        return set(types)
    return {t for t, kind in kinds.items() if kind == entry["ids"][mid]}


def notification_mail(client, known, batch, mention=None):
    colony = client.source == "the-colony"
    state = batch.state
    pending = state.setdefault("pending", {})
    for key, entry in list(pending.items()):
        entry["ids"] = {mid: kind for mid, kind in entry["ids"].items() if mid not in known}
        if isinstance(entry.get("types"), dict):
            entry["types"] = {mid: t for mid, t in entry["types"].items() if mid in entry["ids"]}
        if not entry["ids"]: del pending[key]
    kinds = NATIVE_KINDS[client.source]

    def discover(items):
        for event in items:
            try:
                native = event.get("notification_type" if colony else "type")
                kind = kinds.get(native)
                if kind is None: continue
                raw_post = event.get("post_id" if colony else "relatedPostId")
                if raw_post is None: continue  # Outside addressable post/thread mail.
                post_id = uuid(raw_post)
                raw_comment = event.get("comment_id" if colony else "relatedCommentId")
                mid = uuid(raw_comment) if raw_comment else post_id
                if mid in known: continue
                key = mid if colony else post_id
                entry = pending.setdefault(key, {"post": post_id, "ids": {}, "cursor": None})
                prior_types = native_types(entry, mid, kinds) if mid in entry["ids"] else set()
                if mid not in entry["ids"] or kind == "mention": entry["ids"][mid] = kind
                # Overlapping notifications for one original are all evidence.
                if not isinstance(entry.get("types"), dict): entry["types"] = {}
                entry["types"][mid] = sorted(prior_types | {native})
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
            resolve_original(client, entry, known, batch, colony, mention)
        except FAILURES as exc:
            if failure(batch, exc) == "http_429": break
        if not entry["ids"]: del pending[key]
        elif entry.get("cursor"): batch.complete = False
    if len(pending) > MAX_PAGES: batch.complete = False


def subscription_mail(client, known, batch, mention):
    """Other-author activity in subscribed Colony/Moltbook threads, after notification work.

    One extra source budget covers the subscribed roots in rotation. A root cut
    short keeps its own page position; the next pass starts at the following root
    when it had already read something this pass, so one slow thread waits a turn
    instead of starving the rest. An unavailable root is skipped, never blocking.
    """
    colony = client.source == "the-colony"
    roots = subscriptions.selected(client.settings)
    entry = subscriptions.progress(batch.state, roots)
    if not roots: return
    end = time.monotonic()+SOURCE_SECONDS
    client.deadline = end
    resume = None
    for root in subscriptions.rotation(entry, roots):
        if time.monotonic() >= end:
            batch.complete, resume = False, root
            break
        progress = entry["roots"].setdefault(root, {})
        consumed = [False]
        try:
            if not scan_thread(client, root, progress, known, batch, mention, colony, consumed):
                batch.complete, resume = False, subscriptions.restart(roots, root, consumed[0])
                break
        except HTTPError as exc:
            if exc.code in (403, 404, 410):
                exc.close(); batch.unavailable += 1
                progress.clear()
                continue
            if failure(batch, exc) == "http_429":
                resume = subscriptions.restart(roots, root, consumed[0])
                break
        except FAILURES as exc:
            code = failure(batch, exc)
            if code in ("http_429", "budget_exhausted"):
                # A slow root response can spend the deadline without returning data.
                resume = subscriptions.restart(roots, root, consumed[0] or time.monotonic() >= end)
                break
        resume = None
    subscriptions.advance(entry, roots, resume)
    if resume is not None: batch.complete = False


def clear_position(progress):
    for key in ("page", "cursor", "pages"):
        progress.pop(key, None)


def scan_thread(client, root, progress, known, batch, mention, colony, consumed):
    """Read one subscribed thread's public root, then comments from its saved position.

    Every page is delivered as soon as it is read and the position after it is
    saved, so a thread longer than one pass advances page by page and a finished
    cycle restarts at the head to find later activity. Parent ownership comes from
    the comments this root has fetched (a bounded map kept in its state); a parent
    never fetched stays unknown. Returns False when the pass was cut short."""
    raw = client.get("/posts/"+root)
    consumed[0] = True
    post = raw if colony else raw["post"]
    if uuid(post["id"]) != root: raise ValueError("Unexpected thread")
    if post.get("is_deleted") or post.get("is_spam"):
        batch.unavailable += 1
        progress.clear()
        return True
    title = text(post.get("title") or "Public reply")
    author = post.get("author") if isinstance(post.get("author"), dict) else {}
    owned = subscriptions.owners(progress)
    root_own = uuid(author["id"]) == client.owner if author.get("id") else None
    subscriptions.remember(owned, root, root_own)
    retain_original(client, batch, post, root, root, title)  # The root is context, never inbox mail.
    path = "/posts/"+root+"/comments"

    def deliver(originals):
        originals = list(originals)
        for original in originals:  # Ownership first: a parent usually precedes its replies.
            try:
                if original.get("post_id") and uuid(original["post_id"]) != root: continue
                author = original.get("author") if isinstance(original.get("author"), dict) else {}
                subscriptions.remember(owned, uuid(original["id"]),
                                       uuid(author["id"]) == client.owner if author.get("id") else None)
            except FAILURES as exc:
                failure(batch, exc)
        for original in originals:
            try:
                mid = uuid(original["id"])
                if original.get("post_id") and uuid(original["post_id"]) != root: continue
                if original.get("is_deleted") or original.get("is_spam"): continue
                author = original.get("author") if isinstance(original.get("author"), dict) else {}
                # Self-exclusion cannot depend on an evictable ancestry cache.
                if author.get("id") and uuid(author["id"]) == client.owner:
                    retain_original(client, batch, original, mid, root, title)
                    continue
                if mid in known or mid == root: continue
                message = notification_message(client, original, mid, root, title)
                # An explicit null parent is a reply to the root; a missing field proves nothing.
                target = message["parent_id"]
                parent = root_own if target == root or (target is None and "parent_id" in original) else owned.get(target)
                body = original.get("body" if colony else "content")
                message["addressing"] = addressing.resolve(direct=parent is True, mention=addressing.mentions(mention, body), thread=parent is False)
                batch.messages.append({**message, "kind": "thread_activity", "discovery": "subscription"})
                known.add(mid)
            except FAILURES as exc:
                failure(batch, exc)

    try:
        for _ in range(MAX_PAGES):
            if colony:
                number = progress.get("page") if type(progress.get("page")) is int and progress.get("page") >= 1 else 1
                items, following = page(client, path, "comments", {"page": number})
                deliver(items)
                done = following is None
                if done: clear_position(progress)
                else: progress["page"] = following["page"]
            else:
                cursor, pages = progress.get("cursor"), progress.get("pages")
                position = {"cursor": cursor} if isinstance(cursor, str) and cursor else None
                pages = pages if position and type(pages) is int and pages >= 0 else 0
                items, following = page(client, path, "comments", position)
                deliver(descendants(items, batch))
                done = following is None or pages+1 >= MAX_PAGES
                if done: clear_position(progress)
                else: progress["cursor"], progress["pages"] = following["cursor"], pages+1
            subscriptions.store_owners(progress, owned)
            if done: return True
        return False
    except (HTTPError, MailError) as exc:
        # A saved position the board no longer accepts restarts at the head next pass.
        if (isinstance(exc, HTTPError) and exc.code in (400, 404, 410)) or str(exc) == "pagination_no_progress":
            clear_position(progress)
        raise
    finally:
        subscriptions.store_owners(progress, owned)


def resolve_original(client, entry, known, batch, colony, mention=None):
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
    tree = {}  # Moltbook comment tree seen so far: id -> (authored by us, original).
    kinds = NATIVE_KINDS[client.source]
    if not colony: retain_original(client, batch, post, post_id, post_id, title)
    accept_original(client, post, post_id, ids, known, batch, title, entry=entry, kinds=kinds, mention=mention, tree=tree)
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
                accept_original(client, original, post_id, ids, known, batch, title, entry=entry, kinds=kinds, mention=mention, tree=tree)
            except FAILURES as exc:
                failure(batch, exc)
    entry["cursor"] = following
    if following is None: batch.unavailable += len(ids)


def retain_original(client, batch, original, mid, post_id, title):
    """Keep a fully fetched public root, parent or own message for the local cache."""
    if original.get("is_deleted") or original.get("is_spam"): return
    try:
        addressing.cache_original(batch, notification_message(client, original, mid, post_id, title))
    except FAILURES:
        pass  # The cache never decides collection; a malformed extra is simply not kept.


def notification_addressing(source, types, original, mid, post_id, mention, tree):
    """Evidence order: a native reply to our comment, then a top-level comment on
    our post or a reply whose parent this pass saw us author. Thread activity
    requires a parent confirmed as someone else's; missing ownership is unknown.
    Explicit textual @mentions add attention even when the board sent only post activity."""
    reply, activity = ("reply_to_comment", "comment_on_post") if source == "the-colony" else ("comment_reply", "post_comment")
    parent = uuid(original["parent_id"]) if original.get("parent_id") else None
    direct, thread = reply in types, False
    if activity in types and mid != post_id:
        if parent == post_id or tree.get(parent, (False,))[0]:
            direct = True
        elif "parent_id" in original and original["parent_id"] is None:
            direct = True
        elif parent in tree and tree[parent][0] is False:
            thread = True
    body = original.get("body" if source == "the-colony" else "content")
    textual = addressing.mentions(mention, original.get("title"), body)
    return addressing.resolve(direct=direct, mention="mention" in types or textual, thread=thread)


def accept_original(client, original, post_id, ids, known, batch, title, *, entry=None, kinds=None, mention=None, tree=None):
    mid = uuid(original["id"])
    author = original.get("author")
    if author is None: author = {}
    if mid not in ids:
        # An unrelated tree member is remembered as a possible parent, never judged.
        if tree is not None and mid != post_id:
            try:
                if original.get("post_id") and uuid(original["post_id"]) != post_id:
                    return
                own = uuid(author["id"]) == client.owner if author.get("id") else None
                tree[mid] = (own, original)
                if own: retain_original(client, batch, original, mid, post_id, title)
            except FAILURES:
                pass
        return
    if not isinstance(author, dict): raise ValueError("Invalid author")
    own = uuid(author["id"]) == client.owner if author.get("id") else None
    if original.get("post_id") and uuid(original["post_id"]) != post_id:
        raise ValueError("Unexpected comment thread")
    if tree is not None and mid != post_id: tree[mid] = (own, original)
    if original.get("is_deleted") or original.get("is_spam"): return
    if own:
        retain_original(client, batch, original, mid, post_id, title)
        del ids[mid]
        return
    message = notification_message(client, original, mid, post_id, title)
    types = native_types(entry, mid, kinds) if entry is not None and kinds else set()
    message["addressing"] = notification_addressing(client.source, types, original, mid, post_id, mention, tree or {})
    batch.messages.append({**message, "kind": ids[mid]})
    if tree and message["parent_id"] in tree:
        retain_original(client, batch, tree[message["parent_id"]][1], message["parent_id"], post_id, title)
    del ids[mid]
    known.add(mid)


def notification_message(client, original, mid, post_id, title):
    """Normalize a public original; collection alone filters out our own messages."""
    colony = client.source == "the-colony"
    author = original.get("author")
    if author is None: author = {}
    if not isinstance(author, dict): raise ValueError("Invalid author")
    name = author.get("username" if colony else "name")
    if name is not None: name = text(name)
    url = client.host+("/posts/" if colony else "/post/")+post_id
    if mid != post_id: url += "#comment-"+mid
    return {"id": mid, "thread_id": post_id,
        "parent_id": uuid(original["parent_id"]) if original.get("parent_id") else None,
        "author": name, "title": title, "body": text(original["body" if colony else "content"]),
        "url": url, "created_at": timestamp(original["created_at"])}


def alias_pattern(aliases):
    return re.compile(r"(?<![\w-])(?:"+"|".join(re.escape(a) for a in aliases)+r")(?![\w-])", re.I) if aliases else None


def postingboard_mail(client, known, batch, explicit=None):
    # Ownership is evidence from this pass, never persisted preview metadata.
    # Finalize after hydration so a child can precede its parent, even across pages.
    ownership, addressed = {}, []
    try:
        postingboard_scan(client, known, batch, explicit, ownership, addressed)
    finally:
        for message, reasons, activity in addressed:
            parent = message["parent_id"]
            target = (message["thread_id"], parent)
            message["addressing"] = postingboard_addressing(reasons, explicit, message["title"], message["body"],
                direct=ownership.get(target) is True,
                thread=activity and (parent is None or target in ownership))


def postingboard_scan(client, known, batch, explicit, ownership, addressed):
    settings = client.settings
    mention = alias_pattern(settings.get("mention_aliases", []))
    state = batch.state
    pending = state.get("pending", {})
    for mid in [m for m in pending if m in known]:
        del pending[mid]
    if settings.get("inbox") or settings.get("alias_search") or pending:
        # Discovery beyond watched roots shares one budget: a third for pages,
        # the rest for full originals. Its cursors never touch thread cursors.
        state["pending"] = pending
        end = time.monotonic()+SOURCE_SECONDS
        client.deadline = time.monotonic()+SOURCE_SECONDS/3
        modes = [("inbox", None)]*bool(settings.get("inbox"))+[("search", t) for t in settings.get("alias_search", [])]
        for mode, term in modes:
            try:
                if mode == "inbox": postingboard_inbox(client, known, batch, pending)
                else: postingboard_search(client, term, known, batch, pending)
            except FAILURES as exc:
                if failure(batch, exc) == "http_429": return
        client.deadline = end
        for mid in list(pending)[:MAX_PAGES]:
            if time.monotonic() >= end: break
            entry = pending.pop(mid)
            pending[mid] = entry  # Rotation: one failing original cannot block later ones.
            try:
                resolve_postingboard(client, mid, entry, mention, known, batch, pending, explicit, ownership=ownership)
            except FAILURES as exc:
                code = failure(batch, exc)
                if code == "http_429": return
                if code == "budget_exhausted": break
        if pending: batch.complete = False
    cursors = state.setdefault("threads", {})
    subscribed = subscriptions.selected(settings)
    entry = subscriptions.progress(state, subscribed)
    configured = settings.get("threads") or []  # Missing or empty for a subscription-only setup.
    for root in [r for r in cursors if r not in configured and r not in subscribed]:
        del cursors[root]  # Progress of a dropped subscription; configured roots keep theirs.
    for root in subscribed:
        entry["roots"].setdefault(root, {})
    for thread in dict.fromkeys([*configured, *subscribed]):
        path = "/v1/posts/"+thread
        end = time.monotonic()+SOURCE_SECONDS
        first = None
        # A busy fresh page cannot consume the backfill's two-thirds budget.
        client.deadline = time.monotonic()+SOURCE_SECONDS/3
        try:
            first = client.get(path, {"limit": 30}, authenticated=True)
            postingboard_items(client, first, thread, mention, known, batch, explicit=explicit,
                               subscribed=thread in subscribed, ownership=ownership, addressed=addressed)
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
                following = postingboard_items(client, raw, thread, mention, known, batch, cursors=cursors, explicit=explicit,
                                               subscribed=thread in subscribed, ownership=ownership, addressed=addressed)
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


def forward_pages(client, path, params, state, key, batch, record):
    """Follow one independent forward sequence. Its saved position moves only
    after a page's candidates are recorded, so progress never skips a candidate."""
    cursor = state.get(key, 0)
    for page_number in range(MAX_PAGES):
        raw = client.get(path, {**params, "after": cursor, "limit": 30}, authenticated=True)
        items = raw["items"]
        if not isinstance(items, list): raise ValueError("Invalid page")
        for item in items:
            record(item)
        newest = raw.get("resume_after", raw.get("newest_cursor"))
        if newest is None and not items: newest = cursor
        if type(newest) is not int or not cursor <= newest < 2**63: raise ValueError("Invalid checkpoint")
        following = raw["next_after"]
        if following is not None and (type(following) is not int or not cursor < following <= newest):
            raise MailError("pagination_no_progress")
        state[key] = newest if following is None else following
        if following is None: return
        if page_number+1 == MAX_PAGES:
            batch.complete = False
            return
        cursor = following


def candidate(item, pending, known, *, reasons=(), term=None):
    """Record or merge one preview. Every Inbox reason and search term that
    offered a message is kept, so the full original can be judged against all of them."""
    mid = uuid(item["id"])
    if mid in known: return
    entry = pending.get(mid)
    if entry is None:
        seq = item["seq"]
        if type(seq) is not int or not 0 <= seq < 2**63: raise ValueError("Invalid sequence")
        title = item.get("title")
        entry = pending[mid] = {"root": uuid(item.get("root_id") or item.get("thread_id") or mid), "seq": seq,
                                "parent": uuid(item["reply_to_id"]) if item.get("reply_to_id") else None,
                                "title": text(title) if title is not None else None, "reasons": [], "terms": []}
    entry["reasons"] = sorted(set(entry["reasons"]) | set(reasons))
    if term is not None and term not in entry["terms"]: entry["terms"].append(term)


INBOX_REASONS = ("mention", "direct_reply", "reply_to_your_thread")


def discovery_of(entry, settings, title, body):
    """Native Inbox reasons name the message as addressed; otherwise only a
    configured or recorded term found in the full text is a truthful reason.
    The stored reason is bounded: documented reasons first, or the first
    matched term, so a valid message never exceeds the discovery limit."""
    if entry.get("reasons"):
        reasons = sorted(entry["reasons"], key=lambda r: (r not in INBOX_REASONS, r))[:3]
        return "inbox:"+"+".join(reasons)
    terms = dict.fromkeys([*entry.get("terms", []), *settings.get("alias_search", [])])
    matched = next((t for t in terms if alias_pattern([t]).search(title+"\n"+body)), None)
    return None if matched is None else "search:"+matched


def postingboard_inbox(client, known, batch, pending):
    def record(item):
        reasons = item.get("reasons", [])
        if not isinstance(reasons, list): raise ValueError("Invalid reasons")
        reasons = {r for r in reasons if isinstance(r, str) and re.fullmatch(r"[a-z_]{1,32}", r)}
        candidate(item, pending, known, reasons=reasons or {"inbox"})
    forward_pages(client, "/v1/inbox", {}, batch.state, "inbox_after", batch, record)


def postingboard_search(client, alias, known, batch, pending):
    cursors = batch.state.setdefault("search", {})
    try:
        forward_pages(client, "/v1/search", {"q": alias}, cursors, alias, batch,
                      lambda item: candidate(item, pending, known, term=alias))
    except HTTPError as exc:
        if exc.code == 400: cursors[alias] = 0  # A rejected cursor restarts; known IDs dedupe.
        raise


def postingboard_post(client, post, mid, root, title, *, seq=None):
    if uuid(post["id"]) != mid or uuid(post.get("root_id") or mid) != root: raise ValueError("Unexpected post")
    seq = post.get("seq", seq)
    if type(seq) is not int or not 0 <= seq < 2**63: raise ValueError("Invalid sequence")
    author = text(post["author"]) if post.get("author") is not None else None
    return {"id": mid, "thread_id": root, "provider_seq": seq, "author": author,
            "parent_id": uuid(post["reply_to_id"]) if post.get("reply_to_id") else None,
            "title": text(post.get("title") or title or "Untitled thread"), "body": text(post["body"]),
            "url": client.host+"/v1/posts/"+mid, "created_at": timestamp(post["created_at"])}


def postingboard_addressing(reasons, explicit, title, body, *, direct=False, thread=False):
    """Native Inbox reasons and explicit @aliases are evidence; a search hit alone is
    an occurrence of a term, so it stays unknown unless the text addresses us."""
    return addressing.resolve(direct=direct or "direct_reply" in reasons,
                              mention="mention" in reasons or addressing.mentions(explicit, title, body),
                              thread=thread or "reply_to_your_thread" in reasons)


def postingboard_discovery_kind(discovery, reasons, mention, body, parent, root):
    """Use the retained discovery for singular and watched-page originals alike."""
    if discovery is None: return None
    if discovery.startswith("search:") or "mention" in reasons or (mention and mention.search(body)):
        return "mention"
    return "reply_to_comment" if parent and parent != root else "reply_to_post"


def resolve_postingboard(client, mid, entry, mention, known, batch, pending, explicit=None, *, ownership):
    try:
        post = client.get("/v1/posts/"+mid, authenticated=True)["post"]
    except HTTPError as exc:
        if exc.code not in (404, 410): raise
        exc.close(); batch.unavailable += 1
        del pending[mid]
        return
    root = entry["root"]
    message = postingboard_post(client, post, mid, root, entry.get("title"), seq=entry["seq"])
    parent = message["parent_id"] or entry.get("parent")
    discovery = discovery_of(entry, client.settings, message["title"], message["body"])
    own = post.get("agent_id") is not None and uuid(post["agent_id"]) == client.owner
    if post.get("agent_id") is not None: ownership[root, mid] = own
    if own: addressing.cache_original(batch, message)
    kind = None if own else postingboard_discovery_kind(discovery, entry["reasons"], mention, message["body"], parent, root)
    if kind:
        batch.messages.append({**message, "parent_id": parent, "kind": kind, "discovery": discovery,
            "addressing": postingboard_addressing(set(entry.get("reasons", [])), explicit, message["title"], message["body"])})
        known.add(mid)
    del pending[mid]


def page_authors(client, items):
    """Confirmed authors on one page; missing author IDs supply no evidence.
    Malformed rows are left to the per-item checks."""
    ours, others = set(), set()
    for item in items:
        try:
            mid = uuid(item["id"])
            if item.get("agent_id") is not None:
                (ours if uuid(item["agent_id"]) == client.owner else others).add(mid)
        except FAILURES:
            continue
    return ours, others


def postingboard_items(client, raw, thread, mention, known, batch, *, ownership, addressed,
                      cursors=None, explicit=None, subscribed=False):
    root, replies = raw["post"], raw["replies"]
    if uuid(root["id"]) != thread: raise ValueError("Unexpected root")
    items = replies["items"]
    if not isinstance(items, list): raise ValueError("Invalid replies")
    before = cursors.get(thread) if cursors is not None else None
    pending = batch.state.get("pending", {})
    ours, others = page_authors(client, [root, *items])
    for mid in ours | others:
        # An incomplete page must not replace ownership from an earlier full original.
        ownership.setdefault((thread, mid), mid in ours)
    own = ownership.get((thread, thread)) is True
    if "body" in root:
        try:
            addressing.cache_original(batch, postingboard_post(client, root, thread, thread, None))
        except FAILURES:
            pass  # A root that fails normalization is reported by the item loop below.
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
                    title = text(root.get("title") or "Untitled thread")
                    original = postingboard_post(client, post, mid, thread, title, seq=seq)
                    body = original["body"]
                    author_id = uuid(post["agent_id"]) if post.get("agent_id") else None
                    if author_id is not None:
                        ownership[thread, mid] = author_id == client.owner
                        if mid == thread: own = author_id == client.owner
                    # A watched page can supply the full original of a pending discovery
                    # candidate; its retained reason completes that discovery here.
                    entry = pending.get(mid)
                    parent = original["parent_id"] or (entry or {}).get("parent")
                    retained = discovery_of(entry, client.settings, title, body) if entry else None
                    kind = postingboard_discovery_kind(retained, (entry or {}).get("reasons", []), mention, body, parent, thread)
                    if kind is None:
                        kind = ("mention" if mention and mention.search(body) else "reply_to_post" if own and mid != thread
                                else "thread_activity" if subscribed and mid != thread else None)
                    # A subscribed foreign root delivers its other-author replies as thread activity.
                    discovery = retained or (
                        "subscription" if kind == "thread_activity" or (subscribed and thread not in client.settings.get("threads", [])) else "thread")
                    if ownership.get((thread, mid)) is True:
                        pending.pop(mid, None)
                        if mid != thread: addressing.cache_original(batch, original)
                    elif kind:
                        message = {"id": mid, "thread_id": thread, "provider_seq": seq, "kind": kind,
                            "parent_id": parent, "author": original["author"], "title": title, "body": body,
                            "url": client.host+"/v1/posts/"+mid, "created_at": original["created_at"],
                            "discovery": discovery,
                            "addressing": None}
                        batch.messages.append(message)
                        addressed.append((message, set((entry or {}).get("reasons", [])), (own or subscribed) and mid != thread))
                        known.add(mid); pending.pop(mid, None)
        except FAILURES as exc:
            code = failure(batch, exc)
            if code in ("http_429", "budget_exhausted"): raise
            # Cyclic scans retry this item. Its failure cannot freeze siblings.
        if cursors is not None and mid != thread: cursors[thread] = seq
    following = replies["next_before"]
    if following is not None and (type(following) is not int or not items or following != before):
        raise MailError("pagination_no_progress")
    return following


def postingboard_lookup(client, mid, root=None):
    """One authenticated GET; it never acknowledges or marks anything remotely."""
    try:
        post = client.get("/v1/posts/"+mid, authenticated=True)["post"]
        message = postingboard_post(client, post, mid, root or uuid(post.get("root_id") or mid), None)
    except HTTPError as exc:
        return {404: "missing", 410: "deleted"}.get(exc.code, "unavailable"), error_code(exc), None
    except FAILURES as exc:
        return "unavailable", error_code(exc), None
    return "available", None, message


def colony_lookup(client, mid, root=None):
    """Read an anonymous original, including an owner's comment absent from the inbox."""
    try:
        is_post = mid == root
        try:
            post = client.get(("/posts/" if is_post else "/comments/") + mid)
        except HTTPError as exc:
            # An unstored target can be a post or a comment. Known relatives have one endpoint.
            if root is not None or exc.code != 404: raise
            exc.close()
            post = client.get("/posts/" + mid)
            is_post = True
        if uuid(post["id"]) != mid: raise ValueError("Unexpected original")
        thread = mid if is_post else uuid(post["post_id"])
        if post.get("post_id") and uuid(post["post_id"]) != thread: raise ValueError("Unexpected thread")
        if root is not None and thread != root: raise ValueError("Unexpected thread")
        if post.get("is_deleted"): return "deleted", None, None
        if post.get("is_spam"): return "unavailable", "hidden_by_provider", None
        message = notification_message(client, post, mid, thread, text(post.get("title") or "Public reply"))
    except HTTPError as exc:
        return {404: "missing", 410: "deleted"}.get(exc.code, "unavailable"), error_code(exc), None
    except FAILURES as exc:
        return "unavailable", error_code(exc), None
    return "available", None, message


def parent_reference(adapter, thread, parent):
    """Canonical identity for a local join, never a URL to fetch."""
    if adapter == "postingboard": return HOSTS[adapter] + "/v1/posts/" + uuid(parent)
    if adapter == "the-colony":
        url = HOSTS[adapter] + "/posts/" + uuid(thread)
        return url if parent == thread else url + "#comment-" + uuid(parent)
    if adapter == "moltbook":
        url = HOSTS[adapter] + "/post/" + uuid(thread)
        return url if parent == thread else url + "#comment-" + uuid(parent)
    if adapter == "clawdchat":
        # Passive readers use this mapping without loading adapter code.
        return "https://clawdchat.cn/api/v1/" + ("posts/" if parent == thread else "comments/") + uuid(parent)
    if adapter == "botnet":
        url = "https://botnet.com/topics/" + uuid(thread)
        return url if parent == thread else url + "#message-" + quote(parent, safe=":")
    return None


def moltbook_lookup(client, mid, root=None, *, originals=None):
    """Find an anonymous original in its paginated comment tree.

    A stored comment supplies its thread ID. Without that relationship only a
    root can be fetched; a missing post endpoint cannot establish comment absence.
    """
    originals = {} if originals is None else originals
    thread_loaded = False
    try:
        thread = root or mid
        if (thread, thread) not in originals:
            post = client.get("/posts/" + thread)["post"]
            if uuid(post["id"]) != thread: raise ValueError("Unexpected thread")
            originals[thread, thread] = post
        post = originals[thread, thread]
        thread_loaded = True
        if post.get("is_deleted"):
            return ("deleted", None, None) if mid == thread else ("unavailable", "thread_deleted", None)
        if post.get("is_spam"): return "unavailable", "hidden_by_provider", None
        title = text(post.get("title") or "Public reply")
        if mid == thread:
            return "available", None, notification_message(client, post, mid, thread, title)
        if (thread, mid) not in originals:
            position, visited = None, set()
            batch = Batch()
            for _ in range(MAX_PAGES):
                items, following = page(client, "/posts/" + thread + "/comments", "comments", position)
                for original in descendants(items, batch):
                    comment_id = uuid(original["id"])
                    if comment_id == thread: raise ValueError("Comment shares root identity")
                    # Keep encountered parents for this context command. They must
                    # not need another scan against the same remaining time budget.
                    originals[thread, comment_id] = original
                    if comment_id == mid: break
                if (thread, mid) in originals: break
                if batch.error: return "unavailable", batch.error, None
                if following is None: return "missing", None, None
                cursor = following["cursor"]
                if cursor in visited: raise MailError("pagination_no_progress")
                visited.add(cursor)
                position = following
            else:
                return "unavailable", "budget_exhausted", None
        original = originals[thread, mid]
        if original.get("post_id") and uuid(original["post_id"]) != thread:
            raise ValueError("Unexpected thread")
        if original.get("is_deleted"): return "deleted", None, None
        if original.get("is_spam"): return "unavailable", "hidden_by_provider", None
        return "available", None, notification_message(client, original, mid, thread, title)
    except HTTPError as exc:
        if thread_loaded: return "unavailable", error_code(exc), None
        if root is None and exc.code == 404:
            exc.close()
            return "unknown", "thread_unknown", None
        if mid != thread and exc.code in (404, 410):
            code = "thread_missing" if exc.code == 404 else "thread_deleted"
            exc.close()
            return "unavailable", code, None
        return {404: "missing", 410: "deleted"}.get(exc.code, "unavailable"), error_code(exc), None
    except FAILURES as exc:
        return "unavailable", error_code(exc), None


def collect(source, settings, state, known, *, fetch=transport.fetch):
    """One pass over Postingboard, Colony or Moltbook. fetch asks the board: the transport, or an invented board
    in its place."""
    batch = Batch(state=state)
    known = set(known)  # The pass adds what it finds to a set of its own.
    try:
        client = Client(source, settings, fetch=fetch)
        profile = client.get("/v1/me" if source == "postingboard" else "/agents/me", authenticated=True)
        if profile.get("success") is False: raise ValueError("Invalid profile")
        account = profile["agent"] if source == "moltbook" else profile
        if uuid(account["id"]) != uuid(settings["account_id"]):
            raise MailError("account_mismatch")
    except FAILURES as exc:
        return Batch(state=state, complete=False, error=error_code(exc))
    # Aliases come from the identity check already made; no extra profile request.
    configured = [*settings.get("mention_aliases", []), *settings.get("alias_search", [])]
    mention = addressing.mention_pattern(addressing.aliases(account, configured))
    try:
        if source == "postingboard": postingboard_mail(client, known, batch, mention)
        else:
            notification_mail(client, known, batch, mention)
            if batch.error != "http_429":
                subscription_mail(client, known, batch, mention)
    except FAILURES as exc:
        failure(batch, exc)
    return batch


def postingboard_settings(settings):
    """Postingboard's own rules for the settings of a source, which a config is held to in place."""
    settings.setdefault("mention_aliases", [])
    inbox = settings.get("inbox", False)
    if type(inbox) is not bool:
        raise ValueError()
    settings["inbox"] = inbox
    # Alias search is a separate opt-in; each term is also its exact match rule.
    search = settings.get("alias_search", [])
    if not isinstance(search, list) or any(not isinstance(a, str) or not a.strip() or len(a) > 100 for a in search):
        raise ValueError()
    settings["alias_search"] = list(dict.fromkeys(a.strip() for a in search))
    # Local subscriptions can supply roots at collection time.
    threads = settings.get("threads", [])
    if not isinstance(threads, list):
        raise ValueError()
    settings["threads"] = list(dict.fromkeys(uuid(t) for t in threads))


def declaration(name, coverage, *fields, configure=None):
    """What one of the three boards of this module declares. Each has an API key and a UUID for an account."""
    return Board(name=name, coverage=coverage, collect=partial(collect, name), account=uuid, configure=configure,
                 fields=frozenset(("api_key_file", "mention_aliases", *fields)), required=frozenset(("api_key_file",)),
                 since_v1=True)


BOARDS = (
    declaration("postingboard", "Configured roots, optional native Inbox/alias search, and activity in locally subscribed roots. Bounded backfill does not prove complete history.",
                "threads", "inbox", "alias_search", configure=postingboard_settings),
    declaration("the-colony", "Retained reply/mention notifications and available comment pages in subscribed roots, confirmed against anonymous public originals. Retention is not guaranteed.",
                "totp_secret_file"),
    declaration("moltbook", "Retained notifications and available comment trees in subscribed roots, with anonymous public originals. Reply/mention event variants remain provisional."),
)
