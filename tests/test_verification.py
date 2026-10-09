"""Remote reply reconciliation with invented provider originals, never publication."""
from contextlib import closing, contextmanager
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from uuid import UUID

from boardmail import cli, commands, replies, schema, verification
from boardmail.boards import BOARDS
from boardmail.store import Store
from examples.fixtures import KEY, FixtureBoard, named, original, uid
from kit import Clock, Network, edge, fixed, mark, new_inbox, notify, on_statement
from test_clawdchat import Board as ClawdChat, event as clawd_event, key_file as clawd_key_file, original as clawd_original
from test_replies import run_reply_workers

# The boards on which a reply can be verified.
VERIFIED = tuple(name for name, about in BOARDS.items() if about.replies)


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'mail.sqlite3'
        self.store = new_inbox(self.path)
        self.key_file = Path(self.temp.name) / 'example.key'
        self.key_file.write_text(KEY + '\n')
        self.body = 'A synthetic reply. Кириллица.\r\nExact newline.\n'
        self.root, self.target, self.reply = uid(301), uid(310), uid(320)

    def setup_source(self, adapter, *, root_target=False):
        self.adapter, self.source = adapter, 'alias-' + adapter
        self.settings = {'account_id': uid(1), 'adapter': adapter, 'api_key_file': 'missing.key'}
        if adapter == 'postingboard':
            # Postingboard is asked with the key of the account. The other boards give an original to anyone.
            self.settings['api_key_file'] = self.key_file
        if root_target:
            self.target = self.root
        self.deliver(adapter)
        mark(self.store, self.source, self.target, 'read')
        mark(self.store, self.source, self.target, 'needs_reply')
        if adapter == 'postingboard':
            self.client = FixtureBoard(adapter, self.settings)
            self.raw = named(320, 301, 1, body=self.body, reply_to=None if root_target else 310)
            self.client.others[self.reply] = self.raw
        elif adapter == 'clawdchat':
            self.client = ClawdChat()
            self.raw = clawd_original(320, post_id=self.root, post={'id': self.root, 'title': 'Example'},
                parent_id=None if root_target else self.target, content=self.body, author={'id': uid(1), 'name': 'owner'})
            self.client.originals = {self.reply: self.raw}
        else:
            self.client = FixtureBoard(adapter, self.settings)
            flags = {'held': False} if adapter == 'the-colony' else {'verification_status': 'verified', 'is_deleted': False, 'is_spam': False}
            self.client.root = {**original(301, 301, 1, colony=adapter == 'the-colony'), **flags, 'title': 'Example'}
            self.raw = {**original(320, 301, 1, colony=adapter == 'the-colony', body=self.body), **flags,
                        'parent_id': None if root_target else self.target}
            self.client.comments = [self.raw]
        self.ref = BOARDS[adapter].reference(self.root, self.reply)
        self.key = self.call('prepare', body=self.body)[0]['reply']['idempotency_key']
        self.call('begin', key=self.key)
        self.before = self.path.read_bytes()
        self.before_result = self.call('show')[0]

    def deliver(self, adapter):
        """One pass over an invented board brings the target: a comment of another account under the post of the
        thread, or that post itself where the target is the root. It is another board than the one that the
        verification asks afterwards."""
        number, mention = int(UUID(self.target)), self.target == self.root
        cfg = {**self.settings, 'api_key_file': self.key_file}
        if adapter == 'postingboard':
            cfg, board = {**cfg, 'inbox': True, 'threads': []}, FixtureBoard(adapter, cfg)
            post = named(number, 301)
            (board.roots if mention else board.others)[self.target] = post
            board.inbox = [(1, post, ['mention'])]
        elif adapter == 'clawdchat':
            cfg, board = {**cfg, 'api_key_file': clawd_key_file(self)}, ClawdChat()
            board.events = [{**clawd_event(number, 'mention_post' if mention else 'comment'), 'post_id': self.root}]
            board.originals = {self.target: clawd_original(number, post_id=self.root, title='Example',
                                                           post={'id': self.root, 'title': 'Example'})}
        else:
            colony = adapter == 'the-colony'
            board = FixtureBoard(adapter, cfg)
            board.root = {**original(301, 301, 10 if mention else 1, colony=colony), 'title': 'Example'}
            board.comments, board.events = [], []
            notify(board, original(number, 301, colony=colony), 'mention' if mention else None)
            if mention:  # The notification is of the post, and no comment belongs to it.
                board.comments.clear()
                board.events[0]['comment_id' if colony else 'relatedCommentId'] = None
        with fixed(Clock(1790000000)):  # The wait of the client of Postingboard between two requests only moves the clock.
            result, code = commands.execute(self.store, 'collect', sources={self.source: cfg}, fetch=board)
        self.assertEqual((code, result['added'], result['failed']), (0, 1, False), result)

    def call(self, action='verify', **options):
        if action == 'verify':
            options = {'key': self.key, 'ref': self.ref, **options}
        # The client of the board asks the invented board.
        return commands.outcome(lambda: commands.execute(self.store, 'reply_' + action,
            sources={self.source: self.settings}, source=self.source, id=self.target, fetch=self.client, **options))

    @contextmanager
    def unreached(self, error=None):
        """Inside the block the board is not reached: each request to it ends with a timeout."""
        self.client.fail = error or TimeoutError()
        try:
            yield
        finally:
            self.client.fail = False

    def assert_unverified(self, reason=None):
        result, code = self.call()
        self.assertEqual(code, 1, result)
        self.assertFalse(result['send_allowed'])
        self.assertFalse(result['remote_verified'])
        self.assertEqual(result['reply']['state'], 'unknown')
        self.assertIsNone(result['confirmation_basis'])
        self.assertNotIn('replied_at', result['message'])
        if reason:
            self.assertEqual(result['verification']['reason'], reason, result)
        self.assertEqual(result['reply'], self.before_result['reply'])
        self.assertEqual(result['message'], self.before_result['message'])
        self.assertIsNone(result['verification_receipt'])
        repeated, code = self.call()
        self.assertEqual(code, 1)
        self.assertEqual(repeated['reply'], result['reply'])
        self.assertEqual(repeated['message'], result['message'])
        self.assertTrue(repeated['last_check_saved'])
        return result

    def test_candidate_survives_timeout_and_reopen_without_becoming_evidence(self):
        self.setup_source('postingboard')
        with self.unreached(TimeoutError('private provider prose secret-token')), fixed(Clock(2000)):
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
        self.assertEqual(candidate['last_check'], {'checked_at': 2000, 'reason': 'network_error', 'status': 'unverified'})
        self.assertTrue(failed['last_check_saved'])
        self.assertNotIn('private provider prose', json.dumps(shown))
        self.assertNotIn('secret-token', json.dumps(shown))
        self.assertEqual(shown['reply'], self.before_result['reply'])
        self.assertEqual(shown['message'], self.before_result['message'])
        self.assertIsNone(shown['confirmation_basis'])
        self.assertIsNone(shown['verification_receipt'])
        self.assertFalse(shown['send_allowed'])
        self.assertFalse(shown['remote_verified'])
        self.assertEqual(self.before_result['reply_candidates'], [])
        self.assertTrue(failed['changed'])
        # A later completed failure replaces the diagnostic, even if the local clock moved back.
        self.raw['agent_id'] = uid(2)
        with fixed(Clock(1999)):
            updated, code = self.call()
            before = self.path.read_bytes()
            repeated, _ = self.call()
        self.assertEqual(code, 1)
        self.assertTrue(updated['changed'])
        self.assertTrue(repeated['last_check_saved'])
        self.assertFalse(repeated['changed'])
        self.assertEqual(self.path.read_bytes(), before)
        saved = self.call('show')[0]['reply_candidates'][0]
        self.assertEqual(saved['recorded_at'], candidate['recorded_at'])
        self.assertEqual(saved['last_check'], {'checked_at': 1999, 'reason': 'reply_author_mismatch', 'status': 'unverified'})

    def test_candidate_survives_process_exit_before_provider_read_returns(self):
        self.setup_source('postingboard')
        program = '''import json, os, sys
from pathlib import Path
from boardmail import verification
from boardmail.store import Store
path, source, target, key, ref, settings = sys.argv[1:]
settings = json.loads(settings)
settings['api_key_file'] = Path(settings['api_key_file'])
# The process ends while the board is asked, so no answer comes back.
verification.execute(Store(path), {source: settings}, source, target, key=key, ref=ref,
                     fetch=lambda *asked, **more: os._exit(73))
'''
        child = subprocess.run([sys.executable, '-c', program, str(self.path), self.source,
                                self.target, self.key, self.ref, json.dumps(self.settings, default=str)],
                               cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15)
        self.assertEqual(child.returncode, 73, child.stderr)
        self.store = Store(self.path)
        shown, code = self.call('show')
        self.assertEqual(code, 0)
        self.assertEqual([c['reply_ref'] for c in shown['reply_candidates']], [self.ref])
        self.assertIsNone(shown['reply_candidates'][0]['last_check'])
        self.assertEqual(shown['reply']['state'], 'unknown')
        self.assertIsNone(shown['reply']['reply_ref'])
        self.assertIsNone(shown['confirmation_basis'])
        self.assertIsNone(shown['verification_receipt'])
        self.assertFalse(shown['send_allowed'])

    def test_candidates_are_bounded_without_replacing_an_earlier_url(self):
        self.setup_source('postingboard')
        refs = [BOARDS[self.adapter].reference(self.root, uid(320 + i)) for i in range(9)]
        with self.unreached(), fixed(Clock(2000)):
            for ref in refs[:8]:
                self.assertEqual(self.call(ref=ref)[1], 1)
            shown = self.call('show')[0]
            self.assertEqual({c['reply_ref'] for c in shown['reply_candidates']}, set(refs[:8]))
            before = self.path.read_bytes()
            self.assertEqual(self.call(ref=refs[0])[1], 1)
            self.assertEqual(self.path.read_bytes(), before)
            asked = len(self.client.asked)
            rejected, code = self.call(ref=refs[8])
            self.assertEqual((code, rejected['error']), (2, 'reply_candidate_limit'))
            self.assertEqual(len(self.client.asked), asked)
            self.assertEqual(self.path.read_bytes(), before)
        # The bounded candidate directory does not block independent caller readback.
        confirmed, code = self.call('confirm', key=self.key, ref=refs[8], readback_body=self.body)
        self.assertEqual((code, confirmed['confirmation_basis']), (0, 'caller_supplied_readback'))
        self.assertEqual(confirmed['reply_candidates'], [])
        self.assertIsNone(confirmed['verification_receipt'])

    def test_concurrent_candidate_admission_keeps_exactly_eight_and_reads_only_the_winner(self):
        self.setup_source('postingboard')
        existing = [BOARDS[self.adapter].reference(self.root, uid(400 + i)) for i in range(7)]
        with self.unreached():
            for ref in existing:
                self.assertEqual(self.call(ref=ref)[1], 1)
        contenders = [BOARDS[self.adapter].reference(self.root, uid(n)) for n in (500, 501)]
        barrier, worker = threading.Barrier(2), threading.local()

        def at_once(sql):
            # Each contender waits for the other before its first write, so both ask SQLite for the lock together.
            if sql == 'BEGIN IMMEDIATE' and not getattr(worker, 'waited', False):
                worker.waited = True
                barrier.wait(timeout=5)

        def invoke(index):
            return commands.outcome(lambda: verification.execute(Store(self.path), {self.source: self.settings},
                self.source, self.target, key=self.key, ref=contenders[index], fetch=self.client))

        asked = len(self.client.calls)
        with self.unreached(), on_statement(at_once):
            results = run_reply_workers([lambda: invoke(0), lambda: invoke(1)])
        self.assertEqual(sorted(code for _, code in results), [1, 2])
        winner = next(i for i, (_, code) in enumerate(results) if code == 1)
        loser = 1 - winner
        self.assertEqual(results[winner][0]['verification']['reason'], 'network_error')
        self.assertEqual(results[loser][0]['error'], 'reply_candidate_limit')
        # The winner alone asked the board, and it asked for its own reply.
        self.assertEqual([path for path, _, _ in self.client.calls[asked:]], ['/v1/posts/' + uid(500 + winner)])
        shown = self.call('show')[0]
        self.assertEqual({item['reply_ref'] for item in shown['reply_candidates']},
                         {*existing, contenders[winner]})
        self.assertEqual(len(shown['reply_candidates']), 8)
        self.assertEqual(shown['reply'], self.before_result['reply'])
        self.assertEqual(shown['message'], self.before_result['message'])
        self.assertIsNone(shown['verification_receipt'])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM reply_candidates').fetchone()[0], 8)

    def test_concurrent_valid_provider_reads_confirm_only_one_exact_url_and_receipt(self):
        self.setup_source('postingboard')
        self.client.others[uid(321)] = named(321, 301, 1, body=self.body, reply_to=310)
        refs = [self.ref, BOARDS[self.adapter].reference(self.root, uid(321))]
        barrier, get = threading.Barrier(2), self.client.get

        def answered(path, *args, **kwargs):
            answer = get(path, *args, **kwargs)
            barrier.wait(timeout=5)  # Both valid originals are read before either confirmation is written.
            return answer

        def invoke(index):
            return commands.outcome(lambda: verification.execute(Store(self.path), {self.source: self.settings},
                self.source, self.target, key=self.key, ref=refs[index], fetch=self.client))

        self.client.get = answered
        results = run_reply_workers([lambda: invoke(0), lambda: invoke(1)])
        self.client.get = get
        self.assertEqual(len(self.client.calls), 2)
        self.assertEqual(sorted(code for _, code in results), [0, 2])
        winner = next(i for i, (_, code) in enumerate(results) if code == 0)
        self.assertEqual(results[1 - winner][0]['error'], 'reply_reference_conflict')
        confirmed = results[winner][0]
        shown = self.call('show')[0]
        self.assertEqual((shown['reply']['state'], shown['reply']['reply_ref'], shown['message']['reply_ref']),
                         ('confirmed', refs[winner], refs[winner]))
        self.assertEqual(shown['verification_receipt'], confirmed['verification_receipt'])
        self.assertEqual(shown['verification_receipt']['reply_id'], uid(320 + winner))
        self.assertEqual(shown['verification_receipt']['reply_ref'], refs[winner])
        self.assertEqual(shown['verification_receipt']['idempotency_key'], self.key)
        self.assertEqual(shown['reply']['body'], self.body)
        self.assertFalse(confirmed['send_allowed'] or confirmed['publication_performed'])
        self.assertEqual(shown['message']['read_at'], self.before_result['message']['read_at'])
        self.assertEqual(shown['message']['needs_reply'], self.before_result['message']['needs_reply'])
        self.assertEqual(shown['reply_candidates'], [])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM reply_candidates').fetchone()[0], 2)
            evidence = json.loads(db.execute('SELECT evidence FROM reply_verifications').fetchone()[0])
        self.assertEqual(evidence, shown['verification_receipt'])

    def test_concurrent_failed_provider_return_cannot_overwrite_committed_success(self):
        self.setup_source('postingboard')
        barrier, committed = threading.Barrier(2), threading.Event()
        get, snapshot, worker = self.client.get, {}, threading.local()

        def answered(path, *args, **kwargs):
            barrier.wait(timeout=5)
            if worker.fail:
                if not committed.wait(timeout=5):
                    raise AssertionError('Successful verification did not commit')
                raise TimeoutError()
            return get(path, *args, **kwargs)

        def invoke(fail):
            worker.fail = fail
            outcome = commands.outcome(lambda: verification.execute(Store(self.path), {self.source: self.settings},
                self.source, self.target, key=self.key, ref=self.ref, fetch=self.client))
            if not fail:
                snapshot['bytes'] = self.path.read_bytes()
                committed.set()
            return outcome

        self.client.get = answered
        succeeded, failed = run_reply_workers([lambda: invoke(False), lambda: invoke(True)])
        self.client.get = get
        self.assertEqual((succeeded[1], failed[1]), (0, 1))
        self.assertEqual(failed[0]['verification']['reason'], 'network_error')
        self.assertFalse(failed[0]['last_check_saved'] or failed[0]['remote_verified'])
        self.assertEqual(failed[0]['reply']['state'], 'confirmed')
        self.assertEqual(failed[0]['verification_receipt'], succeeded[0]['verification_receipt'])
        self.assertEqual(failed[0]['reply'], succeeded[0]['reply'])
        self.assertEqual(failed[0]['message'], succeeded[0]['message'])
        self.assertEqual(failed[0]['reply_candidates'], [])
        self.assertEqual(self.call('show')[0]['verification_receipt'], succeeded[0]['verification_receipt'])
        self.assertEqual(self.path.read_bytes(), snapshot['bytes'])

    def test_last_check_keeps_the_last_committed_failure_within_one_second(self):
        self.setup_source('postingboard')
        with fixed(Clock(2000)):
            with self.unreached():
                self.call()
            self.raw['agent_id'] = uid(2)
            result, _ = self.call()
        self.assertEqual(result['reply_candidates'][0]['last_check'],
                         {'checked_at': 2000, 'reason': 'reply_author_mismatch', 'status': 'unverified'})
        self.assertTrue(result['changed'])

    def test_diagnostic_write_failure_preserves_provider_failure_and_saved_candidate(self):
        self.setup_source('postingboard')
        with closing(sqlite3.connect(self.path)) as db, db:
            schema.add(db, 'reply_candidate_checks')
            db.execute("CREATE TRIGGER fail_check BEFORE INSERT ON reply_candidate_checks "
                       "BEGIN SELECT RAISE(ABORT, 'private database detail'); END")
        with self.unreached():
            result, code = self.call()
        self.assertEqual((code, result['verification']['reason']), (1, 'network_error'))
        self.assertEqual(len(self.client.asked), 1)
        self.assertFalse(result['last_check_saved'])
        self.assertTrue(result['changed'])  # The pointer committed before the failed diagnostic write.
        self.assertIsNone(result['reply_candidates'][0]['last_check'])
        self.assertNotIn('private database detail', json.dumps(result))
        before = self.path.read_bytes()
        shown, _ = self.call('show')
        self.assertEqual(shown['reply_candidates'], result['reply_candidates'])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(shown['reply'], self.before_result['reply'])
        self.assertEqual(shown['message'], self.before_result['message'])

    def test_late_failed_check_cannot_write_after_another_caller_confirms(self):
        self.setup_source('postingboard')
        get, committed = self.client.get, {}
        def racing_failure(path, *args, **kwargs):
            # While this request is on its way another caller reads the same original and confirms. Then it fails.
            self.client.get = get
            committed['result'], code = self.call()
            self.assertEqual(code, 0)
            committed['bytes'] = self.path.read_bytes()
            raise TimeoutError()
        self.client.get = racing_failure
        result, code = self.call()
        self.assertEqual(code, 1)
        self.assertFalse(result['last_check_saved'])
        self.assertEqual(result['reply']['state'], 'confirmed')
        self.assertEqual(result['reply_candidates'], [])
        self.assertEqual(result['verification_receipt'], committed['result']['verification_receipt'])
        self.assertEqual(self.path.read_bytes(), committed['bytes'])

    def test_failed_check_cannot_attach_to_a_changed_source_or_attempt(self):
        self.setup_source('postingboard')
        cases = [('sources', 'paused', 1), ('sources', 'account_id', uid(2)),
                 ('adapter_state', 'adapter', 'moltbook'), ('messages', 'thread_id', uid(999)),
                 ('messages', 'reply_ref', 'https://example.invalid/other'),
                 ('reply_attempts', 'body_sha256', 'changed'), ('reply_attempts', 'idempotency_key', 'changed')]
        for table, column, value in cases:
            with self.subTest(column=column):
                with self.store.connect() as db:
                    old = db.execute(f'SELECT {column} FROM {table} WHERE source=?', (self.source,)).fetchone()[0]
                committed = {}
                def changed_then_failed(path, *args, **kwargs):
                    with closing(sqlite3.connect(self.path)) as db, db:
                        db.execute(f'UPDATE {table} SET {column}=? WHERE source=?', (value, self.source))
                    committed['bytes'] = self.path.read_bytes()
                    raise TimeoutError()
                self.client.get = changed_then_failed
                result, code = self.call()
                self.assertEqual((code, result['verification']['reason']), (1, 'network_error'))
                self.assertFalse(result['last_check_saved'])
                self.assertEqual(self.path.read_bytes(), committed['bytes'])
                with closing(sqlite3.connect(self.path)) as db, db:
                    db.execute(f'UPDATE {table} SET {column}=? WHERE source=?', (old, self.source))

    def test_legacy_candidates_have_no_diagnostic_and_keep_positional_writes(self):
        self.setup_source('postingboard')
        with closing(sqlite3.connect(self.path)) as db, db:
            schema.add(db, 'reply_candidates')
            db.execute('INSERT INTO reply_candidates VALUES (?,?,?,?,?,?,?)',
                       (self.source, self.target, self.key, self.ref, self.adapter, uid(1), 1000))
        before = self.path.read_bytes()
        self.assertIsNone(self.call('show')[0]['reply_candidates'][0]['last_check'])
        self.assertEqual(self.path.read_bytes(), before)
        with self.unreached():
            self.call()
        other = BOARDS[self.adapter].reference(self.root, uid(321))
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('INSERT INTO reply_candidates VALUES (?,?,?,?,?,?,?)',
                       (self.source, self.target, self.key, other, self.adapter, uid(1), 1001))
        candidates = {c['reply_ref']: c for c in self.call('show')[0]['reply_candidates']}
        self.assertEqual(candidates[self.ref]['last_check']['reason'], 'network_error')
        self.assertIsNone(candidates[other]['last_check'])

    def test_identical_remote_replies_bind_only_the_first_selected_url_in_either_order(self):
        for order in ((320, 321), (321, 320)):
            with self.subTest(order=order):
                self.path = Path(self.temp.name) / f'identical-{order[0]}.sqlite3'
                self.store = new_inbox(self.path)
                self.setup_source('postingboard')
                self.client.others[uid(321)] = named(321, 301, 1, body=self.body, reply_to=310)
                refs = [BOARDS[self.adapter].reference(self.root, uid(n)) for n in order]
                # Unknown covers a publisher's unresolved outcome. These are provider GET failures, not POSTs.
                with self.unreached():
                    for ref in refs:
                        self.assertEqual(self.call(ref=ref)[1], 1)
                confirmed, code = self.call(ref=refs[0])
                self.assertEqual((code, confirmed['reply']['reply_ref']), (0, refs[0]))
                self.assertEqual(confirmed['verification_receipt']['key_scope'], 'local')
                self.assertEqual(confirmed['verification_receipt']['reply_id'], uid(order[0]))
                before, calls = self.path.read_bytes(), len(self.client.calls)
                rejected, code = self.call(ref=refs[1])
                self.assertEqual((code, rejected['error']), (2, 'reply_reference_conflict'))
                self.assertEqual((self.path.read_bytes(), len(self.client.calls)), (before, calls))
                self.assertEqual(self.call('show')[0]['verification_receipt'], confirmed['verification_receipt'])

    def test_wrong_candidate_does_not_block_a_different_verified_reply(self):
        self.setup_source('postingboard')
        wrong = BOARDS[self.adapter].reference(self.root, uid(999))
        self.assertEqual(self.call(ref=wrong)[1], 1)
        self.assertEqual(self.call('show')[0]['reply_candidates'][0]['reply_ref'], wrong)
        confirmed, code = self.call()
        self.assertEqual((code, confirmed['confirmation_basis']), (0, 'provider_readback'))
        self.assertEqual(confirmed['reply']['reply_ref'], self.ref)
        self.assertEqual(confirmed['reply_candidates'], [])

    def test_candidate_write_failure_prevents_provider_request(self):
        self.setup_source('postingboard')
        with self.unreached():
            self.call()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TRIGGER fail_candidate BEFORE INSERT ON reply_candidates "
                       "BEGIN SELECT RAISE(ABORT, 'stop'); END")
        before, asked = self.path.read_bytes(), len(self.client.asked)
        result, code = self.call(ref=BOARDS[self.adapter].reference(self.root, uid(999)))
        self.assertEqual((code, result['error']), (2, 'local_state_error'))
        self.assertEqual(len(self.client.asked), asked)
        self.assertEqual(self.path.read_bytes(), before)

    def test_all_providers_and_source_aliases_save_exact_evidence_and_independent_marks(self):
        for adapter in VERIFIED:
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
                # Postingboard gives the post to the account that wrote it. The other boards give it to anyone.
                self.assertEqual(evidence['availability_basis'],
                                 'authenticated_original' if adapter == 'postingboard' else 'anonymous_original')
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
        with closing(sqlite3.connect(self.path)) as db, db:
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
        for adapter in VERIFIED:
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
        # The board gives one comment on a page and the reply is the last of 101. A search reads 100 pages at most.
        self.client.per_page, self.client.comments = 1, [original(n, 301) for n in range(1000, 1100)] + [self.raw]
        self.client.calls.clear()
        self.assert_unverified('budget_exhausted')
        self.assertEqual(len([path for path, _, _ in self.client.calls if path.endswith('/comments')]), 200)
        del self.client.comments[0]
        self.assertEqual(self.call()[1], 0)

    def test_each_board_takes_its_own_forms_of_a_reference_and_no_other(self):
        root, reply = self.root, self.reply
        hosts = {'postingboard': ['getpostingboard.dev'], 'the-colony': ['thecolony.ai'],
                 'moltbook': ['www.moltbook.com', 'moltbook.com'], 'clawdchat': ['clawdchat.cn']}
        own = {'postingboard': [f'/v1/posts/{reply}'],
               'the-colony': [f'/post/{root}#comment-{reply}', f'/posts/{root}#comment-{reply}'],
               'moltbook': [f'/post/{root}#comment-{reply}'],
               'clawdchat': [f'/api/v1/comments/{reply}', f'/post/{root}#comment-{reply}']}
        no_board = [f'/{reply}', f'/v1/posts/{reply}#comment-{reply}', f'/api/v1/comments/{reply}#comment-{reply}',
                    f'/v1/posts/{reply}/', f'/topics/{root}#comment-{reply}', f'/post/{root}', f'/post/{root}#message-{reply}']
        self.assertEqual(set(hosts), set(VERIFIED))
        for adapter in VERIFIED:
            self.setup_source(adapter)
            for host in sorted(host for named in hosts.values() for host in named):
                for form in sorted({form for forms in own.values() for form in forms} | set(no_board)):
                    with self.subTest(adapter=adapter, host=host, form=form):
                        result, _ = self.call(ref='https://' + host + form)
                        self.assertEqual(result.get('error') != 'reply_reference_unsupported',
                                         host in hosts[adapter] and form in own[adapter], result)

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
        commands.execute(self.store, 'pause', source=self.source)
        before = self.path.read_bytes()
        self.assertEqual(self.call()[0]['error'], 'source_paused')
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.path.read_bytes(), before)

    def test_state_is_rechecked_after_network_without_holding_a_write_lock(self):
        self.setup_source('postingboard')
        get = self.client.get
        def racing_get(path, *args, **kwargs):
            # A second SQLite connection can write while the request is in flight.
            commands.execute(self.store, 'pause', source=self.source)
            return get(path, *args, **kwargs)
        self.client.get = racing_get
        result, code = self.call()
        self.assertEqual((code, result['error']), (2, 'source_paused'))
        self.assertEqual(self.call('show')[0]['reply']['state'], 'unknown')
        self.assertIsNone(self.store.show(self.source, self.target)['replied_at'])
        commands.execute(self.store, 'resume', source=self.source)
        def conflicting_get(path, *args, **kwargs):
            mark(self.store, self.source, self.target, 'replied', ref='https://example.invalid/other')
            return get(path, *args, **kwargs)
        self.client.get = conflicting_get
        result, code = self.call()
        self.assertEqual((code, result['error']), (2, 'reply_reference_conflict'))
        self.assertEqual(self.call('show')[0]['reply']['state'], 'unknown')

    def test_unrecorded_alias_does_not_take_its_identity_from_current_config(self):
        self.setup_source('moltbook')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('DELETE FROM adapter_state WHERE source=?', (self.source,))
        for remove_table in (False, True):
            with self.subTest(remove_table=remove_table):
                if remove_table:
                    with closing(sqlite3.connect(self.path)) as db, db:
                        db.execute('DROP TABLE adapter_state')
                    self.store.status()  # The first command that opens the file gives the table back, empty.
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
                    with closing(sqlite3.connect(self.path)) as db, db:
                        db.execute(f'UPDATE {table} SET {column}=? WHERE source=?', (value, self.source))
                    return get(path, *args, **kwargs)
                self.client.get = changed_get
                result, code = self.call()
                self.assertEqual((code, result['error']), (2, expected))
                self.assertEqual(self.call('show')[0]['reply']['state'], 'unknown')
                if column == 'idempotency_key':
                    self.assertEqual(self.call('show')[0]['reply_candidates'], [])
                with closing(sqlite3.connect(self.path)) as db, db:
                    db.execute(f'UPDATE {table} SET {column}=? WHERE source=?', (old, self.source))

    def test_receipt_and_confirmation_roll_back_together(self):
        self.setup_source('postingboard')
        with self.unreached():
            self.call()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TRIGGER fail_mark BEFORE UPDATE OF replied_at ON messages BEGIN SELECT RAISE(ABORT, 'stop'); END")
        before = self.path.read_bytes()
        self.assertEqual(self.call()[0]['error'], 'local_state_error')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIsNone(self.call('show')[0]['verification_receipt'])
        self.assertEqual(self.call('show')[0]['reply_candidates'][0]['reply_ref'], self.ref)

    def test_successful_verification_on_a_version_1_file_keeps_other_mail(self):
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
        self.client = FixtureBoard('moltbook', self.settings)
        flags = {'verification_status': 'verified', 'is_deleted': False, 'is_spam': False}
        self.client.root = {**original(100, 100), **flags, 'title': 'Legacy example'}
        self.client.comments = [{**original(320, 100, 2, body=self.body), **flags, 'parent_id': self.target}]
        self.ref = BOARDS['moltbook'].reference(self.root, self.reply)
        other = self.store.show('moltbook', uid(10))
        self.key = self.call('prepare', body=self.body)[0]['reply']['idempotency_key']
        self.call('begin', key=self.key)
        # Version 1 knew a source by the name of its board, so the file says that the moltbook adapter reads it.
        expected_client, expected_ref = self.client, self.ref
        self.settings['adapter'] = 'postingboard'
        self.client = FixtureBoard('postingboard', self.settings)
        self.client.others[self.reply] = named(320, 100, 2, body=self.body, reply_to=11)
        self.ref = BOARDS['postingboard'].reference(self.root, self.reply)
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
        with self.unreached():
            failed, code = self.call()
        self.assertEqual((code, failed['reply']['state']), (1, 'unknown'))
        self.assertEqual(self.call('show')[0]['reply_candidates'][0]['reply_ref'], self.ref)
        verified, code = self.call()
        self.assertEqual((code, verified['remote_verified']), (0, True))
        self.assertEqual(self.store.show('moltbook', uid(10)), other)
        before = self.path.read_bytes()
        self.assertEqual(self.call('show')[0]['verification_receipt'], verified['verification'])
        self.assertEqual(self.path.read_bytes(), before)

    def test_cli_with_db_still_loads_explicit_config_and_uses_same_verifier(self):
        self.setup_source('postingboard')
        config = self.path.parent / 'config.json'
        config.write_text(json.dumps({'database': str(self.path), 'sources': {self.source: self.settings}}, default=str))
        args = cli.parser().parse_args(['--db', str(self.path), '--config', str(config), 'reply', 'verify',
                                      self.source, self.target, '--key', self.key, '--ref', self.ref])
        # The command line hands no board in. The invented one stands where the request leaves the process.
        with Network({'getpostingboard.dev': edge(self.client)}):
            result, code = cli.run(args)
        self.assertEqual([path for path, _, _ in self.client.calls], ['/v1/posts/' + self.reply])
        self.assertEqual((code, result['reply']['state'], result['remote_verified']), (0, 'confirmed', True))


if __name__ == '__main__':
    unittest.main()
