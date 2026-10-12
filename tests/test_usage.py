"""How the command line says that a call is typed, in the error of a line that its parser takes for no call, and
the version that it prints.

tests/usage.txt stores the usage of the first page, of each group and of each command. The other tests hold a
line to what the parser does with a call that is typed as the line says: it takes the call with every part and
with none of the parts in brackets. It takes no call that leaves out a part without brackets, and none that
types both ways of a part with a bar.

Every line that can be a call names a config and an inbox file that are not there. A call that the parser takes
then ends where it looks for one of them, and never finds the inbox of whoever runs the tests.
"""
from contextlib import redirect_stdout
import importlib.metadata
import io
import json
from pathlib import Path
import tempfile
import tomllib
import unittest

from boardmail import cli, table
import kit


ABOUT = """\
How a call is typed, as the command line says it in the usage of an error, where its parser takes a line for no
call. Made by tests/test_usage.py.

The first line answers a word that is no command. Each line after it answers a group with a word that is no
command of the group, and then a command with an option that it does not have.

"""
BEFORE = 'boardmail [--config PATH] [--db PATH] '
NO_OPTION = '--no-such-option'
# What a call has for a word of a usage line that stands for a number. For any other word a name will do.
NUMBERS = {'N': '1', 'SECONDS': '0'}


def words(command):
    group = command.name.partition('_')[0]
    return [group, command.name.removeprefix(group + '_')] if group in table.GROUPS else [command.name]


def parts(arguments):
    """The arguments of a usage line as (a part, whether it stands in brackets). A part is one argument as it is
    typed: a word by position, or an option with the word for its value."""
    found, left = [], arguments.split()
    while left:
        part = [left.pop(0)]
        if part[0].startswith('['):
            while not part[-1].endswith(']'):
                part.append(left.pop(0))
            found.append((' '.join(part)[1:-1], True))
        elif part[0].startswith('--'):
            found.append((f'{part[0]} {left.pop(0)}', False))
        else:
            found.append((part[0], False))
    return found


def filled(part, home):
    """The words of a call for one way to type a part: a choice for its choices, and a value for a word in
    capitals. A path names a file in home that is not there."""
    values = {**NUMBERS, 'PATH': str(home / 'no-file.txt')}
    return [word[1:-1].split(',')[0] if word.startswith('{') else values.get(word, 'x') if word.isupper() else word
            for word in part.split()]


class UsageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.nowhere = ['--config', str(self.home / 'config.json'), '--db', str(self.home / 'inbox.sqlite3')]

    def answer(self, *typed):
        printed = io.StringIO()
        with redirect_stdout(printed):
            code = cli.main([*self.nowhere, *typed])
        return code, printed.getvalue()

    def usage(self, *typed):
        code, printed = self.answer(*typed)
        result = json.loads(printed)
        self.assertEqual((code, result['error']), (2, 'invalid_arguments'), typed)
        return result['usage']

    def test_the_usage_of_the_first_page_of_each_group_and_of_each_command(self):
        lines = [self.usage('nope'), *(self.usage(group, 'nope') for group in table.GROUPS),
                 *(self.usage(*words(command), NO_OPTION) for command in table.COMMANDS.values())]
        kit.check_stored(self, 'usage.txt', ABOUT + '\n'.join(lines) + '\n')

    def test_a_line_is_answered_with_the_usage_of_the_last_parser_that_it_reached(self):
        first = self.usage('nope').removeprefix(BEFORE)
        for line, usage in (
                ('', first),                                                       # no command
                ('--db', first),                                                   # an option without its value
                ('reply', 'reply {list,prepare,begin,show,confirm,verify} ...'),   # a group without its command
                ('tag nope', 'tag {add,remove,show} ...'),
                ('show moltbook', 'show SOURCE ID'),                               # a word by position is left out
                ('show moltbook an-id more', 'show SOURCE ID'),                    # a word too many
                ('--nope show moltbook an-id', 'show SOURCE ID'),                  # no option, before the command
                ('show --db inbox.sqlite3 moltbook an-id', 'show SOURCE ID'),      # an option of the first page
                ('show moltbook an-id --version', 'show SOURCE ID'),
                ('reply begin moltbook an-id', 'reply begin SOURCE ID --key KEY'),
                ('mark done moltbook an-id', 'mark {read,unread,needs-reply,clear-reply,replied} SOURCE ID [--ref URL]'),
                ('tag add topic', 'tag add TAG SOURCE [THREAD] [--message ID]'),
                ('settings --scope', 'settings [--scope {addressed,all}] [--context {brief,none}] [--reset]')):
            with self.subTest(line=line):
                self.assertEqual(self.usage(*line.split()), BEFORE + usage)

    def test_an_error_has_its_usage_after_the_argument_that_it_names(self):
        code, printed = self.answer('reply', 'begin', 'moltbook', 'an-id')
        self.assertEqual((code, printed), (2, (
            '{"event": "error", "error": "invalid_arguments", "argument": "key", '
            '"usage": "boardmail [--config PATH] [--db PATH] reply begin SOURCE ID --key KEY", '
            '"next_action": "fix_the_arguments", "history_complete": false}\n')))

    def test_a_usage_repeats_no_word_of_the_line(self):
        for line in ('show moltbook an-id --invented-option invented-value', 'invented-command',
                     'reply invented-command', 'list --limit invented-value', '--invented-option list'):
            with self.subTest(line=line):
                self.assertNotIn('invented', self.answer(*line.split())[1])

    def test_a_call_that_is_typed_as_its_usage_says_is_taken(self):
        for command in table.COMMANDS.values():
            usage = self.usage(*words(command), NO_OPTION)
            start = BEFORE + ' '.join(words(command))
            self.assertEqual(usage[:len(start)], start)
            found = parts(usage[len(start):])
            needed = [filled(part, self.home) for part, optional in found if not optional]
            least = [word for part in needed for word in part]
            # A part in brackets that has a bar is typed in one of its ways, so a whole call is made for each.
            ways = [[filled(way, self.home) for way in part.split(' | ')] for part, _ in found]
            whole = [[word for part in ways for word in part[min(way, len(part) - 1)]]
                     for way in range(max(map(len, ways), default=1))]
            for call in (least, *whole):
                with self.subTest(command=command.name, call=call):
                    code, printed = self.answer(*words(command), *call)
                    self.assertNotIn('usage', json.loads(printed))
            for place, part in enumerate(needed):
                short = [word for other in needed[:place] + needed[place + 1:] for word in other]
                with self.subTest(command=command.name, left_out=part):
                    self.assertEqual(self.usage(*words(command), *short), usage)
            for part in ways:
                if len(part) > 1:
                    with self.subTest(command=command.name, both=part):
                        self.assertEqual(self.usage(*words(command), *least, *(word for way in part for word in way)),
                                         usage)

    def test_a_call_that_its_command_refuses_has_no_usage(self):
        # The parser took the line. What is wrong is a rule between two arguments, and no usage line says one.
        for line in ('list --thread a-thread', 'tag add topic moltbook'):
            with self.subTest(line=line):
                code, printed = self.answer(*line.split())
                self.assertEqual((code, json.loads(printed)), (2, {
                    'event': 'error', 'error': 'invalid_arguments', 'next_action': 'fix_the_arguments',
                    'history_complete': False}))


class VersionTests(unittest.TestCase):
    def test_the_version_that_is_printed_is_the_version_of_the_package(self):
        project = Path(__file__).resolve().parents[1] / 'pyproject.toml'
        if project.is_file():
            version = tomllib.loads(project.read_text(encoding='utf-8'))['project']['version']
        else:
            # The tests stand next to an installed package and not in its checkout.
            version = importlib.metadata.version('boardmail')
        for line in ('--version', '--version list', '--db inbox.sqlite3 --version'):
            with self.subTest(line=line):
                printed = io.StringIO()
                with redirect_stdout(printed), self.assertRaises(SystemExit) as ended:
                    cli.main(line.split())
                self.assertEqual((ended.exception.code, printed.getvalue()), (0, version + '\n'))


if __name__ == '__main__':
    unittest.main()
