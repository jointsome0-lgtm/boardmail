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

from boardmail import commands, config
from boardmail.boards import BOARDS, collect_all
from boardmail.store import Store
from examples.fixtures import FakeBoard, FixtureBoard, original, settings, together, uid
from kit import Clock, fixed, mark
from test_subscription_providers import ThreadBoard


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
        mark(store, 'example', '1', 'read')
        mark(store, 'example', '1', 'needs_reply')
        mark(store, 'example', '1', 'replied', ref='https://example.invalid/reply')
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
        commands.execute(self.store, 'init')
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
            commands.execute(self.store, 'resume', source='example', sources={'example': {'account_id': 'another-agent'}})
        self.assertTrue(self.store.is_paused('example'))

    def test_legacy_reads_do_not_migrate_and_pause_preserves_records_and_schema_version(self):
        for version in (1, 2):
            with self.subTest(version=version):
                path = self.root / f'v{version}.sqlite3'
                with closing(sqlite3.connect(path)) as db:
                    db.executescript((Path(__file__).parent / 'fixtures/v1.sql').read_text())
                store = Store(path)
                if version == 2:
                    commands.execute(store, 'collect', sources={})  # A pass over no source brings the file up to date.
                messages = store.page()['messages']
                raw = path.read_bytes()
                self.assertFalse(store.is_paused('moltbook'))
                self.assertFalse(store.status()['sources'][0]['paused'])
                self.assertEqual(path.read_bytes(), raw)
                commands.execute(store, 'pause', source='moltbook')
                self.assertEqual(store.page()['messages'], messages)
                self.assertTrue(Store(path).is_paused('moltbook'))
                commands.execute(store, 'resume', source='moltbook')
                self.assertEqual(store.page()['messages'], messages)

    def test_freshness_excludes_only_paused_sources_and_resume_restores_health(self):
        sources = settings(self.root)
        commands.execute(self.store, 'init', sources=sources)
        # Postingboard is paused before the first pass. In that pass the invented Moltbook answers and the
        # invented Colony is down.
        commands.execute(self.store, 'pause', source='postingboard')
        boards = {source: FixtureBoard(source, sources[source]) for source in ('moltbook', 'the-colony')}
        boards['the-colony'].fail = True
        result, code = commands.execute(self.store, 'collect', sources=sources, fetch=together(boards))
        self.assertEqual([(error['source'], error['error']) for error in result['errors']], [('the-colony', 'http_503')])
        commands.execute(self.store, 'pause', source='the-colony')
        result, code = commands.execute(self.store, 'status', require_fresh=True)
        self.assertEqual((code, result['fresh']), (0, True))
        commands.execute(self.store, 'pause', source='moltbook')
        self.assertEqual(commands.execute(self.store, 'status', require_fresh=True)[1], 0)
        commands.execute(self.store, 'resume', source='the-colony')
        result, code = commands.execute(self.store, 'status', require_fresh=True)
        colony = next(s for s in result['sources'] if s['source'] == 'the-colony')
        self.assertEqual((code, colony['status'], colony['error']), (1, 'error', 'http_503'))

    def test_inflight_pass_can_finish_but_cannot_clear_pause_or_start_later_paused_source(self):
        sources = {s: settings(self.root)[s] for s in ('moltbook', 'the-colony')}
        commands.execute(self.store, 'init', sources=sources)
        boards = {s: FixtureBoard(s, sources[s]) for s in sources}
        # What the same pass over Moltbook gives and keeps when nothing comes in between.
        alone = BOARDS['moltbook'].collect(sources['moltbook'], {}, set(), fetch=FixtureBoard('moltbook', sources['moltbook']))
        self.assertEqual((len(alone.messages), bool(alone.state)), (1, True))
        entered, finish = threading.Event(), threading.Event()
        get = boards['moltbook'].get

        def held(path, params=None, **kwargs):
            # The board holds the pass at its first request until the test lets it go.
            if not entered.is_set():
                entered.set()
                if not finish.wait(5):
                    raise RuntimeError('test did not release collector')
            return get(path, params, **kwargs)

        boards['moltbook'].get = held
        with ThreadPoolExecutor(1) as pool:
            running = pool.submit(collect_all, self.store, sources, fetch=together(boards))
            try:
                self.assertTrue(entered.wait(5))
                commands.execute(self.store, 'pause', source='moltbook')
                commands.execute(self.store, 'pause', source='the-colony')
            finally:
                finish.set()
            result = running.result(timeout=5)
            self.assertEqual((result['added'], result['failed']), (1, False))
        self.assertEqual(boards['the-colony'].asked, [])
        self.assertTrue(all(s['paused'] for s in self.store.status()['sources']))
        commands.execute(self.store, 'resume', source='moltbook')
        self.assertEqual(self.store.collection_state('moltbook', uid(2), 'moltbook')[1], alone.state)

    def test_paused_context_never_creates_a_remote_client(self):
        sources = {'the-colony': settings(self.root)['the-colony']}
        commands.execute(self.store, 'init', sources=sources)
        # The invented Colony has a comment under a post of the account. The source is paused after the pass.
        self.assertEqual(commands.execute(self.store, 'collect', sources=sources,
                                          fetch=FixtureBoard('the-colony', sources['the-colony']))[0]['added'], 2)
        commands.execute(self.store, 'pause', source='the-colony')
        before = self.path.read_bytes()
        board = FakeBoard([])  # It has no answer, and it is asked nothing.
        result, code = commands.execute(self.store, 'context', source='the-colony', id=uid(111),
                                        sources=sources, fetch=board)
        self.assertEqual(board.asked, [])
        self.assertFalse(result['fetched'])
        self.assertEqual(result['target']['message']['id'], uid(111))
        self.assertIsNone(result['target']['current_message'])
        self.assertIsNone(result['target']['differs_from_saved'])
        self.assertEqual(code, 1)  # The missing root remains explicitly unknown.
        self.assertEqual(self.path.read_bytes(), before)

    def test_builtin_pages_resume_after_pause_and_reopen_without_losing_marks(self):
        source, owner, root = 'the-colony', uid(1), uid(400)
        sources = {source: settings(self.root)[source]}
        commands.execute(self.store, 'init', sources=sources)
        commands.execute(self.store, 'subscribe', source=source, thread=root)
        # The invented Colony has one thread of three comments and gives two of them on a page.
        clock = Clock(1790000000)
        board = ThreadBoard(source, sources[source], clock)
        board.per_page, calls = 2, board.calls
        board.posts[root] = {**original(400, 400, colony=True), 'title': 'Synthetic subscribed discussion'}
        board.comments[root] = [original(411, 400, colony=True), original(412, 400, author=1, colony=True),
                                {**original(413, 400, colony=True), 'parent_id': uid(412)}]

        def progress(store):
            return store.collection_state(source, owner, source)

        with fixed(clock):
            board.fits = 2  # The time of the first pass is over after the root and one page.
            first = collect_all(self.store, sources, fetch=board)
            board.fits = None
            self.assertEqual((first['added'], first['failed']), (1, False))
            checkpoint = progress(self.store)
            root_progress = checkpoint[1]['subscriptions']['roots'][root]
            self.assertEqual(root_progress['page'], 2)
            self.assertTrue(root_progress['owners'][uid(412)])
            self.assertTrue(first['sources'][0]['backlog_pending'])
            for action in ('read', 'needs_reply'):
                mark(self.store, source, uid(411), action)
            mark(self.store, source, uid(411), 'replied', ref='https://example.invalid/reply')
            marked = self.store.show(source, uid(411))
            commands.execute(self.store, 'pause', source=source)
            reopened = Store(self.path)
            paused_calls = list(calls)
            paused = collect_all(reopened, sources, fetch=board)
            self.assertEqual((paused['added'], paused['failed']), (0, False))
            self.assertEqual(calls, paused_calls)
            self.assertEqual(progress(reopened), checkpoint)
            self.assertEqual(reopened.show(source, uid(411)), marked)
            self.assertTrue(reopened.is_paused(source))
            commands.execute(reopened, 'resume', source=source)
            resumed = collect_all(reopened, sources, fetch=board)
            self.assertEqual((resumed['added'], resumed['failed']), (1, False))
            self.assertFalse(resumed['sources'][0]['backlog_pending'])
            completed = progress(reopened)
            self.assertNotIn('page', completed[1]['subscriptions']['roots'][root])
            self.assertEqual(completed[2], checkpoint[2] + 1)
            self.assertEqual(reopened.show(source, uid(413))['addressing'], 'direct')
            self.assertEqual(reopened.show(source, uid(411)), marked)
            self.assertEqual([call[1]['page'] for call in calls if call[0].endswith('/comments')], [1, 2])
            # A later cycle revisits the head but does not duplicate old mail or marks.
            replay = collect_all(reopened, sources, fetch=board)
            self.assertEqual((replay['added'], replay['failed']), (0, False))
            self.assertEqual(reopened.show(source, uid(411)), marked)
            self.assertTrue(commands.execute(reopened, 'unsubscribe', source=source, thread=root)[0]['changed'])
            boundary = len(calls)
            unsubscribed = collect_all(Store(self.path), sources, fetch=board)
            self.assertEqual((unsubscribed['added'], unsubscribed['failed']), (0, False))
            self.assertFalse(any(path.startswith('/posts/') for path, _, _ in calls[boundary:]))
            self.assertNotIn('subscriptions', progress(reopened)[1])
            self.assertEqual(reopened.subscriptions(source), [])
            self.assertEqual(reopened.show(source, uid(411)), marked)
            self.assertEqual(progress(reopened)[0], {uid(411), uid(413)})


if __name__ == '__main__':
    unittest.main()
