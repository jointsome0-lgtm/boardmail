#!/usr/bin/env python3
"""What an agent reads before its first call, as text, and how much of it and of its results there is.

    python scripts/agent_view.py            the size report
    python scripts/agent_view.py --update   write the stored view again

The stored view is two files. tests/test_agent_view.py fails when either one
differs from what Boardmail supplies now, so a pull request that changes what
an agent reads shows that change as a difference in these files.

    tests/agent_view_cli.txt   the command line
    tests/agent_view_mcp.txt   the MCP server; reading it needs the optional extra

What the view holds
-------------------

The command line part is read from the parser that boardmail.cli.parser()
builds, not from the help that argparse prints, so it is the same text on
every Python. It holds what that parser was given and leaves out what
argparse fills in on its own. For each command that is the summary its
parent lists, the description, the epilog, and every argument and option
with its help, type, choices and default. A usage line, a command name of
its own, a section that was added or renamed, text given to the list of
commands and a missing or changed -h are written where the parser has them.
Today Boardmail's parser has none of these.

The command line part does not hold how the parser treats what is typed:
abbreviated options, prefix characters, arguments read from a file.

The MCP part is read through an in-memory client, the way a client receives
it: the server, its instructions, its capabilities and its tools. It holds
every field the client receives for them, minus what the client also
receives from a server and a tool that were given nothing. So a field that Boardmail
starts to supply shows up here, and a field that a new release of the MCP
library starts to fill in does not. The server version is written as "the
Boardmail version" while it is that, so a release does not change the view.

Boardmail has no MCP prompts and no resources. Adding one changes the
capabilities, which fails the test, and this script then has to learn to
write them down.

The arguments of boardmail-mcp are not in the view. An operator starts that
command, not an agent, and its parser is built inside its main function.

How the text is written
-----------------------

The text is an outline: "key: value", with what belongs to a key indented
under it. A short list or mapping stands on one line in brackets or braces,
a longer list has one "- item" per line. Text under "|" keeps its line
breaks. Text under ">" is one line, broken here after each sentence. A value
in double quotes is written as JSON, because without them it could be taken
for something else: two values that differ are meant to give two texts.

In the command line part the key of an argument is the way a help page
names it: "--after N" is an option followed by one value, "SOURCE" is a
value given by position. "takes" says how many values follow when that is
not exactly one. An option that takes no value is a flag: giving it means
yes. "action" is the argparse name of what an argument does, written when
that is something other than keeping its value or being a flag. "default"
is written when the parser names one; without it, a flag left out means no
and anything else left out has no value.

In the MCP part the keys of a tool are the names of the protocol:
inputSchema, annotations.

Each file starts with its size. Sizes count characters, and JSON is counted
compact. Help text is the summaries, descriptions, usage lines, help
strings, epilogs and section titles that the parser was given; names of
commands and arguments are not in it. The tool catalog as JSON is everything the view
holds for the tools, as one text. The size report prints the same numbers,
and the length of the help that argparse prints on this Python at 80
columns. That last number is not stored: it differs a little between Python
versions.

What the size report says of results
------------------------------------

The stored view ends where the first call begins. What an agent reads after that is results, and the size report
counts those that tests/story_inbox.txt and tests/story_reply.txt hold: the two stories store the result of
every command that they run, on invented boards. A page is a result of check, list or wait that holds messages.
For the pages, for the messages on them, for the brief context of those messages, for the rows that a page has
for the sources and for its summaries of thread activity, and for the results of context and of expand, the
report has how many the stories hold and how many characters they are together as compact JSON. The bodies of
the messages are counted as their text. So the numbers say what a result costs around the mail that it carries,
and they move when a result changes shape. They are not a measure of real mail: the bodies of the stories are
one short sentence each.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parent.parent
STORED = {'cli': ROOT / 'tests/agent_view_cli.txt', 'mcp': ROOT / 'tests/agent_view_mcp.txt'}
STORIES = (ROOT / 'tests/story_inbox.txt', ROOT / 'tests/story_reply.txt')
COMMAND = 'python scripts/agent_view.py --update'
EXTRA = "The MCP part needs the optional extra. From the source checkout, run: python -m pip install '.[mcp]'"
TITLES = {'cli': 'What an agent reads from the boardmail command line before its first call.',
          'mcp': 'What an agent reads from the Boardmail MCP server before its first call.'}
RELEASE = 'the Boardmail version'
TAKES = {0: 'no value', '?': 'one value or none', '*': 'any number of values', '+': 'one or more values'}
# The default argparse gives an argument of a kind when the parser names none. For every other kind it is None.
FILLED = {'store_true': False, 'store_false': True, 'help': argparse.SUPPRESS, 'version': argparse.SUPPRESS}
INLINE = 100    # The longest list or mapping kept on one line, and the longest text left unbroken.
ADDRESS = re.compile(r' at 0x[0-9a-fA-F]+')
WORD = re.compile(r'[\w$.-]+')
CONTROL = re.compile('[\x00-\x1f\x7f-\x9f\u2028\u2029]')
SENTENCE = re.compile(r'(?<!e\.g\.)(?<!i\.e\.)(?<=[.!?]) (?=\S)')
COLOR = re.compile('\x1b\\[[0-9;]*m')
# One step of a stored story after its "== ": the title, what was typed, the exit code and the result.
STEP = re.compile(r'(?s)(?P<title>[^\n]*)\n\$ (?P<command>.*?)\nexit code [^\n]*\n(?P<result>\{.*)')


# The outline.

def bare(text):
    """Whether text can stand on a line as it is and still be read as this text and nothing else."""
    if not text or text != text.strip() or CONTROL.search(text) or text in ('|', '>') or text[0] in '[{':
        return False
    try:
        json.loads(text)
    except ValueError:
        return True
    return False


def label(key):
    if not isinstance(key, str):
        raise TypeError(f'The outline has no way to write the key {key!r}')
    return key if bare(key) and ': ' not in key and key[-1] != ':' and key[:2] != '- ' else json.dumps(key)


def short(value):
    """A value on one line, or None when it is too long for that or holds text of more than one word."""
    if isinstance(value, dict):
        parts = [(key, short(item)) for key, item in value.items()]
        fits = all(isinstance(key, str) and WORD.fullmatch(key) and bare(key) and item is not None
                   for key, item in parts)
        text = '{' + ', '.join(f'{key}: {item}' for key, item in parts) + '}' if fits else None
    elif isinstance(value, list):
        parts = [short(item) for item in value]
        text = None if None in parts else '[' + ', '.join(parts) + ']'
    elif isinstance(value, str):
        text = (value if bare(value) else json.dumps(value)) if WORD.fullmatch(value) else None
    else:
        text = json.dumps(value)
    return text if text is not None and len(text) <= INLINE else None


def lines(head, value, indent=''):
    """The lines for one value under its key, or after its dash in a list."""
    inside = indent + '  '
    if isinstance(value, str):
        if '\n' in value and not CONTROL.search(value.replace('\n', '')):
            yield f'{indent}{head} |'
            for line in value.split('\n'):
                yield inside + line if line else ''
        elif len(value) > INLINE and bare(value):
            yield f'{indent}{head} >'
            for sentence in SENTENCE.split(value):
                yield inside + sentence
        else:
            yield f'{indent}{head} {value if bare(value) else json.dumps(value, ensure_ascii=False)}'
    elif not isinstance(value, (dict, list)):
        yield f'{indent}{head} {json.dumps(value)}'
    elif short(value) is not None:
        yield f'{indent}{head} {short(value)}'
    else:
        yield indent + head
        for key, item in value.items() if isinstance(value, dict) else ((None, item) for item in value):
            yield from lines('-' if key is None else label(key) + ':', item, inside)


def written(part, tree):
    """One part of the view as the text of its file."""
    rows = [f'# {TITLES[part]}', f'# Written by {COMMAND}. The top of that script says how to read this.']
    for key, value in tree.items():
        rows += ['', *lines(label(key) + ':', value)]
    return '\n'.join(rows) + '\n'


# The command line.

def filled(action, prog):
    """The help of an argument with its placeholders filled in, the way argparse fills them."""
    values = {key: value for key, value in vars(action).items() if value is not argparse.SUPPRESS}
    values = {key: getattr(value, '__name__', value) for key, value in {**values, 'prog': prog}.items()}
    if values.get('choices') is not None:
        values['choices'] = ', '.join(map(str, values['choices']))
    return action.help % values


def named(action, whole=True):
    """An argument by the name a help page gives it. An option is named by its strings and, when whole,
    by the value it takes."""
    value = action.metavar if action.metavar is not None else (
        action.dest if not action.option_strings else None if action.choices is not None else action.dest.upper())
    value = ' '.join(value) if isinstance(value, tuple) else value
    if not action.option_strings:
        return value
    if not whole:
        return action.option_strings[0]
    return ', '.join(action.option_strings) + (f' {value}' if value and action.nargs != 0 else '')


def plain(value):
    """A default, a constant or a choice as a value the outline can write."""
    if isinstance(value, (list, tuple, range)):
        return [plain(item) for item in value]
    return value if value is None or isinstance(value, (str, int, float)) else ADDRESS.sub('', repr(value))


def given(action):
    """What an argument was built with, to tell whether two were built alike."""
    return type(action), {key: value for key, value in vars(action).items() if key != 'container'}


def said(text, prog):
    """A description or an epilog the way a help page shows it, with the command name filled in."""
    return text % {'prog': prog} if text and '%(prog)' in text else text


def argument(action, parser, prog):
    """What a parser was given for one argument or option, without what argparse fills in on its own."""
    kinds = {found: name for name, found in parser._registries['action'].items() if name is not None}
    kind = kinds.get(type(action), type(action).__name__)
    count = action.nargs
    entry = {
        'help': filled(action, prog) if action.help else None,
        'takes': None if count is None else TAKES.get(count, f'exactly {count}' if isinstance(count, int) else count),
        'action': None if kind in ('store', 'store_true') else kind,
        'type': action.type and (getattr(action.type, '__name__', None) or ADDRESS.sub('', repr(action.type))),
        'choices': None if action.choices is None else plain(list(action.choices)),
        'default': plain(action.default),
        'const': None if kind in ('store_true', 'store_false') else plain(action.const),
        'required': True if action.option_strings and action.required else None,
        'version': getattr(action, 'version', None),
        'deprecated': getattr(action, 'deprecated', None) or None,
        'not with': [named(other, False) for group in parser._mutually_exclusive_groups
                     if action in group._group_actions for other in group._group_actions
                     if other is not action] or None,
    }
    return {key: found for key, found in entry.items()
            if (action.default is not FILLED.get(kind) if key == 'default' else found is not None)}


def cli_tree(root=None):
    """The command tree as nested values: the size first, then every command by the words that run it."""
    if root is None:
        from boardmail import cli

        root = cli.parser()
    own = argparse.ArgumentParser()    # what argparse gives a parser that was given nothing
    tree, texts, runnable = {'size': {}}, [], 0
    pending = [(root, [COLOR.sub('', root.prog)], None, [])]
    while pending:
        parser, path, summary, aliases = pending.pop(0)
        prog = COLOR.sub('', parser.prog)
        shown, below, helped = {'arguments': {}, 'options': {}}, None, False
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                below = action
            elif given(action) == given(own._actions[0]):
                helped = True
            elif action.help is not argparse.SUPPRESS:
                group = shown['options' if action.option_strings else 'arguments']
                if named(action) in group:
                    raise ValueError(f'{" ".join(path)} has two arguments shown as {named(action)}')
                entry = group[named(action)] = argument(action, parser, prog)
                texts.append(entry.get('help'))
        # The two sections every parser has are written only when their title or description was changed.
        usual = {id(parser._positionals): own._positionals.title, id(parser._optionals): own._optionals.title}
        sections = []
        for group in parser._action_groups:
            if id(group) not in usual or group.title != usual[id(group)] or group.description is not None:
                holds = ['commands' if member is below else named(member, False) for member in group._group_actions]
                sections.append({key: found for key, found in (
                    ('title', group.title), ('description', group.description), ('holds', holds)) if found is not None})
                texts += [group.title, group.description]
        node = {'summary': summary, 'aliases': aliases or None, 'prog': None if prog == ' '.join(path) else prog,
                'usage': parser.usage and parser.usage % {'prog': prog},
                'description': said(parser.description, prog),
                'arguments': shown['arguments'] or None, 'options': shown['options'] or None,
                'one is required of': [[named(member, False) for member in group._group_actions]
                                       for group in parser._mutually_exclusive_groups if group.required] or None,
                'help option': None if helped else False, 'sections': sections or None}
        if below is None:
            runnable += 1
        else:
            summaries = {item.dest: item.help and filled(item, prog) for item in below._choices_actions}
            names = {}
            for name, found in below.choices.items():
                names.setdefault(id(found), (found, []))[1].append(name)
            node['commands'] = {known[0]: summaries.get(known[0]) for found, known in names.values()}
            node['command required'] = below.required
            node['commands help'] = below.help and filled(below, prog)
            node['commands shown as'] = below.metavar
            pending[:0] = [(found, [*path, known[0]], summaries.get(known[0]), known[1:])
                           for found, known in names.values()]
            texts.append(node['commands help'])
        node['epilog'] = said(parser.epilog, prog)
        if parser is root or parser.formatter_class is not root.formatter_class:
            formatter = parser.formatter_class
            node['formatter'] = getattr(formatter, '__name__', None) or ADDRESS.sub('', repr(formatter))
        tree[' '.join(path)] = {key: found for key, found in node.items() if found is not None}
        texts += [summary, node['usage'], node['description'], node['epilog']]
    tree['size'] = {'help pages': len(tree) - 1, 'commands that run': runnable,
                    'help text': sum(map(len, filter(None, texts)))}
    return tree


def printed_help():
    """(pages, characters) of the help that argparse prints for every command at 80 columns on this Python."""
    from boardmail import cli

    before = os.environ.get('COLUMNS')
    os.environ['COLUMNS'] = '80'
    try:
        pending, pages = [cli.parser()], []
        while pending:
            parser = pending.pop()
            pages.append(COLOR.sub('', parser.format_help()))
            pending += {id(found): found for action in parser._actions
                        if isinstance(action, argparse._SubParsersAction) for found in action.choices.values()}.values()
    finally:
        if before is None:
            del os.environ['COLUMNS']
        else:
            os.environ['COLUMNS'] = before
    return len(pages), sum(map(len, pages))


# The MCP server.

def mcp_missing():
    return importlib.util.find_spec('mcp') is None


def mcp_tree(server=None):
    """What an in-memory client receives from the server, as nested values: the size, the server and every
    tool."""
    import asyncio
    import tempfile

    from mcp import Client
    from mcp.server import Server
    from mcp.types import ListToolsResult, Tool

    import boardmail
    from boardmail.mcp import create_server
    from boardmail.store import Store

    async def bare_list(ctx, params):
        return ListToolsResult(tools=[Tool(name='bare', input_schema={'type': 'object'})])

    async def bare_call(ctx, params):
        raise NotImplementedError

    async def received(server):
        async with Client(server, raise_exceptions=True) as client:
            page = await client.list_tools()
            tools = list(page.tools)
            while page.next_cursor:
                page = await client.list_tools(cursor=page.next_cursor)
                tools += page.tools
            return client.server_info, client.server_capabilities, tools, client.instructions

    def wire(model):
        return model.model_dump(mode='json', by_alias=True, exclude_none=True)

    def supplied(model, usual):
        """What a received object holds beyond what the library fills in for one that was given nothing.
        A field that the protocol requires is always held."""
        required = {field.alias or name for name, field in type(model).model_fields.items() if field.is_required()}
        usual = wire(usual)
        return {key: found for key, found in wire(model).items()
                if key in required or key not in usual or usual[key] != found}

    def compact(value):
        return len(json.dumps(value, separators=(',', ':'), ensure_ascii=False))

    with tempfile.TemporaryDirectory() as folder:
        # Listing tools opens no inbox. The folder is there so that nothing can land in the working directory.
        server = server or create_server(Store(Path(folder) / 'inbox.sqlite3'))
        info, capabilities, tools, instructions = asyncio.run(received(server))
    bare = asyncio.run(received(Server('bare', on_list_tools=bare_list, on_call_tool=bare_call)))
    about = supplied(info, bare[0])
    if about['version'] == boardmail.__version__:
        about['version'] = RELEASE
    about |= {'instructions': instructions, 'capabilities': supplied(capabilities, bare[1]) or None}
    entries = [supplied(tool, bare[2][0]) for tool in tools]
    size = {'tools': len(tools),
            'tool descriptions': sum(len(entry.get('description', '')) for entry in entries),
            'input schemas': sum(compact(entry['inputSchema']) for entry in entries),
            'server instructions': len(instructions or ''),
            'tool catalog as JSON': compact(entries)}
    tree = {'size': size, 'server': {key: found for key, found in about.items() if found is not None}}
    for entry in entries:
        tree[f'tool {entry.pop("name")}'] = entry
    return tree


def cli_view():
    return written('cli', cli_tree())


def mcp_view():
    return written('mcp', mcp_tree())


# The results.

def told(story):
    """(command, result) for each step of a stored story: what was typed, and the result as values."""
    steps = []
    for step in ('\n' + story).split('\n== ')[1:]:
        found = STEP.fullmatch(step)
        if found is None:
            raise ValueError(f'The step {step.partition(chr(10))[0]!r} of a story is not a command with its exit code')
        # What was typed may run over several lines.
        steps.append((found['command'], json.loads(found['result'])))
    return steps


def results_size(stories=None):
    """What the results in the stored stories cost: for each kind of thing the report counts, how many the
    stories hold and how many characters they are together. stories are the texts of the stories."""
    if stories is None:
        stories = [path.read_text(encoding='utf-8') for path in STORIES]
    size = {kind: [0, 0] for kind in (
        'pages of check, list and wait', 'messages on those pages', 'bodies of those messages',
        'brief context of those messages', 'source rows on those pages', 'thread summaries on those pages',
        'results of context', 'results of expand')}

    def count(kind, value):
        size[kind][0] += 1
        size[kind][1] += len(value) if isinstance(value, str) else len(
            json.dumps(value, separators=(',', ':'), ensure_ascii=False))

    for story in stories:
        for command, result in told(story):
            if result.get('event') == 'messages':
                count('pages of check, list and wait', result)
                for message in result['messages']:
                    count('messages on those pages', message)
                    count('bodies of those messages', message['body'])
                    if 'brief' in message:
                        count('brief context of those messages', message['brief'])
                for source in result['sources']:
                    count('source rows on those pages', source)
                for thread in result['thread_activity']:
                    count('thread summaries on those pages', thread)
            elif result.get('event') in ('context', 'expanded'):
                count('results of context' if result['event'] == 'context' else 'results of expand', result)
    return {kind: tuple(found) for kind, found in size.items()}


# The size report.

def report(cli_size, mcp_size, results):
    """The size report as text. mcp_size is None when the optional extra is not installed. results is what
    results_size() gives."""
    pages, characters = printed_help()
    version = '.'.join(map(str, sys.version_info[:2]))
    rows = ['MCP server']
    if mcp_size is None:
        rows.append(f'  {EXTRA}')
    else:
        rows += [f'  {"tools":36}{mcp_size["tools"]:>8,}',
                 *(f'  {key:36}{mcp_size[key]:>8,} characters' for key in mcp_size if key != 'tools')]
    rows += ['Command line',
             f'  {"help pages":36}{cli_size["help pages"]:>8,}',
             f'  {"commands that run":36}{cli_size["commands that run"]:>8,}',
             f'  {"help text":36}{cli_size["help text"]:>8,} characters',
             f'  {"printed help":36}{characters:>8,} characters on {pages} pages at 80 columns, Python {version}',
             'Results in ' + ' and '.join(str(path.relative_to(ROOT)) for path in STORIES),
             *(f'  {kind:36}{number:>8,} with {together:>7,} characters' for kind, (number, together) in results.items()),
             'JSON is counted compact. Help text leaves out the names of commands and arguments. '
             'A body is counted as its text.']
    return '\n'.join(rows)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Print how much an agent reads before its first call and in the results of the stored '
                    'stories. The top of this file says what is counted.')
    parser.add_argument('--update', action='store_true',
                        help='Write the view to tests/agent_view_cli.txt and tests/agent_view_mcp.txt')
    args = parser.parse_args(argv)
    trees = {'cli': cli_tree(), 'mcp': None if mcp_missing() else mcp_tree()}
    print(report(trees['cli']['size'], trees['mcp'] and trees['mcp']['size'], results_size()))
    if args.update:
        for part, tree in trees.items():
            if tree is not None:
                STORED[part].write_text(written(part, tree), encoding='utf-8', newline='\n')
        if trees['mcp'] is None:
            print(f'{STORED["mcp"].name} is not written. {EXTRA}', file=sys.stderr)
            return 1
    return 0


if __name__ == '__main__':
    sys.path.insert(0, str(ROOT))    # The view of this checkout, also where another Boardmail is installed.
    raise SystemExit(main())
