"""Moltbook and ClawdChat context uses public originals and exact reply IDs."""
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from boardmail import commands, providers
from boardmail.adapters import Batch
from boardmail.config import MailError
from boardmail.store import Store
from examples.fixtures import FakeBoard, FixtureClient, original, uid
from test_clawdchat import Board as ClawdChat, original as clawd_original
from test_mail import mail


class PublicContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'mail.sqlite3'
        self.store = Store(self.path); self.store.initialize()

    def setup_source(self, adapter):
        self.adapter, self.source = adapter, 'alias-' + adapter
        self.cfg = {'account_id': uid(1), 'adapter': adapter, 'api_key_file': Path('absent.key')}
        if adapter == 'moltbook':
            self.client = FixtureClient(adapter, self.cfg)
            self.root, self.parent, self.target = uid(201), uid(211), uid(212)
            self.parent_raw = original(211, 201, 1, body='Our published answer.')
            self.target_raw = {**original(212, 201, body='Current reply.'), 'parent_id': self.parent}
            self.parent_raw['replies'] = [self.target_raw]
            self.client.comments = [original(209, 201), self.parent_raw]
        else:
            self.client = ClawdChat()
            self.root, self.parent, self.target = uid(100), uid(11), uid(12)
            self.parent_raw = clawd_original(11, content='Our published answer.', author={'id': uid(1), 'name': 'owner'})
            self.target_raw = clawd_original(12, content='Current reply.', parent_id=self.parent)
            self.client.originals = {self.root: clawd_original(100, title='Thread'),
                                    self.parent: self.parent_raw, self.target: self.target_raw}
        self.store.prepare_collection()
        self.store.save_collection(self.source, uid(1), adapter, 0, Batch())
        self.store.save(self.source, uid(1), [
            {**mail(900), 'thread_id': self.root},
            {**mail(902), 'id': self.target, 'thread_id': self.root,
             'parent_id': self.parent, 'body': 'Saved reply.'}])
        self.ref = providers.parent_reference(adapter, self.root, self.parent)
        self.store.mark(self.source, uid(900), 'replied', ref=self.ref)
        self.store.mark(self.source, self.target, 'needs_reply')

    def clawdchat_context(self, *, local=False):
        """The context command with the invented ClawdChat in the place of the transport. There is no key file
        for its client to read."""
        before = len(self.client.asked)
        result = commands.execute(self.store, 'context', source=self.source, id=self.target,
                                  sources={self.source: self.cfg}, local=local, fetch=self.client)
        if local or self.store.is_paused(self.source): self.assertEqual(len(self.client.asked), before)
        return result

    def context(self, *, local=False):
        if self.adapter == 'clawdchat':
            return self.clawdchat_context(local=local)
        with patch.object(providers, 'Client', return_value=self.client) as factory:
            result = commands.execute(self.store, 'context', source=self.source, id=self.target,
                                      sources={self.source: self.cfg}, local=local)
            if local or self.store.is_paused(self.source): factory.assert_not_called()
            return result

    def test_both_sources_resolve_own_parent_current_text_and_exact_exchange_without_writes(self):
        for adapter in ('moltbook', 'clawdchat'):
            with self.subTest(adapter=adapter), patch.object(providers, 'PAGE_SIZE', 1):
                self.setup_source(adapter)
                before = self.path.read_bytes()
                result, code = self.context()
                self.assertEqual((code, result['complete'], result['fetched']), (0, True, True))
                self.assertEqual(result['root']['id'], self.root)
                self.assertEqual(result['parent']['message']['body'], 'Our published answer.')
                self.assertEqual(result['target']['message']['body'], 'Saved reply.')
                self.assertEqual(result['target']['current_message']['body'], 'Current reply.')
                self.assertIs(result['target']['differs_from_saved'], True)
                self.assertTrue(result['target']['message']['needs_reply'])
                self.assertEqual(result['previous_exchange']['status'], 'linked')
                self.assertEqual([m['id'] for m in result['previous_exchange']['messages']], [uid(900)])
                self.assertTrue(all(not auth and 'notifications' not in path for path, _, auth in self.client.calls))
                self.assertEqual(self.path.read_bytes(), before)
                for paused in (False, True):
                    self.store.set_paused(self.source, paused); self.client.calls.clear()
                    result, code = self.context(local=not paused)
                    self.assertEqual((code, result['fetched'], result['previous_exchange']['status']), (1, False, 'linked'))
                    self.assertEqual(self.client.calls, [])

    def test_thread_only_or_other_comment_reference_never_claims_a_previous_exchange(self):
        for adapter in ('moltbook', 'clawdchat'):
            with self.subTest(adapter=adapter):
                self.setup_source(adapter)
                for ref in (providers.parent_reference(adapter, self.root, self.root), self.ref + '/',
                            self.ref.replace(self.parent, uid(999))):
                    self.store.mark(self.source, uid(900), 'replied', ref=ref)
                    result, _ = self.context(local=True)
                    self.assertEqual(result['previous_exchange']['status'], 'unmatched')

    def test_moltbook_partial_search_and_complete_absence_are_distinct(self):
        self.setup_source('moltbook')
        with patch.object(providers, 'PAGE_SIZE', 1), patch.object(providers, 'MAX_PAGES', 1):
            result, code = self.context()
            self.assertEqual(result['target']['remote_status'], 'unavailable')
            self.assertEqual(result['target']['error'], 'budget_exhausted')
            self.assertIsNone(result['target']['differs_from_saved'])
        self.client.comments = []
        result, code = self.context()
        self.assertEqual(result['target']['remote_status'], 'missing')
        self.assertIsNone(result['target']['current_message'])
        self.assertEqual(code, 1)

    def test_moltbook_reuses_received_parent_and_root_within_the_shared_budget(self):
        self.setup_source('moltbook')
        get, remaining = self.client.get, [3]  # One root and two comment pages fit.
        def bounded(path, params=None, **kwargs):
            if not remaining[0]: raise MailError('budget_exhausted')
            remaining[0] -= 1
            return get(path, params, **kwargs)
        self.client.get = bounded
        with patch.object(providers, 'PAGE_SIZE', 1):
            result, code = self.context()
            self.assertEqual((code, result['complete']), (0, True))
            self.assertEqual(result['parent']['message']['body'], 'Our published answer.')
            self.assertEqual(result['previous_exchange']['status'], 'linked')
            self.assertEqual(len(self.client.calls), 3)
            # Originals are cached only for that command, never across later reads.
            self.parent_raw['content'] = 'An edited answer.'
            remaining[0] = 3; self.client.calls.clear()
            result, code = self.context()
            self.assertEqual((code, result['parent']['message']['body']), (0, 'An edited answer.'))
            self.assertEqual(len(self.client.calls), 3)

        def missing_page(path, params=None, **kwargs):
            if path.endswith('/comments'):
                raise HTTPError('https://example.invalid', 404, '', {}, io.BytesIO())
            return get(path, params, **kwargs)
        self.client.get = missing_page
        result, _ = self.context()
        self.assertEqual((result['target']['remote_status'], result['target']['error']), ('unavailable', 'http_404'))

    def test_moltbook_unknown_thread_and_bad_parent_are_not_guessed(self):
        self.setup_source('moltbook')
        self.parent_raw['post_id'] = uid(999)
        result, code = self.context()
        self.assertEqual((code, result['parent']['error']), (1, 'invalid_response'))
        self.assertEqual(result['previous_exchange']['reason'], 'parent_invalid')
        with patch.object(self.client, 'get', side_effect=HTTPError('https://example.invalid', 404, '', {}, io.BytesIO())):
            status, error, message = providers.moltbook_lookup(self.client, uid(999))
        self.assertEqual((status, error, message), ('unknown', 'thread_unknown', None))
        with patch.object(self.client, 'get', side_effect=HTTPError('https://example.invalid', 404, '', {}, io.BytesIO())):
            status, error, message = providers.moltbook_lookup(self.client, self.target, self.root)
        self.assertEqual((status, error, message), ('unavailable', 'thread_missing', None))

    def test_clawdchat_lookup_preserves_unavailable_and_malformed_parent_status(self):
        self.setup_source('clawdchat')
        for answer, status in ((404, 'missing'), (410, 'deleted'), (503, 'unavailable')):
            self.client.originals[self.parent] = answer
            result, code = self.clawdchat_context()
            self.assertEqual((code, result['parent']['status'], result['parent']['error']),
                             (1, status, 'http_' + str(answer)))
            self.assertEqual(result['previous_exchange']['status'], 'linked')
        self.parent_raw['is_deleted'] = True
        self.client.originals[self.parent] = self.parent_raw
        result, code = self.clawdchat_context()
        self.assertEqual((code, result['parent']['status'], result['parent']['error']), (1, 'deleted', None))
        self.assertEqual(result['previous_exchange']['status'], 'linked')
        del self.parent_raw['is_deleted']
        self.parent_raw['post']['is_deleted'] = True
        result, code = self.clawdchat_context()
        self.assertEqual((code, result['parent']['status'], result['parent']['error']), (1, 'unavailable', 'thread_deleted'))
        self.assertEqual(result['previous_exchange']['status'], 'linked')
        del self.parent_raw['post']['is_deleted']
        self.parent_raw['post_id'] = uid(999)
        self.client.originals[self.parent] = self.parent_raw
        result, code = self.clawdchat_context()
        self.assertEqual((code, result['parent']['error']), (1, 'invalid_response'))
        self.assertEqual(result['previous_exchange']['reason'], 'parent_invalid')

    def test_conflicting_stored_root_is_rejected_locally_and_after_lookup_failure(self):
        source = 'clawdchat'
        cfg = {'account_id': uid(1), 'adapter': source}
        target = dict(mail(10), parent_id=uid(100))
        self.store.save(source, uid(1), [target, dict(mail(100), thread_id=uid(999))])
        for action in ('read', 'needs_reply', 'replied'):
            self.store.mark(source, uid(10), action,
                            ref='https://example.invalid/reply' if action == 'replied' else None)
        saved = self.store.show(source, uid(10))
        brief = self.store.page(context='brief')['messages'][0]['brief']
        self.assertEqual(brief['root']['reason'], 'thread_mismatch')
        before = self.path.read_bytes()
        board = ClawdChat()
        board.down = 503
        for local in (True, False):
            with self.subTest(local=local):
                result, code = commands.execute(self.store, 'context', source=source, id=uid(10),
                                                sources={source: cfg}, local=local, fetch=board)
                self.assertEqual(bool(board.asked), not local)
                self.assertEqual((code, result['complete']), (1, False))
                for role in ('root', 'parent'):
                    self.assertEqual((result[role]['status'], result[role]['error'], result[role]['id']),
                                     ('unavailable', 'invalid_response', uid(100)))
                    self.assertIsNone(result[role]['message'])
                self.assertEqual(result['target']['message'], saved)
                self.assertEqual(result['target']['status'], 'available')
                self.assertEqual(result['previous_exchange']['reason'], 'parent_invalid')
                self.assertEqual(self.path.read_bytes(), before)

    def test_current_originals_replace_conflicting_stored_relatives(self):
        self.setup_source('clawdchat')
        self.store.save(self.source, uid(1), [
            dict(mail(100), thread_id=uid(999)), dict(mail(11), thread_id=uid(999))])
        saved = self.store.show(self.source, self.target)
        before = self.path.read_bytes()
        result, code = self.clawdchat_context()
        self.assertEqual((code, result['complete']), (0, True))
        for role, expected in (('root', self.root), ('parent', self.parent)):
            self.assertEqual((result[role]['status'], result[role]['origin'], result[role]['id']),
                             ('available', 'remote', expected))
            self.assertEqual(result[role]['message']['thread_id'], self.root)
        self.assertEqual(result['target']['message'], saved)
        self.assertEqual(result['target']['current_message']['body'], 'Current reply.')
        self.assertEqual(result['previous_exchange']['status'], 'linked')
        self.assertEqual(self.path.read_bytes(), before)

    def test_matching_local_fourclaw_anchor_and_botnet_absent_parent_stay_available(self):
        self.store.save('fourclaw', 'reader', [mail(100),
                        dict(mail(10), parent_id=uid(100)), mail(11)])
        self.store.save('botnet', 'reader', [mail(100), dict(mail(20), id='opaque:message-20')])
        before = self.path.read_bytes()
        board = FakeBoard([])  # It has no answer, and it is asked nothing.
        for mid in (uid(10), uid(11)):
            result, code = commands.execute(self.store, 'context', source='fourclaw', id=mid, local=True, fetch=board)
            self.assertEqual((code, result['complete'], result['parent']['id']), (0, True, uid(100)))
        result, code = commands.execute(self.store, 'context', source='botnet', id='opaque:message-20', local=True,
                                        fetch=board)
        self.assertEqual((code, result['complete'], result['parent']['status']), (0, True, 'none'))
        self.assertEqual(board.asked, [])
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
