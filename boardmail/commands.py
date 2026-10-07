"""Shared CLI/MCP commands and safe, transport-independent results."""
from copy import deepcopy
import errno
from functools import wraps
import sqlite3

from . import config, providers, replies, table, tags, transport, verification
from .config import MailError
from .errors import exit_code, next_action

LOOKUP_ADAPTERS = ("postingboard", "the-colony", "moltbook", "clawdchat", "botnet")
LOCAL_FAILURES = (OSError, ValueError, sqlite3.Error, KeyError, TypeError, OverflowError)
# The function that runs each command of the command table.
HANDLERS = {}
# What providers.parent_reference is given that it cannot make a reference from.
NO_REFERENCE = object()


def error_result(error):
    result = {"event": "error", "error": error, "next_action": next_action(error),
              "history_complete": False}
    if error == "message_not_found":
        result["identifier_hint"] = ("Use the source and remote message id returned by list. "
                                     "arrival_seq is a local arrival cursor, not a remote message id.")
    return result, exit_code(error)


def local_state_result(exc, source=None, message_id=None):
    """Fixed diagnostic codes; exception prose never enters the shared result."""
    result, code = error_result("local_state_error")
    reason = None
    if isinstance(exc, OSError):
        if exc.errno == errno.EROFS:
            reason = "read_only"
        elif exc.errno in (errno.EACCES, errno.EPERM):
            reason = "permission_denied"
    elif isinstance(exc, sqlite3.Error):
        sqlite_code = getattr(exc, "sqlite_errorcode", None)
        if type(sqlite_code) is int:
            if sqlite_code & 0xff == sqlite3.SQLITE_READONLY:
                reason = "read_only"
            elif sqlite_code & 0xff == sqlite3.SQLITE_PERM:
                reason = "permission_denied"
    if reason is not None:
        result["reason"] = reason
    if all(config.converted(config.identifier, value) for value in (source, message_id)):
        result["recovery"] = {"command": "reply show", "tool": "boardmail_reply_show",
                              "arguments": {"source": source, "id": message_id}, "read_only": True}
        result["send_allowed"] = False
    return result, code


def outcome(operation):
    try:
        result, code = operation()
    except MailError as exc:
        return error_result(str(exc))
    except LOCAL_FAILURES as exc:
        return local_state_result(exc)
    except KeyboardInterrupt:
        result, code = {"event": "cancelled"}, 4
    result.setdefault("history_complete", False)
    return result, code


def execute(store, name, /, *, sources=None, cancelled=None, fetch=transport.fetch, **given):
    """Run a command of the command table with what an entry point was given for its arguments.

    fetch asks a board for a command that can ask one: the transport, or an invented board in its place."""
    command = table.COMMANDS.get(name)
    if command is None:
        raise MailError("invalid_arguments")
    arguments = table.checked(command, given)
    if command.sources:
        arguments["sources"] = sources
    if command.waits:
        arguments["cancelled"] = cancelled
    if table.OPEN_WORLD in command.hints:
        arguments["fetch"] = fetch
    return HANDLERS[name](store, **arguments)


def handles(name):
    """Mark the function that runs a command. It is called with the store and with every argument of the command
    by name, as table.checked() leaves them. It gets sources or cancelled as well where the entry of the command
    says so, and fetch where the command can ask a board. It returns the result and the exit code."""
    def keep(function):
        HANDLERS[name] = function
        return function
    return keep


@handles("init")
def run_init(store, *, sources):
    store.initialize(sources)
    return {"event": "initialized", **store.status()}, 0


@handles("collect")
def run_collect(store, *, sources, fetch):
    if sources is None:
        raise MailError("config_missing")
    result = providers.collect_all(store, sources, fetch=fetch)
    return result, 1 if result["failed"] else 0


@handles("settings")
def run_settings(store, *, scope, context, reset):
    settings = store.settings(scope=scope, context=context, reset=reset)
    return {"event": "settings", "settings": settings, "applies_to": ["check", "list", "wait"],
            "consumer": "one_per_database", "next_action": "check_or_list"}, 0


def follow(store, sources, source, thread, subscribed):
    changed = store.set_subscription(source, thread, subscribed, (sources or {}).get(source))
    return {"event": "subscribed" if subscribed else "unsubscribed", "source": source,
            "thread": thread, "subscribed": subscribed, "changed": changed,
            "history": "available", "collection_performed": False,
            "next_action": "collect_then_read_messages_and_thread_activity" if subscribed else "read_saved_mail_or_collect"}, 0


@handles("subscribe")
def run_subscribe(store, *, sources, source, thread):
    return follow(store, sources, source, thread, True)


@handles("unsubscribe")
def run_unsubscribe(store, *, sources, source, thread):
    return follow(store, sources, source, thread, False)


@handles("subscriptions")
def run_subscriptions(store, *, source):
    return {"event": "subscriptions", "subscriptions": store.subscriptions(source),
            "history": "available", "collection_performed": False,
            "next_action": "subscribe_or_collect"}, 0


@handles("tags")
def run_tags(store):
    return tags.execute(store, "list")


@handles("tag_add")
def run_tag_add(store, *, tag, source, thread, id):
    return tags.execute(store, "add", tag=tag, source=source, thread=thread, id=id)


@handles("tag_remove")
def run_tag_remove(store, *, tag, source, thread, id):
    return tags.execute(store, "remove", tag=tag, source=source, thread=thread, id=id)


@handles("tag_show")
def run_tag_show(store, *, tag):
    return tags.execute(store, "show", tag=tag)


def pause(store, sources, source, paused):
    changed = store.set_paused(source, paused, (sources or {}).get(source))
    return {"event": "paused" if paused else "resumed", "source": source,
            "paused": paused, "changed": changed, "collection_performed": False}, 0


@handles("pause")
def run_pause(store, *, sources, source):
    return pause(store, sources, source, True)


@handles("resume")
def run_resume(store, *, sources, source):
    return pause(store, sources, source, False)


@handles("status")
def run_status(store, *, require_fresh, stale_after):
    result = {"event": "status", **store.status(stale_after), "freshness_required": bool(require_fresh)}
    # Reading unknown, error or stale state is itself a success unless freshness was required.
    return result, 1 if require_fresh and not result["fresh"] else 0


def reading(store, scope, context):
    """How check, list and wait show a page: as the inbox has saved it, unless the call says otherwise."""
    settings = store.settings()
    return {"scope": scope or settings["scope"], "context": context or settings["context"]}


@handles("check")
def run_check(store, *, sources, fetch, after, limit, scope, context):
    shown = reading(store, scope, context)
    if sources is None:
        raise MailError("config_missing")
    result = providers.collect_all(store, sources, fetch=fetch)
    return {"event": "messages", **store.page(after, limit, **shown), "collection_performed": True,
            "collection": {key: result[key] for key in ("added", "failed", "errors")}}, 1 if result["failed"] else 0


@handles("list")
def run_list(store, *, after, limit, scope, context, unread, through, source, thread, tag, untagged):
    shown = reading(store, scope, context)
    return {"event": "messages", **store.page(after, limit, unread=unread, through=through,
            source=source, thread=thread, tag=tag, untagged=untagged, **shown), "collection_performed": False}, 0


@handles("wait")
def run_wait(store, *, cancelled, after, limit, scope, context, timeout):
    shown = reading(store, scope, context)
    if not table.fits(table.TIMEOUT, timeout):
        raise MailError("invalid_arguments")
    result = store.wait(after, timeout, limit, cancelled=cancelled, **shown)
    result["collection_performed"] = False
    return result, {"messages": 0, "timeout": 3, "cancelled": 4}[result["event"]]


def message(store, source, message_id, event):
    result, _ = replies.execute(store, 'show', source, message_id)
    return {'event': event, 'message': result['message'],
            'reply_attempt': replies.summary(source, message_id, result['reply'])}, 0


@handles("show")
def run_show(store, *, source, id):
    return message(store, source, id, 'message')


@handles("mark")
def run_mark(store, *, action, ref, source, id):
    store.mark(source, id, action.replace("-", "_"), ref=ref)
    return message(store, source, id, 'marked')


@handles("context")
def run_context(store, *, sources, fetch, source, id, local):
    return context(store, source, id, remote_settings(store, source, sources, local), fetch=fetch)


@handles("expand")
def run_expand(store, *, sources, fetch, source, thread, through, after, limit, local):
    return expand(store, source, thread, after, through, limit, remote_settings(store, source, sources, local),
                  fetch=fetch)


@handles("reply_list")
def run_reply_list(store, *, after, limit):
    with store.connect() as db:
        return {'event': 'reply_attempts', **replies.pending(db, after, limit),
                'collection_performed': False, 'publication_performed': False}, 0


def journal(function):
    """A reply command: where the inbox file fails it, the result names the read that recovers the attempt."""
    @wraps(function)
    def run(store, *, source, id, **more):
        try:
            return function(store, source=source, id=id, **more)
        except LOCAL_FAILURES as exc:
            return local_state_result(exc, source, id)
    return run


@handles("reply_prepare")
@journal
def run_reply_prepare(store, *, source, id, body, replace_key):
    return replies.execute(store, 'prepare', source, id, body=body, replace_key=replace_key)


@handles("reply_begin")
@journal
def run_reply_begin(store, *, source, id, key):
    return replies.execute(store, 'begin', source, id, key=key)


@handles("reply_show")
@journal
def run_reply_show(store, *, source, id):
    return replies.execute(store, 'show', source, id)


@handles("reply_confirm")
@journal
def run_reply_confirm(store, *, source, id, key, ref, readback_body):
    return replies.execute(store, 'confirm', source, id, key=key, ref=ref, readback_body=readback_body)


@handles("reply_verify")
@journal
def run_reply_verify(store, *, sources, fetch, source, id, key, ref):
    return verification.execute(store, sources, source, id, key=key, ref=ref, fetch=fetch)


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
    """Original lookups for one command through a single client and budget.

    fetch asks the board: the transport, or an invented board in its place. The clients of ClawdChat and Botnet
    are handed it. Postingboard, Colony and Moltbook still take their client from client_factory."""

    def __init__(self, adapter, settings, client_factory=None, fetch=transport.fetch):
        self.adapter = adapter
        if adapter == "clawdchat":
            from . import adapter_clawdchat
            client = client_factory(adapter, settings) if client_factory else adapter_clawdchat.Client(settings, fetch=fetch)
            self.lookup = adapter_clawdchat.lookup
        elif adapter == "botnet":
            from . import adapter_botnet
            client = client_factory(adapter, settings) if client_factory else adapter_botnet.Client(settings, fetch=fetch)
            self.lookup = adapter_botnet.lookup
        else:
            client = (client_factory or providers.Client)(adapter, settings)
            self.lookup = {"postingboard": providers.postingboard_lookup, "the-colony": providers.colony_lookup,
                           "moltbook": providers.moltbook_lookup}[adapter]
        self.client = CachedClient(client)
        self.originals = {}

    def __call__(self, mid, root=None):
        if self.adapter == "moltbook":
            return self.lookup(self.client, mid, root, originals=self.originals)
        return self.lookup(self.client, mid, root)


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
    if root is not None:
        # Relatives must belong to the requested thread. A current original can
        # replace a conflicting local relative; the primary target keeps its snapshot.
        relations = remote[2] or stored
        if relations is not None and relations["thread_id"] != root:
            found = element("unavailable", error="invalid_response", id=mid)
            if lookup is not None:
                found["remote_status"] = remote[0]
            return found, None, True
        if stored is not None and stored["thread_id"] != root:
            stored = None
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
    if parent_id is None and adapter == "botnet":
        return element("none")  # A topic groups multiple independent message trees.
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


def context(store, source, message_id, settings, *, client_factory=None, fetch=transport.fetch):
    """Thread root, immediate parent and target. Reads local rows first, then
    supported originals when configured. Nothing is marked, locally or remotely."""
    lookup = None
    adapter = settings.get("adapter", source) if settings is not None else store.adapter(source)
    if settings is not None:
        kind = config.uuid
        if adapter == "botnet":
            from .adapter_botnet import message_id as kind
        config.converted(kind, message_id, error="invalid_message_id")
        lookup = Lookup(adapter, settings, client_factory, fetch)
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


def expand(store, source, thread, after, through, limit, settings, *, client_factory=None, fetch=transport.fetch):
    """Every saved message of one thread within (after, through], each with the
    context a singular lookup would give, through one client and one budget.

    The common root is returned once; a parent that is the root becomes a
    same_as_root reference after its availability was counted. Marks stay
    unchanged; an empty page fetches nothing. Context can lie outside the interval."""
    adapter = settings.get("adapter", source) if settings is not None else store.adapter(source)
    if settings is not None:
        config.converted(config.uuid, thread, error="invalid_arguments")
    page = store.page(after, limit, through=through, source=source, thread=thread, scope="all", context="none")
    rows = page["messages"]
    lookup = Lookup(adapter, settings, client_factory, fetch) if settings is not None and rows else None
    resolver = lambda mid, root=None: resolve(store, source, adapter, lookup, mid, root)
    root = resolver(thread, thread) if rows else (element("unknown", id=thread), None, True)
    items, complete, exhausted = [], current(root[0]) if rows else True, False
    for row in rows:
        mid = row["id"]
        # The thread is the trusted relationship: an original that now belongs elsewhere
        # is rejected by the lookup instead of attaching another thread's root here.
        target, relations, authoritative = root if mid == thread else resolver(mid, thread)
        if relations is None or relations["thread_id"] != thread:
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
        ref = config.converted(providers.parent_reference, adapter, relations["thread_id"], parent["id"],
                               otherwise=NO_REFERENCE)
        if ref is NO_REFERENCE:
            result["reason"] = "parent_invalid"
        elif ref is None:
            result["reason"] = "unsupported_source"
        else:
            result["reply_ref"] = ref
            result["messages"] = store.replied_with(source, ref, exclude_id=target["id"])
            result["status"] = "linked" if result["messages"] else "unmatched"
    return result
