"""Source pause contracts with invented mail and no board requests."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from boardmail import commands, config, providers
from boardmail.adapters import Batch, collect_all
from boardmail.store import Store
from examples.fixtures import settings, uid
from test_mail import mail


class PauseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'mail.sqlite3'
        self.store = Store(self.path)

    def cli(self, *args):
        result = subprocess.run([sys.executable, '-m', 'boardmail', *map(str, args)],
                                capture_output=True, text=True, timeout=10)
        return result.returncode, json.loads(result.stdout)

    def test_cli_pause_preserves_mail_marks_and_progress_across_processes(self):
        examples = Path(__file__).resolve().parents[1] / 'examples'
        for name in ('custom_board.py', 'custom_feed.json', 'custom_config.json'):
            shutil.copyfile(examples / name, self.root / name)
        cfg = self.root / 'custom_config.json'
        base = ('--config', cfg)
        self.assertEqual(self.cli(*base, 'init')[0], 0)
        self.assertEqual(self.cli(*base, 'collect')[1]['added'], 1)
        store = Store(self.root / 'custom.sqlite3')
        store.mark('example', '1', 'read')
        store.mark('example', '1', 'needs_reply')
        store.mark('example', '1', 'replied', ref='https://example.invalid/reply')
        before = store.show('example', '1')
        progress = store.collection_state('example', 'demo-agent', str(self.root / 'custom_board.py'))
        raw_config = cfg.read_bytes()
        for changed in (True, False):
            code, result = self.cli(*base, 'pause', 'example')
            self.assertEqual((code, result['event'], result['changed'], result['collection_performed']),
                             (0, 'paused', changed, False))
        # A paused adapter must not even load, including on a fresh CLI process.
        adapter = self.root / 'custom_board.py'
        original = adapter.read_bytes()
        adapter.write_text('raise RuntimeError("paused adapter was loaded")\n')
        for command in ('collect', 'check'):
            code, result = self.cli(*base, command)
            collection = result['collection'] if command == 'check' else result
            self.assertEqual((code, collection['added'], collection['failed']), (0, 0, False))
        code, status = self.cli('--db', store.path, 'status', '--require-fresh')
        self.assertEqual((code, status['sources'][0]['status'], status['sources'][0]['paused']), (0, 'paused', True))
        self.assertEqual(store.collection_state('example', 'demo-agent', str(adapter)), progress)
        self.assertEqual(store.show('example', '1'), before)
        self.assertEqual(store.wait(1, 0)['event'], 'timeout')
        adapter.write_bytes(original)
        for changed in (True, False):
            code, result = self.cli('--db', store.path, 'resume', 'example')
            self.assertEqual((code, result['event'], result['changed']), (0, 'resumed', changed))
        self.assertEqual(self.cli(*base, 'collect')[1]['added'], 1)
        self.assertEqual([m['id'] for m in store.page()['messages']], ['1', '2'])
        self.assertEqual(store.show('example', '1'), before)
        self.assertEqual(cfg.read_bytes(), raw_config)

    def test_configured_source_can_be_paused_before_collection_and_typos_do_not_write(self):
        self.store.initialize()
        cfg = self.root / 'config.json'
        cfg.write_text(json.dumps({'database': 'unused.sqlite3', 'sources': {
            'example': {'account_id': 'demo-agent', 'adapter': 'does-not-exist.py'}}}))
        code, result = self.cli('--config', cfg, '--db', self.path, 'pause', 'example')
        self.assertEqual((code, result['paused']), (0, True))
        self.assertFalse(collect_all(self.store, config.load(cfg)['sources'])['failed'])
        before = self.path.read_bytes()
        for command in ('pause', 'resume'):
            code, result = self.cli('--db', self.path, command, 'typo')
            self.assertEqual((code, result['error'], result['next_action']),
                             (2, 'source_not_found', 'check_source_name_in_status_or_config'))
            self.assertEqual(self.path.read_bytes(), before)
        with self.assertRaisesRegex(config.MailError, 'account_mismatch'):
            self.store.set_paused('example', False, {'account_id': 'another-agent'})
        self.assertTrue(self.store.is_paused('example'))

    def test_legacy_reads_do_not_migrate_and_pause_preserves_records_and_schema_version(self):
        for version in (1, 2):
            with self.subTest(version=version):
                path = self.root / f'v{version}.sqlite3'
                with closing(sqlite3.connect(path)) as db:
                    db.executescript((Path(__file__).parent / 'fixtures/v1.sql').read_text())
                store = Store(path)
                if version == 2:
                    store.prepare_collection()
                messages = store.page()['messages']
                raw = path.read_bytes()
                self.assertFalse(store.is_paused('moltbook'))
                self.assertFalse(store.status()['sources'][0]['paused'])
                self.assertEqual(path.read_bytes(), raw)
                store.set_paused('moltbook', True)
                self.assertEqual(store.page()['messages'], messages)
                self.assertTrue(Store(path).is_paused('moltbook'))
                with closing(sqlite3.connect(path)) as db:
                    self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], version)
                store.set_paused('moltbook', False)
                self.assertEqual(store.page()['messages'], messages)

    def test_freshness_excludes_only_paused_sources_and_resume_restores_health(self):
        self.store.initialize(settings())
        self.store.save('moltbook', uid(2), [])
        self.store.failure('the-colony', uid(1), 'http_503')
        self.store.set_paused('the-colony', True)
        self.store.set_paused('postingboard', True)
        result, code = commands.execute(self.store, 'status', require_fresh=True)
        self.assertEqual((code, result['fresh']), (0, True))
        self.store.set_paused('moltbook', True)
        self.assertEqual(commands.execute(self.store, 'status', require_fresh=True)[1], 0)
        self.store.set_paused('the-colony', False)
        result, code = commands.execute(self.store, 'status', require_fresh=True)
        colony = next(s for s in result['sources'] if s['source'] == 'the-colony')
        self.assertEqual((code, colony['status'], colony['error']), (1, 'error', 'http_503'))

    def test_inflight_pass_can_finish_but_cannot_clear_pause_or_start_later_paused_source(self):
        sources = {s: settings()[s] for s in ('moltbook', 'the-colony')}
        self.store.initialize(sources)
        entered, finish = threading.Event(), threading.Event()

        def collect(*args, **kwargs):
            entered.set()
            if not finish.wait(5):
                raise RuntimeError('test did not release collector')
            return Batch(messages=[mail(10)], state={'cursor': 'saved'})

        with patch.object(providers, 'collect', side_effect=collect) as remote, ThreadPoolExecutor(1) as pool:
            running = pool.submit(collect_all, self.store, sources)
            try:
                self.assertTrue(entered.wait(5))
                self.store.set_paused('moltbook', True)
                self.store.set_paused('the-colony', True)
            finally:
                finish.set()
            result = running.result(timeout=5)
            self.assertEqual((result['added'], result['failed'], remote.call_count), (1, False, 1))
        self.assertTrue(all(s['paused'] for s in self.store.status()['sources']))
        self.store.set_paused('moltbook', False)
        self.assertEqual(self.store.collection_state('moltbook', uid(2), 'moltbook')[1], {'cursor': 'saved'})

    def test_paused_context_never_creates_a_remote_client(self):
        self.store.initialize(settings())
        self.store.save('postingboard', uid(3), [mail(10)])
        self.store.set_paused('postingboard', True)
        before = self.path.read_bytes()
        with patch.object(providers, 'Client', side_effect=AssertionError('remote lookup while paused')):
            result, code = commands.execute(self.store, 'context', source='postingboard', id=uid(10), sources=settings())
        self.assertFalse(result['fetched'])
        self.assertEqual(result['target']['message']['id'], uid(10))
        self.assertEqual(code, 1)  # The missing root remains explicitly unknown.
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
