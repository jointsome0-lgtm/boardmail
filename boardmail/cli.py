"""JSON commands; waiting only reads SQLite and never calls a provider."""
import argparse
import json
from pathlib import Path
import signal
import threading

from . import commands, config
from .config import MailError
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
    for command in ("check","list","wait"):
        s = sub.add_parser(command)
        s.add_argument("--after",type=int,default=0)
        s.add_argument("--limit",type=int,default=100)
        if command == "list":
            s.add_argument("--unread",action="store_true")
        elif command == "wait":
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
    data = config.load(args.config) if args.command in ("collect", "check") or args.db is None else None
    store = Store(args.db or data["database"])
    options = {key: value for key, value in vars(args).items() if key not in ("config", "db", "command")}
    def invoke():
        return commands.execute(store, args.command, sources=data["sources"] if data else None, **options)
    if args.command == "wait":
        cancelled = threading.Event()
        options["cancelled"] = cancelled
        previous = {}
        try:
            for sig in (signal.SIGINT,signal.SIGTERM):
                previous[sig] = signal.signal(sig,lambda *_:cancelled.set())
            return invoke()
        finally:
            for sig,handler in previous.items():
                signal.signal(sig,handler)
    return invoke()


def main(argv=None):
    result, code = commands.outcome(lambda: run(parser().parse_args(argv)))
    print(json.dumps(result,ensure_ascii=True))
    return code
