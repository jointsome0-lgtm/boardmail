"""Shared CLI/MCP commands and safe, transport-independent results."""
from copy import deepcopy
import math
import sqlite3

from . import config, providers, reader, replies, verification
from .adapters import next_action
from .config import MailError

LOOKUP_ADAPTERS = ("postingboard", "the-colony", "moltbook", "clawdchat")
EXPAND_LIMIT = 20


def error_result(error):
    return {"event": "error", "error": error, "next_action": next_action(error),
            "history_complete": False}, 5 if error in ("database_missing", "config_missing") else 2


def outcome(operation):
    try:
        result, code = operation()
    except MailError as exc:
        return error_result(str(exc))
    except (OSError, ValueError, sqlite3.Error, KeyError, TypeError, OverflowError):
        return error_result("local_state_error")
    except KeyboardInterrupt:
        result, code = {"event": "cancelled"}, 4
    result.setdefault("history_complete", False)
    return result, code


def execute(store, command, *, sources=None, after=0, limit=None, unread=False, timeout=1800, source=None,
            id=None, action=None, ref=None, cancelled=None, require_fresh=False, stale_after=None, local=False,
            scope=None, context_mode=None, reset=False, through=None, thread=None,
            body=None, key=None, readback_body=None, replace_key=None):
    if command == 'reply_verify':
        if any(value is not None for value in (body, readback_body, replace_key)) or local:
            raise MailError('invalid_arguments')
        return verification.execute(store, sources, source, id, key=key, ref=ref)
    if command.startswith('reply_'):
        return replies.execute(store, command.removeprefix('reply_'), source, id, body=body,
                               key=key, readback_body=readback_body, ref=ref, replace_key=replace_key)
    if limit is None:
        limit = EXPAND_LIMIT if command == "expand" else 100
    if command in ("subscribe", "unsubscribe", "subscriptions"):
        if source is not None:
            try:
                config.identifier(source)
            except (ValueError, TypeError, AttributeError):
                raise MailError("invalid_arguments") from None
        if command == "subscriptions":
            return {"event": "subscriptions", "subscriptions": store.subscriptions(source),
                    "history": "available", "collection_performed": False,
                    "next_action": "subscribe_or_collect"}, 0
        if source is None:
            raise MailError("invalid_arguments")
        try:
            thread = config.uuid(thread)
        except (ValueError, TypeError, AttributeError):
            raise MailError("invalid_thread_id") from None
        subscribed = command == "subscribe"
        changed = store.set_subscription(source, thread, subscribed, (sources or {}).get(source))
        return {"event": "subscribed" if subscribed else "unsubscribed", "source": source,
                "thread": thread, "subscribed": subscribed, "changed": changed,
                "history": "available", "collection_performed": False,
                "next_action": "collect_then_read_messages_and_thread_activity" if subscribed else "read_saved_mail_or_collect"}, 0
    if command == "expand":
        if (type(after) is not int or type(through) is not int or type(limit) is not int
                or not 0 <= after <= through <= 2**63-1 or not 1 <= limit <= 100):
            raise MailError("invalid_arguments")
        for value in (source, thread):
            try:
                config.identifier(value)
            except (ValueError, TypeError, AttributeError):
                raise MailError("invalid_arguments") from None
        return expand(store, source, thread, after, through, limit, remote_settings(store, source, sources, local))
    if command in ("check", "list", "wait"):
        if type(after) is not int or not 0 <= after <= 2**63-1 or type(limit) is not int or not 1 <= limit <= 500:
            raise MailError("invalid_arguments")
        reader.validate_options(scope, context_mode)
        if through is not None and (command != "list" or type(through) is not int or not after <= through <= 2**63-1):
            raise MailError("invalid_arguments")
        if command == "list":
            if thread is not None and source is None:
                raise MailError("invalid_arguments")
            for value in (source, thread):
                if value is not None:
                    try:
                        config.identifier(value)
                    except (ValueError, TypeError, AttributeError):
                        raise MailError("invalid_arguments") from None
        settings = store.settings()
        reading = {"scope": scope or settings["scope"], "context": context_mode or settings["context"]}
    if command == "settings":
        settings = store.settings(scope=scope, context=context_mode, reset=reset)
        return {"event": "settings", "settings": settings, "applies_to": ["check", "list", "wait"],
                "consumer": "one_per_database", "next_action": "check_or_list"}, 0
    if command == "init":
        store.initialize(sources)
        return {"event": "initialized", **store.status()}, 0
    if command in ("pause", "resume"):
        paused = command == "pause"
        changed = store.set_paused(source, paused, (sources or {}).get(source))
        return {"event": "paused" if paused else "resumed", "source": source,
                "paused": paused, "changed": changed, "collection_performed": False}, 0
    if command in ("collect", "check"):
        if sources is None:
            raise MailError("config_missing")
        result = providers.collect_all(store, sources)
        if command == "check":
            return {"event": "messages", **store.page(after, limit, **reading), "collection_performed": True,
                    "collection": {key: result[key] for key in ("added", "failed", "errors")}}, 1 if result["failed"] else 0
        return result, 1 if result["failed"] else 0
    if command == "status":
        if stale_after is not None and (type(stale_after) is not int or not 0 <= stale_after <= 2**31-1):
            raise MailError("invalid_arguments")
        result = {"event": "status", **store.status(stale_after), "freshness_required": bool(require_fresh)}
        # Reading unknown, error or stale state is itself a success unless freshness was required.
        return result, 1 if require_fresh and not result["fresh"] else 0
    if command == "list":
        return {"event": "messages", **store.page(after, limit, unread=unread, through=through,
                source=source, thread=thread, **reading), "collection_performed": False}, 0
    if command == "wait":
        if not math.isfinite(timeout) or timeout < 0:
            raise MailError("invalid_arguments")
        result = store.wait(after, timeout, limit, cancelled=cancelled, **reading)
        result["collection_performed"] = False
        return result, {"messages": 0, "timeout": 3, "cancelled": 4}[result["event"]]
    if command not in ("mark", "show", "context"):
        raise MailError("invalid_arguments")
    try:
        message_id = config.identifier(id)
    except (ValueError, TypeError, AttributeError):
        raise MailError("invalid_message_id") from None
    if command == "context":
        return context(store, source, message_id, remote_settings(store, source, sources, local))
    if command == "show":
        result, _ = replies.execute(store, 'show', source, message_id)
        attempt = None
        if result['reply'] is not None:
            attempt = {'state': result['reply']['state'], 'next_action': result['next_action'],
                       'show': {'command': 'reply show', 'tool': 'boardmail_reply_show',
                                'arguments': {'source': source, 'id': message_id}}}
        return {'event': 'message', 'message': result['message'], 'reply_attempt': attempt}, 0
    store.mark(source, message_id, action.replace("-", "_"), ref=ref)
    return {"event": "marked", "message": store.show(source, message_id)}, 0


def remote_settings(store, source, sources, local):
    """Settings enabling a remote lookup, or None for a local read."""
    settings = None if local or not sources or store.is_paused(source) else sources.get(source)
    return settings if settings and settings.get("adapter", source) in LOOKUP_ADAPTERS else None


def element(status, message=None, *, origin=None, error=None, id=None):
    return {"id": message["id"] if message else id, "status": status, "origin": origin, "error": error,
            "message": message, "current_message": None, "differs_from_saved": None}


class CachedClient:
    """One command's remote reads: every distinct GET happens once, within one budget.

    Responses and failures are memoized so repeated roots, parents and
    comment pages cost no further requests. Once the budget is exhausted, later
    reads fail immediately instead of pacing or retrying against a spent deadline.
    """
    def __init__(self, client):
        self.client, self.cache, self.exhausted = client, {}, False

    def __getattr__(self, name):
        return getattr(self.client, name)

    def get(self, path, params=None, *, authenticated=False):
        key = (path, tuple(sorted((params or {}).items())), authenticated)
        if key not in self.cache:
            if self.exhausted:
                raise MailError("budget_exhausted")
            try:
                self.cache[key] = self.client.get(path, params, authenticated=authenticated)
            except providers.FAILURES as exc:
                if isinstance(exc, MailError) and str(exc) in ("budget_exhausted", "source_timeout"):
                    self.exhausted = True
                self.cache[key] = exc
                raise
        value = self.cache[key]
        if isinstance(value, BaseException):
            raise value
        return deepcopy(value)


class Lookup:
    """Original lookups for one command through a single client and budget."""

    def __init__(self, adapter, settings, client_factory=None):
        self.adapter = adapter
        if adapter == "clawdchat":
            from . import adapter_clawdchat
            client = client_factory(adapter, settings) if client_factory else adapter_clawdchat.Client(settings)
            self.fetch = adapter_clawdchat.lookup
        else:
            client = (client_factory or providers.Client)(adapter, settings)
            self.fetch = {"postingboard": providers.postingboard_lookup, "the-colony": providers.colony_lookup,
                          "moltbook": providers.moltbook_lookup}[adapter]
        self.client = CachedClient(client)
        self.originals = {}

    def __call__(self, mid, root=None):
        if self.adapter == "moltbook":
            return self.fetch(self.client, mid, root, originals=self.originals)
        return self.fetch(self.client, mid, root)


def resolve(store, source, adapter, lookup, mid, root=None):
    """Element plus the relationships to trust: a fetched original outranks a stored row."""
    stored = store.find(source, mid)
    # Moltbook exposes comments through their thread. Known roots need no comment probe.
    lookup_root = root
    if root is None and stored and (adapter == "moltbook" or
            adapter in ("the-colony", "clawdchat") and stored["thread_id"] == mid):
        lookup_root = stored["thread_id"]
    remote = lookup(mid, lookup_root) if lookup is not None else ("unknown", None, None)
    if remote[2] is not None:
        remote = (remote[0], remote[1], {"source": source, **remote[2]})
    if stored is not None:
        found = element("available", stored, origin="local")
        if lookup is not None:
            found["remote_status"], found["error"] = remote[:2]
            found["current_message"] = current = remote[2]
            if current is not None and (stored["id"], stored["thread_id"]) == (current["id"], current["thread_id"]):
                # Reply titles are display labels, often inherited from the thread.
                fields = ("title", "body") if stored["id"] == stored["thread_id"] else ("body",)
                found["differs_from_saved"] = any(stored[field] != current[field] for field in fields)
        return found, remote[2] or stored, remote[2] is not None
    found = element(remote[0], remote[2], origin="remote" if remote[2] else None, error=remote[1], id=mid)
    if lookup is not None:
        found["remote_status"] = remote[0]
    return found, remote[2], True


def parent_of(resolver, adapter, relations, root, authoritative):
    """The immediate parent element implied by trusted relationships; the root itself when a reply names none."""
    root_id = relations["thread_id"]
    parent_id = relations["parent_id"]
    if parent_id is None and root_id != relations["id"]:
        # A reply attaches to the root unless the board recorded a reply target.
        # Rows stored before reply targets were kept cannot say which; only a fetch can.
        recorded = authoritative or adapter != "postingboard" or relations.get("discovery") is not None
        if not recorded:
            return element("unknown")
        parent_id = root_id
    if parent_id is None:
        return element("none")
    if parent_id == root_id:
        return root
    parent = resolver(parent_id, root_id)[0]
    if parent["message"] is not None and parent["message"]["thread_id"] != root_id:
        parent = element("unavailable", error="invalid_response", id=parent_id)
    return parent


def context(store, source, message_id, settings, *, client_factory=None):
    """Thread root, immediate parent and target. Reads local rows first, then
    supported originals when configured. Nothing is marked, locally or remotely."""
    lookup = None
    adapter = settings.get("adapter", source) if settings is not None else store.adapter(source)
    if settings is not None:
        try:
            config.uuid(message_id)
        except (ValueError, TypeError, AttributeError):
            raise MailError("invalid_message_id") from None
        lookup = Lookup(adapter, settings, client_factory)
    resolver = lambda mid, root=None: resolve(store, source, adapter, lookup, mid, root)
    target, relations, authoritative = resolver(message_id)
    if relations is None:
        root = parent = element("unknown")
    else:
        root_id = relations["thread_id"]
        root = target if root_id == message_id else resolver(root_id, root_id)[0]
        parent = parent_of(resolver, adapter, relations, root, authoritative)
    complete = target["status"] == "available" and root["status"] == "available" and parent["status"] in ("available", "none")
    exchange = previous_exchange(store, source, adapter, target, parent, relations)
    return {"event": "context", "source": source, "id": message_id, "fetched": lookup is not None,
            "target": target, "parent": parent, "root": root, "complete": complete,
            "previous_exchange": exchange}, 0 if complete else 1


def current(item):
    """Available now: a fetched original, or a stored row whose original was confirmed."""
    return item["status"] == "available" and item.get("remote_status", "available") == "available"


def expand(store, source, thread, after, through, limit, settings, *, client_factory=None):
    """Every saved message of one thread within (after, through], each with the
    context a singular lookup would give, through one client and one budget.

    The common root is returned once; a parent that is the root becomes a
    same_as_root reference after its availability was counted. Marks stay
    unchanged; an empty page fetches nothing. Context can lie outside the interval."""
    adapter = settings.get("adapter", source) if settings is not None else store.adapter(source)
    if settings is not None:
        try:
            config.uuid(thread)
        except (ValueError, TypeError, AttributeError):
            raise MailError("invalid_arguments") from None
    page = store.page(after, limit, through=through, source=source, thread=thread, scope="all", context="none")
    rows = page["messages"]
    lookup = Lookup(adapter, settings, client_factory) if settings is not None and rows else None
    resolver = lambda mid, root=None: resolve(store, source, adapter, lookup, mid, root)
    root = resolver(thread, thread) if rows else (element("unknown", id=thread), None, True)
    items, complete, exhausted = [], current(root[0]) if rows else True, False
    for row in rows:
        mid = row["id"]
        # The thread is the trusted relationship: an original that now belongs elsewhere
        # is rejected by the lookup instead of attaching another thread's root here.
        target, relations, authoritative = root if mid == thread else resolver(mid, thread)
        if relations["thread_id"] != thread:
            target = element("unavailable", error="invalid_response", id=mid)
            relations, authoritative = None, True
            parent = element("unknown")
        else:
            parent = parent_of(resolver, adapter, relations, root[0], authoritative)
        exchange = previous_exchange(store, source, adapter, target, parent, relations)
        done = current(target) and current(root[0]) and (parent["status"] == "none" or current(parent))
        exhausted = exhausted or "budget_exhausted" in (target["error"], parent["error"])
        if parent is root[0]:
            parent = {"id": thread, "status": "same_as_root"}
        items.append({"id": mid, "arrival_seq": row["arrival_seq"], "target": target, "parent": parent,
                      "previous_exchange": exchange, "complete": done})
        complete = complete and done
    exhausted = exhausted or root[0]["error"] == "budget_exhausted" or bool(lookup and lookup.client.exhausted)
    return {"event": "expanded", "source": source, "thread": thread, "after": after, "through": through,
            "next_after": page["next_after"], "more": page["more"], "checkpoint_safe": False,
            "next_action": page["next_action"], "collection_performed": False,
            "fetched": settings is not None, "complete": complete, "budget_exhausted": exhausted,
            "root": root[0], "items": items}, 0 if complete else 1


def previous_exchange(store, source, adapter, target, parent, relations):
    result = {"status": "unknown", "reason": None, "reply_ref": None, "messages": []}
    if relations is None:
        result["reason"] = "no_parent_identity"
    elif relations["id"] == relations["thread_id"]:
        result["status"] = "none"
    elif relations["parent_id"] is None:
        result["reason"] = "no_parent_identity" if parent["status"] == "unknown" else "parent_not_recorded_by_board"
    elif parent["error"] == "invalid_response":
        result["reason"] = "parent_invalid"
    else:
        try:
            ref = providers.parent_reference(adapter, relations["thread_id"], parent["id"])
        except (ValueError, TypeError, AttributeError):
            result["reason"] = "parent_invalid"
            return result
        if ref is None:
            result["reason"] = "unsupported_source"
        else:
            result["reply_ref"] = ref
            result["messages"] = store.replied_with(source, ref, exclude_id=target["id"])
            result["status"] = "linked" if result["messages"] else "unmatched"
    return result
