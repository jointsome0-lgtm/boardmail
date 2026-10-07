"""Committed state after injected failures and competing SQLite writers."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from boardmail import commands
from boardmail.store import Store
from examples.fixtures import settings, uid
from kit import DESCRIBED, arrive, described, mark
from test_mail import mail


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'mail.sqlite3'
        self.store = Store(self.path)
        # Keep independent observers outside the test-only connection patches.
        self.connect = sqlite3.connect

    def snapshot(self):
        with closing(self.connect(self.path)) as db:
            schema = db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchall()
            tables = [row[1] for row in schema if row[0] == 'table']
            return {'version': db.execute('PRAGMA user_version').fetchone()[0],
                    'schema': schema, 'rows': {
                        table: db.execute('SELECT * FROM "' + table + '" ORDER BY rowid').fetchall()
                        for table in tables}}

    def legacy(self):
        with closing(self.connect(self.path)) as db:
            db.executescript((Path(__file__).parent / 'fixtures/v1.sql').read_text())

    def migrate(self):
        """A collection brings the file up to date before it looks at a source. This one has no source."""
        commands.execute(Store(self.path), 'collect', sources={})

    def test_late_checkpoint_failure_rolls_back_entire_batch_and_can_retry(self):
        source, owner = 'custom', uid(2)
        sources = {source: described(owner), **settings()}
        commands.execute(self.store, 'init', sources=sources)
        arrive(self.store, source, owner, [mail(10)], originals=[mail(100)], state={'cursor': 'old'})
        for action in ('read', 'needs_reply'):
            mark(self.store, source, uid(10), action)
        mark(self.store, source, uid(10), 'replied', ref='https://example.invalid/reply')
        # Another source has a subscription and is paused, so the file holds every kind of row.
        commands.execute(self.store, 'subscribe', sources=sources, source='moltbook', thread=uid(100))
        commands.execute(self.store, 'pause', sources=sources, source='moltbook')
        revision = self.store.collection_state(source, owner, str(DESCRIBED))[2]
        with closing(self.connect(self.path)) as db:
            db.execute("""CREATE TRIGGER fail_checkpoint BEFORE UPDATE ON adapter_state
                BEGIN SELECT RAISE(ABORT, 'synthetic_checkpoint_failure'); END""")
            db.commit()
        before = self.snapshot()
        # What the next pass gives. The inbox file fails it at its last write, the position of the source.
        gives = dict(messages=[{**mail(10), 'body': 'Replay must not change marks'}, mail(11), mail(12)],
                     originals=[{**mail(100), 'body': 'Replacement context'}, mail(101)],
                     state={'cursor': 'new'}, complete=False, error='http_503', unavailable=2)
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'synthetic_checkpoint_failure'):
            arrive(self.store, source, owner, **gives)
        # This independent connection observes DML, arrival sequence, local marks,
        # health, subscriptions and progress exactly as before the failed commit.
        self.assertEqual(self.snapshot(), before)
        with closing(self.connect(self.path)) as db:
            db.execute('DROP TRIGGER fail_checkpoint')
            db.commit()
        result = arrive(self.store, source, owner, **gives)
        self.assertEqual((result['added'], [error['error'] for error in result['errors']]), (2, ['http_503']))
        known, state, current = self.store.collection_state(source, owner, str(DESCRIBED))
        self.assertEqual((known, state, current), ({uid(10), uid(11), uid(12)}, {'cursor': 'new'}, revision + 1))
        self.assertEqual([row['arrival_seq'] for row in self.store.page()['messages']], [1, 2, 3])
        self.assertTrue(self.store.is_paused('moltbook'))
        self.assertEqual(self.snapshot()['rows']['messages'][0], before['rows']['messages'][0])

    def test_failure_after_migration_version_write_rolls_back_schema_and_data(self):
        self.legacy()
        before = self.snapshot()
        observed = []

        class FailingMigration(sqlite3.Connection):
            def execute(connection, sql, *args):
                result = super().execute(sql, *args)
                if sql == 'PRAGMA user_version=2':
                    observed.append((connection.execute('PRAGMA user_version').fetchone()[0],
                                     connection.execute('SELECT adapter FROM adapter_state').fetchone()[0],
                                     {row[1] for row in connection.execute('PRAGMA table_info(messages)')}))
                    raise RuntimeError('synthetic_migration_failure')
                return result

        def failing_connect(*args, **kwargs):
            return self.connect(*args, factory=FailingMigration, **kwargs)

        with patch('sqlite3.connect', failing_connect):
            with self.assertRaisesRegex(RuntimeError, 'synthetic_migration_failure'):
                self.migrate()
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0][:2], (2, 'moltbook'))
        self.assertTrue({'discovery', 'addressing'} <= observed[0][2])
        self.assertEqual(self.snapshot(), before)
        self.migrate()
        after = self.snapshot()
        self.assertEqual(after['rows']['messages'], [row + (None, None) for row in before['rows']['messages']])
        self.assertEqual(after['rows']['sources'], before['rows']['sources'])
        self.assertEqual(after['rows']['adapter_state'], [('moltbook', 'moltbook', 0, '{}', 0)])

    def test_competing_migrations_serialize_and_keep_legacy_mail_and_marks(self):
        self.legacy()
        before = self.snapshot()
        start = threading.Barrier(2)
        first_acquired, second_attempted = threading.Event(), threading.Event()
        lock, connections = threading.Lock(), []
        attempts = []

        class CoordinatedConnection(sqlite3.Connection):
            def execute(connection, sql, *args):
                if sql == 'BEGIN IMMEDIATE':
                    with lock:
                        attempts.append(connection)
                        first = len(attempts) == 1
                    if not first:
                        if not first_acquired.wait(2):
                            raise RuntimeError('first migration did not acquire its write lock')
                        second_attempted.set()
                result = super().execute(sql, *args)
                if sql == 'BEGIN IMMEDIATE' and first:
                    first_acquired.set()
                    if not second_attempted.wait(2):
                        raise RuntimeError('second migration did not attempt its write lock')
                return result

        def coordinated_connect(*args, **kwargs):
            connection = self.connect(*args, factory=CoordinatedConnection, **kwargs)
            if 'mode=rw' in args[0]:  # The connection of a migration. The status that follows it only reads.
                with lock:
                    connections.append(connection)
            return connection

        def migrate():
            start.wait(timeout=2)
            self.migrate()

        with patch('sqlite3.connect', coordinated_connect):
            with ThreadPoolExecutor(2) as pool:
                futures = [pool.submit(migrate) for _ in range(2)]
                for future in futures:
                    future.result(timeout=10)  # Propagate either worker's failure.
        self.assertEqual(len(connections), 2)
        self.assertIsNot(connections[0], connections[1])
        self.assertEqual(len(attempts), 2)
        after = self.snapshot()
        self.assertEqual(after['rows']['messages'], [row + (None, None) for row in before['rows']['messages']])
        self.assertEqual(after['rows']['sources'], before['rows']['sources'])
        self.assertEqual(after['rows']['adapter_state'], [('moltbook', 'moltbook', 0, '{}', 0)])
        self.migrate()
        self.assertEqual(self.snapshot(), after)


if __name__ == '__main__':
    unittest.main()
