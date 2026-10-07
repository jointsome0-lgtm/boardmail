"""Interrupted publishers and local recovery; every message and external effect is invented."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from boardmail import commands, replies
from boardmail.store import Store
from examples.fixtures import uid
from test_mail import mail


def run_reply_workers(operations):
    """Join every worker before propagating errors; no unbounded executor shutdown."""
    outcomes = [None] * len(operations)

    def invoke(index, operation):
        try:
            outcomes[index] = (True, operation())
        except BaseException as exc:
            outcomes[index] = (False, exc, exc.__traceback__)

    workers = [threading.Thread(target=invoke, args=(index, operation), daemon=True)
               for index, operation in enumerate(operations)]
    for worker in workers:
        worker.start()
    deadline = time.monotonic() + 10
    for worker in workers:
        worker.join(timeout=max(0, deadline - time.monotonic()))
    if any(worker.is_alive() for worker in workers):
        raise AssertionError('Reply race worker exceeded the bounded join')
    for outcome in outcomes:
        if not outcome[0]:
            raise outcome[1].with_traceback(outcome[2])
    return [outcome[1] for outcome in outcomes]


class WriteBarrierStore(Store):
    """Gate only the first write, before Store acquires its real SQLite transaction."""
    def __init__(self, path, before_write, after_write=None):
        super().__init__(path)
        self.before_write, self.after_write = before_write, after_write

    @contextmanager
    def connect(self, *, write=False, create=False):
        gate = self.before_write if write else None
        if gate is not None:
            self.before_write = None
            gate()
        with super().connect(write=write, create=create) as db:
            yield db
        if gate is not None and self.after_write is not None:
            self.after_write()


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

    def test_marks_expose_recovery_without_changing_attempt_or_other_marks(self):
        for state in (None, 'prepared', 'unknown', 'confirmed'):
            with self.subTest(state=state):
                path = self.root / f'{state}.sqlite3'
                store = Store(path); store.initialize()
                store.save('moltbook', uid(2), [mail(10)])
                if state is not None:
                    prepared, _ = replies.execute(store, 'prepare', 'moltbook', uid(10), body=self.body)
                    key = prepared['reply']['idempotency_key']
                    if state != 'prepared':
                        replies.execute(store, 'begin', 'moltbook', uid(10), key=key)
                    if state == 'confirmed':
                        replies.execute(store, 'confirm', 'moltbook', uid(10), key=key,
                                        ref=self.ref, readback_body=self.body)
                original, _ = replies.execute(store, 'show', 'moltbook', uid(10))
                for action, changed_fields in (
                        ('read', {'read_at'}), ('unread', {'read_at'}),
                        ('needs-reply', {'needs_reply'}), ('clear-reply', {'needs_reply'}),
                        ('replied', {'replied_at', 'reply_ref'})):
                    with self.subTest(action=action):
                        before = store.show('moltbook', uid(10))
                        args = ['mark', action, 'moltbook', uid(10)]
                        if action == 'replied':
                            args += ['--ref', self.ref]
                        run = subprocess.run([sys.executable, '-m', 'boardmail', '--db', str(path), *args],
                                             capture_output=True, text=True, timeout=10)
                        self.assertEqual(run.returncode, 0, run.stderr)
                        result = json.loads(run.stdout)
                        self.assertEqual(result['event'], 'marked')
                        after = result['message']
                        self.assertEqual(after, store.show('moltbook', uid(10)))
                        self.assertEqual({k: v for k, v in before.items() if k not in changed_fields},
                                         {k: v for k, v in after.items() if k not in changed_fields})
                        if action == 'read': self.assertIsNotNone(after['read_at'])
                        elif action == 'unread': self.assertIsNone(after['read_at'])
                        elif action == 'needs-reply': self.assertTrue(after['needs_reply'])
                        elif action == 'clear-reply': self.assertFalse(after['needs_reply'])
                        else:
                            self.assertIsNotNone(after['replied_at'])
                            self.assertEqual(after['reply_ref'], self.ref)
                        summary = result['reply_attempt']
                        snapshot = path.read_bytes()
                        shown, _ = commands.execute(store, 'show', source='moltbook', id=uid(10))
                        self.assertEqual(summary, shown['reply_attempt'])
                        if state is None:
                            self.assertIsNone(summary)
                        else:
                            self.assertEqual(summary['state'], state)
                            self.assertEqual(set(summary), {'state', 'next_action', 'show'})
                            route = summary['show']
                            recovered = subprocess.run([sys.executable, '-m', 'boardmail', '--db', str(path),
                                *route['command'].split(), route['arguments']['source'], route['arguments']['id']],
                                capture_output=True, text=True, timeout=10)
                            self.assertEqual(recovered.returncode, 0, recovered.stderr)
                            journal = json.loads(recovered.stdout)
                            self.assertEqual(journal['reply'], original['reply'])
                            self.assertEqual(journal['confirmation_basis'], original['confirmation_basis'])
                        self.assertEqual(path.read_bytes(), snapshot)

    def test_repeated_same_reference_mark_preserves_reply_recovery(self):
        for number, state, next_action in ((10, 'unknown', 'read_back_before_retry'),
                                           (11, 'confirmed', 'do_not_publish_again')):
            with self.subTest(state=state):
                target = uid(number)
                self.store.mark('moltbook', target, 'read')
                self.store.mark('moltbook', target, 'needs_reply')
                prepared, code = self.command('prepare', id=target, body=self.body)
                self.assertEqual(code, 0)
                key = prepared['reply']['idempotency_key']
                begun, code = self.command('begin', id=target, key=key)
                self.assertEqual((code, begun['send_allowed']), (0, True))
                if state == 'confirmed':
                    confirmed, code = self.command('confirm', id=target, key=key,
                                                  ref=self.ref, readback_body=self.body)
                    self.assertEqual((code, confirmed['confirmation_basis']),
                                     (0, 'caller_supplied_readback'))
                original, code = self.command('show', id=target)
                self.assertEqual((code, original['reply']['state']), (0, state))
                incoming = original['message']
                journal = {k: v for k, v in original.items() if k != 'message'}
                for clock in (100, 200):
                    with self.subTest(clock=clock):
                        with patch('boardmail.store.time.time', return_value=clock):
                            marked, code = commands.outcome(lambda: commands.execute(
                                self.store, 'mark', source='moltbook', id=target,
                                action='replied', ref=self.ref))
                        self.assertEqual((code, marked['event']), (0, 'marked'))
                        message = marked['message']
                        self.assertEqual(message, self.store.show('moltbook', target))
                        self.assertEqual(message['reply_ref'], self.ref)
                        self.assertIsNotNone(message['replied_at'])
                        # First-versus-latest replied_at semantics are deliberately unspecified.
                        excluded = {'replied_at', 'reply_ref'}
                        self.assertEqual({k: v for k, v in incoming.items() if k not in excluded},
                                         {k: v for k, v in message.items() if k not in excluded})
                        summary = marked['reply_attempt']
                        self.assertEqual((summary['state'], summary['next_action']),
                                         (state, next_action))
                        route = summary['show']
                        self.assertEqual(route, {'command': 'reply show',
                            'tool': 'boardmail_reply_show',
                            'arguments': {'source': 'moltbook', 'id': target}})
                        recovered, code = commands.outcome(lambda: commands.execute(
                            self.store, 'reply_show', **route['arguments']))
                        self.assertEqual(code, 0)
                        self.assertEqual({k: v for k, v in recovered.items() if k != 'message'},
                                         journal)
                        self.assertEqual(recovered['message'], message)

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

    def test_recovery_guidance_does_not_override_first_send_or_resolve_unknown(self):
        absent, _ = self.cli('show')
        prepared, _ = self.cli('prepare', '--body-file', self.file)
        key = prepared['reply']['idempotency_key']
        begun, _ = self.cli('begin', '--key', key)
        self.assertTrue(begun['send_allowed'])
        for result in (absent, prepared, begun):
            self.assertIsNone(result['recovery_guidance'])
        before = self.path.read_bytes()
        shown, _ = self.cli('show')
        guidance = shown['recovery_guidance']
        self.assertIsInstance(guidance, str)
        self.assertTrue(guidance.strip())
        for action, args in (('begin', ('--key', key)), ('prepare', ('--body-file', self.file))):
            result, code = self.cli(action, *args)
            self.assertEqual((code, result['recovery_guidance'], result['send_allowed']), (0, guidance, False))
            self.assertEqual(result['reply'], shown['reply'])
        self.assertEqual(self.path.read_bytes(), before)
        self.store.mark('moltbook', uid(10), 'replied', ref=self.ref)
        before = self.path.read_bytes()
        marked, _ = self.cli('show')
        self.assertEqual((marked['recovery_guidance'], marked['reply']), (guidance, shown['reply']))
        self.assertIsNone(marked['confirmation_basis'])
        self.assertEqual(self.path.read_bytes(), before)
        confirmed, code = self.command('confirm', key=key, ref=self.ref, readback_body=self.body)
        self.assertEqual((code, confirmed['reply']['state']), (0, 'confirmed'))
        self.assertIsNone(confirmed['recovery_guidance'])
        self.assertIsNone(self.cli('show')[0]['recovery_guidance'])

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

    def test_concurrent_replacement_and_begin_preserve_body_key_binding_in_both_orders(self):
        for index, first_action in enumerate(('replace', 'begin')):
            with self.subTest(first_commit=first_action):
                target = uid(10 + index)
                prepared = self.command('prepare', id=target, body=self.body)[0]['reply']
                old_key = prepared['idempotency_key']
                new_body = self.body + 'Replacement text.\r\n'
                incoming = self.store.show('moltbook', target)
                barrier, acquired = threading.Barrier(2), threading.Event()
                second_begin, committed = threading.Event(), threading.Event()
                worker, observed, connections = threading.local(), [], []
                sqlite_connect = sqlite3.connect

                class ContendingConnection(sqlite3.Connection):
                    def execute(connection, sql, *args):
                        if sql != 'BEGIN IMMEDIATE' or worker.action == first_action:
                            return super().execute(sql, *args)
                        if not acquired.wait(timeout=5):
                            raise AssertionError('The first reply transaction did not acquire its lock')

                        def authorizer(operation, argument, *_):
                            if operation == sqlite3.SQLITE_TRANSACTION and argument == 'BEGIN':
                                # This callback runs inside the second real SQLite BEGIN,
                                # before the first transaction is allowed to continue.
                                observed.append((acquired.is_set(), committed.is_set()))
                                second_begin.set()
                            return sqlite3.SQLITE_OK

                        connection.set_authorizer(authorizer)
                        try:
                            return super().execute(sql, *args)
                        finally:
                            connection.set_authorizer(None)

                class HeldStore(Store):
                    @contextmanager
                    def connect(store, *, write=False, create=False):
                        with super().connect(write=write, create=create) as db:
                            if write:
                                acquired.set()
                                if not second_begin.wait(timeout=5):
                                    raise AssertionError('The second SQLite BEGIN did not start')
                            yield db
                        if write:
                            committed.set()

                def connect(*args, **kwargs):
                    db = sqlite_connect(*args, factory=ContendingConnection, **kwargs)
                    connections.append(db)
                    return db

                stores = {action: (HeldStore(self.path) if action == first_action else Store(self.path))
                          for action in ('replace', 'begin')}

                def invoke(action):
                    worker.action = action
                    barrier.wait(timeout=5)
                    options = {'body': new_body, 'replace_key': old_key} if action == 'replace' else {'key': old_key}
                    return commands.outcome(lambda: replies.execute(stores[action],
                        'prepare' if action == 'replace' else 'begin', 'moltbook', target, **options))

                with patch('boardmail.store.sqlite3.connect', connect):
                    replaced, begun = run_reply_workers([lambda: invoke('replace'), lambda: invoke('begin')])
                self.assertEqual(observed, [(True, False)])
                self.assertTrue(committed.is_set())
                self.assertEqual(len(connections), 2)
                self.assertIsNot(connections[0], connections[1])
                shown = self.command('show', id=target)[0]['reply']
                self.assertEqual(self.store.show('moltbook', target), incoming)
                if first_action == 'replace':
                    self.assertEqual((replaced[1], replaced[0]['changed'], replaced[0]['send_allowed']),
                                     (0, True, False))
                    self.assertEqual((begun[1], begun[0]['error']), (2, 'reply_key_mismatch'))
                    new_key = replaced[0]['reply']['idempotency_key']
                    self.assertNotEqual(new_key, old_key)
                    self.assertEqual((shown['state'], shown['body'], shown['idempotency_key']),
                                     ('prepared', new_body, new_key))
                    stale, code = self.command('begin', id=target, key=old_key)
                    self.assertEqual((code, stale['error']), (2, 'reply_key_mismatch'))
                    first_send, code = self.command('begin', id=target, key=new_key)
                    self.assertEqual((code, first_send['send_allowed'], first_send['reply']['body']),
                                     (0, True, new_body))
                    active_key = new_key
                else:
                    self.assertEqual((begun[1], begun[0]['changed'], begun[0]['send_allowed']), (0, True, True))
                    self.assertEqual((replaced[1], replaced[0]['error']), (2, 'reply_already_started'))
                    self.assertEqual((shown['state'], shown['body'], shown['idempotency_key']),
                                     ('unknown', self.body, old_key))
                    self.assertEqual(begun[0]['reply'], shown)
                    active_key = old_key
                repeat, code = self.command('begin', id=target, key=active_key)
                self.assertEqual((code, repeat['send_allowed']), (0, False))
                self.assertEqual(repeat['reply']['body_sha256'], replies.digest(repeat['reply']['body']))

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

    def test_invalid_reply_marks_preserve_message_and_unknown_attempt(self):
        self.store.mark('moltbook', uid(10), 'read')
        self.store.mark('moltbook', uid(10), 'needs_reply')
        key = self.command('prepare', body=self.body)[0]['reply']['idempotency_key']
        self.command('begin', key=key)
        invalid_refs = (
            'https://board.example.invalid:wrong-port/reply/1',
            'https://board.example.invalid:65536/reply/1',
            'https://[broken/reply/1',
            'https://@board.example.invalid/reply/1',
            'https://:@board.example.invalid/reply/1',
            'https://board.example.invalid/reply/1\n',
            'https://board.example.invalid/reply/\t1',
            'https://board.example.invalid/reply/1\x7f',
        )
        for ref in invalid_refs:
            with self.subTest(ref=ref):
                before = self.path.read_bytes()
                message_before = self.store.show('moltbook', uid(10))
                attempt_before = self.command('show')[0]['reply']
                result, code = commands.outcome(lambda: commands.execute(
                    self.store, 'mark', source='moltbook', id=uid(10), action='replied', ref=ref))
                self.assertEqual(code, 2)
                self.assertEqual(result['error'], 'reply_ref_required')
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(self.store.show('moltbook', uid(10)), message_before)
                self.assertEqual(self.command('show')[0]['reply'], attempt_before)

    def test_reply_reference_1024_character_boundary_accepts_mark_and_confirm(self):
        for number, (scheme, character) in enumerate(
                ((scheme, character) for scheme in ('http', 'https') for character in ('a', 'я')), 20):
            with self.subTest(scheme=scheme, character=character):
                target = uid(number)
                self.store.save('moltbook', uid(2), [mail(number)])
                self.store.mark('moltbook', target, 'read')
                self.store.mark('moltbook', target, 'needs_reply')
                prefix = scheme + '://board.example.invalid/reply/'
                ref = prefix + character * (1024 - len(prefix))
                self.assertEqual(len(ref), 1024)
                if character == 'я':
                    self.assertGreater(len(ref.encode('utf-8')), 1024)
                prepared, code = self.command('prepare', id=target, body=self.body)
                self.assertEqual(code, 0)
                key = prepared['reply']['idempotency_key']
                self.command('begin', id=target, key=key)
                attempt_before = self.command('show', id=target)[0]['reply']
                self.assertEqual(attempt_before['state'], 'unknown')
                marked, code = commands.outcome(lambda: commands.execute(
                    self.store, 'mark', source='moltbook', id=target, action='replied', ref=ref))
                self.assertEqual((code, marked['message']['reply_ref']), (0, ref))
                self.assertEqual(self.command('show', id=target)[0]['reply'], attempt_before)
                confirmed, code = self.command('confirm', id=target, key=key, ref=ref, readback_body=self.body)
                self.assertEqual((code, confirmed['reply']['state']), (0, 'confirmed'))
                self.assertEqual(confirmed['reply']['idempotency_key'], key)
                self.assertEqual(confirmed['reply']['body'], self.body)
                self.assertEqual(confirmed['reply']['body_sha256'], replies.digest(self.body))
                self.assertEqual(confirmed['reply']['readback_sha256'], replies.digest(self.body))
                self.assertEqual(confirmed['message']['reply_ref'], ref)
                self.assertIsNotNone(confirmed['message']['read_at'])
                self.assertTrue(confirmed['message']['needs_reply'])
                self.assertEqual(confirmed['confirmation_basis'], 'caller_supplied_readback')
                self.assertFalse(confirmed['remote_verified'])
                self.assertIsNone(confirmed['verification_receipt'])

    def test_reply_reference_1025_characters_rejects_mark_and_confirm_without_writing(self):
        for number, (scheme, character) in enumerate(
                ((scheme, character) for scheme in ('http', 'https') for character in ('a', 'я')), 20):
            with self.subTest(scheme=scheme, character=character):
                target = uid(number)
                self.store.save('moltbook', uid(2), [mail(number)])
                self.store.mark('moltbook', target, 'read')
                self.store.mark('moltbook', target, 'needs_reply')
                key = self.command('prepare', id=target, body=self.body)[0]['reply']['idempotency_key']
                self.command('begin', id=target, key=key)
                prefix = scheme + '://board.example.invalid/reply/'
                ref = prefix + character * (1025 - len(prefix))
                self.assertEqual(len(ref), 1025)
                before = self.path.read_bytes()
                message_before = self.store.show('moltbook', target)
                attempt_before = self.command('show', id=target)[0]['reply']
                self.assertEqual(attempt_before['state'], 'unknown')
                self.assertEqual(attempt_before['idempotency_key'], key)
                self.assertEqual(attempt_before['body'], self.body)
                for action in ('mark', 'confirm'):
                    with self.subTest(action=action):
                        if action == 'mark':
                            result, code = commands.outcome(lambda: commands.execute(
                                self.store, 'mark', source='moltbook', id=target, action='replied', ref=ref))
                        else:
                            result, code = self.command('confirm', id=target, key=key, ref=ref, readback_body=self.body)
                        self.assertEqual((code, result['error']), (2, 'reply_ref_required'))
                        self.assertEqual(self.path.read_bytes(), before)
                        self.assertEqual(self.store.show('moltbook', target), message_before)
                        self.assertEqual(self.command('show', id=target)[0]['reply'], attempt_before)

    def test_valid_reply_marks_and_corrected_mark_allow_confirmation(self):
        self.store.mark('moltbook', uid(10), 'read')
        self.store.mark('moltbook', uid(10), 'needs_reply')
        key = self.command('prepare', body=self.body)[0]['reply']['idempotency_key']
        self.command('begin', key=key)
        attempt_before = self.command('show')[0]['reply']
        for ref in ('http://board.example.invalid:8080/reply/1?view=full#reply',
                    'https://[::1]:443/reply/1', self.ref + '-incorrect'):
            with self.subTest(ref=ref):
                result, code = commands.outcome(lambda: commands.execute(
                    self.store, 'mark', source='moltbook', id=uid(10), action='replied', ref=ref))
                self.assertEqual((code, result['message']['reply_ref']), (0, ref))
                self.assertEqual(self.command('show')[0]['reply'], attempt_before)
        result, code = self.command('confirm', key=key, ref=self.ref, readback_body=self.body)
        self.assertEqual((code, result['error']), (2, 'reply_reference_conflict'))
        self.store.mark('moltbook', uid(10), 'replied', ref=self.ref)
        result, code = self.command('confirm', key=key, ref=self.ref, readback_body=self.body)
        self.assertEqual((code, result['reply']['state']), (0, 'confirmed'))
        self.assertEqual(result['message']['reply_ref'], self.ref)
        self.assertIsNotNone(result['message']['read_at'])
        self.assertTrue(result['message']['needs_reply'])

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
