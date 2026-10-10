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


def number(kind):
    return {'integer': int, 'number': float}.get(kind['type'])


def add(holder, argument, said, strict=True):
    kind, positional = argument.kind, not argument.typed.startswith('-')
    flag, _, word = ('', '', argument.typed) if positional else argument.typed.partition(' ')
    given = {'help': said}
    if kind['type'] == 'boolean':
        given['action'] = 'store_true'
    elif strict:
        # A help page shows the choices where there are some, and the word for the value where there are none.
        given.update(type=Path if argument.file else number(kind), choices=kind.get('enum'),
                     default=kind.get('default'), metavar=None if 'enum' in kind else word or None)
    if positional:
        holder.add_argument(argument.name, nargs=None if argument.required else '?', **given)
    else:
        holder.add_argument(flag, dest=argument.name, required=strict and argument.required, **given)


def parser(strict=True):
    """The parser of the command line. One that is not strict has no help page and takes a line whatever the
    values of its arguments are and whichever options it leaves out: see refused()."""
    p = Parser(prog="boardmail", description=table.FIRST_PAGE.description, epilog=table.FIRST_PAGE.epilog,
               add_help=strict)
    for argument in table.BEFORE:
        add(p, argument, table.told(None, argument, typed=True), strict)
    sub = p.add_subparsers(dest="command",required=True)
    groups = {}
    for command in table.COMMANDS.values():
        holder, word = sub, command.name
        group = word.partition('_')[0]
        if group in table.GROUPS:
            if group not in groups:
                page = table.GROUPS[group]
                s = sub.add_parser(group, help=page.summary, description=page.description, epilog=page.epilog,
                                   add_help=strict)
                groups[group] = s.add_subparsers(dest=group + '_action', required=True)
            holder, word = groups[group], word.removeprefix(group + '_')
        page = table.page(command)
        # The page of a command is one text with no lines of its own, and the parser breaks it into lines as wide
        # as the terminal. The first page and the page of a group are shown in the lines that they are written in.
        s = holder.add_parser(word, help=page.summary, description=page.description, add_help=strict,
                              formatter_class=argparse.HelpFormatter)
        either = {}
        for rule, *names in command.rules:
            if strict and rule == table.NOT_BOTH:
                either.update(dict.fromkeys(names, s.add_mutually_exclusive_group()))
        for argument in command.arguments:
            add(either.get(argument.name, s), argument, table.told(command, argument, typed=True), strict)
    return p


def named(args):
    name = args.command
    if name in table.GROUPS:
        name += '_' + getattr(args, name + '_action')
    return table.COMMANDS[name]


def refused(argv):
    """The argument that a command line is refused for, where the parser did not take the line. argparse says so
    only in a sentence, so a parser that is not strict reads the line again and the command table checks what
    it found: every argument, also one that a command looks at late. A number is made of a word that is one, as
    the parser makes it, and no file is read.

    None where that parser takes no line either: a word that is no argument, an option without its value, or an
    argument by position that is left out, which a line does not show because the words after it take its
    place. None as well where the table refuses no argument, as with an option that is given twice and holds a
    right value the second time."""
    try:
        args, rest = parser(strict=False).parse_known_args(argv)
        if not rest:
            command, given = named(args), {}
            for argument in command.arguments:
                word, make = getattr(args, argument.name), number(argument.kind)
                given[argument.name] = word if make is None else config.converted(make, word, otherwise=word)
            every = tuple(argument._replace(late=False) for argument in command.arguments)
            table.checked(command._replace(arguments=every), given)
    except MailError as exc:
        return exc.argument
    return None


def taken(argv):
    try:
        return parser().parse_args(argv)
    except MailError:
        raise MailError("invalid_arguments", argument=refused(argv)) from None


def run(args):
    command = named(args)
    name = command.name
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
        given[argument.name] = replies.read_body(value, argument.name) if argument.file else value
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
    result, code = commands.outcome(lambda: run(taken(argv)))
    print(json.dumps(result,ensure_ascii=True))
    return code
