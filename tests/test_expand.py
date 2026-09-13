"""Bounded thread expansion: one client, one budget, saved rows only. All data is invented."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from boardmail import adapter_clawdchat, cli, commands, providers
from boardmail.config import MailError
from boardmail.store import Store
from examples.fixtures import FixtureClient, named, original, settings, uid
from test_clawdchat import FixtureClient as ClawdClient, original as clawd_original
from test_mail import mail

NO_CLIENT = AssertionError('remote client constructed')


class ExpandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'mail.sqlite3'
        self.cfg = settings()['postingboard']
        self.sources = {'postingboard': self.cfg}
        self.store = Store(self.path); self.store.initialize(self.sources)
        self.fixture = FixtureClient('postingboard', self.cfg)
        self.fixture.others = {uid(600): named(600, 600), uid(601): named(601, 600, 3, body='Our answer.'),
                               uid(602): named(602, 600, reply_to=601, body='Edited follow-up.'),
                               uid(603): named(603, 600, reply_to=601), uid(604): named(604, 600, reply_to=600)}
        # Arrival order (by created_at, then id): root, two replies to our answer, a reply to the root, another thread.
        self.store.save('postingboard', self.cfg['account_id'], [
            {**mail(600), 'thread_id': uid(600)},
            {**mail(602), 'thread_id': uid(600), 'parent_id': uid(601)},
            {**mail(603), 'thread_id': uid(600), 'parent_id': uid(601)},
            mail(611),
            {**mail(604), 'thread_id': uid(600), 'parent_id': uid(600)}])
        self.store.mark('postingboard', uid(611), 'replied', ref=providers.HOSTS['postingboard'] + '/v1/posts/' + uid(601))

    def expand(self, thread=600, *, through=5, after=0, limit=None, local=False, sources='configured', client=None):
        sources = self.sources if sources == 'configured' else sources
        with patch.object(providers, 'Client', return_value=client or self.fixture):
            args = {} if limit is None else {'limit': limit}
            return commands.execute(self.store, 'expand', source='postingboard', thread=uid(thread), through=through,
                                    after=after, local=local, sources=sources, **args)

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
        self.store.save('postingboard', self.cfg['account_id'], [{**mail(605), 'thread_id': uid(600), 'parent_id': uid(600)}])
        self.store.mark('postingboard', uid(603), 'read')
        self.fixture.calls.clear()
        result, code = self.expand(after=2)
        self.assertEqual((code, [i['id'] for i in result['items']], result['next_after'], result['more']), (0, [uid(603), uid(604)], 4, False))
        third, fourth = result['items']
        self.assertIsNotNone(third['target']['message']['read_at'])
        self.assertEqual(third['parent']['message']['body'], 'Our answer.')
        self.assertEqual(fourth['parent'], {'id': uid(600), 'status': 'same_as_root'})
        self.assertEqual(fourth['previous_exchange']['reply_ref'], providers.HOSTS['postingboard'] + '/v1/posts/' + uid(600))
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
        with patch.object(providers, 'Client', return_value=self.fixture):
            context, code = commands.execute(self.store, 'context', source='postingboard', id=uid(603), sources=self.sources)
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
        get, remaining = self.fixture.get, [3]
        def bounded(path, params=None, **kwargs):
            if not remaining[0]: raise MailError('budget_exhausted')
            remaining[0] -= 1
            return get(path, params, **kwargs)
        self.fixture.get = bounded
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
        self.store.save('fourclaw', 'demo', [{**mail(800), 'thread_id': uid(800)}])
        with patch.object(providers, 'Client', side_effect=NO_CLIENT), patch.object(adapter_clawdchat, 'Client', side_effect=NO_CLIENT):
            cases = [('local', dict(local=True)), ('unconfigured', dict(sources=None)), ('other source', dict(sources={'moltbook': settings()['moltbook']}))]
            for name, args in cases:
                with self.subTest(case=name):
                    result, code = self.expand(client=NO_CLIENT, **args)
                    self.assertEqual((code, result['fetched'], result['complete'], result['root']['origin']), (1, False, False, 'local'))
                    self.assertNotIn('remote_status', result['root'])
                    self.assertEqual([i['parent']['status'] for i in result['items']], ['none', 'unknown', 'unknown', 'same_as_root'])
                    self.assertEqual([i['complete'] for i in result['items']], [True, False, False, True])
                    # A recorded parent identity still links the exchange although the parent text is unknown.
                    self.assertEqual((result['items'][1]['previous_exchange']['status'], result['items'][1]['parent']['id']), ('linked', uid(601)))
            self.store.set_paused('postingboard', True)
            result, code = self.expand(client=NO_CLIENT)
            self.assertEqual((code, result['fetched'], result['items'][3]['complete']), (1, False, True))
            self.store.set_paused('postingboard', False)
            result, code = commands.execute(self.store, 'expand', source='fourclaw', thread=uid(800), through=10,
                                            sources={'fourclaw': {'adapter': 'fourclaw', 'account_id': 'demo'}})
            self.assertEqual((code, result['fetched'], result['complete'], result['items'][0]['target']['origin']), (0, False, True, 'local'))

    def test_empty_interval_and_invalid_arguments_touch_no_board(self):
        before = self.path.read_bytes()
        with patch.object(providers, 'Client', side_effect=NO_CLIENT):
            result, code = commands.execute(self.store, 'expand', source='postingboard', thread=uid(600), through=5, after=5, sources=self.sources)
            self.assertEqual((code, result['items'], result['fetched'], result['complete'], result['budget_exhausted']), (0, [], True, True, False))
            self.assertEqual((result['root']['id'], result['root']['status'], result['next_after'], result['more']), (uid(600), 'unknown', 5, False))
            result, code = commands.execute(self.store, 'expand', source='postingboard', thread=uid(999), through=5, sources=self.sources)
            self.assertEqual((code, result['items'], result['root']['status']), (0, [], 'unknown'))
            for args in (dict(through=None), dict(through=3, after=4), dict(through=5, limit=0), dict(through=5, limit=101),
                         dict(through=5, limit=True), dict(through=5.0), dict(through=2**63), dict(through=5, thread='not-a-uuid'),
                         dict(through=5, thread=None), dict(through=5, thread='x\x00y'), dict(through=5, source=None)):
                with self.subTest(args=args):
                    call = {'source': 'postingboard', 'thread': uid(600), **args}
                    with self.assertRaisesRegex(MailError, '^invalid_arguments$'):
                        commands.execute(self.store, 'expand', sources=self.sources, **call)
        self.assertEqual(self.path.read_bytes(), before)

    def test_cli_expands_with_config_and_reads_offline_with_db_alone(self):
        root = Path(self.temp.name)
        (root/'example.key').write_text('synthetic-key')
        (root/'config.json').write_text(json.dumps({'database': 'other.sqlite3', 'sources': {'postingboard': {
            'account_id': self.cfg['account_id'], 'api_key_file': 'example.key', 'threads': [uid(600)]}}}))
        before = self.path.read_bytes()
        def run(*args):
            out = io.StringIO()
            with patch.object(providers, 'Client', lambda *_: self.fixture), redirect_stdout(out):
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
        self.store = Store(self.path); self.store.initialize()

    def test_moltbook_comment_pages_are_read_once_for_every_target_and_parent(self):
        cfg = {**settings()['moltbook'], 'adapter': 'moltbook'}
        client = FixtureClient('moltbook', cfg)
        client.comments = [original(209, 201), original(211, 201, 1, body='Our published answer.'),
                           {**original(212, 201, body='Current reply.'), 'parent_id': uid(211)}, original(213, 201)]
        self.store.save('molt', cfg['account_id'], [
            {**mail(211), 'thread_id': uid(201)}, {**mail(212), 'thread_id': uid(201), 'parent_id': uid(211), 'body': 'Saved reply.'},
            {**mail(213), 'thread_id': uid(201)}])
        self.store.mark('molt', uid(211), 'replied', ref=providers.parent_reference('moltbook', uid(201), uid(211)))
        before = self.path.read_bytes()
        with patch.object(providers, 'PAGE_SIZE', 1), patch.object(providers, 'Client', return_value=client):
            result, code = commands.execute(self.store, 'expand', source='molt', thread=uid(201), through=3, sources={'molt': cfg})
        self.assertEqual((code, result['complete'], result['budget_exhausted']), (0, True, False))
        self.assertEqual((result['root']['origin'], result['root']['remote_status']), ('remote', 'available'))
        first, second, third = result['items']
        self.assertEqual((second['target']['current_message']['body'], second['target']['differs_from_saved']), ('Current reply.', True))
        self.assertEqual((second['parent']['current_message']['body'], second['parent']['origin'], second['parent']['differs_from_saved']),
                         ('Our published answer.', 'local', True))
        self.assertEqual((second['previous_exchange']['status'], [m['id'] for m in second['previous_exchange']['messages']]), ('linked', [uid(211)]))
        self.assertEqual((first['parent'], third['parent']), ({'id': uid(201), 'status': 'same_as_root'},) * 2)
        # One thread read and each of the four one-comment pages exactly once, although
        # every target restarts the comment scan from the first page.
        self.assertEqual([path for path, _, _ in client.calls], ['/posts/' + uid(201)] + ['/posts/' + uid(201) + '/comments'] * 4)
        self.assertTrue(all(not auth for _, _, auth in client.calls))
        self.assertEqual(self.path.read_bytes(), before)
        # The cache belongs to one operation: an edit is visible to the next call.
        client.comments[2]['content'] = 'Edited again.'
        client.calls.clear()
        with patch.object(providers, 'PAGE_SIZE', 1), patch.object(providers, 'Client', return_value=client):
            result, code = commands.execute(self.store, 'expand', source='molt', thread=uid(201), through=3, sources={'molt': cfg})
        self.assertEqual((len(client.calls), result['items'][1]['target']['current_message']['body']), (5, 'Edited again.'))

    def test_clawdchat_shares_root_and_parents_and_rejects_relocated_originals(self):
        cfg = {'account_id': uid(1), 'adapter': 'clawdchat', 'api_key_file': Path('absent.key')}
        client = ClawdClient()
        client.originals = {uid(100): clawd_original(100, title='Thread'),
                            uid(11): clawd_original(11, content='Our answer.', author={'id': uid(1), 'name': 'owner'}),
                            uid(12): clawd_original(12, parent_id=uid(11)), uid(13): clawd_original(13, parent_id=uid(11)),
                            uid(14): clawd_original(14, post_id=uid(999), post={'id': uid(999), 'title': 'Elsewhere'}),
                            uid(15): MailError('http_404')}
        self.store.save('clawd', uid(1), [
            {**mail(12), 'thread_id': uid(100), 'parent_id': uid(11)}, {**mail(13), 'thread_id': uid(100), 'parent_id': uid(11)},
            {**mail(14), 'thread_id': uid(100), 'parent_id': uid(11)}, {**mail(15), 'thread_id': uid(100)}])
        before = self.path.read_bytes()
        with patch.object(adapter_clawdchat, 'Client', return_value=client):
            result, code = commands.execute(self.store, 'expand', source='clawd', thread=uid(100), through=4, sources={'clawd': cfg})
        self.assertEqual((code, result['complete'], result['budget_exhausted'], result['root']['id']), (1, False, False, uid(100)))
        self.assertEqual([i['complete'] for i in result['items']], [True, True, False, False])
        relocated, missing = result['items'][2], result['items'][3]
        self.assertEqual((relocated['target']['remote_status'], relocated['target']['error'], relocated['target']['message']['thread_id']),
                         ('unavailable', 'invalid_response', uid(100)))
        self.assertEqual((missing['target']['remote_status'], missing['target']['error'], missing['parent']), ('missing', 'http_404', {'id': uid(100), 'status': 'same_as_root'}))
        self.assertEqual([path for path, _, _ in client.calls],
                         ['/posts/' + uid(100), '/comments/' + uid(12), '/comments/' + uid(11), '/comments/' + uid(13), '/comments/' + uid(14), '/comments/' + uid(15)])
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
