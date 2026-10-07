"""What the adapters share whose mail is notifications of the account, confirmed against public originals, and
activity in subscribed threads. It names no board: each board hands in what differs, on its client."""
import time
from urllib.error import HTTPError

from . import adapter_common, addressing, subscriptions
from .adapter_common import FAILURES, MAX_PAGES, PAGE_SIZE, SOURCE_SECONDS, failure, text, timestamp
from .errors import MailError, uuid


class Client(adapter_common.Client):
    """The client of one such board, and what the board says of itself.

    profile is the path of the account that the key belongs to, and account() takes the account out of what it
    gives. kinds maps the native type of a notification to the kind of its mail. event names the fields of a
    notification that hold its type, its post and its comment. reply and activity are the native types of a
    reply to a comment and of a comment on a post. body and author name the fields of an original that hold its
    text and the name of its author. pages stands between the host and the id of a post in a web address.
    by_thread says that an original is read out of the comment tree of its post and not at an address of its own.
    post() takes the post out of what its address gives."""
    profile = None
    kinds = {}
    event = (None, None, None)
    reply = activity = None
    body = author = None
    pages = None
    by_thread = False

    @staticmethod
    def account(profile):
        return profile

    def post(self, raw):
        return raw

    def page(self, path, key, position=None):
        """(items, the position after them or None at the end) of one page. Here the board pages by cursor."""
        return page(self, path, key, position)

    def comments(self, path, progress, batch):
        """(comments, what the saved progress of the thread becomes or None at the end) of the next page of a
        subscribed thread. Here the board pages by cursor and gives a tree."""
        cursor, pages = progress.get("cursor"), progress.get("pages")
        position = {"cursor": cursor} if isinstance(cursor, str) and cursor else None
        pages = pages if position and type(pages) is int and pages >= 0 else 0
        items, following = self.page(path, "comments", position)
        done = following is None or pages+1 >= MAX_PAGES
        return descendants(items, batch), None if done else {"cursor": following["cursor"], "pages": pages+1}


def page(client, path, key, position=None):
    """One page of a board that pages by cursor, through whatever stands for its client."""
    params = {"limit": PAGE_SIZE, **(position or {})}
    if path.endswith("/comments"):
        params["sort"] = "old"
    raw = client.get(path, params, authenticated=path == "/notifications")
    items = raw[key]
    if not isinstance(items, list): raise ValueError("Invalid page")
    more = raw["has_more"]
    if type(more) is not bool: raise ValueError("Invalid continuation")
    following = {"cursor": raw.get("next_cursor")}
    if more and (not isinstance(following["cursor"], str) or not following["cursor"]):
        raise ValueError("Missing cursor")
    if more and (not items or following == position): raise MailError("pagination_no_progress")
    return items, following if more else None


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
    state = batch.state
    pending = state.setdefault("pending", {})
    for key, entry in list(pending.items()):
        entry["ids"] = {mid: kind for mid, kind in entry["ids"].items() if mid not in known}
        if isinstance(entry.get("types"), dict):
            entry["types"] = {mid: t for mid, t in entry["types"].items() if mid in entry["ids"]}
        if not entry["ids"]: del pending[key]
    kinds = client.kinds
    of_type, of_post, of_comment = client.event

    def discover(items):
        for event in items:
            try:
                native = event.get(of_type)
                kind = kinds.get(native)
                if kind is None: continue
                raw_post = event.get(of_post)
                if raw_post is None: continue  # Outside addressable post/thread mail.
                post_id = uuid(raw_post)
                raw_comment = event.get(of_comment)
                mid = uuid(raw_comment) if raw_comment else post_id
                if mid in known: continue
                key = post_id if client.by_thread else mid
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
        items, following = client.page("/notifications", "notifications")
        discover(items)
        if position:
            try:
                items, following = client.page("/notifications", "notifications", position)
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
            resolve_original(client, entry, known, batch, mention)
        except FAILURES as exc:
            if failure(batch, exc) == "http_429": break
        if not entry["ids"]: del pending[key]
        elif entry.get("cursor"): batch.complete = False
    if len(pending) > MAX_PAGES: batch.complete = False


def subscription_mail(client, known, batch, mention):
    """Other-author activity in subscribed threads, after notification work.

    One extra source budget covers the subscribed roots in rotation. A root cut
    short keeps its own page position; the next pass starts at the following root
    when it had already read something this pass, so one slow thread waits a turn
    instead of starving the rest. An unavailable root is skipped, never blocking.
    """
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
            if not scan_thread(client, root, progress, known, batch, mention, consumed):
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


def scan_thread(client, root, progress, known, batch, mention, consumed):
    """Read one subscribed thread's public root, then comments from its saved position.

    Every page is delivered as soon as it is read and the position after it is
    saved, so a thread longer than one pass advances page by page and a finished
    cycle restarts at the head to find later activity. Parent ownership comes from
    the comments this root has fetched (a bounded map kept in its state); a parent
    never fetched stays unknown. Returns False when the pass was cut short."""
    raw = client.get("/posts/"+root)
    consumed[0] = True
    post = client.post(raw)
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
                body = original.get(client.body)
                message["addressing"] = addressing.resolve(direct=parent is True, mention=addressing.mentions(mention, body), thread=parent is False)
                batch.messages.append({**message, "kind": "thread_activity", "discovery": "subscription"})
                known.add(mid)
            except FAILURES as exc:
                failure(batch, exc)

    try:
        for _ in range(MAX_PAGES):
            items, following = client.comments(path, progress, batch)
            deliver(items)
            if following is None: clear_position(progress)
            else: progress.update(following)
            done = following is None
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


def resolve_original(client, entry, known, batch, mention=None):
    post_id, ids = entry["post"], entry["ids"]
    mid = next(iter(ids))
    path = "/posts/"+post_id if client.by_thread else ("/posts/" if mid == post_id else "/comments/")+mid
    try:
        raw = client.get(path)
    except HTTPError as exc:
        # Only this lookup establishes absence, not a failed comment page.
        if exc.code not in (403, 404, 410): raise
        exc.close()
        batch.unavailable += len(ids)
        return
    post = client.post(raw)
    if client.by_thread and uuid(post["id"]) != post_id: raise ValueError("Unexpected post")
    title = text(post.get("title") or "Public reply")
    tree = {}  # The comment tree seen so far: id -> (authored by us, original).
    kinds = client.kinds
    if client.by_thread: retain_original(client, batch, post, post_id, post_id, title)
    accept_original(client, post, post_id, ids, known, batch, title, entry=entry, kinds=kinds, mention=mention, tree=tree)
    following = None
    if client.by_thread and ids.keys()-{post_id}:
        try:
            items, following = client.page("/posts/"+post_id+"/comments", "comments", entry.get("cursor"))
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


def notification_addressing(client, types, original, mid, post_id, mention, tree):
    """Evidence order: a native reply to our comment, then a top-level comment on
    our post or a reply whose parent this pass saw us author. Thread activity
    requires a parent confirmed as someone else's; missing ownership is unknown.
    Explicit textual @mentions add attention even when the board sent only post activity."""
    reply, activity = client.reply, client.activity
    parent = uuid(original["parent_id"]) if original.get("parent_id") else None
    direct, thread = reply in types, False
    if activity in types and mid != post_id:
        if parent == post_id or tree.get(parent, (False,))[0]:
            direct = True
        elif "parent_id" in original and original["parent_id"] is None:
            direct = True
        elif parent in tree and tree[parent][0] is False:
            thread = True
    body = original.get(client.body)
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
    message["addressing"] = notification_addressing(client, types, original, mid, post_id, mention, tree or {})
    batch.messages.append({**message, "kind": ids[mid]})
    if tree and message["parent_id"] in tree:
        retain_original(client, batch, tree[message["parent_id"]][1], message["parent_id"], post_id, title)
    del ids[mid]
    known.add(mid)


def notification_message(client, original, mid, post_id, title):
    """Normalize a public original; collection alone filters out our own messages."""
    author = original.get("author")
    if author is None: author = {}
    if not isinstance(author, dict): raise ValueError("Invalid author")
    name = author.get(client.author)
    if name is not None: name = text(name)
    url = client.host+client.pages+post_id
    if mid != post_id: url += "#comment-"+mid
    return {"id": mid, "thread_id": post_id,
        "parent_id": uuid(original["parent_id"]) if original.get("parent_id") else None,
        "author": name, "title": title, "body": text(original[client.body]),
        "url": url, "created_at": timestamp(original["created_at"])}


def reference(host, pages, thread, parent):
    """Canonical identity for a local join, never a URL to fetch."""
    url = host + pages + uuid(thread)
    return url if parent == thread else url + "#comment-" + uuid(parent)


def mail(client, known, batch, mention):
    notification_mail(client, known, batch, mention)
    if batch.error != "http_429":
        subscription_mail(client, known, batch, mention)


def collect(client, source, settings, state, known, fetch):
    """One pass over such a board, whose client is of the class client."""
    return adapter_common.collect(lambda: client(source, settings, fetch=fetch), settings, state, known, client.profile,
                                  mail, client.account)
