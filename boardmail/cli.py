"""JSON commands; waiting only reads SQLite and never calls a provider."""
import argparse
import json
from pathlib import Path
import signal
import threading

from . import commands, config, replies
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
    s = sub.add_parser("settings", help="Read or save this inbox's reading preferences",
                       description="One consumer per database. Defaults: addressed scope, brief local context.",
                       epilog="Examples:\n  boardmail settings\n  boardmail settings --scope all --context none\n"
                              "  boardmail settings --reset\n"
                              "Preferences affect check/list/wait only. Their flags override a saved preference once.")
    s.add_argument("--scope", choices=("addressed", "all"), help="Default scope for check/list/wait")
    s.add_argument("--context", dest="context_mode", choices=("brief", "none"), help="Default local context for check/list/wait")
    s.add_argument("--reset", action="store_true", help="Restore defaults; cannot combine with other settings flags")
    for command, summary in (("subscribe", "Collect activity in a selected thread"),
                             ("unsubscribe", "Stop subscription collection for a selected thread")):
        s = sub.add_parser(command, help=summary,
                           description=summary + ". Local and idempotent; changes later collection passes.",
                           epilog=f"Example: boardmail {command} SOURCE THREAD\n"
                                  "Use a source with a built-in adapter and the root UUID from a message or board.\n"
                                  "The first collection can import older available replies within the provider's limits.\n"
                                  "Ordinary activity is summarized in addressed scope; unknown recipients remain visible.\n"
                                  "Unsubscribe preserves saved mail and marks; an in-flight source pass may finish.\n"
                                  "Source pauses still apply. Run collect/check separately; this command makes no requests.")
        s.add_argument("source", metavar="SOURCE", help="Source using a built-in adapter, from status or config")
        s.add_argument("thread", metavar="THREAD", help="Selected root UUID; not a message URL")
    s = sub.add_parser("subscriptions", help="List local thread subscriptions",
                       description="Read selected threads without collection, migration or marking mail.",
                       epilog="Examples:\n  boardmail subscriptions\n  boardmail subscriptions --source SOURCE\n"
                              "Subscriptions are shared by CLI and MCP clients of this database; changes need no MCP restart.")
    s.add_argument("--source", metavar="SOURCE", help="Show only this source's subscriptions")
    sub.add_parser('tags', help='List local topics and unread counts without message bodies',
                   description='Group selected threads across boards; always includes an untagged queue.',
                   epilog='Run collect separately, then tags. Copy a topic read action to read only its unread mail.\n'
                          'Tags may overlap; read marks are shared. No collection, marking or migration on this read.')
    s = sub.add_parser('tag', help='Group whole threads for local reading',
                       description='Tags group saved mail. Subscriptions independently control collection.',
                       epilog='Names: 1 to 64 lowercase letters, digits, underscores or hyphens; start with a letter or digit.\n'
                              'Examples: htalk, agent-memory. Tagging never subscribes, fetches or marks mail.')
    actions = s.add_subparsers(dest='tag_action', required=True)
    for action in ('add', 'remove'):
        a = actions.add_parser(action, help=action.capitalize() + ' a thread membership',
                               description='Local and idempotent. Select exactly one thread ID or saved message ID.',
                               epilog=f'Examples:\n  boardmail tag {action} htalk SOURCE THREAD\n'
                                      f'  boardmail tag {action} htalk SOURCE --message ID\n'
                                      'Use the exact local thread_id, including non-UUID custom-adapter IDs.\n'
                                      'The source must already belong to this inbox. The root need not be saved.\n'
                                      'Adding a tag includes older unread messages immediately, without collection.')
        a.add_argument('tag', metavar='TAG', help='Local topic name, e.g. htalk or agent-memory')
        a.add_argument('source', metavar='SOURCE', help='Source name in this inbox, from status')
        a.add_argument('thread', nargs='?', metavar='THREAD', help='Exact local thread_id; omit with --message')
        a.add_argument('--message', dest='id', metavar='ID', help='Use this saved message\'s local thread_id')
    a = actions.add_parser('show', help='Show the saved threads belonging to a tag',
                           description='Local titles, known links, unread counts and subscription state; no message bodies.',
                           epilog='Example: boardmail tag show agent-memory\n'
                                  'Includes threads with no saved messages. Missing labels and links stay null.\n'
                                  'Local subscription state does not guarantee collection or complete history.')
    a.add_argument('tag', metavar='TAG', help='Local topic name')
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
                                  "Handle messages and thread_activity, mark explicitly, then save next_after.\n"
                                  "Use list to drain more pages. An empty page or timeout does not prove\n"
                                  "there is no remote mail. Put --db PATH before the command.")
        s.add_argument("--after", type=int, default=0, metavar="N",
                       help="Last processed arrival_seq checkpoint, starting at 0; default %(default)s")
        s.add_argument("--limit", type=int, default=100, metavar="N",
                       help="Arrivals scanned per page, before scope filtering; 1 to 500, default %(default)s")
        s.add_argument("--scope", choices=("addressed", "all"),
                       help="Override saved scope once; addressed summarizes only proven thread activity")
        s.add_argument("--context", dest="context_mode", choices=("brief", "none"),
                       help="Override saved context once; brief uses bounded local excerpts, never fetches")
        if command == "list":
            s.add_argument("--unread", action="store_true", help="Only messages without a local read mark")
            s.add_argument("--through", type=int, metavar="N", help="Inclusive arrival_seq upper bound for replay")
            s.add_argument("--source", metavar="SOURCE", help="Read only this source")
            s.add_argument("--thread", metavar="ID", help="Read only this thread; pair with --source")
            selection = s.add_mutually_exclusive_group()
            selection.add_argument('--tag', metavar='TAG', help='Read messages in threads with this local tag')
            selection.add_argument('--untagged', action='store_true', help='Read messages in threads with no local tags')
            s.epilog += ("\nWith --unread, --source, --thread, --through, --tag or --untagged, checkpoint_safe is false.\n"
                         "Keep your delivery checkpoint; paginate this view with the same filters and its next_after.\n"
                         "Start each new topic visit at 0, so late tags include older unread messages:\n"
                         "  boardmail list --tag htalk --unread --scope all --after 0\n"
                         "  boardmail list --untagged --unread --scope all --after 0")
        elif command == "wait":
            s.add_argument("--timeout", type=float, default=1800, metavar="SECONDS",
                           help="Nonnegative, finite seconds; 0 checks once, default %(default)s. Run collection separately")
            s.epilog += "\nRun collect or check separately; wait only watches the local database."
    for command, summary in (("show", "Read one saved message, its marks and reply attempt state"),
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
                        "through the board; it records the URL locally and does not publish anything.\n"
                        "The result includes reply_attempt and its reply show route.\n"
                        "A replied mark does not resolve an unknown attempt.")
        s.add_argument("source", metavar="SOURCE", help="Source name returned in a message")
        s.add_argument("id", metavar="ID", help="Exact message ID from a Boardmail result")
        if command == "show":
            s.epilog += ("\nreply_attempt is null when no attempt was saved; otherwise it gives state, next_action\n"
                         "and arguments for reply show. A replied mark does not resolve an unknown attempt.")
        if command == "context":
            s.add_argument("--local", action="store_true", help="Use only stored records; no remote lookup")
            s.epilog += ("\nConfigured active Postingboard/Colony sources can fetch current originals.\n"
                         "Use --local for stored context only. With --db alone, context stays local;\n"
                         "add --config before context to enable remote reads.\n"
                         "Exit 1 with complete: false means incomplete context; inspect target, parent and root.")
    s = sub.add_parser("expand", help="Read every saved message of one thread interval with current context",
                       description="Expand one bounded interval of a saved thread: each selected message with its "
                                   "target, parent and previous exchange, plus the common root once. Marks nothing.",
                       epilog="Example: boardmail expand SOURCE THREAD --through 120 --after 100\n"
                              "Copy source, thread and bounds from a thread_activity summary; through is inclusive.\n"
                              "Later arrivals and mark changes never enter the interval; checkpoint_safe is false,\n"
                              "so keep your delivery checkpoint. Retry incomplete pages with the same bounds;\n"
                              "continue with --after next_after and the same --through while more is true.\n"
                              "One remote budget covers the whole page; repeated originals are read once.\n"
                              "A parent equal to the root is returned as {id, status: same_as_root}.\n"
                              "Exit 1 with complete: false means some current original is not confirmed;\n"
                              "saved text stays in each target. Configured Postingboard/Colony/Moltbook/ClawdChat\n"
                              "sources fetch current originals unless --local is given or the source is paused.")
    s.add_argument("source", metavar="SOURCE", help="Source name returned in a message")
    s.add_argument("thread", metavar="THREAD", help="Exact thread_id from a Boardmail result")
    s.add_argument("--through", type=int, required=True, metavar="N", help="Inclusive arrival_seq upper bound")
    s.add_argument("--after", type=int, default=0, metavar="N",
                   help="Exclusive arrival_seq lower bound; default %(default)s")
    s.add_argument("--limit", type=int, default=commands.EXPAND_LIMIT, metavar="N",
                   help="Saved messages per page; 1 to 100, default %(default)s")
    s.add_argument("--local", action="store_true", help="Use only stored records; no remote lookup")
    s = sub.add_parser('reply', help='Save and recover a reply attempt without publishing',
                       description='One durable reply per incoming message. Publish externally; verify a known reply URL or confirm your own readback.',
                       epilog='Prepare exact text, then begin BEFORE the external POST. After any interruption, show the saved attempt.\n'
                              'Read back an unknown outcome. An empty search does not authorize another send.\n'
                              'Idempotent replay needs the same key/body and provider guarantees still valid at retry time, including key retention.\n'
                              'Verify reads the provider and records matching evidence. Confirm records your own readback without a remote request.')
    actions = s.add_subparsers(dest='reply_action', required=True)
    for action, summary in (('prepare', 'Save exact reply text and a stable idempotency key'),
                            ('begin', 'Record an unknown outcome before the external POST'),
                            ('show', 'Recover the saved reply and independent incoming marks'),
                            ('confirm', 'Record caller readback matching the saved reply text'),
                            ('verify', 'Read a known reply from its provider and confirm only matching evidence')):
        a = actions.add_parser(action, help=summary, description=summary + '.',
                               epilog='An empty board lookup does not prove the reply was never published.\n'
                                      'Boardmail does not publish or retry. Only verify performs remote reads.')
        a.add_argument('source', metavar='SOURCE', help='Source from the saved incoming message')
        a.add_argument('id', metavar='ID', help='Exact incoming message ID')
        if action == 'prepare':
            a.add_argument('--body-file', type=Path, required=True, metavar='PATH',
                           help='Nonempty UTF-8 reply, at most 65536 bytes; preserves every newline')
            a.add_argument('--replace-key', metavar='KEY',
                           help='Explicitly replace this still-prepared draft; rejected after begin')
            a.epilog += '\nExample: boardmail reply prepare SOURCE ID --body-file reply.txt\nSame text returns the existing key and state.'
        if action in ('begin', 'confirm', 'verify'):
            a.add_argument('--key', required=True, metavar='KEY', help='Exact saved idempotency_key; stale keys are rejected')
        if action == 'begin':
            a.epilog += ('\nExample: boardmail reply begin SOURCE ID --key KEY\n'
                         'Only the first successful begin returns send_allowed: true. Repeated begin requires reconciliation.')
        if action in ('begin', 'show'):
            a.epilog += ('\nIdempotent replay requires provider guarantees still valid for this operation and key at retry time.\n'
                         'An expired or unknown key-retention period cannot authorize replay; keep unresolved outcomes unknown.')
        if action == 'confirm':
            a.add_argument('--ref', required=True, metavar='URL', help='Published reply URL independently checked by the caller')
            a.add_argument('--readback-file', type=Path, required=True, metavar='PATH',
                           help='Exact UTF-8 body read from the published reply, not your draft file')
            a.epilog += ('\nExample: boardmail reply confirm SOURCE ID --key KEY --ref https://example.org/reply --readback-file readback.txt\n'
                         'Check the account, thread, reply target and provider status yourself. Matching text alone cannot establish those.\n'
                         'Atomically records the caller receipt and replied mark; leaves read and needs-reply unchanged.')
        if action == 'verify':
            a.add_argument('--ref', required=True, metavar='URL', help='Known reply URL on the configured provider, including its exact reply ID')
            a.epilog += ('\nExample: boardmail --config config.json reply verify SOURCE ID --key KEY --ref URL\n'
                         'Checks author ID, thread, immediate target, exact body and provider status. Requires config; respects pauses.\n'
                         'Supports Postingboard, The Colony, Moltbook and ClawdChat. Reads fixed API endpoints, never an arbitrary URL.\n'
                         'A missing URL needs independent discovery. Unavailable, incomplete or mismatching evidence never authorizes sending.')
    return p


def run(args):
    # Local reads need no config when --db is supplied; an explicit config still
    # enables remote context lookups unless --local is given.
    needed = args.command in ("collect", "check") or (args.command == 'reply' and args.reply_action == 'verify') or args.db is None or (
        args.config is not None and (args.command in ("pause", "resume", "subscribe", "unsubscribe") or
                                    args.command in ("context", "expand") and not args.local))
    data = config.load(args.config or Path.home()/".config/boardmail/config.json") if needed else None
    store = Store(args.db or data["database"])
    options = {key: value for key, value in vars(args).items() if key not in ("config", "db", "command")}
    command = args.command
    if command == 'tag':
        command = 'tag_' + options.pop('tag_action')
    if command == 'reply':
        command = 'reply_' + options.pop('reply_action')
        for file_key, body_key in (('body_file', 'body'), ('readback_file', 'readback_body')):
            if file_key in options:
                options[body_key] = replies.read_body(options.pop(file_key))
    def invoke():
        return commands.execute(store, command, sources=data["sources"] if data else None, **options)
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
