"""What an inbox file has after each command: its version number, tables, columns and indexes.

Each command runs once on a fresh copy of three files: a new inbox, the bundled version-1 file, and that file
after its first collect. tests/file_shape.txt stores what the command leaves there. This is the test to rewrite
when the file gets a single shape.
"""
import argparse
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest

from boardmail import cli
from examples.fixtures import uid
import kit


START = 1_800_000_000
ME, WRITER = uid(2), uid(3)
THREAD = uid(100)                     # a post of ours. The version-1 file has two replies to it:
DONE, ASKED = uid(10), uid(11)        # one that it marks as answered, and one that it does not
ANSWER, WAITING = uid(221), uid(222)  # our answer to the second, twice: the board has verified only the first
TEXT = 'An invented answer.'

BOARD = kit.Moltbook(ME, 'sample-agent')
BOARD.post(THREAD, ME, 'sample-agent', 'Example discussion', 'An invented post of ours.', '2026-01-01T12:00:00Z')
for minute, question in enumerate((DONE, ASKED), 1):
    BOARD.comment(question, THREAD, WRITER, 'sample-writer', f'Invented question {minute}.',
                  f'2026-01-01T12:0{minute}:00Z', notify='post_comment')
BOARD.comment(ANSWER, THREAD, ME, 'sample-agent', TEXT, '2026-01-01T13:00:00Z', parent=ASKED)
BOARD.comment(WAITING, THREAD, ME, 'sample-agent', TEXT, '2026-01-01T13:01:00Z',
              parent=ASKED)['verification_status'] = 'pending'

# A row is what is typed after boardmail. A word in capitals stands for one of these.
NAMES = {'MESSAGE': ASKED, 'THREAD': THREAD,
         'KEY': '00000000-0000-4000-8000-000000000001',  # the first key that a command makes inside kit.fixed()
         'ANSWER': BOARD.url(THREAD, ANSWER), 'WAITING': BOARD.url(THREAD, WAITING)}
ABOUT = """\
What an inbox file has after each command: its version number, tables, columns and indexes.
Made by tests/test_file_shape.py. Each command ran once on a fresh copy of each of three files:

  new inbox             what init creates, filled by one collect, which adds nothing to it
  version 1             tests/fixtures/v1.sql
  version 1, collected  that file after its first collect

A table is written in the words of the statements that the file itself keeps: its columns, then what holds
for the whole table, then its indexes. A line with + is something that the file has after the command and did
not have before it. A table that is added is named there and written out under the new inbox or at the end.
"same shape" says that the command added nothing and took nothing away. "file unchanged" says more: the file
is the same byte for byte.

Where commands stand together, the others ran first and the row is about the last one, counted from the file
as they left it. MESSAGE is a message that all three files hold, and THREAD is its thread. KEY is the key that
reply prepare gives. ANSWER and WAITING are where two replies of ours are on the board: it has verified the
first and not yet the second.
"""

PREPARE = 'reply prepare moltbook MESSAGE --body-file answer.txt'
BEGIN = 'reply begin moltbook MESSAGE --key KEY'
# Where a row is a tuple, the last command is the one that the row is about and the others are the steps
# that it needs first.
READS = [
    'status',
    'list',
    'wait --timeout 0',
    'show moltbook MESSAGE',
    'context --local moltbook MESSAGE',
    'expand --local --through 2 moltbook THREAD',
    'tags',
    'tag show follow-up',
    'subscriptions',
    'settings',
    'reply list',
    'reply show moltbook MESSAGE',
    # These two read the board as well.
    'context moltbook MESSAGE',
    'expand --through 2 moltbook THREAD',
]
WRITES = [
    'init',  # refused: the file is there
    'collect',
    'check',
    'mark read moltbook MESSAGE',
    'mark unread moltbook MESSAGE',
    'mark needs-reply moltbook MESSAGE',
    'mark clear-reply moltbook MESSAGE',
    'mark replied --ref ANSWER moltbook MESSAGE',
    'settings --scope all --context none',
    'settings --reset',
    'tag add follow-up moltbook THREAD',
    'tag remove follow-up moltbook THREAD',
    'subscribe moltbook THREAD',
    'unsubscribe moltbook THREAD',
    'pause moltbook',
    'resume moltbook',
    PREPARE,
    BEGIN,  # refused: nothing is prepared
    (PREPARE, BEGIN),
    (PREPARE, BEGIN, 'reply confirm moltbook MESSAGE --key KEY --ref ANSWER --readback-file answer.txt'),
    (PREPARE, BEGIN, 'reply verify moltbook MESSAGE --key KEY --ref WAITING'),
    (PREPARE, BEGIN, 'reply verify moltbook MESSAGE --key KEY --ref ANSWER'),
]

# The first word of a part of CREATE TABLE that is not a column.
RULES = ('CONSTRAINT', 'PRIMARY', 'UNIQUE', 'CHECK', 'FOREIGN')


def commands(parser, words=()):
    """Every command of the parser, as the words that are typed for it."""
    below = [action for action in parser._actions if isinstance(action, argparse._SubParsersAction)]
    if not below:
        return [' '.join(words)]
    return [name for word, under in below[0].choices.items() for name in commands(under, (*words, word))]


def parts(text):
    """What stands between the commas of text. A comma inside brackets does not count."""
    found, depth = [''], 0
    for char in text:
        depth += (char == '(') - (char == ')')
        if char == ',' and not depth:
            found.append('')
        else:
            found[-1] += char
    return [part.strip() for part in found]


def shape(path):
    """(the version number of an inbox file, what it holds). What it holds maps 'table NAME' to the lines of
    that table, in the words of the statements that the file keeps: its columns in their order, then what holds
    for the whole table, then its indexes. So nothing that a statement says can change unseen."""
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        holds = {}
        # A statement is kept for everything except what SQLite makes by itself, and tables come first here.
        for kind, name, table, sql in db.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE sql IS NOT NULL "
                "AND name NOT LIKE 'sqlite_%' ORDER BY type != 'table', type, name").fetchall():
            written = ' '.join(sql.split())
            if kind == 'index':
                holds[f'table {table}'].append(written)
            elif kind == 'table':
                head, _, rest = written.partition('(')
                inside, _, tail = rest.rpartition(')')
                found = parts(inside)
                whole_table = [part for part in found if re.split(r'[\s(]', part)[0].upper() in RULES]
                holds[f'table {name}'] = [part for part in found if part not in whole_table] + whole_table + [
                    extra for extra in (tail.strip(), head.strip()) if extra not in ('', f'CREATE TABLE {name}')]
            else:
                holds[f'{kind} {name}'] = [written]
        return db.execute('PRAGMA user_version').fetchone()[0], holds


def whole(holds):
    """What a file holds as the text that is stored."""
    return ''.join(f'{name}\n' + ''.join(f'    {line}\n' for line in lines) for name, lines in holds.items())


def changes(old, new):
    """What a file holds now and did not hold before, and the other way round, one line for each."""
    lines = []
    for sign, here, there in (('+', new, old), ('-', old, new)):
        for name, parts in here.items():
            if name not in there:
                lines.append(f'{sign} {name}')
            else:
                lines += [f'{sign} {name.split()[-1]}: {part}' for part in parts if part not in there[name]]
    return lines


class FileShapeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.inbox, self.earlier = self.home / 'inbox.sqlite3', self.home / 'earlier.sqlite3'
        (self.home / 'moltbook.key').write_text('an-invented-key\n')
        (self.home / 'answer.txt').write_text(TEXT)
        (self.home / 'config.json').write_text(json.dumps({'database': 'inbox.sqlite3', 'sources': {
            'moltbook': {'account_id': ME, 'api_key_file': 'moltbook.key'}}}))
        self.enterContext(kit.Network({BOARD.HOST: BOARD}))

    def run_row(self, row):
        """Run the commands of a row on the inbox as it is. The exit code of the last one. self.earlier is then
        the file as it was just before that one."""
        def story(step):
            for number, typed in enumerate(row, 1):
                if number == len(row):
                    self.earlier.write_bytes(self.inbox.read_bytes() if self.inbox.exists() else b'')
                step(typed, ' '.join(NAMES.get(word, word) for word in typed.split()), None)

        with kit.fixed(kit.Clock(START)):
            *first, last = kit.told(story, self.home, 'cli')
        self.assertEqual([step.outcome for step in first], [0] * len(first), first)
        return last.outcome

    def starts(self):
        """The three files that every command starts from, by name."""
        self.assertEqual(self.run_row(['init']), 0)
        created = shape(self.inbox)
        self.assertEqual(self.run_row(['collect']), 0)
        self.assertEqual(shape(self.inbox), created, 'The first collect changed what init had created')
        files = {'new inbox': self.inbox.read_bytes()}
        self.inbox.unlink()
        with closing(sqlite3.connect(self.inbox)) as db:
            db.executescript((kit.TESTS / 'fixtures/v1.sql').read_text())
        files['version 1'] = self.inbox.read_bytes()
        self.assertEqual(self.run_row(['collect']), 0)
        files['version 1, collected'] = self.inbox.read_bytes()
        return files

    def test_the_file_after_each_command(self):
        files, text, shapes, ran = self.starts(), [ABOUT], {}, []
        for name, start in files.items():
            self.inbox.write_bytes(start)
            version, shapes[name] = shape(self.inbox)
            text.append(f'== {name}\nversion {version}\n{whole(shapes[name])}')
        # One table has one shape, whichever command adds it and to whichever file.
        tables = dict(shapes['new inbox'])
        for title, part in (('Commands that only read', READS), ('Commands that can write', WRITES)):
            text.append(f'== {title}\n')
            for row in part:
                row = (row,) if isinstance(row, str) else row
                ran.append(row[-1])
                lines = [f'$ boardmail {typed}\n' for typed in row]
                for name, start in files.items():
                    self.inbox.write_bytes(start)
                    code = self.run_row(row)
                    (was, old), (version, new) = shape(self.earlier), shape(self.inbox)
                    same = self.inbox.read_bytes() == self.earlier.read_bytes()
                    if part is READS:
                        with self.subTest(read=row[-1], file=name):
                            self.assertTrue(same, 'A command that only reads changed the file')
                    for thing in new:
                        if thing not in old:
                            self.assertEqual(tables.setdefault(thing, new[thing]), new[thing],
                                             f'{thing} has two shapes')
                    found = changes(old, new) or ['same shape' if old == new else 'the same in another order']
                    if same and part is READS:
                        found = ['file unchanged']
                    versions = str(was) if was == version else f'{was} -> {version}'
                    lead = f'  {name + ":":22}exit {code}  version {versions}  '
                    lines.append(lead + ('\n' + ' ' * len(lead)).join(found) + '\n')
                text.append(''.join(lines))
        for command in commands(cli.parser()):
            self.assertTrue(any(f'{typed} '.startswith(f'{command} ') for typed in ran), f'No row runs {command}')
        text.append('== Tables that init does not create\n' + whole({
            name: lines for name, lines in sorted(tables.items()) if name not in shapes['new inbox']}))
        kit.check_stored(self, 'file_shape.txt', '\n'.join(text))


if __name__ == '__main__':
    unittest.main()
