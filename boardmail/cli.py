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
    p = Parser(
        prog="boardmail",
        description="Collect board replies and mentions into a local inbox. Commands return JSON.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="After configuring an account:\n"
               "  boardmail init                         # new database only\n"
               "  boardmail check --after 0 --limit 50    # collect and read\n\n"
               "Use boardmail COMMAND --help for arguments and examples.\n"
               "Setup: https://github.com/jointsome0-lgtm/boardmail#install-and-configure",
    )
    p.add_argument("--config", type=Path, metavar="PATH",
                   help="Config JSON; default ~/.config/boardmail/config.json")
    p.add_argument("--db", type=Path, metavar="PATH",
                   help="Override the configured SQLite file; local reads then need no config")
    sub = p.add_subparsers(dest="command",required=True)
    sub.add_parser("init", help="Create a new inbox database",
                   description="Create a new database. Never overwrites an existing file; not for upgrades.")
    sub.add_parser("collect", help="Fetch one pass of remote mail",
                   description="Collect from configured sources. Partial failure can still save messages.",
                   epilog="Read the returned source errors before collecting again. Waiting never collects mail.")
    for command, summary in (("pause", "Stop collection and remote context for one source"),
                             ("resume", "Enable a source for the next collection")):
        s = sub.add_parser(command, help=summary, description=summary + ". Keeps messages and progress.")
        s.add_argument("source", metavar="SOURCE", help="Source name from status or config")
    s = sub.add_parser("status", help="Show local counts and source health",
                       description="Read collection health without contacting a board.")
    s.add_argument("--require-fresh", action="store_true",
                   help="Exit 1 if an active source is unknown, errored or stale; exclude paused sources")
    s.add_argument("--stale-after", type=int, metavar="SECONDS",
                   help="Age after which collection is stale; nonnegative seconds, default 540")
    for command, summary in (("check", "Collect once, then read a local arrival page"),
                             ("list", "Read a page of saved messages"),
                             ("wait", "Wait for new local arrivals; never fetch remote mail")):
        s = sub.add_parser(command, help=summary, description=summary + ".",
                           epilog="Process messages before saving next_after. Use list to drain more pages."
                                  " An empty page or timeout does not prove there is no remote mail.")
        s.add_argument("--after", type=int, default=0, metavar="N",
                       help="Last processed arrival_seq checkpoint, starting at 0; default %(default)s")
        s.add_argument("--limit", type=int, default=100, metavar="N",
                       help="Messages per page, 1 to 500; default %(default)s")
        if command == "list":
            s.add_argument("--unread", action="store_true", help="Only messages without a local read mark")
        elif command == "wait":
            s.add_argument("--timeout", type=float, default=1800, metavar="SECONDS",
                           help="Nonnegative, finite seconds; 0 checks once, default %(default)s. Run collection separately")
    for command, summary in (("show", "Read one saved message and its marks"),
                             ("mark", "Change a local read/reply mark"),
                             ("context", "Read the target, parent and root; mark nothing")):
        s = sub.add_parser(command, help=summary, description=summary + ".")
        if command == "mark":
            s.add_argument("action", choices=("read","unread","needs-reply","clear-reply","replied"),
                           help="read/unread set/clear reading; needs-reply/clear-reply set/clear the reply obligation;"
                                " replied records a published reply without changing other marks")
            s.add_argument("--ref", metavar="URL", help="Published HTTP(S) reply URL; required only for replied")
            s.epilog = "Example: boardmail mark replied SOURCE ID --ref https://example.org/your-reply"
        s.add_argument("source", metavar="SOURCE", help="Source name returned in a message")
        s.add_argument("id", metavar="ID", help="Exact message ID from a Boardmail result")
        if command == "context":
            s.add_argument("--local", action="store_true", help="Use only stored records; no remote lookup")
            s.epilog = "Configured active Postingboard/Colony sources can fetch current originals."
            s.epilog += " With --db alone, context stays local; add --config to enable remote reads."
    return p


def run(args):
    # Local reads need no config when --db is supplied; an explicit config still
    # enables remote context lookups unless --local is given.
    needed = args.command in ("collect", "check") or args.db is None or (
        args.config is not None and (args.command in ("pause", "resume") or
                                    args.command == "context" and not args.local))
    data = config.load(args.config or Path.home()/".config/boardmail/config.json") if needed else None
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
