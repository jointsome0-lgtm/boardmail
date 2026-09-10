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
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("formatter_class", argparse.RawDescriptionHelpFormatter)
        super().__init__(*args, **kwargs)

    def error(self, message):
        raise MailError("invalid_arguments")


def parser():
    p = Parser(
        prog="boardmail",
        description="Collect board replies and mentions into a local inbox. Commands return JSON.",
        epilog="After configuring an account:\n"
               "  boardmail init                         # new database only\n"
               "  boardmail check --after 0 --limit 50    # collect and read\n\n"
               "For each returned message, use its source and exact id:\n"
               "  boardmail show SOURCE ID\n"
               "  boardmail context SOURCE ID\n"
               "  boardmail mark read SOURCE ID\n"
               "After processing the page, save next_after and continue with list --after N.\n"
               "Publishing a reply happens through the board; mark replied records its URL.\n\n"
               "Put --config PATH and --db PATH before the command.\n"
               "Use boardmail COMMAND --help for arguments and examples.\n"
               "Exit 0: success; 1: partial collection, incomplete context or failed health check;\n"
               "2: invalid input or operation error; 3: wait timeout; 4: cancelled;\n"
               "5: missing config or database. Read the JSON result for details;\n"
               "errors may include error and next_action.\n"
               "Setup: https://github.com/jointsome0-lgtm/boardmail#install-and-configure",
    )
    p.add_argument("--config", type=Path, metavar="PATH",
                   help="Config JSON; default ~/.config/boardmail/config.json")
    p.add_argument("--db", type=Path, metavar="PATH",
                   help="Override the configured SQLite file; local reads then need no config")
    sub = p.add_subparsers(dest="command",required=True)
    sub.add_parser("init", help="Create a new inbox database",
                   description="Create a new database. Never overwrites an existing file; not for upgrades.",
                   epilog="Example: boardmail --config /path/config.json init\n"
                          "Configure the account first. An existing inbox is ready for check; do not init it again.\n"
                          "Setup: https://github.com/jointsome0-lgtm/boardmail#install-and-configure")
    sub.add_parser("collect", help="Fetch one pass of remote mail",
                   description="Collect from configured sources. Partial failure can still save messages.",
                   epilog="Example: boardmail collect\n"
                          "Inspect added, failed and errors. Partial failure can still save mail.\n"
                          "Use list to read saved messages, or check to combine collection and reading.")
    for command, summary in (("pause", "Stop collection and remote context for one source"),
                             ("resume", "Enable a source for the next collection")):
        s = sub.add_parser(command, help=summary, description=summary + ". Keeps messages and progress.",
                           epilog=f"Example: boardmail {command} SOURCE\n"
                                  "Use a source name from status. This command does not collect mail.")
        s.add_argument("source", metavar="SOURCE", help="Source name from status or config")
    s = sub.add_parser("status", help="Show local counts and source health",
                       description="Read collection health without contacting a board.",
                       epilog="Examples:\n"
                              "  boardmail status\n"
                              "  boardmail status --require-fresh --stale-after 540\n"
                              "A health read exits 0; --require-fresh makes unhealthy active sources exit 1.")
    s.add_argument("--require-fresh", action="store_true",
                   help="Exit 1 if an active source is unknown, errored or stale; exclude paused sources")
    s.add_argument("--stale-after", type=int, metavar="SECONDS",
                   help="Age after which collection is stale; nonnegative seconds, default 540")
    for command, summary in (("check", "Collect once, then read a local arrival page"),
                             ("list", "Read a page of saved messages"),
                             ("wait", "Wait for new local arrivals; never fetch remote mail")):
        s = sub.add_parser(command, help=summary, description=summary + ".",
                           epilog=f"Example: boardmail {command} --after 0 --limit 50\n"
                                  "Replace 0 with your saved next_after after processing a page.\n"
                                  "Read messages, mark them explicitly, then save next_after.\n"
                                  "Use list to drain more pages. An empty page or timeout does not prove\n"
                                  "there is no remote mail. Put --db PATH before the command.")
        s.add_argument("--after", type=int, default=0, metavar="N",
                       help="Last processed arrival_seq checkpoint, starting at 0; default %(default)s")
        s.add_argument("--limit", type=int, default=100, metavar="N",
                       help="Messages per page, 1 to 500; default %(default)s")
        if command == "list":
            s.add_argument("--unread", action="store_true", help="Only messages without a local read mark")
        elif command == "wait":
            s.add_argument("--timeout", type=float, default=1800, metavar="SECONDS",
                           help="Nonnegative, finite seconds; 0 checks once, default %(default)s. Run collection separately")
            s.epilog += "\nRun collect or check separately; wait only watches the local database."
    for command, summary in (("show", "Read one saved message and its marks"),
                             ("mark", "Change a local read/reply mark"),
                             ("context", "Read the target, parent and root; mark nothing")):
        s = sub.add_parser(command, help=summary, description=summary + ".",
                           epilog=f"Example: boardmail {command} SOURCE ID\n"
                                  "Copy source and id from a check/list result. Reading does not mark mail read.")
        if command == "mark":
            s.add_argument("action", choices=("read","unread","needs-reply","clear-reply","replied"),
                           help="read/unread set/clear reading; needs-reply/clear-reply set/clear the reply obligation;"
                                " replied records a published reply without changing other marks")
            s.add_argument("--ref", metavar="URL", help="Published HTTP(S) reply URL; required only for replied")
            s.epilog = ("Examples:\n"
                        "  boardmail mark read SOURCE ID\n"
                        "  boardmail mark needs-reply SOURCE ID\n"
                        "  boardmail mark replied SOURCE ID --ref https://example.org/your-reply\n"
                        "Copy source and id from a check/list result. Mark replied only after publishing\n"
                        "through the board; it records the URL locally and does not publish anything.")
        s.add_argument("source", metavar="SOURCE", help="Source name returned in a message")
        s.add_argument("id", metavar="ID", help="Exact message ID from a Boardmail result")
        if command == "context":
            s.add_argument("--local", action="store_true", help="Use only stored records; no remote lookup")
            s.epilog += ("\nConfigured active Postingboard/Colony sources can fetch current originals.\n"
                         "Use --local for stored context only. With --db alone, context stays local;\n"
                         "add --config before context to enable remote reads.\n"
                         "Exit 1 with complete: false means incomplete context; inspect target, parent and root.")
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
