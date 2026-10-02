"""Committed state after injected failures and competing SQLite writers."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from boardmail.adapters import Batch
from boardmail.store import Store
from examples.fixtures import settings, uid
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

    def test_late_checkpoint_failure_rolls_back_entire_batch_and_can_retry(self):
        source, owner = 'moltbook', uid(2)
        self.store.initialize(settings())
        self.store.save_collection(source, owner, source, 0,
                                   Batch(messages=[mail(10)], originals=[mail(100)], state={'cursor': 'old'}))
        for mark in ('read', 'needs_reply'):
            self.store.mark(source, uid(10), mark)
        self.store.mark(source, uid(10), 'replied', ref='https://example.invalid/reply')
        self.store.set_subscription(source, uid(100), True)
        self.store.set_paused(source, True)
        revision = self.store.collection_state(source, owner, source)[2]
        with closing(self.connect(self.path)) as db:
            db.execute("""CREATE TRIGGER fail_checkpoint BEFORE UPDATE ON adapter_state
                BEGIN SELECT RAISE(ABORT, 'synthetic_checkpoint_failure'); END""")
            db.commit()
        before = self.snapshot()
        batch = Batch(messages=[{**mail(10), 'body': 'Replay must not change marks'}, mail(11), mail(12)],
                      originals=[{**mail(100), 'body': 'Replacement context'}, mail(101)],
                      state={'cursor': 'new'}, complete=False, error='http_503', unavailable=2)
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'synthetic_checkpoint_failure'):
            self.store.save_collection(source, owner, source, revision, batch)
        # This independent connection observes DML, arrival sequence, local marks,
        # health, subscriptions and progress exactly as before the failed commit.
        self.assertEqual(self.snapshot(), before)
        with closing(self.connect(self.path)) as db:
            db.execute('DROP TRIGGER fail_checkpoint')
            db.commit()
        self.assertEqual(self.store.save_collection(source, owner, source, revision, batch), (2, False))
        known, state, current = self.store.collection_state(source, owner, source)
        self.assertEqual((known, state, current), ({uid(10), uid(11), uid(12)}, {'cursor': 'new'}, revision + 1))
        self.assertEqual([row['arrival_seq'] for row in self.store.page()['messages']], [1, 2, 3])
        self.assertTrue(self.store.is_paused(source))
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

        with patch('boardmail.store.sqlite3.connect', failing_connect):
            with self.assertRaisesRegex(RuntimeError, 'synthetic_migration_failure'):
                self.store.prepare_collection()
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0][:2], (2, 'moltbook'))
        self.assertTrue({'discovery', 'addressing'} <= observed[0][2])
        self.assertEqual(self.snapshot(), before)
        self.store.prepare_collection()
        after = self.snapshot()
        self.assertEqual(after['version'], 2)
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
            with lock:
                connections.append(connection)
            return connection

        def migrate():
            start.wait(timeout=2)
            Store(self.path).prepare_collection()

        with patch('boardmail.store.sqlite3.connect', coordinated_connect):
            with ThreadPoolExecutor(2) as pool:
                futures = [pool.submit(migrate) for _ in range(2)]
                for future in futures:
                    future.result(timeout=10)  # Propagate either worker's failure.
        self.assertEqual(len(connections), 2)
        self.assertIsNot(connections[0], connections[1])
        self.assertEqual(len(attempts), 2)
        after = self.snapshot()
        self.assertEqual(after['version'], 2)
        self.assertEqual(after['rows']['messages'], [row + (None, None) for row in before['rows']['messages']])
        self.assertEqual(after['rows']['sources'], before['rows']['sources'])
        self.assertEqual(after['rows']['adapter_state'], [('moltbook', 'moltbook', 0, '{}', 0)])
        self.store.prepare_collection()
        self.assertEqual(self.snapshot(), after)


if __name__ == '__main__':
    unittest.main()
