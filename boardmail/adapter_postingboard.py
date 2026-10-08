"""Postingboard: watched and subscribed roots, the native Inbox and alias search, read with the key of the account."""
from functools import partial
import re
import time
from urllib.error import HTTPError

from . import adapter_common, addressing, subscriptions, transport
from .adapter_common import FAILURES, MAX_PAGES, SOURCE_SECONDS, error_code, failure, text, timestamp
from .errors import MailError, uuid

NAME = "postingboard"
HOST = "https://getpostingboard.dev"
transport.BOARDS[NAME] = transport.Board(protocol='getpostingboard/1', **adapter_common.SHARED)


class Client(adapter_common.Client):
    host = HOST
    pause = 1.1


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
                code = failure(batch, exc)
                if code == "http_429": return
                if code == "budget_exhausted": break
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



def reference(thread, parent):
    """Canonical identity for a local join, never a URL to fetch."""
    return HOST + "/v1/posts/" + uuid(parent)


def collect(settings, state, known, *, fetch=transport.fetch):
    """One pass over Postingboard. fetch asks the board: the transport, or an invented board in its place."""
    return adapter_common.collect(partial(Client, NAME, settings, fetch=fetch), settings, state, known, "/v1/me",
                                  postingboard_mail)


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


def postingboard_reply(client, mid, thread, check):
    """Replies.read of Postingboard: the post of the account itself, read with its key."""
    original = client.get("/v1/posts/" + mid, authenticated=True)["post"]
    root = uuid(original["root_id"])
    if original.get("thread_id") is not None and uuid(original["thread_id"]) != root:
        raise MailError("reply_thread_mismatch")
    return original, root, uuid(original["agent_id"]), original["reply_to_id"], original["body"], "authenticated_original"



BOARD = adapter_common.declare(
    NAME, partial(Client, NAME), collect,
    "Configured roots, optional native Inbox/alias search, and activity in locally subscribed roots. Bounded backfill does not prove complete history.",
    postingboard_lookup, dict(hosts=("getpostingboard.dev",), direct=("v1", "posts"), read=postingboard_reply), reference,
    "threads", "inbox", "alias_search", configure=postingboard_settings, parents_since_discovery=True)
