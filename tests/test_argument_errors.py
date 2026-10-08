"""What a command answers to an argument that it must not get: the error code and the argument that the error
names, through each entry point.

The cases come from the command table: for each argument of each command the values that its kind rules out, and
for each rule of a command the ways to break it. MORE adds what a command checks beyond the kind of one argument,
and which code wins where two arguments are wrong. A case is one call that works, changed in what its line names.
It runs twice, on an inbox with mail and where no inbox file is. tests/argument_errors_cli.txt and
tests/argument_errors_mcp.txt store the answers.
"""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shlex
import tempfile
from typing import NamedTuple
import unittest

from boardmail import cli, table
from examples.fixtures import uid
import kit


START = 1_800_000_000
ME, WRITER = uid(2), uid(3)
THREAD, ASKED, ANSWER = uid(100), uid(11), uid(221)  # a post of ours, a question under it, and our answer to that
TEXT = 'An invented answer.'
KEY = '00000000-0000-4000-8000-000000000001'  # the first key that a command makes inside kit.fixed()

BOARD = kit.Moltbook(ME, 'sample-agent')
BOARD.post(THREAD, ME, 'sample-agent', 'Example discussion', 'An invented post of ours.', '2026-01-01T12:00:00Z')
BOARD.comment(ASKED, THREAD, WRITER, 'sample-writer', 'An invented question.', '2026-01-01T12:01:00Z',
              notify='post_comment')
BOARD.comment(ANSWER, THREAD, ME, 'sample-agent', TEXT, '2026-01-01T13:00:00Z', parent=ASKED)

# The inbox that the cases run on: one question, and our answer to it as far as reply begin.
INBOX = ['init', 'collect', f'reply prepare moltbook {ASKED} --body-file answer.txt',
         f'reply begin moltbook {ASKED} --key {KEY}']
ENTRIES = ('cli', 'mcp')
# A value that an argument has in a call that works.
RIGHT = {'source': 'moltbook', 'id': ASKED, 'thread': THREAD, 'tag': 'follow-up', 'untagged': True,
         'action': 'replied', 'ref': BOARD.url(THREAD, ANSWER), 'key': KEY, 'body': TEXT, 'readback_body': TEXT,
         'through': 2}
# What a call that works is given besides the arguments that its command requires.
ALSO = {'wait': {'timeout': 0}, 'mark': {'ref': RIGHT['ref']}}
# Fewer calls than this with one error code through both entry points that names an argument: the lists were misread.
NAMED_BY_BOTH = 400
OUT = object()      # in a case: the argument is left out
NO_FILE = object()  # in a case: the command line names a file that is not there

NO_UUID = 'not-a-uuid'
NO_URL = 'not-a-url'
BROKEN = 'a\nb'            # no name, id or key has a line break
NO_NAME = 'follow-up\n'    # no tag name ends with one, though the pattern of a tool's schema lets this one pass
FOLLOWED = [('thread is no UUID', {'thread': NO_UUID}),
            ('source has a line break and thread is no UUID', {'source': BROKEN, 'thread': NO_UUID})]
MEMBERSHIP = [('tag is no name and source has a line break', {'tag': NO_NAME, 'source': BROKEN}),
              ('tag is no name and neither thread nor id is given', {'tag': NO_NAME, 'thread': OUT})]
# (what the line says, what the call is given in place of what works, the entry points that can say it if not both)
MORE = {
    'settings': [('reset together with scope', {'reset': True, 'scope': 'all'})],
    'subscribe': FOLLOWED,
    'unsubscribe': FOLLOWED,
    'tag_add': MEMBERSHIP,
    'tag_remove': MEMBERSHIP,
    'list': [('through is below after', {'after': 3, 'through': 2}),
             ('tag is no name and source has a line break', {'tag': NO_NAME, 'source': BROKEN})],
    'show': [('source and id each have a line break', {'source': BROKEN, 'id': BROKEN})],
    'mark': [('read with a ref', {'action': 'read'}),
             ('read with a null ref', {'action': 'read', 'ref': None}, ('mcp',)),
             ('replied without a ref', {'ref': OUT}),
             ('replied with a null ref', {'ref': None}, ('mcp',)),
             ('replied with a ref that is no URL', {'ref': NO_URL}),
             ('replied without a ref and id has a line break', {'ref': OUT, 'id': BROKEN})],
    # The board is asked for a message or a thread only by its UUID. A stored one is found by any id.
    'context': [('id is no UUID', {'id': NO_UUID}),
                ('id is no UUID and local', {'id': NO_UUID, 'local': True})],
    'expand': [('through is below after', {'after': 3}),
               ('thread is no UUID', {'thread': NO_UUID}),
               ('thread is no UUID and local', {'thread': NO_UUID, 'local': True})],
    'reply_prepare': [('body of spaces', {'body': ' \n'}),
                      ('body with a NUL', {'body': 'a\0b'}),
                      ('body of 65536 characters that take two bytes each', {'body': 'é' * 65536}),
                      ('body file that is not there', {'body': NO_FILE}, ('cli',)),
                      ('body file that is not UTF-8', {'body': b'\xff'}, ('cli',)),
                      ('body of spaces and source has a line break', {'body': ' ', 'source': BROKEN})],
    'reply_confirm': [('ref is no URL', {'ref': NO_URL}),
                      ('ref is no URL and the readback is spaces', {'ref': NO_URL, 'readback_body': ' '}),
                      ('the readback is spaces and key has a line break', {'readback_body': ' ', 'key': BROKEN})],
    'reply_verify': [('ref is no URL', {'ref': NO_URL}),
                     ('ref is no URL and source has a line break', {'ref': NO_URL, 'source': BROKEN})],
}
ABOUT = """\
What a command answers to an argument that it must not get, {entry}.
Made by tests/test_argument_errors.py.

A line is one call that works, changed in what the line names. The values are in the test. After it stand the
answers of two calls: first on an inbox with mail, then where no inbox file is. An answer is the error code and
{outcome}, and then the argument that the error names, where it names one.
Where a call is not refused, the event of its result stands in place of an error code. "nothing wrong" is the
call as it works.

An error names the argument whose value was refused. It names none where a call has a word that is no argument of
the command, or breaks a rule between two arguments.
{more}"""
OUTCOME = {'cli': ('through the command line', 'the exit code', """\
The command line names none for an argument by position that is left out either: the words after it take its
place, so the line does not show which one it was.
"""),
           'mcp': ('through a tool call', 'whether the result is flagged as an error', '')}


class Case(NamedTuple):
    command: str
    title: str
    given: dict  # what the call is given, by the names of the command table
    entries: tuple = ENTRIES


def right(command):
    """The arguments of a call of a command that works."""
    given = {argument.name: RIGHT[argument.name] for argument in command.arguments if argument.required}
    given.update((first, RIGHT[first]) for rule, first, _ in command.rules if rule == table.ONE_OF)
    return {**given, **ALSO.get(command.name, {})}


def wrong(argument):
    """What the kind of an argument rules out: (what the line says, the value, the entry points that can say it)."""
    kind, name = argument.kind, argument.name
    if argument.required:
        yield f'{name} is left out', OUT, ENTRIES
    if 'enum' in kind:
        yield f'{name} is a word that is no choice', 'other', ENTRIES
    elif kind['type'] == 'boolean':
        yield f'{name} is a word', 'yes', ('mcp',)
    elif kind['type'] == 'string':
        yield f'{name} is empty', '', ENTRIES
        lengths = {kind.get('maxLength', 1024) + 1}
        if not argument.file:
            # Whatever a tool's schema says, the command line takes no name, id, key or URL of more than 1024
            # characters.
            yield f'{name} has a line break', BROKEN, ENTRIES
            lengths.add(1025)
        for length in sorted(lengths):
            yield f'{name} has {length} characters', 'a' * length, ENTRIES
        if 'pattern' in kind:
            yield f'{name} has a capital letter', 'Follow-up', ENTRIES
            yield f'{name} ends with a line break', NO_NAME, ENTRIES
    else:
        yield f'{name} is {kind["minimum"] - 1}', kind['minimum'] - 1, ENTRIES
        if 'maximum' in kind:
            yield f'{name} is {kind["maximum"] + 1}', kind['maximum'] + 1, ENTRIES
        yield f'{name} is a word', 'many', ENTRIES
        if kind['type'] == 'integer':
            yield f'{name} is 1.5', 1.5, ENTRIES
            yield f'{name} is 2.0', 2.0, ENTRIES
        else:
            yield f'{name} is nan', 'nan', ('cli',)
            yield f'{name} is inf', 'inf', ('cli',)
        most = (argument.tool_kind or {}).get('maximum')
        if most is not None:
            yield f'{name} is {most + 1}, more than a tool takes', most + 1, ('mcp',)


def cases():
    for command in table.COMMANDS.values():
        works = right(command)
        yield Case(command.name, 'nothing wrong', works)
        for argument in command.arguments:
            given = dict(works)
            for rule, first, second in command.rules:
                # The other argument of a rule is as the rule wants it, so that the case is about this one alone.
                if rule == table.ONE_OF and argument.name == second:
                    del given[first]
                elif rule == table.NEEDS and argument.name == first:
                    given[second] = RIGHT[second]
            for title, value, entries in wrong(argument):
                yield Case(command.name, title, {**given, argument.name: value}, entries)
        for rule, first, second in command.rules:
            both = {**works, first: RIGHT[first], second: RIGHT[second]}
            if rule == table.ONE_OF:
                yield Case(command.name, f'{first} and {second} are both given', both)
                yield Case(command.name, f'neither {first} nor {second} is given', {**works, first: OUT, second: OUT})
            elif rule == table.NEEDS:
                yield Case(command.name, f'{first} is given without {second}', {**both, second: OUT})
            elif rule == table.NOT_BOTH:
                yield Case(command.name, f'{first} and {second} are both given', both)
        yield Case(command.name, 'an argument that the command does not have', {**works, 'nope': 'x'})
        for title, change, *entries in MORE.get(command.name, ()):
            yield Case(command.name, title, {**works, **change}, *entries)
    yield Case('nope', 'a command that is not there', {})


def typed(name, given, home):
    """What is typed after boardmail for a call of a command with these arguments. A text that the command line
    reads from a file is written to one in home."""
    if name not in table.COMMANDS:
        return name
    command, group = table.COMMANDS[name], name.partition('_')[0]
    words = [group, command.name.removeprefix(group + '_')] if group in table.GROUPS else [command.name]
    arguments = {argument.name: argument for argument in command.arguments}
    for name, value in sorted(given.items(), key=lambda item: [*arguments, item[0]].index(item[0])):
        argument = arguments.get(name)
        if argument is None:
            words += ['--' + name, str(value)]
            continue
        if argument.file:
            path = home / f'{name}.txt'
            path.unlink(missing_ok=True)
            if value is not NO_FILE:
                path.write_bytes(value if isinstance(value, bytes) else value.encode())
            value = path
        if not argument.typed.startswith('-'):
            words.append(str(value))
        elif argument.kind['type'] == 'boolean':
            words += [argument.typed] * value
        else:
            words += [argument.typed.split()[0], str(value)]
    return shlex.join(words)


def answer(step):
    result = json.loads(step.text)
    outcome = step.outcome if type(step.outcome) is int else 'error' if step.outcome else 'ok'
    return ' '.join(str(part) for part in (result.get('error', result['event']), outcome, result.get('argument'))
                    if part is not None)


class ArgumentErrorTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.inbox = self.home / 'inbox.sqlite3'
        (self.home / 'moltbook.key').write_text('an-invented-key\n')
        (self.home / 'answer.txt').write_text(TEXT)
        (self.home / 'config.json').write_text(json.dumps({'database': 'inbox.sqlite3', 'sources': {
            'moltbook': {'account_id': ME, 'api_key_file': 'moltbook.key'}}}))
        self.enterContext(kit.Network({BOARD.HOST: BOARD}))
        self.enterContext(kit.fixed(kit.Clock(START)))
        made = kit.told(lambda step: [step(command, command, None) for command in INBOX], self.home, 'cli')
        self.assertEqual([step.outcome for step in made], [0] * len(INBOX), made)

    def file(self):
        return self.inbox.read_bytes() if self.inbox.exists() else None

    def answers(self, entry, start):
        """What each case is answered through an entry point, on an inbox file that starts as these bytes or, with
        None, is not there."""
        def story(step):
            for case in cases():
                if entry not in case.entries:
                    continue
                self.inbox.unlink(missing_ok=True)
                if start is not None:
                    self.inbox.write_bytes(start)
                given = {name: value for name, value in case.given.items() if value is not OUT}
                title = f'{case.command}: {case.title}'
                if entry == 'cli':
                    result = step(title, typed(case.command, given, self.home), None)
                else:
                    result = step(title, None, case.command, **given)
                if result['event'] == 'error':
                    self.assertEqual(self.file(), start, f'{title} was refused and still changed the inbox file')
                if 'argument' in result:
                    # An error never repeats a word of the caller. It names an argument as the command table does.
                    self.assertIn(result['argument'], table.order(table.COMMANDS[case.command]), title)

        return {step.title: answer(step) for step in kit.told(story, self.home, entry)}

    def check(self, entry):
        start = self.file()
        with_mail, without = self.answers(entry, start), self.answers(entry, None)
        through, outcome, more = OUTCOME[entry]
        text = [ABOUT.format(entry=through, outcome=outcome, more=more)]
        wide = max(len(case.title) for case in cases()) + 2
        for case in cases():
            if f'\n== {case.command}\n' not in text:
                text.append(f'\n== {case.command}\n')
            title = f'{case.command}: {case.title}'
            if title in with_mail:
                text.append(f'{case.title:{wide}}{with_mail[title]:40}{without[title]}\n')
        kit.check_stored(self, f'argument_errors_{entry}.txt', ''.join(text))

    def test_the_command_line(self):
        self.check('cli')

    @unittest.skipIf(kit.mcp_missing(), kit.NO_EXTRA)
    def test_tool_calls(self):
        self.check('mcp')

    def test_both_entry_points_name_the_same_argument(self):
        # Read from the two stored lists, which the tests above hold to what the entry points answer.
        wide, stored = max(len(case.title) for case in cases()) + 2, {}
        for entry in ENTRIES:
            answers, command = stored.setdefault(entry, {}), None
            for line in (kit.TESTS / f'argument_errors_{entry}.txt').read_text(encoding='utf-8').splitlines():
                if line.startswith('== '):
                    command = line[3:]
                elif command is not None and line:
                    answers[command, line[:wide].rstrip()] = line[wide:wide + 40].split(), line[wide + 40:].split()
        named = 0
        for command, title in sorted(stored['cli'].keys() & stored['mcp'].keys()):
            for typed, called in zip(stored['cli'][command, title], stored['mcp'][command, title]):
                if typed[0] != called[0] or len(called) < 3:
                    continue  # the entry points call it differently, or the error is about no argument
                named += 1
                if len(typed) == 3:
                    self.assertEqual(typed[2], called[2], (command, title))
                else:
                    # The command line cannot say which argument by position was left out.
                    by_position = [argument.name for argument in table.COMMANDS[command].arguments
                                   if not argument.typed.startswith('-')]
                    self.assertEqual((title, called[2] in by_position), (f'{called[2]} is left out', True), command)
        self.assertGreater(named, NAMED_BY_BOTH)

class CommandLineTests(unittest.TestCase):
    """What the command line names where its parser takes no line: argparse says only that it took none."""

    def test_a_line_that_the_parser_does_not_take_is_read_again_for_its_argument(self):
        for line, named in (('list --limit many', 'limit'),          # a word that is no number
                            ('list --scope other', 'scope'),         # a word that is no choice
                            ('reply begin moltbook an-id', 'key'),   # an option that is left out
                            ('reply list --after many', 'after'),    # an argument that its command looks at late
                            ('list --tag Bad --limit many', 'tag'),  # the first in the order of the command table
                            ('reply begin moltbook', None),          # an argument by position that is left out
                            ('list --nope 1', None),                 # a word that is no argument
                            ('list --tag x --untagged', None),       # a rule between two
                            ('list --limit', None),                  # an option without its value
                            ('list --limit many --limit 1', None),   # an option that is given twice
                            ('nope', None)):                         # no command
            with self.subTest(line=line):
                printed = io.StringIO()
                with redirect_stdout(printed):
                    code = cli.main(shlex.split(line))
                result = json.loads(printed.getvalue())
                self.assertEqual((code, result['error'], result.get('argument')), (2, 'invalid_arguments', named))

    def test_a_refused_line_that_asks_for_help_gets_no_help_page(self):
        printed = io.StringIO()
        with redirect_stdout(printed):
            code = cli.main(['list', '--limit', 'many', '--help'])
        self.assertEqual((code, json.loads(printed.getvalue())['error']), (2, 'invalid_arguments'))


if __name__ == '__main__':
    unittest.main()
