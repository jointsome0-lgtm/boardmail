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

from boardmail import commands, config
from boardmail.adapters import Batch, collect_all
from boardmail.store import Store
from examples.fixtures import FixtureBoard, original, settings, together, uid
from kit import Clock, fixed
from test_mail import mail
from test_subscription_providers import ThreadBoard


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
        sources = {source: {'account_id': uid(n)} for n, source in enumerate(config.SUBSCRIPTION_ADAPTERS, 1)}
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

    def test_v1_subscription_rejects_adapter_rebind_before_migration(self):
        for earlier_alias in (False, True):
            with self.subTest(earlier_alias=earlier_alias):
                path = self.root / f'legacy-{earlier_alias}.sqlite3'
                with closing(sqlite3.connect(path)) as db:
                    db.executescript((Path(__file__).parent / 'fixtures/v1.sql').read_text())
                store = Store(path)
                if earlier_alias:
                    store.set_subscription('research', uid(200), True,
                                           {'account_id': uid(3), 'adapter': 'postingboard'})
                saved = store.page()['messages']
                with store.connect() as db:
                    source = dict(db.execute("SELECT * FROM sources WHERE source='moltbook'").fetchone())
                before = path.read_bytes()
                with self.assertRaisesRegex(config.MailError, '^adapter_mismatch$'):
                    commands.execute(store, 'subscribe', source='moltbook', thread=uid(100),
                                     sources={'moltbook': {'account_id': uid(2), 'adapter': 'the-colony'}})
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(store.page()['messages'], saved)
                self.assertEqual(store.subscriptions('moltbook'), [])

                self.assertTrue(store.set_subscription('moltbook', uid(100), True,
                                                       {'account_id': uid(2), 'adapter': 'moltbook'}))
                with store.connect() as db:
                    self.assertEqual(dict(db.execute("SELECT * FROM sources WHERE source='moltbook'").fetchone()), source)
                store.prepare_collection()
                known, state, revision = store.collection_state('moltbook', uid(2), 'moltbook')
                self.assertEqual((known, state, revision), ({uid(10), uid(11)}, {}, 0))
                self.assertEqual(store.page()['messages'], saved)
                self.assertEqual(store.subscriptions('moltbook')[0]['thread'], uid(100))

    def test_v1_new_source_alias_keeps_explicit_adapter_during_migration(self):
        for source in ('research', 'postingboard'):
            with self.subTest(source=source):
                path = self.root / f'new-alias-{source}.sqlite3'
                with closing(sqlite3.connect(path)) as db:
                    db.executescript((Path(__file__).parent / 'fixtures/v1.sql').read_text())
                store = Store(path)
                saved = store.page()['messages']
                self.assertTrue(store.set_subscription(source, uid(200), True,
                                                       {'account_id': uid(1), 'adapter': 'the-colony'}))
                self.assertFalse(store.set_subscription(source, uid(200), True))
                store.prepare_collection()
                self.assertEqual(store.collection_state(source, uid(1), 'the-colony'), (set(), {}, 0))
                self.assertEqual(store.adapter('moltbook'), 'moltbook')
                self.assertEqual(store.page()['messages'], saved)

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
                # The invented Colony is asked for the thread, under the adapter that the config names.
                cfg = {**sources[source], 'api_key_file': settings(self.root)['the-colony']['api_key_file']}
                board = ThreadBoard('the-colony', cfg)
                self.assertFalse(collect_all(self.store, {source: cfg}, fetch=board)['failed'])
                self.assertEqual([path for path, _, _ in board.calls], ['/agents/me', '/notifications', '/posts/' + uid(100)])

    def test_every_builtin_gets_current_source_selections_and_pause_still_applies(self):
        fused = ('postingboard', 'the-colony', 'moltbook')
        self.assertEqual(config.SUBSCRIPTION_ADAPTERS[:3], fused)
        key = settings(self.root)['moltbook']['api_key_file']
        sources = {f'board{n}': {'account_id': uid(n), 'adapter': adapter, **({'api_key_file': key} if adapter in fused else {})}
                   for n, adapter in enumerate(config.SUBSCRIPTION_ADAPTERS, 1)}
        before = deepcopy(sources)
        self.store.initialize(sources)
        for source in sources:
            self.store.set_subscription(source, uid(100), True)
        # Postingboard, Colony and Moltbook are invented boards: the threads that a board is asked for are the
        # ones that its collector was handed. Each other board gets a collector that notes what it is handed.
        boards = {cfg['adapter']: (FixtureBoard if cfg['adapter'] == 'postingboard' else ThreadBoard)(cfg['adapter'], cfg)
                  for cfg in sources.values() if cfg['adapter'] in fused}
        seen = []

        def collect(cfg, state, known, **asks):
            seen.append((cfg['adapter'], list(cfg['subscriptions'])))
            return Batch(state=state)

        def threads(adapter):
            """The threads that the invented board was asked for since the last look."""
            prefix = '/v1/posts/' if adapter == 'postingboard' else '/posts/'
            asked = [path.removeprefix(prefix) for path, _, _ in boards[adapter].calls if path.startswith(prefix)]
            boards[adapter].calls.clear()
            return asked

        with fixed(Clock(1790000000)), \
                patch('boardmail.adapters.importlib.import_module', return_value=SimpleNamespace(API_VERSION=1, collect=collect)):
            self.assertFalse(collect_all(self.store, sources, fetch=together(boards))['failed'])
            self.assertEqual([threads(adapter) for adapter in fused], [[uid(100)]] * 3)
            self.assertEqual(seen, [(adapter, [uid(100)]) for adapter in config.SUBSCRIPTION_ADAPTERS[3:]])
            seen.clear()
            self.store.set_subscription('board1', uid(100), False)
            self.store.set_subscription('board2', uid(101), True)
            self.store.set_paused('board3', True)
            self.assertFalse(collect_all(self.store, sources, fetch=together(boards))['failed'])
            self.assertEqual([threads(adapter) for adapter in fused[:2]], [[], [uid(100), uid(101)]])
        self.assertEqual(boards['moltbook'].calls, [])
        self.assertEqual(seen, [(adapter, [uid(100)]) for adapter in config.SUBSCRIPTION_ADAPTERS[3:]])
        self.assertEqual(sources, before)

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
        sources = {'moltbook': settings(self.root)['moltbook']}
        self.store.initialize(sources)
        self.store.set_subscription('moltbook', uid(100), True)
        # The invented Moltbook has the thread, with one comment of another account under it.
        board = ThreadBoard('moltbook', sources['moltbook'])
        board.posts[uid(100)] = {**original(100, 100), 'title': 'Subscribed thread'}
        board.comments[uid(100)] = [original(10, 100)]
        entered, finish = threading.Event(), threading.Event()
        get = board.get

        def held(path, params=None, **kwargs):
            # The board holds the first pass at its first request until the test lets it go.
            if not entered.is_set():
                entered.set()
                if not finish.wait(5):
                    raise RuntimeError('test did not release collector')
            return get(path, params, **kwargs)

        def snapshots():
            """For each pass, the threads that it asked the board for."""
            passes = []
            for path, _, _ in board.calls:
                if path == '/agents/me': passes.append([])
                elif path.startswith('/posts/') and not path.endswith('/comments'): passes[-1].append(path.removeprefix('/posts/'))
            return passes

        board.get = held
        with ThreadPoolExecutor(1) as pool:
            running = pool.submit(collect_all, self.store, sources, fetch=board)
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
            self.assertEqual(collect_all(self.store, sources, fetch=board)['added'], 0)
            self.store.set_subscription('moltbook', uid(100), True)
            self.assertEqual(collect_all(self.store, sources, fetch=board)['added'], 0)
            self.assertEqual(self.store.show('moltbook', uid(10)), saved)
        self.assertEqual(snapshots(), [[uid(100)], [], [uid(100)]])


if __name__ == '__main__':
    unittest.main()
