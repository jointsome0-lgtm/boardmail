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
        settings = None if local or not sources or store.is_paused(source) else sources.get(source)
        return context(store, source, message_id, settings if settings and settings.get("adapter", source) == "postingboard" else None)
    if command == "mark":
        store.mark(source, message_id, action.replace("-", "_"), ref=ref)
    return {"event": "marked" if command == "mark" else "message",
            "message": store.show(source, message_id)}, 0


def element(status, message=None, *, origin=None, error=None, id=None):
    return {"id": message["id"] if message else id, "status": status, "origin": origin, "error": error,
            "message": message, "current_message": None, "differs_from_saved": None}


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
    adapter = "postingboard" if lookup is not None else store.adapter(source)

    def resolve(mid, root=None):
        """Element plus the relationships to trust: a fetched original outranks a stored row."""
        stored = store.find(source, mid)
        remote = lookup(mid, root) if lookup is not None else ("unknown", None, None)
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

    target, relations, authoritative = resolve(message_id)
    if relations is None:
        root = parent = element("unknown")
    else:
        root_id = relations["thread_id"]
        root = target if root_id == message_id else resolve(root_id, root_id)[0]
        parent_id = relations["parent_id"]
        if parent_id is None and root_id != message_id:
            # A reply attaches to the root unless the board recorded a reply target.
            # Rows stored before reply targets were kept cannot say which; only a fetch can.
            recorded = authoritative or adapter != "postingboard" or relations.get("discovery") is not None
            parent_id = root_id if recorded else None
            if not recorded:
                parent = element("unknown")
        if parent_id is None and root_id == message_id:
            parent = element("none")
        elif parent_id == root_id:
            parent = root
        elif parent_id is not None:
            parent = resolve(parent_id, root_id)[0]
            if parent["message"] is not None and parent["message"]["thread_id"] != root_id:
                parent = element("unavailable", error="invalid_response", id=parent_id)
    complete = target["status"] == "available" and root["status"] == "available" and parent["status"] in ("available", "none")
    return {"event": "context", "source": source, "id": message_id, "fetched": lookup is not None,
            "target": target, "parent": parent, "root": root, "complete": complete}, 0 if complete else 1
