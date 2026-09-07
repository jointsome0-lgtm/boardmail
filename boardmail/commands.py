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


def execute(store, command, *, sources=None, after=0, limit=100, unread=False,
            timeout=1800, source=None, id=None, action=None, ref=None, cancelled=None):
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
        return {"event": "status", **store.status()}, 0
    if command == "list":
        return {"event": "messages", **store.page(after, limit, unread=unread), "collection_performed": False}, 0
    if command == "wait":
        if not math.isfinite(timeout) or timeout < 0:
            raise MailError("invalid_arguments")
        result = store.wait(after, timeout, limit, cancelled=cancelled)
        result["collection_performed"] = False
        return result, {"messages": 0, "timeout": 3, "cancelled": 4}[result["event"]]
    if command not in ("mark", "show"):
        raise MailError("invalid_arguments")
    try:
        message_id = config.identifier(id)
    except (ValueError, TypeError, AttributeError):
        raise MailError("invalid_message_id") from None
    if command == "mark":
        store.mark(source, message_id, action.replace("-", "_"), ref=ref)
    return {"event": "marked" if command == "mark" else "message",
            "message": store.show(source, message_id)}, 0
