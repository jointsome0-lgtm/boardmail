"""Adapter interface v1: collect(settings, state, known) returns a Batch."""
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
import io
import importlib
import json
import re
import runpy
from urllib.parse import urlsplit

from . import transport
from .config import LEGACY_ADAPTERS, PACKAGED_ADAPTERS, MailError, identifier
from .errors import next_action


@dataclass
class Batch:
    """Public originals and resumable progress, committed together by the core.

    complete describes this adapter's scan, never complete remote history.
    Partial progress is normal; error is reserved for a failed operation.
    """
    messages: list = field(default_factory=list)
    state: dict = field(default_factory=dict)
    complete: bool = True
    error: str | None = None
    unavailable: int = 0
    originals: list = field(default_factory=list)


def validate(batch):
    if not isinstance(batch, Batch) or not isinstance(batch.messages, list) or not isinstance(batch.state, dict) or not isinstance(batch.originals, list):
        raise MailError("invalid_adapter_result")
    if type(batch.complete) is not bool or type(batch.unavailable) is not int or batch.unavailable < 0:
        raise MailError("invalid_adapter_result")
    if batch.error is not None and (not isinstance(batch.error, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", batch.error)):
        raise MailError("invalid_adapter_result")
    try:
        json.dumps(batch.state, allow_nan=False)
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        raise MailError("invalid_adapter_result") from None
    try:
        for item in batch.messages + batch.originals:
            for key in ("id", "thread_id"):
                identifier(item[key])
            if item.get("parent_id") is not None: identifier(item["parent_id"])
            if item.get("discovery") is not None and len(identifier(item["discovery"])) > 128: raise ValueError()
            for key in ("title", "body", "url"):
                if not isinstance(item[key], str): raise ValueError()
                item[key].encode("utf-8")
            if item.get("author") is not None:
                if not isinstance(item["author"], str): raise ValueError()
                item["author"].encode("utf-8")
            for key in ("created_at", "provider_seq"):
                if key == "provider_seq" and item.get(key) is None: continue
                if type(item[key]) is not int or not -(2**63) <= item[key] < 2**63: raise ValueError()
            url = urlsplit(item["url"])
            if url.scheme not in ("http", "https") or not url.hostname or url.username is not None or url.password is not None:
                raise ValueError()
            url.port  # Validate a supplied port as well as the host.
        for item in batch.messages:
            if item["kind"] not in ("mention", "reply_to_post", "reply_to_comment", "thread_activity"):
                raise ValueError()
            if item.get("addressing") not in (None, "direct", "mention", "direct+mention", "thread"):
                raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        raise MailError("invalid_adapter_result") from None


def collect_all(store, sources, *, client_factory=None, fetch=transport.fetch):
    """One pass over every source that is not paused.

    fetch asks a board: the transport, or an invented board in its place. The modules of ClawdChat, Botnet,
    Fruitflies and 4claw are handed it. Postingboard, Colony and Moltbook still take their client from
    client_factory, and an adapter file of an operator gets the three arguments of the interface."""
    # This is the only automatic migration point. Local readers never migrate.
    store.prepare_collection()
    added, errors = 0, []
    for source, settings in sources.items():
        if store.is_paused(source):
            continue
        adapter = str(settings.get("adapter", source))
        try:
            known, state, revision = store.collection_state(source, settings["account_id"], adapter)
            if adapter in LEGACY_ADAPTERS or adapter in PACKAGED_ADAPTERS:
                # Runtime selections are independent of the MCP operator's fixed config.
                # A pass keeps its snapshot; unsubscribe does not cancel in-flight work.
                settings = {**settings, "subscriptions": [item["thread"] for item in store.subscriptions(source)]}
            if adapter in LEGACY_ADAPTERS:
                from . import providers
                batch = providers.collect(adapter, settings, state, known, client_factory=client_factory)
            else:
                # Shipped modules and trusted configured files load only during collect.
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    try:
                        module = vars(importlib.import_module(PACKAGED_ADAPTERS[adapter])) if adapter in PACKAGED_ADAPTERS else runpy.run_path(adapter)
                    except (Exception, SystemExit):
                        raise MailError("adapter_load_failed") from None
                    if type(module.get("API_VERSION")) is not int or module["API_VERSION"] != 1:
                        raise MailError("adapter_version_unsupported")
                    try:
                        asks = {"fetch": fetch} if adapter in PACKAGED_ADAPTERS else {}
                        batch = module["collect"](settings, state, frozenset(known), **asks)
                    except (Exception, SystemExit):
                        raise MailError("adapter_failed") from None
            validate(batch)
            count, stale = store.save_collection(source, settings["account_id"], adapter, revision, batch)
            added += count
            error = "collection_conflict" if stale else batch.error
            if error:
                errors.append({"source": source, "error": error, "next_action": next_action(error)})
        except MailError as exc:
            error = str(exc)
            if error not in ("account_mismatch", "adapter_mismatch"):
                store.failure(source, settings["account_id"], error)
            errors.append({"source": source, "error": error, "next_action": next_action(error)})
    return {"event": "collected", "added": added, "failed": bool(errors), "errors": errors,
            "history_complete": False, **store.status()}
