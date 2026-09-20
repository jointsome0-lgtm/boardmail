"""Interrupted publishers and local recovery; every message and external effect is invented."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from boardmail import commands, replies
from boardmail.store import Store
from examples.fixtures import uid
from test_mail import mail


class ReplyRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'inbox.sqlite3'
        self.store = Store(self.path); self.store.initialize()
        self.store.save('moltbook', uid(2), [mail(10), mail(11)])
        self.body = 'A synthetic reply. Кириллица, `backticks`, $HOME.\r\nSecond line.\n'
        self.ref = 'https://board.example.invalid/post/100#comment-200'
        self.file = self.root / 'reply.txt'; self.file.write_bytes(self.body.encode('utf-8'))

    def command(self, action, *, id=None, **options):
        return commands.outcome(lambda: commands.execute(Store(self.path), 'reply_' + action,
            source='moltbook', id=id or uid(10), **options))

    def cli(self, action, *args):
        run = subprocess.run([sys.executable, '-m', 'boardmail', '--db', str(self.path), 'reply', action,
                              'moltbook', uid(10), *map(str, args)], capture_output=True, text=True, timeout=10)
        return json.loads(run.stdout), run.returncode

    def test_message_show_exposes_reply_state_and_followable_recovery_without_writing(self):
        self.store.mark('moltbook', uid(10), 'needs_reply')
        for phase, state, next_action in (
                ('empty', None, None),
                ('prepared', 'prepared', 'begin_before_publishing'),
                ('begun', 'unknown', 'read_back_before_retry'),
                ('marked_replied', 'unknown', 'read_back_before_retry'),
                ('confirmed', 'confirmed', 'do_not_publish_again')):
            with self.subTest(phase=phase):
                if phase == 'prepared':
                    key = self.command('prepare', body=self.body)[0]['reply']['idempotency_key']
                elif phase == 'begun':
                    self.command('begin', key=key)
                elif phase == 'marked_replied':
                    self.store.mark('moltbook', uid(10), 'replied', ref=self.ref)
                elif phase == 'confirmed':
                    self.command('confirm', key=key, ref=self.ref, readback_body=self.body)
                before = self.path.read_bytes()
                run = subprocess.run([sys.executable, '-m', 'boardmail', '--db', str(self.path),
                    'show', 'moltbook', uid(10)], capture_output=True, text=True, timeout=10)
                self.assertEqual(run.returncode, 0, run.stderr)
                result = json.loads(run.stdout)
                self.assertEqual(result['message'], self.store.show('moltbook', uid(10)))
                self.assertEqual(result['event'], 'message')
                summary = result['reply_attempt']
                if state is None:
                    self.assertIsNone(summary)
                else:
                    self.assertEqual((summary['state'], summary['next_action']), (state, next_action))
                    self.assertNotIn('body', summary)
                    route = summary['show']
                    self.assertEqual(route['tool'], 'boardmail_reply_show')
                    args = route['arguments']
                    recovered = subprocess.run([sys.executable, '-m', 'boardmail', '--db', str(self.path),
                        *route['command'].split(), args['source'], args['id']],
                        capture_output=True, text=True, timeout=10)
                    self.assertEqual(recovered.returncode, 0, recovered.stderr)
                    attempt = json.loads(recovered.stdout)['reply']
                    self.assertEqual((attempt['state'], attempt['body'], attempt['idempotency_key']),
                                     (state, self.body, key))
                self.assertEqual(self.path.read_bytes(), before)

    def test_process_exit_after_external_effect_recovers_same_body_key_and_receipt(self):
        self.store.mark('moltbook', uid(10), 'read')
        self.store.mark('moltbook', uid(10), 'needs_reply')
        incoming = self.store.show('moltbook', uid(10))
        effect = self.root / 'external-effect.json'
        # A separate process performs a fake external write, then dies before recording its receipt.
        script = '''
import json, os, sys
from pathlib import Path
from boardmail import commands
from boardmail.store import Store
store = Store(Path(sys.argv[1]))
body = Path(sys.argv[3]).read_bytes().decode('utf-8')
target = dict(source='moltbook', id=sys.argv[2])
prepared, code = commands.execute(store, 'reply_prepare', body=body, **target)
assert code == 0 and not prepared['send_allowed']
key = prepared['reply']['idempotency_key']
begun, code = commands.execute(store, 'reply_begin', key=key, **target)
assert code == 0 and begun['send_allowed']
Path(sys.argv[4]).write_text(json.dumps({'key':key, 'body':body, 'writes':1}), encoding='utf-8')
os._exit(79)
'''
        run = subprocess.run([sys.executable, '-c', script, str(self.path), uid(10), str(self.file), str(effect)],
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 79, run.stderr)
        external = json.loads(effect.read_text())
        shown, code = self.cli('show')
        self.assertEqual(code, 0)
        reply = shown['reply']
        self.assertEqual((reply['state'], reply['body'], reply['idempotency_key']),
                         ('unknown', external['body'], external['key']))
        self.assertEqual(reply['body_sha256'], hashlib.sha256(self.file.read_bytes()).hexdigest())
        self.assertEqual((shown['next_action'], shown['send_allowed']), ('read_back_before_retry', False))
        self.assertEqual(self.store.show('moltbook', uid(10)), incoming)
        repeated, code = self.cli('prepare', '--body-file', self.file)
        self.assertEqual((code, repeated['changed'], repeated['reply']), (0, False, reply))
        again, code = self.cli('begin', '--key', external['key'])
        self.assertEqual((code, again['send_allowed'], again['reply']), (0, False, reply))
        # Reconciliation reads the external effect, rather than assuming a missing receipt means no write.
        readback = self.root / 'readback.txt'; readback.write_bytes(external['body'].encode('utf-8'))
        confirmed, code = self.cli('confirm', '--key', external['key'], '--ref', self.ref, '--readback-file', readback)
        self.assertEqual((code, confirmed['reply']['state']), (0, 'confirmed'))
        self.assertEqual(confirmed['reply']['readback_sha256'], reply['body_sha256'])
        self.assertEqual(confirmed['message']['reply_ref'], self.ref)
        self.assertEqual(confirmed['message']['read_at'], incoming['read_at'])
        self.assertTrue(confirmed['message']['needs_reply'])
        self.assertEqual(confirmed['confirmation_basis'], 'caller_supplied_readback')
        self.assertFalse(confirmed['remote_verified'])
        self.assertFalse(confirmed['publication_performed'])
        saved = self.path.read_bytes()
        repeat, code = self.cli('confirm', '--key', external['key'], '--ref', self.ref, '--readback-file', readback)
        self.assertEqual((code, repeat['changed'], repeat['reply']), (0, False, confirmed['reply']))
        self.assertEqual(saved, self.path.read_bytes())
        self.assertEqual(json.loads(effect.read_text())['writes'], 1)
        self.assertFalse(self.cli('begin', '--key', external['key'])[0]['send_allowed'])

    def test_concurrent_prepare_and_begin_have_one_identity_and_one_first_send(self):
        for action, kwargs in [('prepare', {'body':self.body}), ('begin', None)]:
            if kwargs is None:
                kwargs = {'key': self.command('show')[0]['reply']['idempotency_key']}
            barrier = threading.Barrier(2)
            def invoke():
                barrier.wait(timeout=5)
                return self.command(action, **kwargs)
            with ThreadPoolExecutor(2) as pool:
                futures = [pool.submit(invoke) for _ in range(2)]
                results = [future.result(timeout=10) for future in futures]
            self.assertEqual([code for _, code in results], [0, 0])
            self.assertEqual(sum(result['changed'] for result, _ in results), 1)
            self.assertEqual(len({result['reply']['idempotency_key'] for result, _ in results}), 1)
            self.assertEqual(sum(result['send_allowed'] for result, _ in results), int(action == 'begin'))

    def test_confirmation_basis_requires_a_confirmed_attempt_not_a_reply_mark(self):
        absent, _ = self.cli('show')
        prepared, _ = self.cli('prepare', '--body-file', self.file)
        key = prepared['reply']['idempotency_key']
        unknown, _ = self.cli('begin', '--key', key)
        self.store.mark('moltbook', uid(10), 'replied', ref=self.ref)
        before = self.path.read_bytes()
        marked, _ = self.cli('show')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(marked['reply']['state'], 'unknown')
        self.assertEqual(marked['message']['reply_ref'], self.ref)
        for state, result in [('absent', absent), ('prepared', prepared),
                              ('unknown', unknown), ('independently_marked_replied', marked)]:
            with self.subTest(state=state):
                self.assertIsNone(result['confirmation_basis'])
        readback = self.root / 'readback.txt'; readback.write_bytes(self.body.encode('utf-8'))
        confirmed, code = self.cli('confirm', '--key', key, '--ref', self.ref, '--readback-file', readback)
        self.assertEqual((code, confirmed['reply']['state']), (0, 'confirmed'))
        self.assertEqual(confirmed['confirmation_basis'], 'caller_supplied_readback')
        shown, _ = self.cli('show')
        self.assertEqual(shown['confirmation_basis'], 'caller_supplied_readback')

    def test_explicit_draft_replacement_fences_stale_begin_and_never_replaces_unknown(self):
        first = self.command('prepare', body=self.body)[0]['reply']
        original = self.path.read_bytes()
        result, code = self.command('prepare', body='Changed draft')
        self.assertEqual((code, result['error']), (2, 'reply_body_conflict'))
        self.assertEqual(self.path.read_bytes(), original)
        second, code = self.command('prepare', body='Changed draft', replace_key=first['idempotency_key'])
        self.assertEqual(code, 0)
        key = second['reply']['idempotency_key']
        self.assertNotEqual(key, first['idempotency_key'])
        original = self.path.read_bytes()
        for action, args in [('begin', {'key': first['idempotency_key']}),
                             ('prepare', {'body':self.body, 'replace_key':first['idempotency_key']})]:
            result, code = self.command(action, **args)
            self.assertEqual((code, result['error']), (2, 'reply_key_mismatch'))
            self.assertEqual(self.path.read_bytes(), original)
        self.assertTrue(self.command('begin', key=key)[0]['send_allowed'])
        original = self.path.read_bytes()
        result, code = self.command('prepare', body='Yet another draft', replace_key=key)
        self.assertEqual((code, result['error']), (2, 'reply_already_started'))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.command('show')[0]['reply']['idempotency_key'], key)

    def test_confirmation_requires_started_key_matching_readback_and_consistent_reference(self):
        key = self.command('prepare', body=self.body)[0]['reply']['idempotency_key']
        valid = {'key': key, 'ref': self.ref, 'readback_body':self.body}
        result, code = self.command('confirm', **valid)
        self.assertEqual((code, result['error']), (2, 'reply_not_started'))
        self.command('begin', key=key)
        for change, error in [({'key':'wrong-key'}, 'reply_key_mismatch'),
                              ({'readback_body':self.body.replace('\r\n', '\n')}, 'reply_readback_mismatch'),
                              ({'readback_body':self.body[:-1]}, 'reply_readback_mismatch'),
                              ({'ref':'https://user:password@example.invalid/reply'}, 'reply_ref_required')]:
            before = self.path.read_bytes()
            result, code = self.command('confirm', **{**valid, **change})
            self.assertEqual((code, result['error']), (2, error))
            self.assertEqual(self.path.read_bytes(), before)
        self.command('confirm', **valid)
        before = self.path.read_bytes()
        result, code = self.command('confirm', **{**valid, 'ref':self.ref + '0'})
        self.assertEqual((code, result['error']), (2, 'reply_reference_conflict'))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.store.show('moltbook', uid(10))['reply_ref'], self.ref)

    def test_legacy_reply_mark_is_never_overwritten_or_mistaken_for_verification(self):
        self.store.mark('moltbook', uid(10), 'replied', ref=self.ref)
        before = self.path.read_bytes()
        shown, code = self.command('show')
        self.assertEqual((code, shown['reply'], shown['next_action']), (0, None, 'inspect_recorded_reply'))
        self.assertIsNone(shown['confirmation_basis'])
        result, code = self.command('prepare', body=self.body)
        self.assertEqual((code, result['error']), (2, 'reply_already_recorded'))
        self.assertEqual(before, self.path.read_bytes())
        key = self.command('prepare', id=uid(11), body=self.body)[0]['reply']['idempotency_key']
        self.command('begin', id=uid(11), key=key)
        other = self.ref + '-manual'
        self.store.mark('moltbook', uid(11), 'replied', ref=other)
        before = self.path.read_bytes()
        result, code = self.command('confirm', id=uid(11), key=key, ref=self.ref, readback_body=self.body)
        self.assertEqual((code, result['error']), (2, 'reply_reference_conflict'))
        self.assertEqual(before, self.path.read_bytes())

    def test_missing_or_invalid_inputs_do_not_create_a_journal(self):
        before = self.path.read_bytes()
        for action, options, error in [
            ('prepare', {'body':''}, 'invalid_reply_body'),
            ('prepare', {'body':'x' * (replies.MAX_BODY_BYTES + 1)}, 'invalid_reply_body'),
            ('prepare', {'body':'я' * (replies.MAX_BODY_BYTES // 2 + 1)}, 'invalid_reply_body'),
            ('prepare', {'body':'bad\ud800'}, 'invalid_reply_body'),
            ('prepare', {'body':'null\0byte'}, 'invalid_reply_body'),
            ('prepare', {'body':self.body, 'id':'missing'}, 'message_not_found'),
            ('prepare', {'body':self.body, 'replace_key':'missing'}, 'reply_key_mismatch'),
            ('begin', {'key':'missing'}, 'reply_not_prepared'),
            ('show', {'body':self.body}, 'invalid_arguments'),
            ('begin', {}, 'invalid_arguments'),
        ]:
            result, code = self.command(action, **options)
            self.assertEqual((code, result['error']), (2, error))
            self.assertNotIn(self.body, json.dumps(result))
            self.assertEqual(self.path.read_bytes(), before)
        with self.store.connect() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='reply_attempts'").fetchone())
        self.file.write_bytes(b'\xff')
        self.assertEqual(self.cli('prepare', '--body-file', self.file)[0]['error'], 'invalid_reply_body')
        self.assertEqual(self.path.read_bytes(), before)

    def test_confirmation_transaction_rolls_back_both_receipt_and_mark_on_failure(self):
        key = self.command('prepare', body=self.body)[0]['reply']['idempotency_key']
        self.command('begin', key=key)
        with self.store.connect(write=True) as db:
            db.execute("""CREATE TRIGGER refuse_reply_mark BEFORE UPDATE OF replied_at ON messages
                          BEGIN SELECT RAISE(ABORT, 'simulated storage failure'); END""")
        before = self.path.read_bytes()
        result, code = self.command('confirm', key=key, ref=self.ref, readback_body=self.body)
        self.assertEqual((code, result['error']), (2, 'local_state_error'))
        self.assertEqual(self.path.read_bytes(), before)
        shown = self.command('show')[0]
        self.assertEqual(shown['reply']['state'], 'unknown')
        self.assertIsNone(shown['message']['replied_at'])

    def test_older_databases_read_without_migration_and_prepare_preserves_schema_and_marks(self):
        for version in (1, 2):
            with self.subTest(version=version):
                path = self.root / f'old-{version}.sqlite3'
                with closing(sqlite3.connect(path)) as db:
                    db.executescript((Path(__file__).parent / 'fixtures/v1.sql').read_text())
                store = Store(path)
                if version == 2:
                    store.prepare_collection()
                messages = store.page()['messages']
                target = messages[0]
                before = path.read_bytes()
                shown, _ = commands.execute(store, 'reply_show', source=target['source'], id=target['id'])
                self.assertIsNone(shown['reply'])
                message, _ = commands.execute(store, 'show', source=target['source'], id=target['id'])
                self.assertIsNone(message['reply_attempt'])
                self.assertEqual(message['message'], shown['message'])
                self.assertEqual(path.read_bytes(), before)
                # Choose an incoming without an earlier replied mark in the legacy fixture.
                target = next(m for m in messages if m['reply_ref'] is None)
                result, _ = commands.execute(store, 'reply_prepare', source=target['source'], id=target['id'], body=self.body)
                self.assertEqual(result['reply']['state'], 'prepared')
                self.assertEqual(store.page()['messages'], messages)
                with store.connect() as db:
                    self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], version)

    def test_every_source_uses_the_same_local_protocol_even_when_paused(self):
        from boardmail.config import COVERAGE
        for n, source in enumerate([*COVERAGE, 'custom'], 1):
            with self.subTest(source=source):
                self.store.save(source, uid(2), [mail(20)])
                self.store.set_paused(source, True)
                original = self.store.collection_state(source, uid(2), source)
                with patch('boardmail.providers.collect', side_effect=AssertionError('No network allowed')):
                    result, _ = commands.execute(self.store, 'reply_prepare', source=source, id=uid(20), body=self.body)
                    key = result['reply']['idempotency_key']
                    result, _ = commands.execute(self.store, 'reply_begin', source=source, id=uid(20), key=key)
                    self.assertTrue(result['send_allowed'])
                    commands.execute(self.store, 'reply_confirm', source=source, id=uid(20), key=key,
                                     ref=self.ref, readback_body=self.body)
                self.assertTrue(self.store.is_paused(source))
                self.assertEqual(self.store.collection_state(source, uid(2), source), original)


if __name__ == '__main__':
    unittest.main()
