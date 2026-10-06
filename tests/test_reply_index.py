"""A new inbox has the index for reply lookups; an older file gets it from collection, never from a read."""
from contextlib import closing, redirect_stdout
import io
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from boardmail import cli


TESTS = Path(__file__).resolve().parent


class ReplyIndexTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for name in ('custom_board.py', 'custom_feed.json', 'custom_config.json'):
            shutil.copyfile(TESTS.parent / 'examples' / name, self.root / name)
        self.config, self.db = self.root / 'custom_config.json', self.root / 'custom.sqlite3'

    def cli(self, *args):
        printed = io.StringIO()
        with redirect_stdout(printed):
            code = cli.main([str(arg) for arg in args])
        return code, json.loads(printed.getvalue())

    def file(self):
        """(whether the inbox file has an index on source and reply reference, its version number)"""
        with closing(sqlite3.connect(self.db.as_uri() + '?mode=ro', uri=True)) as db:
            indexes = [name for _, name, *_ in db.execute('PRAGMA index_list(messages)')]
            columns = [[column for _, _, column in db.execute(f'PRAGMA index_info("{name}")')] for name in indexes]
            return ['source', 'reply_ref'] in columns, db.execute('PRAGMA user_version').fetchone()[0]

    def test_new_inbox_has_the_index(self):
        self.assertEqual(self.cli('--config', self.config, 'init')[0], 0)
        self.assertEqual(self.file(), (True, 2))

    def test_older_file_gets_the_index_from_collection_and_not_from_a_read(self):
        with closing(sqlite3.connect(self.db)) as db:
            db.executescript((TESTS / 'fixtures/v1.sql').read_text())
        before = self.db.read_bytes()
        code, page = self.cli('--db', self.db, 'list')
        self.assertEqual((code, len(page['messages'])), (0, 2))
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(self.file(), (False, 1))
        code, result = self.cli('--config', self.config, 'collect')
        self.assertEqual((code, result['added']), (0, 1))
        self.assertEqual(self.file(), (True, 2))


if __name__ == '__main__':
    unittest.main()
