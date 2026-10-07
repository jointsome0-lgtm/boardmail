"""Colony and Moltbook, read-only; confirmed prefixes survive failures with error health."""
import base64
import binascii
from functools import partial
import hashlib
import hmac
from http.client import HTTPException
import json
import time
from urllib.error import HTTPError

from . import adapter_common, addressing, subscriptions, transport
from .adapter_common import FAILURES, MAX_PAGES, PAGE_SIZE, SOURCE_SECONDS, error_code, failure, text, timestamp
from .adapters import Batch, public_comment
from .errors import MailError, uuid

HOSTS = {"the-colony":"https://thecolony.ai", "moltbook":"https://www.moltbook.com"}
transport.BOARDS.update({
    'the-colony': transport.Board(protocol=None, **adapter_common.SHARED),
    'moltbook': transport.Board(protocol=None, **adapter_common.SHARED),
})
MAX_AUTH_ERROR_BYTES = 4096
COLONY_AUTH_CODES = frozenset({
    "AUTH_2FA_REQUIRED", "AUTH_2FA_INVALID", "AUTH_INVALID_TOKEN",
    "AUTH_TOKEN_REVOKED", "AUTH_PENDING_ACTIVATION", "AUTH_IP_DENIED", "AUTH_AGENT_ONLY",
})


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


class Client(adapter_common.Client):
    """The client of Colony or Moltbook."""
    prefix = "/api/v1"

    def __init__(self, source, settings, *, fetch=transport.fetch):
        super().__init__(source, settings, fetch=fetch)
        self.host = HOSTS[source]

    def sign_in(self, key):
        if self.source != "the-colony":
            return key
        body = {"api_key": key}
        if "totp_secret_file" in self.settings:
            body["totp_code"] = colony_totp(self.settings["totp_secret_file"])
        return self._request("/auth/token", body=body)["access_token"]

    def refused(self, exc, token, path):
        if self.source == "the-colony" and (token or path == "/auth/token") and exc.code in (400, 401, 403):
            return colony_auth_error(exc)
        return None


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
def parent_reference(adapter, thread, parent):
    """Canonical identity for a local join, never a URL to fetch."""
    url = HOSTS[adapter] + ("/posts/" if adapter == "the-colony" else "/post/") + uuid(thread)
    return url if parent == thread else url + "#comment-" + uuid(parent)


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



def mail(client, known, batch, mention):
    notification_mail(client, known, batch, mention)
    if batch.error != "http_429":
        subscription_mail(client, known, batch, mention)


def collect(source, settings, state, known, *, fetch=transport.fetch):
    """One pass over Colony or Moltbook. fetch asks the board: the transport, or an invented board in its place."""
    return adapter_common.collect(partial(Client, source, settings, fetch=fetch), settings, state, known, "/agents/me",
                                  mail, (lambda profile: profile["agent"]) if source == "moltbook" else None)


def colony_reply(client, mid, thread, check):
    """Replies.read of Colony."""
    return public_comment(client.get("/comments/" + mid), body="body")


def moltbook_reply(client, mid, thread, check):
    """Replies.read of Moltbook: the comment out of the comment tree of its thread, and the root of that thread
    is checked like the comment."""
    originals = {}
    status, error, _ = moltbook_lookup(client, mid, thread, originals=originals)
    if status != "available":
        raise MailError(error or "reply_" + status)
    original = originals[thread, mid]
    check(originals[thread, thread])
    return public_comment(original, top_by_depth=True)



def declaration(name, coverage, find, replies, *fields, **asked):
    """What one of the two boards of this module declares."""
    return adapter_common.declare(name, partial(Client, name), partial(collect, name), coverage, find, replies,
                                  partial(parent_reference, name), *fields, **asked)


BOARDS = (
    declaration("the-colony", "Retained reply/mention notifications and available comment pages in subscribed roots, confirmed against anonymous public originals. Retention is not guaranteed.",
                colony_lookup, dict(hosts=("thecolony.ai",), pages=("post", "posts"), read=colony_reply, explicit=("held",)),
                "totp_secret_file", root_as_thread=True),
    declaration("moltbook", "Retained notifications and available comment trees in subscribed roots, with anonymous public originals. Reply/mention event variants remain provisional.",
                moltbook_lookup,
                dict(hosts=("www.moltbook.com", "moltbook.com"), pages=("post",), read=moltbook_reply, verified=True,
                     explicit=("is_deleted", "is_spam")),
                keeps=True, root_as_thread=True, comment_by_thread=True),
)
