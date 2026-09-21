"""Discover pending replies without trusting independent inbox marks."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from boardmail import commands, replies
from boardmail.store import Store
from examples.fixtures import uid
from test_mail import mail


class ReplyDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'mail.sqlite3'
        self.store = Store(self.path); self.store.initialize()

    def attempt(self, source, number, state):
        self.store.save(source, uid(2), [mail(number)])
        result, _ = replies.execute(self.store, 'prepare', source, uid(number), body='Synthetic reply')
        key = result['reply']['idempotency_key']
        if state != 'prepared':
            replies.execute(self.store, 'begin', source, uid(number), key=key)
        if state == 'confirmed':
            replies.execute(self.store, 'confirm', source, uid(number), key=key,
                            ref='https://example.invalid/reply', readback_body='Synthetic reply')
        return key

    def cli(self, *argv):
        run = subprocess.run([sys.executable, '-B', '-m', 'boardmail', '--db', str(self.path), *argv],
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout)

    def test_no_table_and_legacy_databases_remain_unchanged(self):
        for version in (None, 1, 2):
            with self.subTest(version=version):
                if version is not None:
                    self.path.unlink()
                    with closing(sqlite3.connect(self.path)) as db:
                        db.executescript((Path(__file__).parent / 'fixtures/v1.sql').read_text())
                    if version == 2:
                        self.store.prepare_collection()
                before = self.path.read_bytes()
                status = self.cli('status')
                page = self.cli('reply', 'list')
                self.assertEqual(status['reply_attempts'],
                                 {k: page[k] for k in ('counts', 'items', 'has_more', 'next_after', 'next')})
                self.assertEqual(page['counts'], {'prepared': 0, 'unknown': 0, 'confirmed': 0})
                self.assertEqual(page['items'], [])
                self.assertFalse(page['has_more']); self.assertIsNone(page['next'])
                self.assertEqual(page['next_after'], 0)
                self.assertEqual(self.path.read_bytes(), before)

    def test_status_routes_survive_replied_and_pages_cover_multiple_sources(self):
        expected = []
        for n in range(45):
            source = 'moltbook' if n % 2 else 'custom'
            state = 'unknown' if n % 2 else 'prepared'
            self.attempt(source, 10 + n, state)
            expected.append((source, uid(10 + n), state))
        self.attempt('custom', 100, 'confirmed')
        self.attempt('moltbook', 101, 'confirmed')
        self.store.mark('moltbook', uid(11), 'replied', ref='https://example.invalid/independent')
        self.store.mark('moltbook', uid(11), 'read')
        self.store.mark('moltbook', uid(11), 'clear_reply')
        before = self.path.read_bytes()
        with patch('boardmail.providers.Client.get', side_effect=AssertionError('Discovery must not fetch')):
            status, code = commands.execute(self.store, 'status')
            self.assertEqual(code, 0)
            self.assertEqual(status['counts']['replied'], 3)
            page = status['reply_attempts']
            self.assertEqual(len(page['items']), 20)
            observed, sizes = [], []
            while True:
                self.assertEqual(page['counts'], {'prepared': 23, 'unknown': 22, 'confirmed': 2})
                sizes.append(len(page['items']))
                for item in page['items']:
                    observed.append((item['source'], item['id'], item['state']))
                    self.assertNotIn('body', item); self.assertNotIn('idempotency_key', item)
                    route = item['show']
                    recovered = self.cli(*route['command'].split(), route['arguments']['source'], route['arguments']['id'])
                    self.assertEqual(recovered['reply']['state'], item['state'])
                    self.assertEqual(recovered['next_action'], item['next_action'])
                if not page['has_more']:
                    self.assertIsNone(page['next']); break
                route = page['next']
                self.assertEqual(route['tool'], 'boardmail_reply_list')
                page = self.cli(*route['command'].split(), '--after', str(route['arguments']['after']),
                                '--limit', str(route['arguments']['limit']))
            self.assertEqual(sizes, [20, 20, 5])
            self.assertEqual(observed, expected)
        self.assertEqual(self.path.read_bytes(), before)

    def test_exact_page_boundary_and_cursor_validation(self):
        self.attempt('moltbook', 10, 'prepared')
        self.attempt('moltbook', 11, 'unknown')
        before = self.path.read_bytes()
        page = self.cli('reply', 'list', '--limit', '2')
        self.assertFalse(page['has_more']); self.assertIsNone(page['next'])
        self.assertEqual(len(page['items']), 2)
        page = self.cli('reply', 'list', '--after', str(page['next_after']), '--limit', '2')
        self.assertEqual(page['items'], []); self.assertEqual(page['next_after'], 2)
        self.assertEqual(page['counts'], {'prepared': 1, 'unknown': 1, 'confirmed': 0})
        for field, values in (('after', [-1, 2**63, True, 1.5]), ('limit', [0, 101, True, 1.5])):
            for value in values:
                with self.subTest(field=field, value=value):
                    result, code = commands.outcome(lambda: commands.execute(self.store, 'reply_list', **{field: value}))
                    self.assertEqual((code, result['error']), (2, 'invalid_arguments'))
        self.assertEqual(self.path.read_bytes(), before)

    def test_status_counts_and_pending_items_share_one_snapshot(self):
        key = self.attempt('moltbook', 10, 'unknown')
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('PRAGMA journal_mode=WAL')
        pending = replies.pending

        def confirm_after_status_started(db):
            replies.execute(self.store, 'confirm', 'moltbook', uid(10), key=key,
                            ref='https://example.invalid/reply', readback_body='Synthetic reply')
            return pending(db)

        with patch('boardmail.store.replies.pending', side_effect=confirm_after_status_started):
            status = self.store.status()
        self.assertEqual(status['counts']['replied'], 0)
        self.assertEqual(status['reply_attempts']['counts']['unknown'], 1)
        self.assertEqual(status['reply_attempts']['items'][0]['state'], 'unknown')
        later = self.store.status()
        self.assertEqual(later['counts']['replied'], 1)
        self.assertEqual(later['reply_attempts']['counts']['confirmed'], 1)
        self.assertEqual(later['reply_attempts']['items'], [])
