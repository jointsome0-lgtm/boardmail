"""Local selections and collection boundaries; all mail and adapters are invented."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from boardmail import commands, config, providers
from boardmail.adapters import Batch, collect_all
from boardmail.store import Store
from examples.fixtures import settings, uid
from test_mail import mail


class SubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'mail.sqlite3'
        self.store = Store(self.path)

    def cli(self, *args):
        result = subprocess.run([sys.executable, '-m', 'boardmail', '--db', str(self.path), *map(str, args)],
                                capture_output=True, text=True, timeout=10)
        return result.returncode, json.loads(result.stdout)

    def test_all_boards_cli_persistence_idempotence_and_saved_marks(self):
        sources = {source: {'account_id': uid(n)} for n, source in enumerate(config.COVERAGE, 1)}
        self.store.initialize(sources)
        self.store.save('postingboard', uid(1), [mail(10)])
        for action in ('read', 'needs_reply'):
            self.store.mark('postingboard', uid(10), action)
        self.store.mark('postingboard', uid(10), 'replied', ref='https://example.invalid/reply')
        saved = self.store.show('postingboard', uid(10))
        thread = 'abcdef00-0000-0000-0000-000000000100'
        for source in sources:
            for changed in (True, False):
                code, result = self.cli('subscribe', source, thread.upper())
                self.assertEqual((code, result['event'], result['thread'], result['changed']),
                                 (0, 'subscribed', thread, changed))
                self.assertFalse(result['collection_performed'])
                self.assertEqual(result['history'], 'available')
        code, selected = self.cli('subscriptions')
        self.assertEqual(code, 0)
        self.assertEqual([row['source'] for row in selected['subscriptions']], sorted(sources))
        self.assertEqual(self.store.status()['subscriptions'], selected['subscriptions'])
        raw = self.path.read_bytes()
        self.assertEqual(self.cli('subscriptions', '--source', 'postingboard')[1]['subscriptions'],
                         self.store.subscriptions('postingboard'))
        self.assertEqual(raw, self.path.read_bytes())
        for changed in (True, False):
            result = self.cli('unsubscribe', 'postingboard', thread)[1]
            self.assertEqual((result['event'], result['subscribed'], result['changed']),
                             ('unsubscribed', False, changed))
        self.assertEqual(len(self.store.subscriptions()), 5)
        self.assertEqual(self.store.show('postingboard', uid(10)), saved)
        self.cli('subscribe', 'postingboard', thread)
        self.assertEqual(self.store.save('postingboard', uid(1), [mail(10)]), 0)
        self.assertEqual(self.store.show('postingboard', uid(10)), saved)

    def test_old_databases_read_without_migration_and_explicit_selection_preserves_mail(self):
        for version in (1, 2):
            with self.subTest(version=version):
                path = self.root / f'v{version}.sqlite3'
                with closing(sqlite3.connect(path)) as db:
                    db.executescript((Path(__file__).parent / 'fixtures/v1.sql').read_text())
                store = Store(path)
                if version == 2:
                    store.prepare_collection()
                    with store.connect(write=True) as db:
                        db.execute('DROP TABLE subscriptions')
                saved = store.page()['messages']
                before = path.read_bytes()
                self.assertEqual(store.subscriptions(), [])
                self.assertEqual(store.status()['subscriptions'], [])
                self.assertEqual(path.read_bytes(), before)
                if version == 1:
                    with self.assertRaisesRegex(config.MailError, 'subscription_config_required'):
                        store.set_subscription('moltbook', uid(100), True)
                    self.assertEqual(path.read_bytes(), before)
                for active in (False, True, True, False):
                    commands.execute(store, 'subscribe' if active else 'unsubscribe', source='moltbook',
                                     thread=uid(100), sources={'moltbook': {'account_id': uid(2)}})
                    self.assertEqual(len(Store(path).subscriptions()), int(active))
                    self.assertEqual(store.page()['messages'], saved)
                    with store.connect() as db:
                        self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], version)

    def test_alias_before_collection_and_invalid_operations_do_not_write(self):
        self.store.initialize()
        cfg = self.root / 'config.json'
        cfg.write_text(json.dumps({'database': 'unused.sqlite3', 'sources': {
            'research': {'adapter': 'postingboard', 'account_id': uid(1), 'api_key_file': 'unused.key', 'threads': []},
            'custom': {'adapter': 'does-not-exist.py', 'account_id': 'demo-agent'}}}))
        raw = cfg.read_bytes()
        result = self.cli('--config', cfg, 'subscribe', 'research', uid(100))
        self.assertEqual((result[0], result[1].get('changed')), (0, True), result)
        self.assertFalse((self.root / 'unused.sqlite3').exists())
        self.assertEqual(self.cli('subscribe', 'research', uid(100))[1]['changed'], False)
        self.assertEqual(self.cli('unsubscribe', 'research', uid(100))[1]['changed'], True)
        self.assertEqual(cfg.read_bytes(), raw)
        before = self.path.read_bytes()
        for args, expected in [
                (('subscribe', 'typo', uid(100)), 'source_not_found'),
                (('subscribe', 'research', 'https://example.invalid/thread'), 'invalid_thread_id'),
                (('--config', cfg, 'subscribe', 'custom', uid(100)), 'subscriptions_unsupported')]:
            code, result = self.cli(*args)
            self.assertEqual((code, result['error']), (2, expected))
            self.assertEqual(self.path.read_bytes(), before)
        for override, expected in [({'account_id': uid(99)}, 'account_mismatch'),
                                   ({'adapter': 'moltbook'}, 'adapter_mismatch')]:
            sources = config.load(cfg)['sources']
            sources['research'].update(override)
            with self.assertRaisesRegex(config.MailError, expected):
                commands.execute(self.store, 'subscribe', sources=sources, source='research', thread=uid(100))
            self.assertEqual(self.path.read_bytes(), before)

    def test_source_without_recorded_adapter_requires_config_and_recovers_before_collection(self):
        for source in ('colony', 'moltbook'):
            with self.subTest(source=source):
                self.path = self.root / f'{source}.sqlite3'
                self.store = Store(self.path)
                self.store.initialize()
                sources = {source: {'account_id': uid(1), 'adapter': 'the-colony',
                                    'api_key_file': str(self.root / 'unused.key')}}
                self.store.set_paused(source, True, sources[source])
                before = self.path.read_bytes()
                code, result = self.cli('subscribe', source, uid(100))
                self.assertEqual((code, result.get('error'), result.get('next_action')),
                                 (2, 'subscription_config_required', 'rerun_with_config_to_identify_source_adapter'))
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(self.store.subscriptions(), [])
                cfg = self.root / f'{source}.json'
                cfg.write_text(json.dumps({'database': str(self.path), 'sources': sources}))
                self.assertEqual(self.cli('--config', cfg, 'subscribe', source, uid(100))[0], 0)
                self.assertTrue(self.store.is_paused(source))
                self.assertFalse(self.cli('subscribe', source, uid(100))[1]['changed'])
                self.store.set_paused(source, False)
                with patch.object(providers, 'collect', return_value=Batch()) as collect:
                    self.assertFalse(collect_all(self.store, sources)['failed'])
                    self.assertEqual(collect.call_args.args[0], 'the-colony')
                    self.assertEqual(collect.call_args.args[1]['subscriptions'], [uid(100)])

    def test_every_builtin_gets_current_source_selections_and_pause_still_applies(self):
        sources = {f'board{n}': {'account_id': uid(n), 'adapter': adapter}
                   for n, adapter in enumerate(config.COVERAGE, 1)}
        original = deepcopy(sources)
        self.store.initialize(sources)
        for source in sources:
            self.store.set_subscription(source, uid(100), True)
        seen = []

        def collect(cfg, state, known):
            seen.append((cfg['adapter'], list(cfg['subscriptions'])))
            return Batch(state=state)

        with patch.object(providers, 'collect', side_effect=lambda adapter, cfg, state, known, **kw: collect(cfg, state, known)), \
                patch('boardmail.adapters.importlib.import_module', return_value=SimpleNamespace(API_VERSION=1, collect=collect)):
            self.assertFalse(collect_all(self.store, sources)['failed'])
            self.assertEqual(seen, [(adapter, [uid(100)]) for adapter in config.COVERAGE])
            seen.clear()
            self.store.set_subscription('board1', uid(100), False)
            self.store.set_subscription('board2', uid(101), True)
            self.store.set_paused('board3', True)
            self.assertFalse(collect_all(self.store, sources)['failed'])
        self.assertEqual(seen[:2], [('postingboard', []), ('the-colony', [uid(100), uid(101)])])
        self.assertEqual(len(seen), 5)
        self.assertNotIn('moltbook', [name for name, roots in seen])
        self.assertEqual(sources, original)

    def test_custom_adapter_keeps_its_own_settings(self):
        sources = {'example': {'account_id': 'demo-agent', 'adapter': '/unused.py', 'subscriptions': 'custom-option'}}
        self.store.initialize(sources)
        received = []
        def collect(cfg, state, known):
            received.append(dict(cfg))
            return Batch()
        with patch('boardmail.adapters.runpy.run_path', return_value={'API_VERSION': 1, 'collect': collect}):
            self.assertFalse(collect_all(self.store, sources)['failed'])
        self.assertEqual(received, [sources['example']])

    def test_unsubscribe_during_collection_keeps_pass_snapshot_without_resurrecting_selection(self):
        sources = {'moltbook': settings()['moltbook']}
        self.store.initialize(sources)
        self.store.set_subscription('moltbook', uid(100), True)
        entered, finish = threading.Event(), threading.Event()
        snapshots = []

        def collect(adapter, cfg, state, known, **kwargs):
            snapshots.append(list(cfg['subscriptions']))
            entered.set()
            if not finish.wait(5):
                raise RuntimeError('test did not release collector')
            return Batch(messages=[dict(mail(10), kind='thread_activity', addressing='thread')]
                         if cfg['subscriptions'] else [], state={'last_pass': 'saved'})

        with patch.object(providers, 'collect', side_effect=collect), ThreadPoolExecutor(1) as pool:
            running = pool.submit(collect_all, self.store, sources)
            try:
                self.assertTrue(entered.wait(5))
                Store(self.path).set_subscription('moltbook', uid(100), False)
            finally:
                finish.set()
            self.assertEqual(running.result(timeout=5)['added'], 1)
            self.assertEqual(self.store.subscriptions(), [])
            page, _ = commands.execute(self.store, 'list')
            self.assertEqual((page['messages'], page['thread_activity'][0]['count']), ([], 1))
            self.store.mark('moltbook', uid(10), 'read')
            saved = self.store.show('moltbook', uid(10))
            self.assertEqual(collect_all(self.store, sources)['added'], 0)
            self.store.set_subscription('moltbook', uid(100), True)
            self.assertEqual(collect_all(self.store, sources)['added'], 0)
            self.assertEqual(self.store.show('moltbook', uid(10)), saved)
        self.assertEqual(snapshots, [[uid(100)], [], [uid(100)]])


if __name__ == '__main__':
    unittest.main()
