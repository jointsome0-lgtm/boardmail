"""Exact recorded reply links and anonymous Colony context, with invented mail."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError
from uuid import UUID

from boardmail import commands, providers
from boardmail.store import Store
from examples.fixtures import FixtureBoard, named, original, settings, uid
from kit import Clock, arrive, fixed, mark, notify
from test_mail import mail


class ExchangeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'mail.sqlite3'
        self.source = 'work-colony'
        self.cfg = {**settings(self.temp.name)['the-colony'], 'adapter': 'the-colony'}
        self.sources = {self.source: self.cfg, 'other-colony': {**self.cfg, 'account_id': uid(99)}}
        self.store = Store(self.path); commands.execute(self.store, 'init', sources=self.sources)
        # The invented Colony has an answer of the account under its post. The mail of the account is a comment
        # under that post, a comment under another post, and a follow-up to the answer. Another account of the
        # operator has a comment of its own as mail.
        self.fixture, other = (FixtureBoard('the-colony', self.sources[source]) for source in self.sources)
        self.ref = self.fixture.host + '/posts/' + uid(101) + '#comment-' + uid(120)
        self.parent = original(120, 101, int(UUID(self.cfg['account_id'])), colony=True, body='Our published answer.')
        self.target = {**original(130, 101, colony=True, body='A new follow-up.'), 'parent_id': uid(120)}
        self.fixture.comments, self.fixture.events, other.comments, other.events = [self.parent], [], [], []
        notify(self.fixture, original(201, 101, colony=True))
        notify(self.fixture, original(202, 777, colony=True))
        notify(self.fixture, self.target, 'reply_to_comment')
        notify(other, original(204, 101, colony=True))
        for source, board in ((self.source, self.fixture), ('other-colony', other)):
            self.assertFalse(commands.execute(self.store, 'collect', sources={source: self.sources[source]},
                                              fetch=board)[0]['failed'])
        for n in (201, 202, 130):
            mark(self.store, self.source, uid(n), 'replied', ref=self.ref)
        mark(self.store, self.source, uid(201), 'needs_reply')
        mark(self.store, 'other-colony', uid(204), 'replied', ref=self.ref)
        self.fixture.calls.clear()

    def context(self, mid=130, *, local=False):
        """The context command with the invented Colony in the place of the transport. The board gives an
        original to anyone, so the client does not sign the account in."""
        before = len(self.fixture.asked)
        result = commands.execute(self.store, 'context', source=self.source, id=uid(mid),
                                  sources=self.sources, local=local, fetch=self.fixture)
        if local or self.store.is_paused(self.source): self.assertEqual(len(self.fixture.asked), before)
        return result

    def test_all_exact_links_include_cross_thread_answers_but_exclude_other_source_and_target(self):
        before = self.path.read_bytes()
        result, code = self.context()
        self.assertEqual((code, result['parent']['origin'], result['parent']['message']['body']),
                         (0, 'remote', 'Our published answer.'))
        self.assertEqual(result['target']['current_message']['body'], 'A new follow-up.')
        self.assertIs(result['target']['differs_from_saved'], False)
        exchange = result['previous_exchange']
        self.assertEqual((exchange['status'], exchange['reason'], exchange['reply_ref']), ('linked', None, self.ref))
        self.assertEqual([m['id'] for m in exchange['messages']], [uid(201), uid(202)])
        self.assertEqual([m['needs_reply'] for m in exchange['messages']], [True, False])
        self.assertEqual(exchange['messages'][1]['thread_id'], uid(777))
        self.assertEqual(self.fixture.calls, [('/comments/' + uid(130), {}, False),
                         ('/posts/' + uid(101), {}, False), ('/comments/' + uid(120), {}, False)])
        self.assertEqual(self.path.read_bytes(), before)

    def test_local_and_paused_reads_keep_links_when_parent_text_is_not_stored(self):
        for paused in (False, True):
            commands.execute(self.store, 'pause' if paused else 'resume', source=self.source)
            before = self.path.read_bytes()
            result, code = self.context(local=not paused)
            self.assertEqual((code, result['fetched'], result['parent']['status']), (1, False, 'unknown'))
            self.assertEqual(result['previous_exchange']['status'], 'linked')
            self.assertEqual(len(result['previous_exchange']['messages']), 2)
            self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.fixture.calls, [])

    def test_unavailable_parent_text_does_not_erase_local_links(self):
        get = self.fixture.get
        for status, expected in ((404, 'missing'), (410, 'deleted'), (503, 'unavailable')):
            def fail_parent(path, params=None, **kw):
                if path.endswith(uid(120)):
                    raise HTTPError('https://private.invalid/secret', status, 'private prose', {}, io.BytesIO())
                return get(path, params, **kw)
            self.fixture.get = fail_parent
            result, code = self.context()
            self.assertEqual((code, result['parent']['status'], result['parent']['error']), (1, expected, 'http_%d' % status))
            self.assertEqual(result['previous_exchange']['status'], 'linked')
            self.assertNotIn('private', json.dumps(result))
        self.fixture.get = get
        for flag, expected, error in (('is_deleted', 'deleted', None), ('is_spam', 'unavailable', 'hidden_by_provider')):
            self.parent[flag] = True
            result, code = self.context()
            self.assertEqual((code, result['parent']['status'], result['parent']['error']), (1, expected, error))
            self.assertIsNone(result['parent']['message'])
            self.assertEqual(result['previous_exchange']['status'], 'linked')
            del self.parent[flag]

    def test_invalid_parent_thread_does_not_claim_a_previous_exchange(self):
        self.parent['post_id'] = uid(999)
        result, code = self.context()
        self.assertEqual((code, result['parent']['error']), (1, 'invalid_response'))
        self.assertEqual(result['previous_exchange'], {'status': 'unknown', 'reason': 'parent_invalid',
                                                      'reply_ref': None, 'messages': []})

    def test_no_explicit_parent_never_joins_by_root_and_root_target_has_no_exchange(self):
        self.target['parent_id'] = None  # Fresh original outranks the saved reply target.
        mark(self.store, self.source, uid(201), 'replied', ref=self.fixture.host + '/posts/' + uid(101))
        result, code = self.context()
        self.assertEqual((code, result['parent']['id']), (0, uid(101)))
        self.assertEqual((result['previous_exchange']['status'], result['previous_exchange']['reason']),
                         ('unknown', 'parent_not_recorded_by_board'))
        self.assertEqual(result['previous_exchange']['messages'], [])
        self.target['parent_id'] = uid(101)  # The same root is a valid link when explicitly addressed.
        result, code = self.context()
        self.assertEqual([m['id'] for m in result['previous_exchange']['messages']], [uid(201)])
        self.fixture.calls.clear()
        result, code = self.context(101)  # An unstored post is found after the comment endpoint says 404.
        self.assertEqual((code, result['target']['message']['id'], result['previous_exchange']['status']), (0, uid(101), 'none'))
        self.assertEqual(len(self.fixture.calls), 2)
        # The post becomes mail: it is one of another account, and the board tells the account that it is named there.
        self.fixture.root['author'] = {'id': uid(10), 'username': 'sample-writer'}
        self.fixture.events.append({'id': uid(5100), 'notification_type': 'mention', 'post_id': uid(101), 'is_read': True})
        self.assertEqual(commands.execute(self.store, 'collect', sources={self.source: self.cfg},
                                          fetch=self.fixture)[0]['added'], 1)
        self.fixture.calls.clear()
        result, code = self.context(101)
        self.assertEqual((code, result['previous_exchange']['status'], len(self.fixture.calls)), (0, 'none', 1))

    def test_noncanonical_refs_do_not_match_and_same_thread_alone_is_not_a_link(self):
        for ref in (self.ref + '/', self.ref.replace('https://', 'http://'),
                    self.ref.replace(self.fixture.host, 'https://impostor.invalid'),
                    self.ref.replace(uid(120), uid(121))):
            for n in (201, 202): mark(self.store, self.source, uid(n), 'replied', ref=ref)
            result, code = self.context(local=True)
            self.assertEqual(result['previous_exchange'], {'status': 'unmatched', 'reason': None,
                                                          'reply_ref': self.ref, 'messages': []})

    def test_postingboard_exact_refs_and_unsupported_adapters_are_distinct(self):
        # The same two messages from the invented Postingboard, which says in its inbox that one of them answers a
        # comment, and from an adapter file. The client of Postingboard waits between two requests, so the clock is
        # fixed for its pass and a wait only moves it.
        cfg = {**settings(self.temp.name)['postingboard'], 'inbox': True, 'threads': []}
        board = FixtureBoard('postingboard', cfg)
        board.others = {uid(n): named(n, 600, reply_to=601 if n == 610 else None) for n in (600, 601, 610, 611)}
        board.inbox = [(1, board.others[uid(610)], ['direct_reply']), (2, board.others[uid(611)], ['mention'])]
        with fixed(Clock(1790000000)):
            self.assertEqual(commands.execute(self.store, 'collect', sources={'postingboard': cfg}, fetch=board)[0]['added'], 2)
        arrive(self.store, 'custom', uid(3), [{**mail(610), 'thread_id': uid(600), 'parent_id': uid(601)}, mail(611)])
        for source in ('postingboard', 'custom'):
            self.assertEqual(self.store.show(source, uid(610))['parent_id'], uid(601))
            mark(self.store, source, uid(611), 'replied', ref=providers.HOSTS['postingboard'] + '/v1/posts/' + uid(601))
            result, code = commands.execute(self.store, 'context', source=source, id=uid(610), local=True)
            exchange = result['previous_exchange']
            if source == 'postingboard':
                self.assertEqual((exchange['status'], [m['id'] for m in exchange['messages']]), ('linked', [uid(611)]))
            else:
                self.assertEqual((exchange['status'], exchange['reason']), ('unknown', 'unsupported_source'))
        result, code = self.context(999, local=True)
        self.assertEqual((result['previous_exchange']['status'], result['previous_exchange']['reason']), ('unknown', 'no_parent_identity'))


if __name__ == '__main__': unittest.main()
