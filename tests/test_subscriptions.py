"""Local selections and collection boundaries; all mail and adapters are invented."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from boardmail import commands, config
from boardmail.adapters import Batch
from boardmail.boards import BOARDS, collect_all
from boardmail.store import Store
from examples.fixtures import FixtureBoard, original, settings, together, uid
from kit import Clock, fixed, mark
from test_subscription_providers import ThreadBoard

# The boards of the package that take a subscription to a thread.
SUBSCRIBING = tuple(name for name, about in BOARDS.items() if about.subscriptions)


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

    def follow(self, store, source, thread, settings=None, *, subscribed=True):
        """The command subscribe or unsubscribe, with the settings of the source if the call has a config. Whether
        it changed anything."""
        result, _ = commands.execute(store, 'subscribe' if subscribed else 'unsubscribe', source=source, thread=thread,
                                     sources={source: settings} if settings else None)
        return result['changed']

    def test_all_boards_cli_persistence_idempotence_and_saved_marks(self):
        sources = {source: {'account_id': uid(n)} for n, source in enumerate(SUBSCRIBING, 1)}
        commands.execute(self.store, 'init', sources=sources)
        # The invented Postingboard has mail for the account before any thread is selected. Its client waits
        # between two requests, so the clock is fixed for a pass and a wait only moves it.
        watching = {'postingboard': {**settings(self.root)['postingboard'], **sources['postingboard']}}
        board, clock = FixtureBoard('postingboard', watching['postingboard']), Clock(1790000000)
        with fixed(clock):
            self.assertEqual(collect_all(self.store, watching, fetch=board)['failed'], False)
        message = self.store.page()['messages'][0]['id']
        for action in ('read', 'needs_reply'):
            mark(self.store, 'postingboard', message, action)
        mark(self.store, 'postingboard', message, 'replied', ref='https://example.invalid/reply')
        saved = self.store.show('postingboard', message)
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
        self.assertEqual(self.store.show('postingboard', message), saved)
        self.cli('subscribe', 'postingboard', thread)
        with fixed(clock):
            again = collect_all(self.store, watching, fetch=board)
        self.assertEqual((again['added'], again['failed']), (0, False))
        self.assertIn('/v1/posts/' + thread, [path for path, _, _ in board.calls])
        self.assertEqual(self.store.show('postingboard', message), saved)

    def test_an_older_file_has_no_selection_and_an_explicit_one_keeps_its_mail(self):
        for version in (1, 2):
            with self.subTest(version=version):
                path = self.root / f'v{version}.sqlite3'
                with closing(sqlite3.connect(path)) as db:
                    db.executescript((Path(__file__).parent / 'fixtures/v1.sql').read_text())
                store = Store(path)
                if version == 2:
                    store.status()  # The first command that opens the file gives it every part.
                    # No command takes the table away again. The file has lost it somewhere else.
                    with closing(sqlite3.connect(path)) as db, db:
                        db.execute('DROP TABLE subscriptions')
                saved = store.page()['messages']
                before = path.read_bytes()
                self.assertEqual(store.subscriptions(), [])
                self.assertEqual(store.status()['subscriptions'], [])
                self.assertEqual(path.read_bytes(), before)
                if version == 1:
                    # Version 1 knew a source by the name of its board, so the file says which adapter reads this
                    # one and no config has to.
                    self.assertTrue(self.follow(store, 'moltbook', uid(100)))
                for active in (False, True, True, False):
                    commands.execute(store, 'subscribe' if active else 'unsubscribe', source='moltbook',
                                     thread=uid(100), sources={'moltbook': {'account_id': uid(2)}})
                    self.assertEqual(len(Store(path).subscriptions()), int(active))
                    self.assertEqual(store.page()['messages'], saved)

    def test_alias_before_collection_and_invalid_operations_do_not_write(self):
        commands.execute(self.store, 'init')
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

    def test_a_source_of_version_1_cannot_be_bound_to_another_adapter(self):
        for earlier_alias in (False, True):
            with self.subTest(earlier_alias=earlier_alias):
                path = self.root / f'legacy-{earlier_alias}.sqlite3'
                with closing(sqlite3.connect(path)) as db:
                    db.executescript((Path(__file__).parent / 'fixtures/v1.sql').read_text())
                store = Store(path)
                if earlier_alias:
                    self.follow(store, 'research', uid(200), {'account_id': uid(3), 'adapter': 'postingboard'})
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

                self.assertTrue(self.follow(store, 'moltbook', uid(100), {'account_id': uid(2), 'adapter': 'moltbook'}))
                with store.connect() as db:
                    self.assertEqual(dict(db.execute("SELECT * FROM sources WHERE source='moltbook'").fetchone()), source)
                known, state, revision = store.collection_state('moltbook', uid(2), 'moltbook')
                self.assertEqual((known, state, revision), ({uid(10), uid(11)}, {}, 0))
                self.assertEqual(store.page()['messages'], saved)
                self.assertEqual(store.subscriptions('moltbook')[0]['thread'], uid(100))

    def test_a_new_source_of_a_version_1_file_keeps_the_adapter_that_its_config_names(self):
        for source in ('research', 'postingboard'):
            with self.subTest(source=source):
                path = self.root / f'new-alias-{source}.sqlite3'
                with closing(sqlite3.connect(path)) as db:
                    db.executescript((Path(__file__).parent / 'fixtures/v1.sql').read_text())
                store = Store(path)
                saved = store.page()['messages']
                self.assertTrue(self.follow(store, source, uid(200), {'account_id': uid(1), 'adapter': 'the-colony'}))
                self.assertFalse(self.follow(store, source, uid(200)))
                self.assertEqual(store.collection_state(source, uid(1), 'the-colony'), (set(), {}, 0))
                self.assertEqual(store.adapter('moltbook'), 'moltbook')
                self.assertEqual(store.page()['messages'], saved)

    def test_source_without_recorded_adapter_requires_config_and_recovers_before_collection(self):
        for source in ('colony', 'moltbook'):
            with self.subTest(source=source):
                self.path = self.root / f'{source}.sqlite3'
                self.store = Store(self.path)
                commands.execute(self.store, 'init')
                sources = {source: {'account_id': uid(1), 'adapter': 'the-colony',
                                    'api_key_file': str(self.root / 'unused.key')}}
                commands.execute(self.store, 'pause', source=source, sources=sources)
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
                commands.execute(self.store, 'resume', source=source)
                # The invented Colony is asked for the thread, under the adapter that the config names.
                cfg = {**sources[source], 'api_key_file': settings(self.root)['the-colony']['api_key_file']}
                board = ThreadBoard('the-colony', cfg)
                self.assertFalse(collect_all(self.store, {source: cfg}, fetch=board)['failed'])
                self.assertEqual([path for path, _, _ in board.calls], ['/agents/me', '/notifications', '/posts/' + uid(100)])

    def test_every_builtin_gets_current_source_selections_and_pause_still_applies(self):
        fused = ('postingboard', 'the-colony', 'moltbook')
        self.assertEqual(SUBSCRIBING[:3], fused)
        key = settings(self.root)['moltbook']['api_key_file']
        sources = {f'board{n}': {'account_id': uid(n), 'adapter': adapter, **({'api_key_file': key} if adapter in fused else {})}
                   for n, adapter in enumerate(SUBSCRIBING, 1)}
        before = deepcopy(sources)
        commands.execute(self.store, 'init', sources=sources)
        for source in sources:
            self.follow(self.store, source, uid(100))
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

        with fixed(Clock(1790000000)), patch.dict(BOARDS, {name: replace(BOARDS[name], collect=collect)
                                                           for name in SUBSCRIBING[3:]}):
            self.assertFalse(collect_all(self.store, sources, fetch=together(boards))['failed'])
            self.assertEqual([threads(adapter) for adapter in fused], [[uid(100)]] * 3)
            self.assertEqual(seen, [(adapter, [uid(100)]) for adapter in SUBSCRIBING[3:]])
            seen.clear()
            self.follow(self.store, 'board1', uid(100), subscribed=False)
            self.follow(self.store, 'board2', uid(101))
            commands.execute(self.store, 'pause', source='board3')
            self.assertFalse(collect_all(self.store, sources, fetch=together(boards))['failed'])
            self.assertEqual([threads(adapter) for adapter in fused[:2]], [[], [uid(100), uid(101)]])
        self.assertEqual(boards['moltbook'].calls, [])
        self.assertEqual(seen, [(adapter, [uid(100)]) for adapter in SUBSCRIBING[3:]])
        self.assertEqual(sources, before)

    def test_custom_adapter_keeps_its_own_settings(self):
        # The adapter file of an operator. It keeps what it is handed as its progress.
        adapter = self.root / 'keeps.py'
        adapter.write_text('from boardmail.adapters import Batch\nAPI_VERSION = 1\n\n\n'
                           'def collect(settings, state, known):\n    return Batch(state={"handed": settings})\n')
        sources = {'example': {'account_id': 'demo-agent', 'adapter': str(adapter), 'subscriptions': 'custom-option'}}
        commands.execute(self.store, 'init', sources=sources)
        self.assertFalse(collect_all(self.store, sources)['failed'])
        self.assertEqual(self.store.collection_state('example', 'demo-agent', str(adapter))[1],
                         {'handed': sources['example']})

    def test_unsubscribe_during_collection_keeps_pass_snapshot_without_resurrecting_selection(self):
        sources = {'moltbook': settings(self.root)['moltbook']}
        commands.execute(self.store, 'init', sources=sources)
        self.follow(self.store, 'moltbook', uid(100))
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
                self.follow(Store(self.path), 'moltbook', uid(100), subscribed=False)
            finally:
                finish.set()
            self.assertEqual(running.result(timeout=5)['added'], 1)
            self.assertEqual(self.store.subscriptions(), [])
            page, _ = commands.execute(self.store, 'list')
            self.assertEqual((page['messages'], page['thread_activity'][0]['count']), ([], 1))
            mark(self.store, 'moltbook', uid(10), 'read')
            saved = self.store.show('moltbook', uid(10))
            self.assertEqual(collect_all(self.store, sources, fetch=board)['added'], 0)
            self.follow(self.store, 'moltbook', uid(100))
            self.assertEqual(collect_all(self.store, sources, fetch=board)['added'], 0)
            self.assertEqual(self.store.show('moltbook', uid(10)), saved)
        self.assertEqual(snapshots(), [[uid(100)], [], [uid(100)]])


if __name__ == '__main__':
    unittest.main()
