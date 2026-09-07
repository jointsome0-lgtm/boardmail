"""JSON commands; waiting only reads SQLite and never calls a provider."""
import argparse
import json
import math
from pathlib import Path
import signal
import sqlite3
import threading

from . import config, providers
from .config import MailError
from .adapters import next_action
from .store import Store


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise MailError("invalid_arguments")


def parser():
    p = Parser(prog="boardmail")
    p.add_argument("--config",type=Path,default=Path.home()/".config/boardmail/config.json")
    p.add_argument("--db",type=Path,help="Database override; local reads need no config when supplied")
    sub = p.add_subparsers(dest="command",required=True)
    sub.add_parser("init",help="Create a new database; never overwrite")
    sub.add_parser("collect",help="Collect one retained public backlog pass")
    sub.add_parser("status",help="Local source health and counts")
    for command in ("list","wait"):
        s = sub.add_parser(command)
        s.add_argument("--after",type=int,default=0)
        s.add_argument("--limit",type=int,default=100)
        if command == "list":
            s.add_argument("--unread",action="store_true")
        else:
            s.add_argument("--timeout",type=float,default=1800)
    for command in ("show","mark"):
        s = sub.add_parser(command)
        if command == "mark":
            s.add_argument("action",choices=("read","unread","needs-reply","clear-reply","replied"))
            s.add_argument("--ref")
        s.add_argument("source",help="Source name returned in a message")
        s.add_argument("id")
    return p


def run(args):
    data = config.load(args.config) if args.command=="collect" or args.db is None else None
    store = Store(args.db or data["database"])
    if args.command in ("list","wait"):
        if not 0 <= args.after <= 2**63-1 or not 1 <= args.limit <= 500:
            raise MailError("invalid_arguments")
    if args.command == "init":
        store.initialize(data["sources"] if data else None)
        return {"event":"initialized",**store.status()},0
    if args.command == "collect":
        result = providers.collect_all(store,data["sources"])
        return result,1 if result["failed"] else 0
    if args.command == "status":
        return {"event":"status",**store.status()},0
    if args.command == "list":
        return {"event":"messages",**store.page(args.after,args.limit,unread=args.unread)},0
    if args.command == "wait":
        if not math.isfinite(args.timeout) or args.timeout < 0:
            raise MailError("invalid_arguments")
        cancelled = threading.Event()
        previous = {}
        try:
            for sig in (signal.SIGINT,signal.SIGTERM):
                previous[sig] = signal.signal(sig,lambda *_:cancelled.set())
            result = store.wait(args.after,args.timeout,args.limit,cancelled=cancelled)
        finally:
            for sig,handler in previous.items():
                signal.signal(sig,handler)
        return result,{"messages":0,"timeout":3,"cancelled":4}[result["event"]]
    try:
        message_id = config.identifier(args.id)
    except (ValueError,TypeError,AttributeError):
        raise MailError("invalid_message_id") from None
    if args.command == "mark":
        store.mark(args.source,message_id,args.action.replace("-","_"),ref=args.ref)
    return {"event":"marked" if args.command=="mark" else "message",
            "message":store.show(args.source,message_id)},0


def main(argv=None):
    try:
        result, code = run(parser().parse_args(argv))
    except MailError as exc:
        error = str(exc)
        result, code = {"event":"error","error":error},5 if error in ("database_missing","config_missing") else 2
    except (OSError,ValueError,sqlite3.Error,KeyError,TypeError,OverflowError):
        result, code = {"event":"error","error":"local_state_error"},2
    except KeyboardInterrupt:
        result, code = {"event":"cancelled"},4
    if result.get("event") == "error":
        result["next_action"] = next_action(result["error"])
    result.setdefault("history_complete", False)
    print(json.dumps(result,ensure_ascii=True))
    return code
