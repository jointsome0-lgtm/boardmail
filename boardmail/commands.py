"""Shared CLI/MCP commands and safe, transport-independent results."""
import math
import sqlite3

from . import config, providers
from .adapters import next_action
from .config import MailError


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


def execute(store, command, *, sources=None, after=0, limit=100, unread=False, timeout=1800, source=None,
            id=None, action=None, ref=None, cancelled=None, require_fresh=False, stale_after=None, local=False):
    if command in ("check", "list", "wait"):
        if type(after) is not int or not 0 <= after <= 2**63-1 or type(limit) is not int or not 1 <= limit <= 500:
            raise MailError("invalid_arguments")
    if command == "init":
        store.initialize(sources)
        return {"event": "initialized", **store.status()}, 0
    if command in ("collect", "check"):
        if sources is None:
            raise MailError("config_missing")
        result = providers.collect_all(store, sources)
        if command == "check":
            return {"event": "messages", **store.page(after, limit), "collection_performed": True,
                    "collection": {key: result[key] for key in ("added", "failed", "errors")}}, 1 if result["failed"] else 0
        return result, 1 if result["failed"] else 0
    if command == "status":
        if stale_after is not None and (type(stale_after) is not int or not 0 <= stale_after <= 2**31-1):
            raise MailError("invalid_arguments")
        result = {"event": "status", **store.status(stale_after), "freshness_required": bool(require_fresh)}
        # Reading unknown, error or stale state is itself a success unless freshness was required.
        return result, 1 if require_fresh and not result["fresh"] else 0
    if command == "list":
        return {"event": "messages", **store.page(after, limit, unread=unread), "collection_performed": False}, 0
    if command == "wait":
        if not math.isfinite(timeout) or timeout < 0:
            raise MailError("invalid_arguments")
        result = store.wait(after, timeout, limit, cancelled=cancelled)
        result["collection_performed"] = False
        return result, {"messages": 0, "timeout": 3, "cancelled": 4}[result["event"]]
    if command not in ("mark", "show", "context"):
        raise MailError("invalid_arguments")
    try:
        message_id = config.identifier(id)
    except (ValueError, TypeError, AttributeError):
        raise MailError("invalid_message_id") from None
    if command == "context":
        settings = None if local or not sources else sources.get(source)
        return context(store, source, message_id, settings if settings and settings.get("adapter", source) == "postingboard" else None)
    if command == "mark":
        store.mark(source, message_id, action.replace("-", "_"), ref=ref)
    return {"event": "marked" if command == "mark" else "message",
            "message": store.show(source, message_id)}, 0


def element(status, message=None, *, origin=None, error=None, id=None):
    return {"id": message["id"] if message else id, "status": status, "origin": origin, "error": error, "message": message}


def context(store, source, message_id, settings, *, client_factory=None):
    """Thread root, immediate parent and target. Reads local rows first, then
    Postingboard originals when configured. Nothing is marked, locally or remotely."""
    lookup = None
    if settings is not None:
        try:
            config.uuid(message_id)
        except (ValueError, TypeError, AttributeError):
            raise MailError("invalid_message_id") from None
        client = (client_factory or providers.Client)("postingboard", settings)
        lookup = lambda mid, root=None: providers.postingboard_lookup(client, mid, root)

    def resolve(mid, root=None):
        stored = store.find(source, mid)
        if stored is not None:
            found = element("available", stored, origin="local")
            if lookup is not None:
                found["remote_status"], found["error"], _ = lookup(mid, root)
            return found
        if lookup is None:
            return element("unknown", id=mid)
        status, error, message = lookup(mid, root)
        if message is not None:
            message = {"source": source, **message}
        found = element(status, message, origin="remote" if message else None, error=error, id=mid)
        found["remote_status"] = status
        return found

    target = resolve(message_id)
    relations = target["message"]
    if relations is None:
        root = parent = element("unknown")
    else:
        root_id = relations["thread_id"]
        # Replies attach to the root unless an explicit reply target is known.
        parent_id = relations["parent_id"] or (root_id if root_id != message_id else None)
        root = target if root_id == message_id else resolve(root_id, root_id)
        if parent_id is None:
            parent = element("none")
        elif parent_id == root_id:
            parent = root
        else:
            parent = resolve(parent_id, root_id)
            if parent["message"] is not None and parent["message"]["thread_id"] != root_id:
                parent = element("unavailable", error="invalid_response", id=parent_id)
    complete = target["status"] == "available" and root["status"] == "available" and parent["status"] in ("available", "none")
    return {"event": "context", "source": source, "id": message_id, "fetched": lookup is not None,
            "target": target, "parent": parent, "root": root, "complete": complete}, 0 if complete else 1
