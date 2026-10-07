"""Inbox discovery beyond watched roots, thread context and freshness checks. All data is invented."""
from contextlib import closing, redirect_stdout
from copy import deepcopy
from urllib.error import HTTPError
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from boardmail import boards, cli, commands, config, providers
from boardmail.config import MailError
from boardmail.store import Store
from examples.fixtures import FixtureBoard, named, settings, uid
from kit import Clock, Network, arrive, edge, fixed, mark
from test_mail import mail


def http(code):
    return HTTPError('https://example.invalid/private', code, 'private provider prose', {}, io.BytesIO())


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'inbox.sqlite3'
        self.store = Store(self.path); commands.execute(self.store, 'init')
        self.cfg = {**settings(self.temp.name)['postingboard'], 'inbox': True, 'alias_search': []}
        self.fixture = FixtureBoard('postingboard', self.cfg)
        # The client waits between two requests, and a pass has its time. Here a wait only moves the clock.
        self.clock = Clock(1_000_000)
        self.enterContext(fixed(self.clock))
        # M and N sit in unwatched thread T2; only the Inbox addresses them.
        self.fixture.others = {uid(500): named(500, 500), uid(501): named(501, 500, body='@sample-agent please confirm.'),
                               uid(502): named(502, 500, reply_to=501, body='A direct reply to your comment.')}
        self.fixture.inbox = [(753, self.fixture.others[uid(501)], ['mention']),
                              (754, self.fixture.others[uid(502)], ['direct_reply'])]

    def collect(self, cfg=None):
        # Each call is a fresh process from the store's view: state comes only from SQLite.
        self.fixture.settings = cfg = cfg or self.cfg
        return boards.collect_all(Store(self.path), {'postingboard': cfg}, fetch=self.fixture)

    def state(self):
        _, state, _ = Store(self.path).collection_state('postingboard', self.cfg['account_id'], 'postingboard')
        return state

    def test_empty_inbox_pages_keep_a_checkpoint_for_the_next_collection(self):
        cfg = {**self.cfg, 'threads': []}
        get, afters = self.fixture.get, []
        def deleted_pages(path, params=None, **kw):
            if path == '/v1/inbox':
                afters.append(params['after'])
                if params['after'] in (0, 750):
                    following = 750 if params['after'] == 0 else 751
                    # The board is slow with the first page. The second still fits into the time that a pass
                    # has for discovery, and a third does not.
                    if params['after'] == 0: self.clock.advance(13)
                    return {'items': [], 'resume_after': 754, 'next_after': following,
                            'skipped_deleted_items': 1}
            return get(path, params, **kw)
        self.fixture.get = deleted_pages
        result = self.collect(cfg)
        self.assertEqual(afters, [0, 750])
        self.assertEqual(result['added'], 0)
        self.assertIsNone(result['sources'][0]['error'])
        self.assertTrue(result['sources'][0]['backlog_pending'])
        self.assertEqual(self.state()['inbox_after'], 751)

        afters.clear()
        result = self.collect(cfg)
        self.assertEqual((afters, result['added'], result['failed']), ([751], 2, False))
        self.assertEqual(self.state()['inbox_after'], 754)
        self.assertEqual(self.store.show('postingboard', uid(501))['body'],
                         '@sample-agent please confirm.')
        mark(self.store, 'postingboard', uid(501), 'read')
        saved = self.store.show('postingboard', uid(501))
        afters.clear()
        self.assertEqual(self.collect(cfg)['added'], 0)
        self.assertEqual(afters, [754])
        self.assertEqual(self.state()['inbox_after'], 754)
        self.assertEqual(self.store.show('postingboard', uid(501)), saved)

    def test_failure_after_an_empty_inbox_page_preserves_its_checkpoint(self):
        cfg = {**self.cfg, 'threads': []}
        get, afters, failing = self.fixture.get, [], True
        def deleted_then_failed(path, params=None, **kw):
            if path == '/v1/inbox':
                afters.append(params['after'])
                if params['after'] == 0:
                    return {'items': [], 'resume_after': 754, 'next_after': 750,
                            'skipped_deleted_items': 1}
                if failing: raise http(503)
            return get(path, params, **kw)
        self.fixture.get = deleted_then_failed
        result = self.collect(cfg)
        self.assertEqual((afters, result['added'], result['sources'][0]['error']),
                         ([0, 750], 0, 'http_503'))
        self.assertEqual(self.state()['inbox_after'], 750)
        failing = False; afters.clear()
        result = self.collect(cfg)
        self.assertEqual((afters, result['added'], result['failed']), ([750], 2, False))
        self.assertEqual(self.state()['inbox_after'], 754)

    def test_unfinished_nonempty_inbox_page_resumes_from_its_continuation(self):
        cfg = {**self.cfg, 'threads': []}
        get, afters, started = self.fixture.get, [], self.clock.now
        def split_page(path, params=None, **kw):
            raw = get(path, params, **kw)
            if path == '/v1/inbox':
                afters.append(params['after'])
                if params['after'] == 0:
                    raw['items'] = [item for item in raw['items'] if item['id'] == uid(501)]
                    raw['resume_after'], raw['next_after'] = 754, 753
                    # The page comes at the very end of the time that a pass has for discovery: it is in time,
                    # and no time is left for the page after it.
                    self.clock.now = started + 15
            return raw
        self.fixture.get = split_page
        result = self.collect(cfg)
        self.assertEqual((result['added'], result['failed'], self.state()['inbox_after']),
                         (1, False, 753))
        self.assertTrue(result['sources'][0]['backlog_pending'])
        mark(self.store, 'postingboard', uid(501), 'read')
        saved = self.store.show('postingboard', uid(501))
        result = self.collect(cfg)
        self.assertEqual((afters, result['added'], result['failed']), ([0, 753], 1, False))
        self.assertEqual(self.state()['inbox_after'], 754)
        self.assertEqual(self.store.status()['counts']['total'], 2)
        self.assertEqual(self.store.show('postingboard', uid(501)), saved)

    def test_invalid_empty_inbox_continuation_does_not_save_a_checkpoint(self):
        cfg = {**self.cfg, 'threads': []}
        get = self.fixture.get
        for following in (0, -1, True, '750', 751, 2**63):
            with self.subTest(following=following):
                calls = []
                def invalid_page(path, params=None, **kw):
                    if path == '/v1/inbox':
                        calls.append(params['after'])
                        return {'items': [], 'resume_after': 750, 'next_after': following}
                    return get(path, params, **kw)
                self.fixture.get = invalid_page
                result = self.collect(cfg)
                self.assertEqual((calls, result['added'], result['sources'][0]['error']),
                                 ([0], 0, 'pagination_no_progress'))
                self.assertNotIn('inbox_after', self.state())

    def test_timed_out_original_survives_restart_and_rediscovery_keeps_one_record(self):
        get, slow = self.fixture.get, {uid(501), uid(502)}
        def timing_out(path, params=None, **kw):
            if path.rsplit('/', 1)[-1] in slow: raise MailError('source_timeout')
            return get(path, params, **kw)
        self.fixture.get = timing_out
        result = self.collect()
        self.assertTrue(result['failed']); self.assertEqual(result['sources'][0]['error'], 'source_timeout')
        self.assertEqual(result['added'], 3)  # Watched roots still delivered their own mail.
        state = self.state()
        self.assertEqual(state['inbox_after'], 754); self.assertEqual(set(state['pending']), slow)
        self.assertTrue(result['sources'][0]['backlog_pending'])
        self.assertNotIn(uid(501), self.store.known('postingboard', self.cfg['account_id']))
        # Restart: a new process resolves M from durable candidates, not from the Inbox cursor.
        slow.discard(uid(501))
        self.assertEqual(self.collect()['added'], 1)
        message = self.store.show('postingboard', uid(501))
        self.assertEqual((message['thread_id'], message['kind'], message['discovery'], message['provider_seq']),
                         (uid(500), 'mention', 'inbox:mention', 501))
        self.assertEqual(message['body'], '@sample-agent please confirm.')
        mark(self.store, 'postingboard', uid(501), 'read')
        self.assertEqual(set(self.state()['pending']), {uid(502)})  # A local mark cancels no discovery work.
        slow.clear(); self.fixture.get = get
        self.assertEqual(self.collect()['added'], 1)
        nested = self.store.show('postingboard', uid(502))
        self.assertEqual((nested['kind'], nested['parent_id'], nested['discovery']), ('reply_to_comment', uid(501), 'inbox:direct_reply'))
        # T2 becomes watched and an alias search also finds M: still one record with intact marks.
        watched = {**self.cfg, 'threads': [*self.cfg['threads'], uid(500)], 'alias_search': ['sample-agent']}
        self.fixture.roots[uid(500)] = self.fixture.others[uid(500)]
        self.fixture.comments[uid(500)] = [self.fixture.others[uid(501)], self.fixture.others[uid(502)]]
        self.fixture.search['sample-agent'] = [self.fixture.others[uid(501)]]
        result = self.collect(watched)
        self.assertFalse(result['failed']); self.assertEqual(result['added'], 0)
        after = self.store.show('postingboard', uid(501))
        self.assertEqual(after, {**message, 'read_at': after['read_at']}); self.assertIsNotNone(after['read_at'])
        self.assertEqual(self.store.status()['counts']['total'], 5)
        state = self.state()
        self.assertEqual((state['inbox_after'], state['search'], state['pending']), (754, {'sample-agent': 501}, {}))
        self.assertEqual(set(state['threads']), {uid(301), uid(302), uid(500)})
        self.assertFalse(any(path.endswith('/ack') for path, _, _ in self.fixture.calls))

    def test_inbox_error_stays_visible_beside_watched_thread_mail(self):
        get = self.fixture.get
        def broken(path, params=None, **kw):
            if path == '/v1/inbox': raise http(503)
            return get(path, params, **kw)
        self.fixture.get = broken
        result = self.collect()
        self.assertTrue(result['failed']); self.assertEqual(result['added'], 3)
        health = result['sources'][0]
        self.assertEqual((health['status'], health['error'], health['last_ok']), ('error', 'http_503', None))
        self.assertNotIn('inbox_after', self.state()); self.assertNotIn('private', json.dumps(result))
        self.fixture.get = get
        result = self.collect()
        self.assertFalse(result['failed']); self.assertEqual(result['added'], 2)
        self.assertEqual(result['sources'][0]['status'], 'ok')

    def test_alias_search_is_opt_in_matches_full_bodies_and_adds_no_subscription(self):
        cfg = {**self.cfg, 'inbox': False, 'threads': [], 'alias_search': ['meliora']}
        posts = {uid(600): named(600, 600), uid(601): named(601, 600, body='Thanks Meliora, the summary helped.'),
                 uid(602): named(602, 600, body='Melioration is a preview-only false match: meliora-tools.'),
                 uid(603): named(603, 600, 3, body='Own post mentioning meliora.')}
        self.fixture.others = posts
        self.fixture.search['meliora'] = [posts[uid(601)], posts[uid(602)], posts[uid(603)]]
        self.assertEqual(self.collect({**cfg, 'alias_search': []})['added'], 0)
        result = self.collect(cfg)
        self.assertFalse(result['failed']); self.assertEqual(result['added'], 1)
        found = self.store.show('postingboard', uid(601))
        self.assertEqual((found['kind'], found['discovery'], found['thread_id']), ('mention', 'search:meliora', uid(600)))
        state = self.state()
        self.assertEqual(state['search'], {'meliora': 603}); self.assertEqual(state['pending'], {})
        self.assertNotIn('inbox_after', state); self.assertEqual(state['threads'], {})
        self.assertTrue(all(p.get('q') == 'meliora' for path, p, _ in self.fixture.calls if path == '/v1/search'))
        self.assertEqual(self.collect(cfg)['added'], 0)

    def test_watched_page_body_completes_a_pending_inbox_discovery(self):
        cfg = {**self.cfg, 'threads': [uid(301)]}
        addressed = named(399, 301, body='@sample-agent the watched page carries this body.')
        self.fixture.comments[uid(301)].append(addressed)
        self.fixture.inbox = [(9001, addressed, ['mention'])]
        get = self.fixture.get
        def timing_out(path, params=None, **kw):
            if path.endswith(uid(399)): raise MailError('source_timeout')
            return get(path, params, **kw)
        self.fixture.get = timing_out
        result = self.collect(cfg)
        self.assertTrue(result['failed']); self.assertEqual(result['added'], 3)
        found = self.store.show('postingboard', uid(399))
        self.assertEqual((found['kind'], found['discovery'], found['thread_id']), ('mention', 'inbox:mention', uid(301)))
        self.assertEqual(self.state()['pending'], {})
        mark(self.store, 'postingboard', uid(399), 'read'); self.fixture.get = get
        self.assertEqual(self.collect(cfg)['added'], 0)
        self.assertEqual(self.store.show('postingboard', uid(399)), {**found, 'read_at': self.store.show('postingboard', uid(399))['read_at']})

    def test_foreign_watched_page_resolves_pending_direct_reply_after_restart(self):
        cfg = {**self.cfg, 'inbox': True, 'threads': [], 'mention_aliases': []}
        addressed = named(399, 302, reply_to=313, body='The watched page has the complete answer.')
        self.fixture.comments[uid(302)] = [addressed, named(398, 302), named(397, 302, reply_to=313)]
        self.fixture.inbox = [(9001, addressed, ['direct_reply'])]
        get = self.fixture.get
        def unavailable_original(path, params=None, **kw):
            if path == '/v1/posts/' + uid(399): raise MailError('source_timeout')
            return get(path, params, **kw)
        self.fixture.get = unavailable_original
        first = self.collect(cfg)
        self.assertEqual((first['added'], first['sources'][0]['error']), (0, 'source_timeout'))
        self.assertEqual(self.state()['pending'][uid(399)]['reasons'], ['direct_reply'])

        # A new Store resolves saved discovery from a complete page in a foreign root.
        cfg = {**cfg, 'threads': [uid(302)]}
        result = self.collect(cfg)
        self.assertEqual((result['added'], result['sources'][0]['error']), (1, 'source_timeout'))
        found = self.store.show('postingboard', uid(399))
        self.assertEqual((found['kind'], found['discovery'], found['addressing']),
                         ('reply_to_comment', 'inbox:direct_reply', 'direct'))
        self.assertEqual((found['thread_id'], found['parent_id'], found['body']),
                         (uid(302), uid(313), addressed['body']))
        self.assertEqual(self.state()['pending'], {})
        self.assertEqual([m['id'] for m in self.store.page()['messages']], [uid(399)],
                         'Unrelated foreign-root replies remain excluded')
        for action in ('read', 'needs_reply', 'replied'):
            mark(self.store, 'postingboard', uid(399), action,
                 ref='https://example.invalid/reply' if action == 'replied' else None)
        saved = self.store.show('postingboard', uid(399))
        self.store = Store(self.path)
        self.fixture.get = get
        self.fixture.inbox.append((9002, addressed, ['direct_reply']))
        self.assertEqual(self.collect(cfg)['added'], 0)
        self.assertEqual(self.store.show('postingboard', uid(399)), saved)
        self.assertEqual(self.state()['inbox_after'], 9002)
        self.assertEqual(self.state()['pending'], {})

    def test_foreign_watched_page_resolves_pending_plain_search_match(self):
        cfg = {**self.cfg, 'inbox': False, 'threads': [uid(302)],
               'mention_aliases': [], 'alias_search': ['meliora']}
        addressed = named(399, 302, body='Thanks Meliora, the summary helped.')
        self.fixture.comments[uid(302)] = [addressed, named(398, 302)]
        self.fixture.search['meliora'] = [addressed]
        get = self.fixture.get
        def unavailable_original(path, params=None, **kw):
            if path == '/v1/posts/' + uid(399): raise MailError('source_timeout')
            return get(path, params, **kw)
        self.fixture.get = unavailable_original
        result = self.collect(cfg)
        self.assertEqual((result['added'], result['sources'][0]['error']), (1, 'source_timeout'))
        found = self.store.show('postingboard', uid(399))
        self.assertEqual((found['kind'], found['discovery'], found['addressing']),
                         ('mention', 'search:meliora', None))
        self.assertEqual(found['body'], addressed['body'])
        self.assertEqual(self.state()['pending'], {})
        self.assertEqual([m['id'] for m in self.store.page()['messages']], [uid(399)])

    def test_alias_named_inbox_never_reads_the_native_inbox(self):
        cfg = {**self.cfg, 'inbox': False, 'threads': [], 'alias_search': ['inbox']}
        self.fixture.others = {uid(700): named(700, 700, body='My inbox is empty today.')}
        self.fixture.search['inbox'] = [self.fixture.others[uid(700)]]
        result = self.collect(cfg)
        self.assertFalse(result['failed']); self.assertEqual(result['added'], 1)
        self.assertEqual(self.store.show('postingboard', uid(700))['discovery'], 'search:inbox')
        self.assertEqual({path for path, _, _ in self.fixture.calls}, {'/v1/me', '/v1/search', '/v1/posts/'+uid(700)})

    def test_shared_search_candidate_is_judged_by_every_term_and_keeps_inbox_reasons(self):
        cfg = {**self.cfg, 'inbox': False, 'threads': [], 'alias_search': ['first', 'second']}
        posts = {uid(800): named(800, 800), uid(801): named(801, 800, body='Only the SECOND term appears in full.'),
                 uid(802): named(802, 800, reply_to=801, body='first and second both appear.')}
        self.fixture.others = posts
        self.fixture.search = {'first': [posts[uid(801)], posts[uid(802)]], 'second': [posts[uid(801)]]}
        get = self.fixture.get
        def timing_out(path, params=None, **kw):
            if path.endswith(uid(802)): raise MailError('source_timeout')
            return get(path, params, **kw)
        self.fixture.get = timing_out
        result = self.collect(cfg)
        self.assertEqual((result['added'], result['sources'][0]['error']), (1, 'source_timeout'))
        self.assertEqual(self.store.show('postingboard', uid(801))['discovery'], 'search:second')
        self.assertEqual((self.state()['search'], self.state()['pending'][uid(802)]['terms']), ({'first': 802, 'second': 801}, ['first']))
        self.fixture.get = get
        self.fixture.inbox = [(1, posts[uid(802)], ['direct_reply'])]
        self.assertEqual(self.collect({**cfg, 'inbox': True})['added'], 1)
        later = self.store.show('postingboard', uid(802))
        self.assertEqual((later['discovery'], later['kind'], later['parent_id']), ('inbox:direct_reply', 'reply_to_comment', uid(801)))
        self.assertEqual(self.state()['pending'], {})
        # Two valid 70-character terms both match: the stored reason stays within the 128-character limit.
        long_terms = ['a'*70, 'b'*70]
        posts[uid(803)] = named(803, 800, body=' '.join(long_terms))
        self.fixture.search = {t: [posts[uid(803)]] for t in long_terms}
        posts[uid(804)] = named(804, 800, body='Many reasons.')
        self.fixture.inbox = [(2, posts[uid(804)], ['z'*32, 'y'*32, 'x'*32, 'w'*32, 'mention'])]
        result = self.collect({**cfg, 'inbox': True, 'alias_search': long_terms})
        self.assertEqual((result['failed'], result['added'], self.state()['pending']), (False, 2, {}))
        self.assertEqual(self.store.show('postingboard', uid(803))['discovery'], 'search:'+'a'*70)
        many = self.store.show('postingboard', uid(804))['discovery']
        self.assertTrue(many.startswith('inbox:mention+') and len(many) <= 128, many)

    def test_config_accepts_inbox_without_threads_and_rejects_bad_alias_search(self):
        root = Path(self.temp.name)
        def load(source):
            (root/'config.json').write_text(json.dumps({'database': 'mail.sqlite3', 'sources': {'postingboard': {
                'account_id': uid(3), 'api_key_file': 'unused.key', **source}}}))
            return config.load(root/'config.json')['sources']['postingboard']
        loaded = load({'inbox': True})
        self.assertEqual((loaded['threads'], loaded['inbox'], loaded['alias_search']), ([], True, []))
        self.assertEqual(load({'alias_search': [' meliora ']})['alias_search'], ['meliora'])
        self.assertEqual(load({})['threads'], [], 'Roots can be supplied by local subscriptions later')
        for bad in ({'threads': None}, {'inbox': 'yes', 'threads': [uid(1)]}, {'alias_search': ['']}, {'alias_search': ['x'*101]}):
            with self.assertRaises(MailError): load(bad)


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'inbox.sqlite3'
        self.store = Store(self.path); commands.execute(self.store, 'init')
        self.cfg = settings(self.temp.name)['postingboard']
        self.fixture = FixtureBoard('postingboard', self.cfg)
        self.clock = Clock(1_000_000)
        self.fixture.others = {uid(600): named(600, 600), uid(601): named(601, 600, body='A parent comment.'),
                               uid(602): named(602, 600, reply_to=601, body='@sample-agent nested reply.')}
        self.arrives(602)
        self.assertEqual(self.store.show('postingboard', uid(602))['parent_id'], uid(601))

    def arrives(self, *numbers, store=None, **watching):
        """A pass in which the inbox of the invented Postingboard tells the account of these posts, which become
        its mail. The pass watches no thread unless the call names some."""
        self.fixture.inbox += [(len(self.fixture.inbox) + 1, self.fixture.others[uid(n)], ['mention']) for n in numbers]
        sources = {'postingboard': {**self.cfg, 'inbox': True, 'threads': [], 'alias_search': [], **watching}}
        with fixed(self.clock):
            result, code = commands.execute(store or self.store, 'collect', sources=sources, fetch=self.fixture)
        self.assertEqual((code, result['failed']), (0, False), result)
        self.fixture.calls.clear()
        return result['added']

    def context(self, mid, cfg=None):
        # The client waits between two requests to Postingboard. With the clock fixed, the wait only moves it.
        with fixed(self.clock):
            return commands.context(self.store, 'postingboard', mid, cfg, fetch=self.fixture)

    def test_saved_root_compares_text_exactly_without_changing_snapshot_or_marks(self):
        saved = {'title': 'Original title', 'body': 'Cafe\u0301\r\n'}
        current = self.fixture.others[uid(610)] = {**named(610, 610), **saved}
        self.arrives(610)
        for action in ('read', 'needs_reply', 'replied'):
            mark(self.store, 'postingboard', uid(610), action, ref='https://example.invalid/reply' if action == 'replied' else None)
        snapshot = self.store.show('postingboard', uid(610))
        self.assertEqual({field: snapshot[field] for field in saved}, saved)
        before = self.path.read_bytes()
        # Another author and another time do not change the text comparison.
        current.update(author='another-writer', created_at=current['created_at'] + 60)
        cases = [('Original title', 'Cafe\u0301\r\n', False), ('Renamed', 'Cafe\u0301\r\n', True),
                 ('Original title', 'Cafe\u0301\n', True), ('Original title', 'Caf\u00e9\r\n', True)]
        for title, body, differs in cases:
            with self.subTest(title=title, body=body):
                current.update(title=title, body=body)
                self.fixture.calls.clear()
                result, code = self.context(uid(610), self.cfg)
                self.assertEqual((code, result['complete']), (0, True))
                self.assertEqual(result['target'], result['root'])
                self.assertEqual(result['target']['message'], snapshot)
                self.assertEqual(result['target']['current_message']['body'], body)
                self.assertEqual(result['target']['current_message']['source'], 'postingboard')
                self.assertIs(result['target']['differs_from_saved'], differs)
                self.assertIsNone(result['parent']['current_message'])
                self.assertIsNone(result['parent']['differs_from_saved'])
                self.assertEqual([path for path, _, _ in self.fixture.calls], ['/v1/posts/' + uid(610)])
                self.assertEqual(self.path.read_bytes(), before)

    def test_reply_label_is_not_an_edit_and_thread_rename_belongs_to_root(self):
        # The reply is saved under the title of its thread. The board has no title for a reply.
        current = self.fixture.others[uid(602)]
        current['title'] = ''
        self.assertEqual(self.arrives(600, 601), 2)
        self.fixture.others[uid(600)]['title'] = 'Renamed thread'
        before = self.path.read_bytes()
        result, code = self.context(uid(602), self.cfg)
        self.assertEqual(code, 0)
        self.assertIs(result['target']['differs_from_saved'], False)
        self.assertNotEqual(result['target']['message']['title'], result['target']['current_message']['title'])
        self.assertIs(result['parent']['differs_from_saved'], False)
        self.assertIs(result['root']['differs_from_saved'], True)
        self.assertEqual(len(self.fixture.calls), 3)
        current['body'] = 'Edited reply'
        result, code = self.context(uid(602), self.cfg)
        self.assertEqual((code, result['target']['message']['body'], result['target']['current_message']['body']),
                         (0, '@sample-agent nested reply.', 'Edited reply'))
        self.assertIs(result['target']['differs_from_saved'], True)
        self.assertEqual(self.path.read_bytes(), before)

    def test_nested_reply_gets_same_thread_parent_and_root_without_marks(self):
        before = self.path.read_bytes()
        result, code = self.context(uid(602), self.cfg)
        self.assertEqual(code, 0); self.assertTrue(result['complete']); self.assertTrue(result['fetched'])
        self.assertEqual((result['target']['status'], result['target']['origin'], result['target']['remote_status']), ('available', 'local', 'available'))
        self.assertEqual((result['parent']['id'], result['parent']['origin'], result['parent']['message']['body']), (uid(601), 'remote', 'A parent comment.'))
        self.assertEqual((result['root']['id'], result['root']['message']['thread_id']), (uid(600), uid(600)))
        for role in ('root', 'parent'):  # Remote-only elements already carry current text in message.
            self.assertIsNone(result[role]['current_message'])
            self.assertIsNone(result[role]['differs_from_saved'])
        self.assertEqual(self.path.read_bytes(), before); self.assertIsNone(self.store.show('postingboard', uid(602))['read_at'])
        self.assertFalse(any('ack' in path for path, _, _ in self.fixture.calls))
        # A root-level reply's immediate parent is the root itself; a root has no parent.
        result, code = self.context(uid(601), self.cfg)
        self.assertEqual((code, result['parent']['id'], result['parent']['status']), (0, uid(600), 'available'))
        result, code = self.context(uid(600), self.cfg)
        self.assertEqual((code, result['parent']['status'], result['root']['id']), (0, 'none', uid(600)))

    def test_deleted_missing_unavailable_and_unknown_are_distinct(self):
        get = self.fixture.get
        outcomes = {410: 'deleted', 404: 'missing', 503: 'unavailable'}
        for status, expected in outcomes.items():
            def failing(path, params=None, **kw):
                if path.endswith(uid(601)): raise http(status)
                return get(path, params, **kw)
            self.fixture.get = failing
            result, code = self.context(uid(602), self.cfg)
            self.assertEqual((code, result['parent']['status'], result['root']['status']), (1, expected, 'available'))
            self.assertEqual(result['parent']['error'], 'http_%d' % status)
            self.assertIsNone(result['parent']['current_message'])
            self.assertIsNone(result['parent']['differs_from_saved'])
            self.assertNotIn('private', json.dumps(result))
        snapshot = self.store.show('postingboard', uid(602))
        before = self.path.read_bytes()
        for status, expected in outcomes.items():
            def failing_target(path, params=None, **kw):
                if path.endswith(uid(602)): raise http(status)
                return get(path, params, **kw)
            self.fixture.get = failing_target
            result, code = self.context(uid(602), self.cfg)
            self.assertEqual((code, result['target']['status'], result['target']['remote_status']), (0, 'available', expected))
            self.assertEqual(result['target']['message'], snapshot)
            self.assertIsNone(result['target']['current_message'])
            self.assertIsNone(result['target']['differs_from_saved'])
            self.assertEqual(self.path.read_bytes(), before)
        self.fixture.get = get
        result, code = self.context(uid(602), None)
        self.assertEqual((code, result['fetched'], result['parent']['status'], result['root']['status']), (1, False, 'unknown', 'unknown'))
        self.assertEqual(result['target']['origin'], 'local'); self.assertNotIn('remote_status', result['target'])
        for role in ('root', 'parent', 'target'):
            self.assertIsNone(result[role]['current_message'])
            self.assertIsNone(result[role]['differs_from_saved'])
        result, code = self.context(uid(999), self.cfg)
        self.assertEqual((code, result['target']['status'], result['parent']['status']), (1, 'missing', 'unknown'))
        self.fixture.others[uid(601)]['root_id'] = uid(777)
        self.assertEqual(self.context(uid(602), self.cfg)[0]['parent']['error'], 'invalid_response')
        with self.assertRaises(MailError): self.context('../not-a-uuid', self.cfg)

    def test_fetched_relationships_outrank_legacy_local_rows(self):
        self.fixture.roots[uid(900)] = named(900, 900)
        self.fixture.comments[uid(900)] = [named(901, 900, body='Parent.'), named(902, 900, reply_to=901),
                                           named(903, 900, body='@sample-agent, a comment under the post.')]
        # A file that an older release left. Its row of Postingboard is from before reply targets were kept. No
        # release writes such a row today, so the test writes it as that release did.
        legacy = {**mail(902), 'thread_id': uid(900)}
        self.store = Store(Path(self.temp.name)/'older.sqlite3')
        with closing(sqlite3.connect(self.store.path)) as db:
            db.executescript((Path(__file__).parent/'fixtures/v1.sql').read_text())
            with db:
                db.execute("INSERT INTO sources (source,account_id) VALUES ('postingboard',?)", (self.cfg['account_id'],))
                db.execute("INSERT INTO messages (source,id,thread_id,kind,author,title,body,url,created_at,arrived_at) "
                           "VALUES ('postingboard',:id,:thread_id,:kind,:author,:title,:body,:url,:created_at,200)", legacy)
        result, code = self.context(uid(902), self.cfg)
        self.assertEqual((code, result['parent']['id'], result['parent']['message']['body'], result['root']['status']), (0, uid(901), 'Parent.', 'available'))
        # Offline, a row stored before reply targets were kept cannot name its parent.
        result, code = self.context(uid(902), None)
        self.assertEqual((code, result['parent']['status'], result['parent']['id']), (1, 'unknown', None))
        # A row of today says how it was found. The pass that brings the file up to date watches the thread and
        # finds a comment that names the account and answers no other comment. The inbox of the board is empty.
        self.fixture.inbox.clear()
        self.assertEqual(self.arrives(store=self.store, threads=[uid(900)]), 1)
        self.assertEqual(self.store.show('postingboard', uid(903))['discovery'], 'thread')
        result, code = self.context(uid(903), None)
        self.assertEqual((code, result['parent']['id'], result['parent']['status']), (1, uid(900), 'unknown'))
        # A row of another adapter answers the post where it names no other target.
        arrive(self.store, 'custom', uid(2), [{**mail(904), 'thread_id': uid(900)}, {**mail(900), 'thread_id': uid(900)}])
        result, code = commands.context(self.store, 'custom', uid(904), None)
        self.assertEqual((code, result['parent']['id'], result['parent']['origin']), (0, uid(900), 'local'))
        self.assertIsNone(result['target']['current_message'])
        self.assertIsNone(result['target']['differs_from_saved'])

    def test_incompatible_stored_thread_cannot_be_compared(self):
        self.fixture.others[uid(700)] = named(700, 700)
        self.fixture.others[uid(602)].update(root_id=uid(700), thread_id=uid(700), reply_to_id=None)
        before = self.path.read_bytes()
        result, code = self.context(uid(602), self.cfg)
        self.assertEqual((code, result['root']['id']), (0, uid(700)))
        self.assertEqual(result['target']['message']['thread_id'], uid(600))
        self.assertEqual(result['target']['current_message']['thread_id'], uid(700))
        self.assertIsNone(result['target']['differs_from_saved'])
        self.assertEqual(self.path.read_bytes(), before)

    def test_cli_context_with_explicit_config_keeps_the_database_override(self):
        root = Path(self.temp.name)
        # Another message, which the account answered with the comment that the nested reply answers. The board
        # then gets another text for the nested reply.
        self.fixture.others[uid(603)] = named(603, 600)
        self.arrives(603)
        mark(self.store, 'postingboard', uid(603), 'replied', ref=providers.HOSTS['postingboard'] + '/v1/posts/' + uid(601))
        self.fixture.others[uid(602)]['body'] = '@sample-agent nested reply, edited.'
        before = self.path.read_bytes()  # The key of the account is in the folder already, in example.key.
        (root/'config.json').write_text(json.dumps({'database': 'other.sqlite3', 'sources': {'postingboard': {
            'account_id': self.cfg['account_id'], 'api_key_file': 'example.key', 'threads': [uid(600)]}}}))
        def run(*args):
            out = io.StringIO()
            # The command line hands no board in. The invented one stands where a request leaves the process.
            with Network({'getpostingboard.dev': edge(self.fixture)}), fixed(self.clock), redirect_stdout(out):
                code = cli.main(['--config', str(root/'config.json'), '--db', str(self.path), 'context', 'postingboard', uid(602), *args])
            return code, json.loads(out.getvalue())
        code, result = run()
        self.assertEqual((code, result['fetched'], result['parent']['status'], result['target']['origin']), (0, True, 'available', 'local'))
        self.assertEqual((result['target']['message']['body'], result['target']['current_message']['body']),
                         ('@sample-agent nested reply.', '@sample-agent nested reply, edited.'))
        self.assertIs(result['target']['differs_from_saved'], True)
        self.assertEqual(result['previous_exchange']['status'], 'linked')
        self.assertEqual([m['id'] for m in result['previous_exchange']['messages']], [uid(603)])
        code, result = run('--local')
        self.assertEqual((code, result['fetched'], result['parent']['status']), (1, False, 'unknown'))
        self.assertIsNone(result['target']['current_message'])
        self.assertIsNone(result['target']['differs_from_saved'])
        self.assertFalse((root/'other.sqlite3').exists())
        self.assertEqual(self.path.read_bytes(), before)

    def test_cli_context_reads_local_records_offline(self):
        self.arrives(600)
        command = [sys.executable, '-m', 'boardmail', '--db', str(self.path), 'context', 'postingboard']
        for mid, expected in ((uid(602), (1, 'unknown', 'available')), (uid(600), (0, 'none', 'available'))):
            run = subprocess.run([*command, mid], capture_output=True, text=True, timeout=10)
            result = json.loads(run.stdout)
            self.assertEqual((run.returncode, result['parent']['status'], result['root']['status']), expected, result)
            self.assertFalse(result['fetched']); self.assertFalse(result['history_complete'])
        run = subprocess.run([*command, uid(999)], capture_output=True, text=True, timeout=10)
        self.assertEqual((run.returncode, json.loads(run.stdout)['target']['status']), (1, 'unknown'))


class FreshnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'inbox.sqlite3'
        self.store = Store(self.path); commands.execute(self.store, 'init')

    def status(self, *args):
        run = subprocess.run([sys.executable, '-m', 'boardmail', '--db', str(self.path), 'status', *args],
                             capture_output=True, text=True, timeout=10)
        return run.returncode, json.loads(run.stdout)

    def test_age_threshold_and_explicit_freshness_requirement(self):
        code, result = self.status()
        self.assertEqual((code, result['fresh'], result['stale_after'], result['sources']), (0, False, 540, []))
        self.assertEqual(self.status('--require-fresh')[0], 1)
        with fixed(Clock(1000)):
            arrive(self.store, 'moltbook', uid(2))  # A pass long ago that went well.
        code, result = self.status('--require-fresh')
        source = result['sources'][0]
        self.assertEqual((code, result['event'], source['status'], source['stale_after']), (1, 'status', 'stale', 540))
        self.assertGreater(source['last_ok_age'], 540); self.assertFalse(result['freshness_required'] and result['fresh'])
        self.assertEqual(self.status()[0], 0)  # Reading stale state is still a successful read.
        code, result = self.status('--require-fresh', '--stale-after', str(2**31-1))
        self.assertEqual((code, result['fresh'], result['sources'][0]['status'], result['stale_after']), (0, True, 'ok', 2**31-1))
        arrive(self.store, 'moltbook', uid(2))
        code, result = self.status('--require-fresh', '--stale-after', '5')
        self.assertEqual((code, result['sources'][0]['last_ok_age'] >= 0), (0, True))
        # Backlog is a separate fact and never changes freshness.
        arrive(self.store, 'moltbook', uid(2), complete=False)
        code, result = self.status('--require-fresh')
        self.assertEqual((code, result['fresh'], result['sources'][0]['backlog_pending']), (0, True, True))
        arrive(self.store, 'moltbook', uid(2), error='http_503')
        code, result = self.status('--require-fresh')
        self.assertEqual((code, result['sources'][0]['status'], result['sources'][0]['error']), (1, 'error', 'http_503'))
        self.assertEqual(self.status()[0], 0)
        self.assertEqual(self.status('--stale-after', '-1')[1]['error'], 'invalid_arguments')

    def test_stale_boundary_uses_exact_elapsed_time(self):
        with fixed(Clock(100)):
            arrive(self.store, 'moltbook', uid(2))
        for now, expected in ((640.0, 'ok'), (640.5, 'stale')):
            with fixed(Clock(now)):
                source = self.store.status()['sources'][0]
            self.assertEqual((source['status'], source['last_ok_age']), (expected, 540))


if __name__ == '__main__': unittest.main()
