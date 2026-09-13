"""Consumer preferences, lossless scope changes, and bounded local context."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from boardmail import commands
from boardmail.adapters import Batch, validate
from boardmail.config import MailError
from boardmail.store import Store
from examples.fixtures import uid
from test_mail import mail


class ReadingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'inbox.sqlite3'
        self.store = Store(self.path)
        self.store.initialize()

    def run_command(self, command='list', **options):
        result, code = commands.execute(self.store, command, **options)
        self.assertEqual(code, 0, result)
        return result

    def save(self, *kinds):
        return self.store.save('moltbook', uid(2), [dict(mail(10+i), addressing=kind) for i, kind in enumerate(kinds)])

    def test_defaults_readonly_persistence_override_reset_and_other_database(self):
        before = self.path.read_bytes()
        self.assertEqual(self.run_command('settings')['settings'], {
            'scope': 'addressed', 'context': 'brief', 'origin': {'scope': 'default', 'context': 'default'}})
        self.assertEqual(self.path.read_bytes(), before)
        other = Store(Path(self.temp.name) / 'other.sqlite3')
        other.initialize()
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(lambda opts: self.store.settings(**opts), [{'scope': 'all'}, {'context': 'none'}]))
        self.assertEqual(self.run_command()['reading'], {'scope': 'all', 'context': 'none'})
        self.assertEqual(self.run_command(scope='addressed')['reading'], {'scope': 'addressed', 'context': 'none'})
        self.assertEqual(self.store.settings()['scope'], 'all')
        self.assertEqual(other.settings()['scope'], 'addressed')
        self.run_command('settings', reset=True)
        self.assertEqual(self.store.settings()['origin'], {'scope': 'default', 'context': 'default'})

    def test_invalid_preferences_fail_before_collection_and_can_be_reset(self):
        before = self.path.read_bytes()
        with patch('boardmail.providers.collect_all') as collect:
            for args in ({'scope': 'guess'}, {'context_mode': 'full'}, {'through': 2}):
                result, code = commands.outcome(lambda: commands.execute(self.store, 'check', sources={}, **args))
                self.assertEqual((result['error'], code), ('invalid_arguments', 2))
            collect.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)
        self.store.settings(scope='all')
        with self.store.connect(write=True) as db:
            db.execute("UPDATE reader_settings SET value='invalid' WHERE key='scope'")
        result, _ = commands.outcome(lambda: commands.execute(self.store, 'list'))
        self.assertEqual(result['next_action'], 'run_settings_reset')
        self.assertEqual(self.run_command('settings', reset=True)['settings']['scope'], 'addressed')

    def test_page_scans_before_scope_and_never_marks_or_loses_unknown(self):
        self.save('thread', 'thread', 'direct', None, 'mention', 'direct+mention')
        before = self.path.read_bytes()
        first = self.run_command(limit=2)
        self.assertEqual((first['messages'], first['next_after'], first['more'], first['scanned']), ([], 2, True, 2))
        self.assertEqual(first['thread_activity'][0]['count'], 2)
        next_page = self.run_command(after=first['next_after'])
        self.assertEqual([m['addressing'] for m in next_page['messages']], ['direct', None, 'mention', 'direct+mention'])
        all_page = self.run_command(scope='all', context_mode='none')
        self.assertEqual(len(all_page['messages']), 6)
        self.assertFalse(any('brief' in m for m in all_page['messages']))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.store.status()['counts']['unread'], 6)

    def test_filtered_views_preserve_delivery_checkpoint_and_threads_require_source(self):
        self.save('direct', 'thread')
        self.assertTrue(self.run_command()['checkpoint_safe'])
        for arguments in ({'unread': True}, {'source': 'moltbook'}, {'through': 1},
                          {'source': 'moltbook', 'thread': uid(100)}):
            result = self.run_command(**arguments)
            self.assertFalse(result['checkpoint_safe'])
            self.assertEqual(result['next_action'], 'process_filtered_page_keep_delivery_checkpoint')
        with self.assertRaisesRegex(MailError, '^invalid_arguments$'):
            commands.execute(self.store, 'list', thread=uid(100))

    def test_summary_replay_survives_marks_new_arrivals_and_source_id_collisions(self):
        self.save('thread', 'thread', 'direct')
        summary = self.run_command(limit=2, unread=True)['thread_activity'][0]
        self.store.mark('moltbook', uid(10), 'read')
        self.store.save('moltbook', uid(2), [dict(mail(99), addressing='thread')])
        self.store.save('the-colony', uid(1), [dict(mail(10), addressing='thread')])
        args = dict(summary['replay']['arguments'])
        args['context_mode'] = args.pop('context')
        replay = self.run_command(**args)
        self.assertEqual([m['id'] for m in replay['messages']], [uid(10), uid(11)])
        self.assertFalse(replay['more'])
        self.assertFalse(replay['checkpoint_safe'])
        unread = self.run_command(unread=True, limit=1)
        self.assertEqual((unread['next_after'], unread['thread_activity'][0]['unread']), (2, 1))

    def test_wait_wakes_on_thread_summary_and_cancel_keeps_input_checkpoint(self):
        self.save('thread')
        result = self.run_command('wait', timeout=0)
        self.assertEqual((result['event'], result['messages'], result['next_after']), ('messages', [], 1))
        self.assertEqual(result['thread_activity'][0]['count'], 1)
        cancelled = threading.Event()
        cancelled.set()
        result, code = commands.execute(self.store, 'wait', timeout=0, cancelled=cancelled)
        self.assertEqual((code, result['next_after'], result['messages'], result['thread_activity']), (4, 0, [], []))
        result, code = commands.execute(self.store, 'wait', after=1, timeout=0)
        self.assertEqual((code, result['event'], result['next_after']), (3, 'timeout', 1))

    def test_legacy_reads_leave_bytes_unchanged_and_collection_migrates(self):
        legacy = Path(self.temp.name) / 'legacy.sqlite3'
        with sqlite3.connect(legacy) as db:
            db.executescript((Path(__file__).parent / 'fixtures/v1.sql').read_text())
        store = Store(legacy)
        before = legacy.read_bytes()
        result, _ = commands.execute(store, 'list')
        self.assertEqual(len(result['messages']), 2)
        self.assertTrue(all(m['addressing'] is None for m in result['messages']))
        self.assertEqual(result['messages'][0]['brief']['parent']['status'], 'unknown')
        commands.execute(store, 'settings')
        commands.execute(store, 'wait', after=2, timeout=0)
        self.assertEqual(legacy.read_bytes(), before)
        store.prepare_collection()
        self.assertEqual(store.show('moltbook', uid(10))['reply_ref'], 'https://example.invalid/reply/old')
        self.assertEqual(store.page()['next_after'], 2)
        with store.connect() as db:
            self.assertIn('addressing', store._columns(db))
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 2)

    def test_brief_uses_fetched_public_originals_without_inbox_pollution_or_network(self):
        root = dict(mail(100), body='r' * 5000)
        parent = dict(mail(90), body='our public parent')
        incoming = dict(mail(10), parent_id=uid(90), addressing='direct')
        batch = Batch(messages=[incoming], originals=[root, parent])
        validate(batch)
        self.store.save_collection('moltbook', uid(2), 'moltbook', 0, batch)
        self.assertEqual(self.store.status()['counts']['total'], 1)
        before = self.path.read_bytes()
        with patch('boardmail.providers.Client', side_effect=AssertionError('network')):
            brief = self.run_command()['messages'][0]['brief']
        self.assertEqual(brief['root']['status'], 'cached')
        self.assertEqual(len(brief['root']['body']), 600)
        self.assertTrue(brief['root']['truncated'])
        self.assertEqual(brief['parent']['body'], 'our public parent')
        self.assertEqual(self.path.read_bytes(), before)
        with self.store.connect() as db:
            cached = json.loads(db.execute('SELECT value FROM originals WHERE id=?', (uid(100),)).fetchone()[0])
        self.assertEqual(len(cached['body']), 4096)
        self.assertTrue(cached['truncated'])

    def test_missing_context_and_exact_previous_exchange_are_visible_and_bounded(self):
        from boardmail.providers import parent_reference
        self.store.save('moltbook', uid(2), [mail(20), mail(21), mail(22)])
        ref = parent_reference('moltbook', uid(100), uid(90))
        for n in (20, 21, 22):
            self.store.mark('moltbook', uid(n), 'replied', ref=ref)
        self.store.save('moltbook', uid(2), [dict(mail(10), addressing='direct', parent_id=uid(90))])
        brief = self.run_command(after=3)['messages'][0]['brief']
        self.assertEqual(brief['parent']['status'], 'not_available_locally')
        self.assertEqual(brief['previous_exchange']['status'], 'linked')
        self.assertEqual(len(brief['previous_exchange']['messages']), 2)
        self.assertTrue(brief['previous_exchange']['more'])
        self.assertEqual(brief['expand']['arguments'], {'source': 'moltbook', 'id': uid(10)})

    def test_stale_collector_keeps_newer_cache_but_retains_unique_arrivals(self):
        root = dict(mail(100), body='new context')
        self.store.save_collection('moltbook', uid(2), 'moltbook', 0, Batch(originals=[root]))
        stale = Batch(messages=[dict(mail(10), parent_id=uid(100), addressing='direct')],
                      originals=[dict(root, body='stale context')])
        self.assertEqual(self.store.save_collection('moltbook', uid(2), 'moltbook', 0, stale), (1, True))
        result = self.run_command()['messages'][0]
        self.assertEqual(result['brief']['root']['body'], 'new context')
        self.assertEqual(result['brief']['parent']['status'], 'same_as_root')

    def test_invalid_adapter_metadata_is_rejected_but_omitted_metadata_works(self):
        validate(Batch(messages=[mail(10)]))
        for batch in (Batch(messages=[dict(mail(10), addressing='probably-direct')]),
                      Batch(originals=[dict(mail(100), url='https://user:secret@example.invalid')])):
            with self.assertRaisesRegex(MailError, '^invalid_adapter_result$'):
                validate(batch)

    def test_conflicting_parent_cannot_supply_context_or_previous_exchange(self):
        from boardmail.providers import parent_reference
        self.store.save('moltbook', uid(2), [dict(mail(90), thread_id=uid(99)), mail(20)])
        self.store.mark('moltbook', uid(20), 'replied', ref=parent_reference('moltbook', uid(100), uid(90)))
        self.store.save('moltbook', uid(2), [dict(mail(10), parent_id=uid(90), addressing='direct')])
        brief = self.run_command(after=2)['messages'][0]['brief']
        self.assertEqual(brief['parent'], {'id': uid(90), 'status': 'unavailable', 'reason': 'thread_mismatch'})
        self.assertEqual(brief['previous_exchange'], {'status': 'unknown', 'messages': []})

    def test_fourclaw_synthesized_parent_remains_unknown_in_brief(self):
        self.store.save('fourclaw', 'reader', [dict(mail(10), parent_id=uid(100), addressing='thread')])
        brief = self.run_command(scope='all')['messages'][0]['brief']
        self.assertEqual(brief['parent'], {'id': None, 'status': 'unknown'})

    def test_fruitflies_reply_to_our_answer_keeps_that_answer_as_context(self):
        from boardmail import adapter_fruitflies as fruit
        from test_fruitflies import post
        with patch.object(fruit, '_fetch', side_effect=[
                [post(2, 'our answer', author='alice', parent=1, kind='answer')],
                [post(3, 'follow-up', parent=2, kind='answer')], []]):
            batch = fruit.collect({'account_id': 'alice'}, {}, frozenset())
        validate(batch)
        self.store.save_collection('fly', 'alice', 'fruitflies', 0, batch)
        brief = self.run_command()['messages'][0]['brief']
        self.assertEqual(brief['root']['body'], 'our answer')
        self.assertEqual(brief['root']['status'], 'cached')
        self.assertEqual(brief['parent']['status'], 'same_as_root')

    def test_cli_preferences_and_replay_use_the_documented_flags(self):
        def cli(*args):
            process = subprocess.run([sys.executable, '-m', 'boardmail', '--db', str(self.path), *args],
                                     capture_output=True, text=True, check=True)
            return json.loads(process.stdout)
        self.save('thread', 'mention')
        cli('settings', '--scope', 'all', '--context', 'none')
        self.assertEqual(len(cli('list')['messages']), 2)
        focused = cli('list', '--scope', 'addressed')
        summary = focused['thread_activity'][0]
        args = summary['replay']['arguments']
        replay = cli('list', *(part for key, value in args.items() for part in ('--'+key, str(value))))
        self.assertEqual([m['id'] for m in replay['messages']], [uid(10)])
        self.assertEqual(cli('settings')['settings']['scope'], 'all')


if __name__ == '__main__':
    unittest.main()
