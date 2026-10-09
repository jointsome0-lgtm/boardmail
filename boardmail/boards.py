"""The boards that ship with the package, each as its own module declares it.

A board is added here once. The core asks this list what a board is and compares no names for it. Any other
adapter of a source is a file of an operator, which declares nothing. collect_all() is one pass over both kinds.
"""
from contextlib import redirect_stderr, redirect_stdout
from functools import partial
import io
import runpy

from . import adapter_botnet, adapter_clawdchat, adapter_colony, adapter_moltbook, adapter_postingboard, transport
from .adapters import Board, validate
from .errors import MailError, next_action

BOARDS = {board.name: board for board in (adapter_postingboard.BOARD, adapter_colony.BOARD, adapter_moltbook.BOARD,
                                          adapter_clawdchat.BOARD, adapter_botnet.BOARD)}
# What stands for an adapter file where the core asks what a board declares.
FILE = Board(name="", coverage="Configured adapter scope; consult its instructions.", collect=None, subscriptions=False)


def owner(source, settings):
    """The adapter that owns a source: the one that its settings name, and with none the board of its own name.
    It is a name of BOARDS for a board of the package and the path of the file for any other."""
    return str(settings.get("adapter", source))


def declared(adapter):
    """What the board of this name declares, and FILE for any other adapter."""
    return BOARDS.get(adapter, FILE)


def from_file(path):
    """collect of a trusted adapter file on interface v1. The file runs here, and only a collection gets here."""
    try:
        module = runpy.run_path(path)
    except (Exception, SystemExit):
        raise MailError("adapter_load_failed") from None
    if type(module.get("API_VERSION")) is not int or module["API_VERSION"] != 1:
        raise MailError("adapter_version_unsupported")
    # A file with no collect fails where it is called, like any other fault of its code.
    return lambda settings, state, known: module["collect"](settings, state, known)


def collect_all(store, sources, *, fetch=transport.fetch):
    """One pass over every source that is not paused.

    fetch asks a board: the transport, or an invented board in its place. Every board of the package is handed
    it. An adapter file of an operator gets the three arguments of the interface."""
    added, errors = 0, []
    for source, settings in sources.items():
        if store.is_paused(source):
            continue
        adapter = owner(source, settings)
        try:
            known, state, revision = store.collection_state(source, settings["account_id"], adapter)
            board = BOARDS.get(adapter)
            if board:
                # Runtime selections are independent of the MCP operator's fixed config.
                # A pass keeps its snapshot; unsubscribe does not cancel in-flight work.
                settings = {**settings, "subscriptions": [item["thread"] for item in store.subscriptions(source)]}
            # A board of the package and a trusted configured file run the same way from here.
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                collect = partial(board.collect, fetch=fetch) if board else from_file(adapter)
                try:
                    batch = collect(settings, state, frozenset(known))
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
