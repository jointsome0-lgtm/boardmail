"""JSON commands; waiting only reads SQLite and never calls a provider."""
import argparse
import json
from pathlib import Path
import signal
import threading

from . import commands, config, replies, table
from .config import MailError
from .store import Store


class Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("formatter_class", argparse.RawDescriptionHelpFormatter)
        super().__init__(*args, **kwargs)

    def error(self, message):
        raise MailError("invalid_arguments")


def add(holder, argument):
    """Give a parser, or a group of its options, one argument of the command table."""
    kind, positional = argument.kind, not argument.typed.startswith('-')
    flag, _, word = ('', '', argument.typed) if positional else argument.typed.partition(' ')
    given = {'help': argument.help}
    if kind['type'] == 'boolean':
        given['action'] = 'store_true'
    else:
        number = {'integer': int, 'number': float}.get(kind['type'])
        # A help page shows the choices where there are some, and the word for the value where there are none.
        given.update(type=Path if argument.file else number, choices=kind.get('enum'), default=kind.get('default'),
                     metavar=None if 'enum' in kind else word or None)
    if positional:
        holder.add_argument(argument.name, nargs=None if argument.required else '?', **given)
    else:
        holder.add_argument(flag, dest=argument.name, required=argument.required, **given)


def parser():
    p = Parser(prog="boardmail", description=table.FIRST_PAGE.description, epilog=table.FIRST_PAGE.epilog)
    for argument in table.BEFORE:
        add(p, argument)
    sub = p.add_subparsers(dest="command",required=True)
    groups = {}
    for command in table.COMMANDS.values():
        holder, word = sub, command.name
        group = word.partition('_')[0]
        if group in table.GROUPS:
            if group not in groups:
                page = table.GROUPS[group]
                s = sub.add_parser(group, help=page.summary, description=page.description, epilog=page.epilog)
                groups[group] = s.add_subparsers(dest=group + '_action', required=True)
            holder, word = groups[group], word.removeprefix(group + '_')
        s = holder.add_parser(word, help=command.summary, epilog=command.epilog,
                              description=command.description or command.summary + '.')
        either = {}
        for rule, *names in command.rules:
            if rule == table.NOT_BOTH:
                either.update(dict.fromkeys(names, s.add_mutually_exclusive_group()))
        for argument in command.arguments:
            add(either.get(argument.name, s), argument)
    return p


def run(args):
    name = args.command
    if name in table.GROUPS:
        name += '_' + getattr(args, name + '_action')
    command = table.COMMANDS[name]
    # Local reads need no config when --db is supplied; an explicit config still
    # enables remote context lookups unless --local is given.
    # Explicit init config seeds source identities even with a --db override.
    needed = args.db is None or command.sources == table.NEEDED or (
        command.sources == table.GIVEN and args.config is not None and not getattr(args, "local", False))
    data = config.load(args.config or Path.home()/".config/boardmail/config.json") if needed else None
    store = Store(args.db or data["database"])
    given = {}
    for argument in command.arguments:
        value = getattr(args, argument.name)
        given[argument.name] = replies.read_body(value) if argument.file else value
    def invoke(cancelled=None):
        return commands.execute(store, name, sources=data["sources"] if data else None, cancelled=cancelled, **given)
    if not command.waits:
        return invoke()
    cancelled = threading.Event()
    previous = {}
    try:
        for sig in (signal.SIGINT,signal.SIGTERM):
            previous[sig] = signal.signal(sig,lambda *_:cancelled.set())
        return invoke(cancelled)
    finally:
        for sig,handler in previous.items():
            signal.signal(sig,handler)


def main(argv=None):
    result, code = commands.outcome(lambda: run(parser().parse_args(argv)))
    print(json.dumps(result,ensure_ascii=True))
    return code
