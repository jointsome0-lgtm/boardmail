"""Remote reply reconciliation with invented provider originals, never publication."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from boardmail import adapter_clawdchat, cli, commands, providers, verification
from boardmail.adapters import Batch
from boardmail.store import Store
from examples.fixtures import FixtureClient, named, original, uid
from test_clawdchat import FixtureClient as ClawdClient, original as clawd_original
from test_mail import mail


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'mail.sqlite3'
        self.store = Store(self.path); self.store.initialize()
        self.body = 'A synthetic reply. Кириллица.\r\nExact newline.\n'
        self.root, self.target, self.reply = uid(301), uid(310), uid(320)

    def setup_source(self, adapter, *, root_target=False):
        self.adapter, self.source = adapter, 'alias-' + adapter
        self.settings = {'account_id': uid(1), 'adapter': adapter, 'api_key_file': 'missing.key'}
        if root_target:
            self.target = self.root
        self.store.save_collection(self.source, uid(1), adapter, 0, Batch())
        self.store.save(self.source, uid(1), [{**mail(310), 'id': self.target, 'thread_id': self.root}])
        self.store.mark(self.source, self.target, 'read')
        self.store.mark(self.source, self.target, 'needs_reply')
        if adapter == 'postingboard':
            self.client = FixtureClient(adapter, self.settings)
            self.raw = named(320, 301, 1, body=self.body, reply_to=None if root_target else 310)
            self.client.others[self.reply] = self.raw
        elif adapter == 'clawdchat':
            self.client = ClawdClient()
            self.raw = clawd_original(320, post_id=self.root, post={'id': self.root, 'title': 'Example'},
                parent_id=None if root_target else self.target, content=self.body, author={'id': uid(1), 'name': 'owner'})
            self.client.originals = {self.reply: self.raw}
        else:
            self.client = FixtureClient(adapter, self.settings)
            flags = {'held': False} if adapter == 'the-colony' else {'verification_status': 'verified', 'is_deleted': False, 'is_spam': False}
            self.client.root = {**original(301, 301, 1, colony=adapter == 'the-colony'), **flags, 'title': 'Example'}
            self.raw = {**original(320, 301, 1, colony=adapter == 'the-colony', body=self.body), **flags,
                        'parent_id': None if root_target else self.target}
            self.client.comments = [self.raw]
        self.ref = providers.parent_reference(adapter, self.root, self.reply)
        self.key = self.call('prepare', body=self.body)[0]['reply']['idempotency_key']
        self.call('begin', key=self.key)
        self.before = self.path.read_bytes()
        self.before_result = self.call('show')[0]

    def call(self, action='verify', **options):
        if action == 'verify':
            options = {'key': self.key, 'ref': self.ref, **options}
        module = adapter_clawdchat if self.adapter == 'clawdchat' else providers
        with patch.object(module, 'Client', return_value=self.client):
            return commands.outcome(lambda: commands.execute(self.store, 'reply_' + action,
                sources={self.source: self.settings}, source=self.source, id=self.target, **options))

    def assert_unverified(self, reason=None):
        result, code = self.call()
        self.assertEqual(code, 1, result)
        self.assertFalse(result['send_allowed'])
        self.assertFalse(result['remote_verified'])
        self.assertEqual(result['reply']['state'], 'unknown')
        self.assertIsNone(result['confirmation_basis'])
        self.assertIsNone(result['message']['replied_at'])
        if reason:
            self.assertEqual(result['verification']['reason'], reason, result)
        self.assertEqual(result['reply'], self.before_result['reply'])
        self.assertEqual(result['message'], self.before_result['message'])
        self.assertIsNone(result['verification_receipt'])
        before = self.path.read_bytes()
        self.assertEqual(self.call()[1], 1)
        self.assertEqual(self.path.read_bytes(), before)
        return result

    def test_candidate_survives_timeout_and_reopen_without_becoming_evidence(self):
        self.setup_source('postingboard')
        with patch.object(verification, 'read', side_effect=TimeoutError('synthetic timeout')):
            failed, code = self.call()
        self.assertEqual(code, 1)
        self.store = Store(self.path)
        before = self.path.read_bytes()
        shown, code = self.call('show')
        self.assertEqual(code, 0)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(shown['reply_candidates'], failed['reply_candidates'])
        candidate, = shown['reply_candidates']
        self.assertEqual(candidate['reply_ref'], self.ref)
        self.assertEqual(candidate['adapter'], self.adapter)
        self.assertEqual(candidate['account_id'], self.settings['account_id'])
        self.assertEqual(candidate['status'], 'unverified')
        self.assertEqual(candidate['identity_basis'], 'parsed_reference')
        self.assertIsInstance(candidate['recorded_at'], int)
        self.assertEqual(shown['reply'], self.before_result['reply'])
        self.assertEqual(shown['message'], self.before_result['message'])
        self.assertIsNone(shown['confirmation_basis'])
        self.assertIsNone(shown['verification_receipt'])
        self.assertFalse(shown['send_allowed'])
        self.assertFalse(shown['remote_verified'])
        self.assertEqual(self.before_result['reply_candidates'], [])
        self.assertTrue(failed['changed'])

    def test_candidate_survives_process_exit_before_provider_read_returns(self):
        self.setup_source('postingboard')
        program = '''import json, os, sys
from unittest.mock import patch
from boardmail import verification
from boardmail.store import Store
path, source, target, key, ref, settings = sys.argv[1:]
with patch.object(verification, 'read', side_effect=lambda *args: os._exit(73)):
    verification.execute(Store(path), {source: json.loads(settings)}, source, target, key=key, ref=ref)
'''
        child = subprocess.run([sys.executable, '-c', program, str(self.path), self.source,
                                self.target, self.key, self.ref, json.dumps(self.settings)],
                               cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15)
        self.assertEqual(child.returncode, 73, child.stderr)
        self.store = Store(self.path)
        shown, code = self.call('show')
        self.assertEqual(code, 0)
        self.assertEqual([c['reply_ref'] for c in shown['reply_candidates']], [self.ref])
        self.assertEqual(shown['reply']['state'], 'unknown')
        self.assertIsNone(shown['reply']['reply_ref'])
        self.assertIsNone(shown['confirmation_basis'])
        self.assertIsNone(shown['verification_receipt'])
        self.assertFalse(shown['send_allowed'])

    def test_candidates_are_bounded_without_replacing_an_earlier_url(self):
        self.setup_source('postingboard')
        refs = [providers.parent_reference(self.adapter, self.root, uid(320 + i)) for i in range(9)]
        with patch.object(verification, 'read', side_effect=TimeoutError()) as read:
            for ref in refs[:8]:
                self.assertEqual(self.call(ref=ref)[1], 1)
            shown = self.call('show')[0]
            self.assertEqual({c['reply_ref'] for c in shown['reply_candidates']}, set(refs[:8]))
            before = self.path.read_bytes()
            self.assertEqual(self.call(ref=refs[0])[1], 1)
            self.assertEqual(self.path.read_bytes(), before)
            calls = read.call_count
            rejected, code = self.call(ref=refs[8])
            self.assertEqual((code, rejected['error']), (2, 'reply_candidate_limit'))
            self.assertEqual(read.call_count, calls)
            self.assertEqual(self.path.read_bytes(), before)
        # The bounded candidate directory does not block independent caller readback.
        confirmed, code = self.call('confirm', key=self.key, ref=refs[8], readback_body=self.body)
        self.assertEqual((code, confirmed['confirmation_basis']), (0, 'caller_supplied_readback'))
        self.assertEqual(confirmed['reply_candidates'], [])
        self.assertIsNone(confirmed['verification_receipt'])

    def test_wrong_candidate_does_not_block_a_different_verified_reply(self):
        self.setup_source('postingboard')
        wrong = providers.parent_reference(self.adapter, self.root, uid(999))
        self.assertEqual(self.call(ref=wrong)[1], 1)
        self.assertEqual(self.call('show')[0]['reply_candidates'][0]['reply_ref'], wrong)
        confirmed, code = self.call()
        self.assertEqual((code, confirmed['confirmation_basis']), (0, 'provider_readback'))
        self.assertEqual(confirmed['reply']['reply_ref'], self.ref)
        self.assertEqual(confirmed['reply_candidates'], [])

    def test_candidate_write_failure_prevents_provider_request(self):
        self.setup_source('postingboard')
        with patch.object(verification, 'read', side_effect=TimeoutError()):
            self.call()
        with self.store.connect(write=True) as db:
            db.execute("CREATE TRIGGER fail_candidate BEFORE INSERT ON reply_candidates "
                       "BEGIN SELECT RAISE(ABORT, 'stop'); END")
        before = self.path.read_bytes()
        with patch.object(verification, 'read') as read:
            result, code = self.call(ref=providers.parent_reference(self.adapter, self.root, uid(999)))
        self.assertEqual((code, result['error']), (2, 'local_state_error'))
        read.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)

    def test_all_providers_and_source_aliases_save_exact_evidence_and_independent_marks(self):
        for adapter in verification.ADAPTERS:
            with self.subTest(adapter=adapter):
                self.setup_source(adapter)
                marks = self.store.show(self.source, self.target)
                result, code = self.call()
                self.assertEqual(code, 0, result)
                self.assertTrue(result['remote_verified'])
                self.assertFalse(result['send_allowed'])
                self.assertEqual(result['reply']['state'], 'confirmed')
                evidence = result['verification_receipt']
                self.assertEqual(evidence, result['verification'])
                self.assertEqual((evidence['author_id'], evidence['thread_id'], evidence['target_id'], evidence['reply_id']),
                                 (uid(1), self.root, self.target, self.reply))
                self.assertEqual(evidence['body_sha256'], result['reply']['body_sha256'])
                self.assertEqual(evidence['idempotency_key'], self.key)
                self.assertEqual(evidence['key_scope'], 'local')
                with self.store.connect() as db:
                    saved = json.loads(db.execute('SELECT evidence FROM reply_verifications WHERE source=?',
                                                   (self.source,)).fetchone()[0])
                self.assertEqual(saved['key_scope'], 'local')
                self.assertEqual(result['message']['read_at'], marks['read_at'])
                self.assertTrue(result['message']['needs_reply'])
                self.assertEqual(result['message']['reply_ref'], self.ref)
                self.assertEqual(result['confirmation_basis'], 'provider_readback')
                self.assertTrue(all(auth == (adapter == 'postingboard') for _, _, auth in self.client.calls))
                self.assertFalse(any('notifications' in path or '/agents/me' in path for path, _, _ in self.client.calls))
                before, calls = self.path.read_bytes(), len(self.client.calls)
                local = self.call('show')[0]
                self.assertFalse(local['remote_verified'])
                self.assertIsNone(local['verification'])
                self.assertEqual(local['verification_receipt'], evidence)
                self.assertEqual(local['confirmation_basis'], 'provider_readback')
                self.assertEqual((self.path.read_bytes(), len(self.client.calls)), (before, calls))
                # A later failed read cannot erase a previous confirmation or imply absence.
                self.raw['is_hidden'] = True
                failed, code = self.call()
                self.assertEqual(code, 1)
                self.assertEqual(failed['reply']['state'], 'confirmed')
                self.assertEqual(failed['verification_receipt'], evidence)
                self.assertEqual(failed['confirmation_basis'], 'provider_readback')
                self.assertFalse(failed['remote_verified'])
                self.assertEqual(self.path.read_bytes(), before)

    def test_unknown_guidance_survives_failed_verify_and_clears_on_confirmation(self):
        self.setup_source('postingboard')
        shown, _ = self.call('show')
        guidance = shown['recovery_guidance']
        self.assertIsInstance(guidance, str)
        self.raw['reply_to_id'] = None
        failed = self.assert_unverified('reply_target_mismatch')
        self.assertEqual(failed['recovery_guidance'], guidance)
        self.assertEqual(failed['reply'], shown['reply'])
        self.assertIsNone(failed['verification_receipt'])
        self.raw['reply_to_id'] = self.target
        confirmed, code = self.call()
        self.assertEqual((code, confirmed['reply']['state']), (0, 'confirmed'))
        self.assertIsNone(confirmed['recovery_guidance'])
        before = self.path.read_bytes()
        self.raw['reply_to_id'] = None
        failed, code = self.call()
        self.assertEqual((code, failed['reply']['state']), (1, 'confirmed'))
        self.assertIsNone(failed['recovery_guidance'])
        self.assertEqual(failed['verification_receipt'], confirmed['verification_receipt'])
        self.assertEqual(self.path.read_bytes(), before)

    def test_older_receipt_gets_local_key_scope_without_rewriting_or_reverification(self):
        self.setup_source('postingboard')
        verified, code = self.call()
        self.assertEqual(code, 0)
        legacy = dict(verified['verification_receipt'])
        legacy.pop('key_scope', None)
        with self.store.connect(write=True) as db:
            db.execute('UPDATE reply_verifications SET evidence=? WHERE source=?',
                       (json.dumps(legacy), self.source))
        before, calls = self.path.read_bytes(), len(self.client.calls)
        shown, code = self.call('show')
        self.assertEqual(code, 0)
        self.assertEqual(shown['verification_receipt'], {**legacy, 'key_scope': 'local'})
        self.assertFalse(shown['remote_verified'])
        self.assertEqual((self.path.read_bytes(), len(self.client.calls)), (before, calls))

    def test_exact_author_target_thread_text_and_provider_status_are_required(self):
        self.setup_source('moltbook')
        cases = [('author', {'id': uid(2), 'name': 'same displayed name'}, 'reply_author_mismatch'),
                 ('parent_id', uid(999), 'reply_target_mismatch'),
                 ('post_id', uid(999), 'invalid_response'),
                 ('content', self.body.replace('\r\n', '\n'), 'reply_readback_mismatch'),
                 ('content', self.body.rstrip(), 'reply_readback_mismatch'),
                 ('verification_status', 'failed', 'reply_provider_not_verified'),
                 ('verification_status', 'pending', 'reply_provider_not_verified'),
                 ('verification_status', 'future_unknown_status', 'reply_provider_not_verified'),
                 ('is_deleted', True, 'reply_deleted'), ('is_spam', True, 'hidden_by_provider'),
                 ('is_truncated', True, 'reply_incomplete'),
                 ('status', 'pending', 'reply_provider_status_unknown'),
                 ('visibility', 'private', 'reply_not_visible')]
        baseline = deepcopy(self.raw)
        for field, value, reason in cases:
            with self.subTest(field=field, value=value):
                self.raw.clear(); self.raw.update(deepcopy(baseline)); self.raw[field] = value
                self.assert_unverified(reason)
        for field in ('author', 'parent_id', 'verification_status', 'is_deleted', 'is_spam'):
            with self.subTest(missing=field):
                self.raw.clear(); self.raw.update(deepcopy(baseline)); del self.raw[field]
                self.assert_unverified()
        self.raw.clear(); self.raw.update(baseline)
        self.client.root['verification_status'] = 'failed'
        self.assert_unverified('reply_provider_not_verified')

    def test_top_level_requires_explicit_parent_or_moltbook_depth_zero(self):
        for adapter in verification.ADAPTERS:
            with self.subTest(adapter=adapter):
                self.setup_source(adapter, root_target=True)
                parent = 'reply_to_id' if adapter == 'postingboard' else 'parent_id'
                del self.raw[parent]
                self.assert_unverified()
                if adapter == 'moltbook':
                    self.raw['depth'] = 0
                else:
                    self.raw[parent] = None
                self.assertEqual(self.call()[1], 0)

    def test_held_colony_and_hidden_clawdchat_are_not_confirmed(self):
        for adapter, field in [('the-colony', 'held'), ('clawdchat', 'is_hidden')]:
            self.setup_source(adapter)
            self.raw[field] = True
            self.assert_unverified()

    def test_missing_original_network_error_and_partial_scan_keep_unknown(self):
        self.setup_source('moltbook')
        self.client.comments = []
        self.assert_unverified('reply_missing')
        self.client.fail = True
        failed = self.assert_unverified('http_503')
        self.assertNotIn('private provider prose', json.dumps(failed))
        self.assertNotIn('secret-token', json.dumps(failed))
        self.client.fail = False
        self.client.comments = [original(319, 301), self.raw]
        with patch.object(providers, 'PAGE_SIZE', 1), patch.object(providers, 'MAX_PAGES', 1):
            self.assert_unverified('budget_exhausted')

    def test_invalid_references_and_preconditions_never_make_a_request(self):
        self.setup_source('moltbook')
        for ref in [self.ref.replace('www.moltbook.com', 'evil.invalid'), self.ref.replace('https:', 'http:'),
                    self.ref.replace('/post/', '/api/v1/posts/'), self.ref.split('#')[0], self.ref + '?x=1',
                    self.ref.replace('www.', 'key@www.'), self.ref.replace('www.', 'www.moltbook.com:8443@www.'),
                    self.ref.replace('/post/', '/post/%2e%2e/'), self.ref.replace(self.root, uid(999)),
                    self.ref.replace('www.moltbook.com', 'www.moltbook.com:8443')]:
            with self.subTest(ref=ref):
                result, code = self.call(ref=ref)
                self.assertEqual((code, result['error']), (2, 'reply_reference_unsupported'))
        self.assertEqual(self.call(key='stale')[0]['error'], 'reply_key_mismatch')
        self.settings['account_id'] = uid(2)
        self.assertEqual(self.call()[0]['error'], 'account_mismatch')
        self.settings['account_id'] = uid(1)
        self.settings['adapter'] = 'clawdchat'
        self.assertEqual(self.call()[0]['error'], 'adapter_mismatch')
        self.settings['adapter'] = 'custom'
        self.assertEqual(self.call()[0]['error'], 'reply_verification_unsupported')
        self.settings['adapter'] = 'moltbook'
        self.assertEqual(self.path.read_bytes(), self.before)
        self.store.set_paused(self.source, True)
        before = self.path.read_bytes()
        self.assertEqual(self.call()[0]['error'], 'source_paused')
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.path.read_bytes(), before)

    def test_state_is_rechecked_after_network_without_holding_a_write_lock(self):
        self.setup_source('postingboard')
        get = self.client.get
        def racing_get(path, *args, **kwargs):
            # A second SQLite connection can write while the request is in flight.
            self.store.set_paused(self.source, True)
            return get(path, *args, **kwargs)
        self.client.get = racing_get
        result, code = self.call()
        self.assertEqual((code, result['error']), (2, 'source_paused'))
        self.assertEqual(self.call('show')[0]['reply']['state'], 'unknown')
        self.assertIsNone(self.store.show(self.source, self.target)['replied_at'])
        self.store.set_paused(self.source, False)
        def conflicting_get(path, *args, **kwargs):
            self.store.mark(self.source, self.target, 'replied', ref='https://example.invalid/other')
            return get(path, *args, **kwargs)
        self.client.get = conflicting_get
        result, code = self.call()
        self.assertEqual((code, result['error']), (2, 'reply_reference_conflict'))
        self.assertEqual(self.call('show')[0]['reply']['state'], 'unknown')

    def test_unrecorded_alias_does_not_take_its_identity_from_current_config(self):
        self.setup_source('moltbook')
        with self.store.connect(write=True) as db:
            db.execute('DELETE FROM adapter_state WHERE source=?', (self.source,))
        for remove_table in (False, True):
            with self.subTest(remove_table=remove_table):
                if remove_table:
                    with self.store.connect(write=True) as db:
                        db.execute('DROP TABLE adapter_state')
                before = self.path.read_bytes()
                result, code = self.call()
                self.assertEqual((code, result['error']), (2, 'reply_adapter_identity_unknown'))
                self.assertEqual(self.client.calls, [])
                self.assertEqual(self.path.read_bytes(), before)

    def test_key_account_and_destination_changes_during_read_cannot_confirm(self):
        self.setup_source('postingboard')
        get = self.client.get
        for table, column, value, expected in (
                ('reply_attempts', 'idempotency_key', 'stale', 'reply_key_mismatch'),
                ('sources', 'account_id', uid(2), 'account_mismatch'),
                ('adapter_state', 'adapter', 'moltbook', 'adapter_mismatch'),
                ('messages', 'thread_id', uid(999), 'reply_target_mismatch')):
            with self.subTest(column=column):
                with self.store.connect() as db:
                    old = db.execute(f'SELECT {column} FROM {table} WHERE source=?', (self.source,)).fetchone()[0]
                def changed_get(path, *args, **kwargs):
                    with self.store.connect(write=True) as db:
                        db.execute(f'UPDATE {table} SET {column}=? WHERE source=?', (value, self.source))
                    return get(path, *args, **kwargs)
                self.client.get = changed_get
                result, code = self.call()
                self.assertEqual((code, result['error']), (2, expected))
                self.assertEqual(self.call('show')[0]['reply']['state'], 'unknown')
                if column == 'idempotency_key':
                    self.assertEqual(self.call('show')[0]['reply_candidates'], [])
                with self.store.connect(write=True) as db:
                    db.execute(f'UPDATE {table} SET {column}=? WHERE source=?', (old, self.source))

    def test_receipt_and_confirmation_roll_back_together(self):
        self.setup_source('postingboard')
        with patch.object(verification, 'read', side_effect=TimeoutError()):
            self.call()
        with self.store.connect(write=True) as db:
            db.execute("CREATE TRIGGER fail_mark BEFORE UPDATE OF replied_at ON messages BEGIN SELECT RAISE(ABORT, 'stop'); END")
        before = self.path.read_bytes()
        self.assertEqual(self.call()[0]['error'], 'local_state_error')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIsNone(self.call('show')[0]['verification_receipt'])
        self.assertEqual(self.call('show')[0]['reply_candidates'][0]['reply_ref'], self.ref)

    def test_successful_verification_on_v1_keeps_the_schema_and_other_mail(self):
        self.path = self.path.parent / 'legacy.sqlite3'
        db = sqlite3.connect(self.path)
        try:
            db.executescript((Path(__file__).parent / 'fixtures/v1.sql').read_text())
        finally:
            db.close()
        self.store = Store(self.path)
        self.adapter = self.source = 'moltbook'
        self.settings = {'account_id': uid(2), 'api_key_file': 'missing.key'}
        self.root, self.target = uid(100), uid(11)
        self.client = FixtureClient('moltbook', self.settings)
        flags = {'verification_status': 'verified', 'is_deleted': False, 'is_spam': False}
        self.client.root = {**original(100, 100), **flags, 'title': 'Legacy example'}
        self.client.comments = [{**original(320, 100, 2, body=self.body), **flags, 'parent_id': self.target}]
        self.ref = providers.parent_reference('moltbook', self.root, self.reply)
        other = self.store.show('moltbook', uid(10))
        self.key = self.call('prepare', body=self.body)[0]['reply']['idempotency_key']
        self.call('begin', key=self.key)
        # A v1 source name identifies its original built-in even without adapter_state.
        expected_client, expected_ref = self.client, self.ref
        self.settings['adapter'] = 'postingboard'
        self.client = FixtureClient('postingboard', self.settings)
        self.client.others[self.reply] = named(320, 100, 2, body=self.body, reply_to=11)
        self.ref = providers.parent_reference('postingboard', self.root, self.reply)
        before = self.path.read_bytes()
        rejected, code = self.call()
        self.assertEqual((code, rejected.get('error')), (2, 'adapter_mismatch'))
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.path.read_bytes(), before)
        del self.settings['adapter']
        self.client, self.ref = expected_client, expected_ref
        before = self.path.read_bytes()
        self.assertEqual(self.call('show')[0]['reply_candidates'], [])
        self.assertEqual(self.path.read_bytes(), before)
        with patch.object(verification, 'read', side_effect=TimeoutError()):
            failed, code = self.call()
        self.assertEqual((code, failed['reply']['state']), (1, 'unknown'))
        self.assertEqual(self.call('show')[0]['reply_candidates'][0]['reply_ref'], self.ref)
        verified, code = self.call()
        self.assertEqual((code, verified['remote_verified']), (0, True))
        self.assertEqual(self.store.show('moltbook', uid(10)), other)
        with self.store.connect() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='adapter_state'").fetchone())
        before = self.path.read_bytes()
        self.assertEqual(self.call('show')[0]['verification_receipt'], verified['verification'])
        self.assertEqual(self.path.read_bytes(), before)

    def test_cli_with_db_still_loads_explicit_config_and_uses_same_verifier(self):
        self.setup_source('postingboard')
        config = self.path.parent / 'config.json'
        config.write_text(json.dumps({'database': str(self.path), 'sources': {self.source: self.settings}}))
        args = cli.parser().parse_args(['--db', str(self.path), '--config', str(config), 'reply', 'verify',
                                      self.source, self.target, '--key', self.key, '--ref', self.ref])
        with patch.object(providers, 'Client', return_value=self.client):
            result, code = cli.run(args)
        self.assertEqual((code, result['reply']['state'], result['remote_verified']), (0, 'confirmed', True))


if __name__ == '__main__':
    unittest.main()
