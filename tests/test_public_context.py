"""Moltbook and ClawdChat context uses public originals and exact reply IDs."""
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from boardmail import adapter_clawdchat, commands, providers
from boardmail.adapters import Batch
from boardmail.config import MailError
from boardmail.store import Store
from examples.fixtures import FixtureClient, original, uid
from test_clawdchat import FixtureClient as ClawdClient, original as clawd_original
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
            self.client = ClawdClient()
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

    def context(self, *, local=False):
        module = adapter_clawdchat if self.adapter == 'clawdchat' else providers
        with patch.object(module, 'Client', return_value=self.client) as factory:
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
        for error, status in (('http_404', 'missing'), ('http_410', 'deleted'), ('http_503', 'unavailable')):
            self.client.originals[self.parent] = MailError(error)
            result, code = self.context()
            self.assertEqual((code, result['parent']['status'], result['parent']['error']), (1, status, error))
            self.assertEqual(result['previous_exchange']['status'], 'linked')
        self.parent_raw['is_deleted'] = True
        self.client.originals[self.parent] = self.parent_raw
        result, code = self.context()
        self.assertEqual((code, result['parent']['status'], result['parent']['error']), (1, 'deleted', None))
        self.assertEqual(result['previous_exchange']['status'], 'linked')
        del self.parent_raw['is_deleted']
        self.parent_raw['post']['is_deleted'] = True
        result, code = self.context()
        self.assertEqual((code, result['parent']['status'], result['parent']['error']), (1, 'unavailable', 'thread_deleted'))
        self.assertEqual(result['previous_exchange']['status'], 'linked')
        del self.parent_raw['post']['is_deleted']
        self.parent_raw['post_id'] = uid(999)
        self.client.originals[self.parent] = self.parent_raw
        result, code = self.context()
        self.assertEqual((code, result['parent']['error']), (1, 'invalid_response'))
        self.assertEqual(result['previous_exchange']['reason'], 'parent_invalid')


if __name__ == '__main__':
    unittest.main()
