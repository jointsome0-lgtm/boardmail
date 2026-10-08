"""Bounded thread expansion: one client, one budget, saved rows only. All data is invented."""
from contextlib import redirect_stdout
from http.client import HTTPException
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from urllib.error import URLError

from boardmail import cli, commands, reader
from boardmail.config import MailError
from boardmail.boards import BOARDS
from boardmail import adapter_postingboard as postingboard
from examples.fixtures import FakeBoard, FixtureBoard, named, original, settings, uid
from kit import Clock, Network, edge, fixed, mark, new_inbox
from test_clawdchat import Board as ClawdChat, event as clawd_event, key_file as clawd_key_file, original as clawd_original
from test_fourclaw import THREAD, page, threads


class ExpandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'mail.sqlite3'
        self.cfg = settings(self.temp.name)['postingboard']
        self.sources = {'postingboard': self.cfg}
        self.store = new_inbox(self.path, self.sources)
        self.fixture = FixtureBoard('postingboard', self.cfg)
        self.clock = Clock(1790000000)
        current = {uid(600): named(600, 600), uid(601): named(601, 600, 3, body='Our answer.'),
                   uid(602): named(602, 600, reply_to=601, body='Edited follow-up.'),
                   uid(603): named(603, 600, reply_to=601), uid(604): named(604, 600, reply_to=600),
                   uid(605): named(605, 600, reply_to=600)}
        # The mail arrives with the text that the posts had then, in the order of their age: the root, two replies to
        # our answer, a reply to the root, a post of another thread.
        self.fixture.others = {**{mid: {**post, 'body': 'Synthetic text'} for mid, post in current.items()},
                               uid(601): current[uid(601)], uid(611): named(611, 100, body='Synthetic text')}
        self.arrives(600, 602, 603, 604, 611)
        # The posts have another text now, and the post of the other thread is gone.
        self.fixture.others = current
        mark(self.store, 'postingboard', uid(611), 'replied', ref=postingboard.HOST + '/v1/posts/' + uid(601))

    def arrives(self, *numbers):
        """A pass in which the inbox of the invented Postingboard tells the account of these posts, which become
        its mail. The pass watches no thread."""
        self.fixture.inbox += [(len(self.fixture.inbox) + 1, self.fixture.others[uid(n)], ['mention']) for n in numbers]
        with fixed(self.clock):
            result, code = commands.execute(self.store, 'collect', fetch=self.fixture,
                                            sources={'postingboard': {**self.cfg, 'inbox': True, 'threads': []}})
        self.assertEqual((code, result['added'], result['failed']), (0, len(numbers), False), result)
        self.fixture.calls.clear()

    def expand(self, thread=600, *, through=5, after=0, limit=None, local=False, sources='configured', board=None):
        sources = self.sources if sources == 'configured' else sources
        args = {} if limit is None else {'limit': limit}
        # The client waits between two requests to Postingboard. With the clock fixed, the wait only moves it.
        with fixed(self.clock):
            return commands.execute(self.store, 'expand', source='postingboard', thread=uid(thread), through=through,
                                    after=after, local=local, sources=sources, fetch=board or self.fixture, **args)

    def paths(self):
        return [path for path, _, _ in self.fixture.calls]

    def test_bounded_page_reads_each_original_once_and_ignores_later_arrivals_and_marks(self):
        before = self.path.read_bytes()
        result, code = self.expand(limit=2)
        self.assertEqual((code, result['event'], result['complete'], result['fetched']), (0, 'expanded', True, True))
        self.assertEqual((result['after'], result['through'], result['next_after'], result['more']), (0, 5, 2, True))
        self.assertEqual((result['checkpoint_safe'], result['collection_performed'], result['budget_exhausted'], result['next_action']),
                         (False, False, False, 'process_filtered_page_keep_delivery_checkpoint'))
        self.assertEqual([(i['id'], i['arrival_seq'], i['complete']) for i in result['items']], [(uid(600), 1, True), (uid(602), 2, True)])
        root, first, second = result['root'], *result['items']
        self.assertEqual((root['status'], root['remote_status'], root['origin'], root['message']['body']), ('available', 'available', 'local', 'Synthetic text'))
        self.assertEqual((first['target'], first['parent']['status'], first['previous_exchange']['status']), (root, 'none', 'none'))
        self.assertEqual((second['target']['message']['body'], second['target']['current_message']['body'], second['target']['differs_from_saved']),
                         ('Synthetic text', 'Edited follow-up.', True))
        self.assertEqual((second['parent']['origin'], second['parent']['message']['body']), ('remote', 'Our answer.'))
        self.assertEqual((second['previous_exchange']['status'], [m['id'] for m in second['previous_exchange']['messages']]), ('linked', [uid(611)]))
        self.assertEqual(self.paths(), ['/v1/posts/' + uid(n) for n in (600, 602, 601)])
        self.assertEqual(self.path.read_bytes(), before)

        # New arrivals and marks after the summary never enter the interval; continuation keeps through.
        self.arrives(605)
        mark(self.store, 'postingboard', uid(603), 'read')
        self.fixture.calls.clear()
        result, code = self.expand(after=2)
        self.assertEqual((code, [i['id'] for i in result['items']], result['next_after'], result['more']), (0, [uid(603), uid(604)], 4, False))
        third, fourth = result['items']
        self.assertIsNotNone(third['target']['message']['read_at'])
        self.assertEqual(third['parent']['message']['body'], 'Our answer.')
        self.assertEqual(fourth['parent'], {'id': uid(600), 'status': 'same_as_root'})
        self.assertEqual(fourth['previous_exchange']['reply_ref'], postingboard.HOST + '/v1/posts/' + uid(600))
        self.assertEqual(self.paths(), ['/v1/posts/' + uid(n) for n in (600, 603, 601, 604)])
        # The whole interval in one page: root and the shared parent are still single requests.
        self.fixture.calls.clear()
        result, code = self.expand(limit=100)
        self.assertEqual((code, [i['id'] for i in result['items']]), (0, [uid(600), uid(602), uid(603), uid(604)]))
        self.assertEqual(self.paths(), ['/v1/posts/' + uid(n) for n in (600, 602, 601, 603, 604)])
        self.assertEqual(self.expand(after=5, through=5)[0]['items'], [])

    def test_missing_deleted_or_relocated_current_originals_make_the_page_incomplete(self):
        del self.fixture.others[uid(603)]
        result, code = self.expand()
        self.assertEqual((code, result['complete'], result['budget_exhausted']), (1, False, False))
        third = result['items'][2]
        self.assertEqual((third['target']['status'], third['target']['remote_status'], third['target']['error'], third['target']['message']['body']),
                         ('available', 'missing', 'http_404', 'Synthetic text'))
        self.assertEqual([i['complete'] for i in result['items']], [True, True, False, True])
        # Singular context keeps its established meaning: a saved target is complete context.
        with fixed(self.clock):
            context, code = commands.execute(self.store, 'context', source='postingboard', id=uid(603),
                                             sources=self.sources, fetch=self.fixture)
        self.assertEqual((code, context['complete'], context['target']['remote_status']), (0, True, 'missing'))
        # A root that is gone counts before its parent references are collapsed.
        self.fixture.others[uid(603)] = named(603, 600, reply_to=601)
        del self.fixture.others[uid(600)]
        result, code = self.expand()
        self.assertEqual((code, result['root']['remote_status'], result['root']['message']['id']), (1, 'missing', uid(600)))
        self.assertEqual([i['complete'] for i in result['items']], [False, False, False, False])
        self.assertEqual(result['items'][3]['parent'], {'id': uid(600), 'status': 'same_as_root'})
        # An original now claiming another thread never attaches that thread's root.
        self.fixture.others[uid(600)] = named(600, 600)
        self.fixture.others[uid(700)] = named(700, 700)
        self.fixture.others[uid(602)].update(root_id=uid(700), thread_id=uid(700), reply_to_id=None)
        result, code = self.expand()
        second = result['items'][1]
        self.assertEqual((code, result['root']['id'], second['complete']), (1, uid(600), False))
        self.assertEqual((second['target']['remote_status'], second['target']['error'], second['target']['message']['thread_id']),
                         ('unavailable', 'invalid_response', uid(600)))
        self.assertEqual(second['parent']['message']['body'], 'Our answer.')
        self.assertNotIn('/v1/posts/' + uid(700), self.paths())

    def test_exhausted_budget_stops_further_requests_and_keeps_saved_text(self):
        get = self.fixture.get
        def slow(path, params=None, **kwargs):
            # The command has 45 seconds. The second answer takes 43 of them, so one more request goes out, and
            # after the wait before the fourth the time is over.
            if path.endswith(uid(602)): self.clock.advance(43)
            return get(path, params, **kwargs)
        self.fixture.get = slow
        result, code = self.expand()
        self.assertEqual((code, result['complete'], result['budget_exhausted'], result['fetched']), (1, False, True, True))
        self.assertEqual([i['complete'] for i in result['items']], [True, True, False, False])
        third = result['items'][2]
        self.assertEqual((third['target']['status'], third['target']['remote_status'], third['target']['error'], third['target']['message']['body']),
                         ('available', 'unavailable', 'budget_exhausted', 'Synthetic text'))
        # The shared parent was already read for the previous item; the spent budget costs nothing more.
        self.assertEqual((third['parent']['status'], third['parent']['message']['body']), ('available', 'Our answer.'))
        fourth = result['items'][3]
        self.assertEqual((fourth['target']['error'], fourth['parent']), ('budget_exhausted', {'id': uid(600), 'status': 'same_as_root'}))
        self.assertEqual(self.paths(), ['/v1/posts/' + uid(n) for n in (600, 602, 601)])
        self.fixture.get = get
        self.fixture.fail = True
        self.fixture.calls.clear()
        result, code = self.expand()
        self.assertEqual((code, result['complete'], result['budget_exhausted']), (1, False, False))
        self.assertEqual((result['root']['remote_status'], result['root']['error'], result['root']['message']['id']), ('unavailable', 'http_503', uid(600)))
        self.assertEqual({i['target']['error'] for i in result['items']}, {'http_503'})
        self.assertEqual(len(self.fixture.calls), 5)

    def test_local_paused_unconfigured_and_unsupported_reads_never_build_a_client(self):
        # On 4claw the opening post of a thread of another account names this account, and is its mail.
        fourclaw = {'fourclaw': {'adapter': 'fourclaw', 'account_id': 'demo', 'watched_threads': [THREAD]}}
        result, code = commands.execute(self.store, 'collect', sources=fourclaw,
                                        fetch=threads({THREAD: page(opening='@demo an opening.')}))
        self.assertEqual((code, result['added']), (0, 1), result)
        board = FakeBoard([])  # It has no answer, and it is asked nothing.
        cases = [('local', dict(local=True)), ('unconfigured', dict(sources=None)), ('other source', dict(sources={'moltbook': settings()['moltbook']}))]
        for name, args in cases:
            with self.subTest(case=name):
                result, code = self.expand(board=board, **args)
                self.assertEqual((code, result['fetched'], result['complete'], result['root']['origin']), (1, False, False, 'local'))
                self.assertNotIn('remote_status', result['root'])
                self.assertEqual([i['parent']['status'] for i in result['items']], ['none', 'unknown', 'unknown', 'same_as_root'])
                self.assertEqual([i['complete'] for i in result['items']], [True, False, False, True])
                # A recorded parent identity still links the exchange although the parent text is unknown.
                self.assertEqual((result['items'][1]['previous_exchange']['status'], result['items'][1]['parent']['id']), ('linked', uid(601)))
        commands.execute(self.store, 'pause', source='postingboard')
        result, code = self.expand(board=board)
        self.assertEqual((code, result['fetched'], result['items'][3]['complete']), (1, False, True))
        commands.execute(self.store, 'resume', source='postingboard')
        result, code = commands.execute(self.store, 'expand', source='fourclaw', thread=THREAD, through=10,
                                        sources=fourclaw, fetch=board)
        self.assertEqual((code, result['fetched'], result['complete'], result['items'][0]['target']['origin']), (0, False, True, 'local'))
        self.assertEqual(board.asked, [])

    def test_failed_shared_parent_is_attempted_once_and_can_be_retried_next_operation(self):
        get = self.fixture.get
        for error in (URLError('synthetic outage'), OSError('synthetic timeout'),
                      HTTPException('synthetic failure'), ValueError('invalid JSON'), MailError('network_error')):
            with self.subTest(error=type(error).__name__):
                attempts = []
                def failing(path, params=None, **kwargs):
                    attempts.append(path)
                    if path == '/v1/posts/' + uid(601):
                        raise error
                    return get(path, params, **kwargs)
                self.fixture.get = failing
                result, code = self.expand()
                self.assertEqual(attempts.count('/v1/posts/' + uid(601)), 1)
                self.assertEqual((code, result['complete'], result['budget_exhausted']), (1, False, False))
                self.assertEqual([item['complete'] for item in result['items']], [True, False, False, True])
                self.assertEqual(result['items'][1]['target']['message']['body'], 'Synthetic text')
        self.fixture.get = get
        result, code = self.expand()
        self.assertEqual((code, result['complete']), (0, True))

    def test_empty_interval_and_invalid_arguments_touch_no_board(self):
        before = self.path.read_bytes()
        board = FakeBoard([])  # It has no answer, and it is asked nothing.
        result, code = commands.execute(self.store, 'expand', source='postingboard', thread=uid(600), through=5, after=5,
                                        sources=self.sources, fetch=board)
        self.assertEqual((code, result['items'], result['fetched'], result['complete'], result['budget_exhausted']), (0, [], True, True, False))
        self.assertEqual((result['root']['id'], result['root']['status'], result['next_after'], result['more']), (uid(600), 'unknown', 5, False))
        result, code = commands.execute(self.store, 'expand', source='postingboard', thread=uid(999), through=5,
                                        sources=self.sources, fetch=board)
        self.assertEqual((code, result['items'], result['root']['status']), (0, [], 'unknown'))
        for args in (dict(through=None), dict(through=3, after=4), dict(through=5, limit=0), dict(through=5, limit=101),
                     dict(through=5, limit=True), dict(through=5.0), dict(through=2**63), dict(through=5, thread='not-a-uuid'),
                     dict(through=5, thread=None), dict(through=5, thread='x\x00y'), dict(through=5, source=None)):
            with self.subTest(args=args):
                call = {'source': 'postingboard', 'thread': uid(600), **args}
                with self.assertRaisesRegex(MailError, '^invalid_arguments$'):
                    commands.execute(self.store, 'expand', sources=self.sources, fetch=board, **call)
        self.assertEqual(board.asked, [])
        self.assertEqual(self.path.read_bytes(), before)

    def test_cli_expands_with_config_and_reads_offline_with_db_alone(self):
        root = Path(self.temp.name)  # The key of the account is there already, in example.key.
        (root/'config.json').write_text(json.dumps({'database': 'other.sqlite3', 'sources': {'postingboard': {
            'account_id': self.cfg['account_id'], 'api_key_file': 'example.key', 'threads': [uid(600)]}}}))
        before = self.path.read_bytes()
        def run(*args):
            out = io.StringIO()
            # The command line hands no board in. The invented one stands where a request leaves the process.
            with Network({'getpostingboard.dev': edge(self.fixture)}), fixed(self.clock), redirect_stdout(out):
                code = cli.main(['--config', str(root/'config.json'), '--db', str(self.path), 'expand', 'postingboard', uid(600), *args])
            return code, json.loads(out.getvalue())
        code, result = run('--through', '5', '--after', '1', '--limit', '2')
        self.assertEqual((code, result['event'], result['fetched'], [i['id'] for i in result['items']], result['more']),
                         (0, 'expanded', True, [uid(602), uid(603)], True))
        self.assertEqual(self.paths(), ['/v1/posts/' + uid(n) for n in (600, 602, 601, 603)])
        code, result = run('--through', '5', '--local')
        self.assertEqual((code, result['fetched'], len(result['items'])), (1, False, 4))
        for args in (('--after', '1'), ('--through', '5', '--limit', '0'), ('--through', '3', '--after', '4')):
            code, result = run(*args)
            self.assertEqual((code, result['error']), (2, 'invalid_arguments'))
        self.assertFalse((root/'other.sqlite3').exists())
        command = [sys.executable, '-m', 'boardmail', '--db', str(self.path), 'expand', 'postingboard', uid(600), '--through', '5']
        run = subprocess.run(command, capture_output=True, text=True, timeout=10)
        result = json.loads(run.stdout)
        self.assertEqual((run.returncode, result['fetched'], result['items'][3]['parent']), (1, False, {'id': uid(600), 'status': 'same_as_root'}))
        self.assertEqual(self.path.read_bytes(), before)


class ExpandReuseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'mail.sqlite3'
        self.store = new_inbox(self.path)

    def clawd_arrives(self, source, *comments, ours=()):
        """One pass over an invented ClawdChat that tells the account of these comments, which become its mail.
        The board also has the comments of the account itself."""
        board = ClawdChat()
        board.originals = {comment['id']: comment for comment in (*comments, *ours)}
        board.events = [{**clawd_event(0, 'reply' if comment['parent_id'] else 'comment'), 'id': uid(5000 + n),
                         'post_id': comment['post_id'], 'comment_id': comment['id']} for n, comment in enumerate(comments)]
        cfg = {'account_id': uid(1), 'adapter': 'clawdchat', 'api_key_file': clawd_key_file(self)}
        result, code = commands.execute(self.store, 'collect', sources={source: cfg}, fetch=board)
        self.assertEqual((code, result['added'], result['failed']), (0, len(comments), False), result)

    def test_conflicting_saved_relatives_require_local_integrity_or_current_originals(self):
        cfg = {'account_id': uid(1), 'adapter': 'clawdchat'}
        # Two comments of a thread name as what they answer a comment that arrived under another post.
        elsewhere = {'post_id': uid(999), 'post': {'id': uid(999), 'title': 'Elsewhere'}}
        self.clawd_arrives('clawdchat', clawd_original(100, **elsewhere), clawd_original(11, **elsewhere),
                           clawd_original(12, parent_id=uid(100)), clawd_original(13, parent_id=uid(11)))
        mark(self.store, 'clawdchat', uid(12), 'read')
        mark(self.store, 'clawdchat', uid(13), 'needs_reply')
        saved = {mid: self.store.show('clawdchat', mid) for mid in (uid(12), uid(13))}
        before = self.path.read_bytes()
        board = ClawdChat()
        board.originals = {uid(100): clawd_original(100, title='Current thread'),
                           uid(11): clawd_original(11, content='Current parent.'),
                           uid(12): clawd_original(12, parent_id=uid(100)),
                           uid(13): clawd_original(13, parent_id=uid(11))}
        for mode in ('local', 'unavailable', 'current'):
            with self.subTest(mode=mode):
                board.down = None if mode == 'current' else 503
                board.asked.clear()
                result, code = commands.execute(self.store, 'expand', source='clawdchat', thread=uid(100), through=4,
                                                sources={'clawdchat': cfg}, local=mode == 'local', fetch=board)
                self.assertEqual(bool(board.asked), mode != 'local')
                self.assertEqual([item['id'] for item in result['items']], [uid(12), uid(13)])
                self.assertEqual((code, result['complete']), (0, True) if mode == 'current' else (1, False))
                for item in result['items']:
                    self.assertEqual(item['target']['message'], reader.written(saved[item['id']]))
                    self.assertEqual(item['complete'], mode == 'current')
                self.assertEqual(result['items'][0]['parent'], {'id': uid(100), 'status': 'same_as_root'})
                if mode == 'current':
                    self.assertEqual(result['root']['origin'], 'remote')
                    self.assertEqual(result['root']['message']['thread_id'], uid(100))
                    self.assertEqual(result['items'][1]['parent']['message']['body'], 'Current parent.')
                else:
                    self.assertEqual((result['root']['status'], result['root']['error'], result['root']['message']),
                                     ('unavailable', 'invalid_response', None))
                    self.assertEqual((result['items'][1]['parent']['status'], result['items'][1]['parent']['error']),
                                     ('unavailable', 'invalid_response'))
                self.assertFalse(result['checkpoint_safe'])
                self.assertEqual(self.path.read_bytes(), before)

    def test_moltbook_comment_pages_are_read_once_for_every_target_and_parent(self):
        cfg = {**settings(self.temp.name)['moltbook'], 'adapter': 'moltbook'}
        client = FixtureBoard('moltbook', cfg)
        # A comment under the post of the account, the answer of the account to it, a reply to that answer, and one
        # more comment. The three of others are the mail of the account.
        client.comments = [original(209, 201), original(211, 201, 1, body='Our published answer.'),
                           {**original(212, 201, body='Saved reply.'), 'parent_id': uid(211)}, original(213, 201)]
        client.events = [{'id': uid(n + 1000), 'type': kind, 'relatedPostId': uid(201), 'relatedCommentId': uid(n), 'isRead': True}
                         for n, kind in ((209, 'post_comment'), (212, 'comment_reply'), (213, 'post_comment'))]
        result, code = commands.execute(self.store, 'collect', sources={'molt': cfg}, fetch=client)
        self.assertEqual((code, result['added'], result['failed']), (0, 3, False), result)
        client.comments[2]['content'] = 'Current reply.'
        client.per_page = 1  # The board gives one comment on a page.
        client.calls.clear()
        mark(self.store, 'molt', uid(209), 'replied', ref=BOARDS['moltbook'].reference(uid(201), uid(211)))
        before = self.path.read_bytes()
        def expand():
            return commands.execute(self.store, 'expand', source='molt', thread=uid(201), through=3, sources={'molt': cfg},
                                    fetch=client)
        result, code = expand()
        self.assertEqual((code, result['complete'], result['budget_exhausted']), (0, True, False))
        self.assertEqual((result['root']['origin'], result['root']['remote_status']), ('remote', 'available'))
        first, second, third = result['items']
        self.assertEqual((second['target']['current_message']['body'], second['target']['differs_from_saved']), ('Current reply.', True))
        self.assertEqual((second['parent']['message']['body'], second['parent']['origin'], second['parent']['differs_from_saved']),
                         ('Our published answer.', 'remote', None))
        self.assertEqual((second['previous_exchange']['status'], [m['id'] for m in second['previous_exchange']['messages']]), ('linked', [uid(209)]))
        self.assertEqual((first['parent'], third['parent']), ({'id': uid(201), 'status': 'same_as_root'},) * 2)
        # One thread read and each of the four one-comment pages exactly once, although
        # every target restarts the comment scan from the first page.
        self.assertEqual([path for path, _, _ in client.calls], ['/posts/' + uid(201)] + ['/posts/' + uid(201) + '/comments'] * 4)
        self.assertTrue(all(not auth for _, _, auth in client.calls))
        self.assertEqual(self.path.read_bytes(), before)
        # The cache belongs to one operation: an edit is visible to the next call.
        client.comments[2]['content'] = 'Edited again.'
        client.calls.clear()
        result, code = expand()
        self.assertEqual((len(client.calls), result['items'][1]['target']['current_message']['body']), (5, 'Edited again.'))
        get, attempts = client.get, []
        def failed_page(path, params=None, **kwargs):
            attempts.append(path)
            if path.endswith('/comments'):
                raise URLError('synthetic page outage')
            return get(path, params, **kwargs)
        client.get = failed_page
        result, code = expand()
        self.assertEqual(attempts, ['/posts/' + uid(201), '/posts/' + uid(201) + '/comments'])
        self.assertEqual((code, result['complete'], len(result['items'])), (1, False, 3))
        self.assertEqual({item['target']['error'] for item in result['items']}, {'network_error'})

    def test_clawdchat_shares_root_and_parents_and_rejects_relocated_originals(self):
        cfg = {'account_id': uid(1), 'adapter': 'clawdchat', 'api_key_file': Path('absent.key')}
        board = ClawdChat()
        board.originals = {uid(100): clawd_original(100, title='Thread'),
                           uid(11): clawd_original(11, content='Our answer.', author={'id': uid(1), 'name': 'owner'}),
                           uid(12): clawd_original(12, parent_id=uid(11)), uid(13): clawd_original(13, parent_id=uid(11)),
                           uid(14): clawd_original(14, post_id=uid(999), post={'id': uid(999), 'title': 'Elsewhere'}),
                           uid(15): 404}
        # When the mail arrived, the four comments were under one post. Since then one moved and one is gone.
        self.clawd_arrives('clawd', *(clawd_original(n, parent_id=uid(11)) for n in (12, 13, 14)), clawd_original(15),
                           ours=[board.originals[uid(11)]])
        before = self.path.read_bytes()
        result, code = commands.execute(self.store, 'expand', source='clawd', thread=uid(100), through=4,
                                        sources={'clawd': cfg}, fetch=board)
        self.assertEqual((code, result['complete'], result['budget_exhausted'], result['root']['id']), (1, False, False, uid(100)))
        self.assertEqual([i['complete'] for i in result['items']], [True, True, False, False])
        relocated, missing = result['items'][2], result['items'][3]
        self.assertEqual((relocated['target']['remote_status'], relocated['target']['error'], relocated['target']['message']['thread_id']),
                         ('unavailable', 'invalid_response', uid(100)))
        self.assertEqual((missing['target']['remote_status'], missing['target']['error'], missing['parent']), ('missing', 'http_404', {'id': uid(100), 'status': 'same_as_root'}))
        self.assertEqual([path for path, _, _ in board.calls],
                         ['/posts/' + uid(100), '/comments/' + uid(12), '/comments/' + uid(11), '/comments/' + uid(13), '/comments/' + uid(14), '/comments/' + uid(15)])
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
