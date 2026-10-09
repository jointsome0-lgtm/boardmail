"""What an inbox file has: its version number, tables, columns and indexes.

init creates a new inbox with every part. A file that an older release left is short of some, and the first
command that opens it gives it those. tests/file_shape.txt stores the shape of a new inbox and what each older
file gets. Each command runs once on a fresh copy of a new inbox and of each older file: the file then has the
shape of a new inbox and the rows that it had.
"""
import argparse
from contextlib import closing
import json
import os
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
THREAD = uid(100)                     # a post of ours. Every file has two replies to it. Of the version-1 file,
DONE, ASKED = uid(10), uid(11)        # one is marked as answered and one is not
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

# A word in capitals of a row below stands for one of these.
NAMES = {'MESSAGE': ASKED, 'THREAD': THREAD,
         'KEY': '00000000-0000-4000-8000-000000000001',  # the first key that a command makes inside kit.fixed()
         'ANSWER': BOARD.url(THREAD, ANSWER), 'WAITING': BOARD.url(THREAD, WAITING)}
# The files that older releases left, each as the SQL that makes it. The top of a file says how it came about.
OLDER = {'version 1': 'v1.sql',
         'version 1, written by 0.15.1': 'v1-written-0.15.1.sql',
         'version 1, collected by 0.15.1': 'v1-collected-0.15.1.sql',
         'created by 0.15.1': 'created-0.15.1.sql'}
ABOUT = """\
What an inbox file has: its version number, tables, columns and indexes. Made by tests/test_file_shape.py.

init creates a new inbox with every part, and no command adds a part or takes one away. A file that an older
release left is short of some. The first command that opens it gives it those: the file then has the shape of
a new inbox, and every row that it had.

A table is written in the words of the statements that the file itself keeps: its columns, then what holds
for the whole table, then its indexes. Under an older file, a line with + is a part or a row that the first
command gives it.
"""

PREPARE = 'reply prepare moltbook MESSAGE --body-file answer.txt'
BEGIN = 'reply begin moltbook MESSAGE --key KEY'
# A row is what is typed after boardmail. MESSAGE is a message that every file holds, and THREAD is its thread.
# KEY is the key that reply prepare gives. ANSWER and WAITING are where two replies of ours are on the board: it
# has verified the first and not yet the second. Where a row is a tuple, the last command is the one that the row
# is about and the others are the steps that it needs first.
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

# One older file has an answer prepared, so reply begin alone is not refused there.
PREPARED = {(BEGIN, 'version 1, written by 0.15.1'): 0}

# The first word of a part of CREATE TABLE that is not a column.
RULES = ('CONSTRAINT', 'PRIMARY', 'UNIQUE', 'CHECK', 'FOREIGN')
# What a row says in a column that its file did not have.
WITHOUT = {'discovery': None, 'addressing': None, 'paused': 0}
# What a file says of a message apart from its marks. No command changes it.
MAIL = ('arrival_seq', 'source', 'id', 'thread_id', 'parent_id', 'provider_seq', 'kind', 'author', 'title', 'body',
        'url', 'created_at', 'arrived_at')


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


def rows(path):
    """The rows of an inbox file: for each table its columns and its rows, in the order of the file."""
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        found = {}
        for table, in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                                 "ORDER BY name").fetchall():
            cursor = db.execute(f'SELECT * FROM {table} ORDER BY rowid')
            found[table] = [column[0] for column in cursor.description], cursor.fetchall()
        return found


def mail(found):
    """What the rows of a file say of each message apart from its marks."""
    columns, lines = found['messages']
    return [tuple(line[columns.index(name)] for name in MAIL) for line in lines]


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

    def told(self, row):
        """Run the commands of a row on the inbox as it is. The steps as kit.told gives them. self.earlier is
        then the file as it was just before the last one."""
        def story(step):
            for number, typed in enumerate(row, 1):
                if number == len(row):
                    self.earlier.write_bytes(self.inbox.read_bytes() if self.inbox.exists() else b'')
                step(typed, ' '.join(NAMES.get(word, word) for word in typed.split()), None)

        with kit.fixed(kit.Clock(START)):
            return kit.told(story, self.home, 'cli')

    def run_row(self, row):
        """Run the commands of a row. The exit code of the last one; the others have to end well."""
        *first, last = self.told(row)
        self.assertEqual([step.outcome for step in first], [0] * len(first), first)
        return last.outcome

    def starts(self):
        """The files that every command starts from, by name: a new inbox, then each older file."""
        self.assertEqual(self.run_row(['init']), 0)
        created = shape(self.inbox)
        self.assertEqual(self.run_row(['collect']), 0)
        self.assertEqual(shape(self.inbox), created, 'The first collect changed what init had created')
        files = {'new inbox': self.inbox.read_bytes()}
        for name, fixture in OLDER.items():
            self.inbox.unlink()
            with closing(sqlite3.connect(self.inbox)) as db:
                db.executescript((kit.TESTS / 'fixtures' / fixture).read_text(encoding='utf-8'))
            files[name] = self.inbox.read_bytes()
        return files

    def given(self, had, has):
        """The rows that a file has and did not have, one line for each. It fails where a row that the file had
        is gone or says something else."""
        lines = []
        for table, (columns, now) in has.items():
            names, before = had.get(table, (columns, []))
            self.assertEqual(columns[:len(names)], names, table)
            for row in now:
                if row[:len(names)] not in before:
                    lines.append(f'+ row of {table}: {", ".join(map(str, row))}')
                else:
                    self.assertEqual(dict(zip(columns[len(names):], row[len(names):])),
                                     {name: WITHOUT[name] for name in columns[len(names):]}, table)
            self.assertEqual([row[:len(names)] for row in now][:len(before)], before, table)
        return lines

    def first(self, files):
        """The shape of a new inbox, and for each older file its rows after the first command that opens it."""
        self.inbox.write_bytes(files['new inbox'])
        new, after = shape(self.inbox), {}
        for name in OLDER:
            self.inbox.write_bytes(files[name])
            self.assertEqual(self.run_row(['status']), 0)
            self.assertEqual(shape(self.inbox), new, name)
            after[name] = rows(self.inbox)
        return new, after

    def test_a_new_inbox_has_every_part_and_the_first_command_gives_them_to_an_older_file(self):
        files = self.starts()
        (version, holds), after = self.first(files)
        text = [ABOUT, f'== new inbox\nversion {version}\n{whole(holds)}']
        for name, fixture in OLDER.items():
            self.inbox.write_bytes(files[name])
            was, old = shape(self.inbox)
            lines = [f'tests/fixtures/{fixture}', f'version {was}' if was == version else f'version {was} -> {version}',
                     *changes(old, holds), *self.given(rows(self.inbox), after[name])]
            text.append(f'== {name}\n' + ''.join(f'{line}\n' for line in lines))
        kit.check_stored(self, 'file_shape.txt', '\n'.join(text))

    def test_every_command_leaves_every_file_with_the_shape_of_a_new_inbox(self):
        files, ran = self.starts(), []
        new, after = self.first(files)
        for part in (READS, WRITES):
            for row in part:
                row = (row,) if isinstance(row, str) else row
                ran.append(row[-1])
                codes = {}
                for name, start in files.items():
                    with self.subTest(command=row[-1], file=name):
                        self.inbox.write_bytes(start)
                        codes[name] = self.run_row(row)
                        if codes[name] == 2 and self.inbox.read_bytes() == self.earlier.read_bytes():
                            # The command was refused before it wrote. What a command gives an older file is in
                            # the transaction of what it writes: all of it or nothing.
                            continue
                        self.assertEqual(shape(self.inbox), new)
                        if part is WRITES:
                            had = mail(rows(self.earlier))
                            self.assertEqual(mail(rows(self.inbox))[:len(had)], had)
                        elif name in OLDER:
                            # What the first command gives is the same, whichever command it is.
                            self.assertEqual(rows(self.inbox), after[name])
                        else:
                            self.assertEqual(self.inbox.read_bytes(), start,
                                             'A command that only reads changed a file that has every part')
                # A command ends on an older file as it ends on a new inbox.
                self.assertEqual(codes, {name: PREPARED.get((row[-1], name), codes['new inbox']) for name in files})
        for command in commands(cli.parser()):
            self.assertTrue(any(f'{typed} '.startswith(f'{command} ') for typed in ran), f'No row runs {command}')

    def test_an_older_file_that_cannot_be_written_is_not_read_and_stays_as_it_was(self):
        files = self.starts()
        beside = sorted(path.name for path in self.home.iterdir())
        for name in OLDER:
            with self.subTest(file=name):
                self.inbox.write_bytes(files[name])
                os.chmod(self.inbox, 0o400)
                os.chmod(self.home, 0o500)
                try:
                    if os.access(self.inbox, os.W_OK):
                        self.skipTest('This process can write a file that nobody may write')
                    step, = self.told(['status'])
                finally:
                    os.chmod(self.home, 0o700)
                    os.chmod(self.inbox, 0o600)
                result = json.loads(step.text)
                self.assertEqual((step.outcome, result['error'], result.get('reason')),
                                 (2, 'local_state_error', 'read_only'))
                self.assertEqual(self.inbox.read_bytes(), files[name])
                self.assertEqual(sorted(path.name for path in self.home.iterdir()), beside)
        # A file that has every part is read where nothing can be written.
        self.inbox.write_bytes(files['new inbox'])
        os.chmod(self.inbox, 0o400)
        os.chmod(self.home, 0o500)
        try:
            self.assertEqual(self.run_row(['status']), 0)
        finally:
            os.chmod(self.home, 0o700)
            os.chmod(self.inbox, 0o600)
        self.assertEqual(self.inbox.read_bytes(), files['new inbox'])


if __name__ == '__main__':
    unittest.main()
