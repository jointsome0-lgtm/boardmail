"""Topic reading preserves the inbox; all threads and messages are invented."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from boardmail import commands
from boardmail.adapters import Batch
from boardmail.store import Store
from examples.fixtures import uid
from test_mail import mail


class TagTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'mail.sqlite3'
        self.store = Store(self.path)
        self.store.initialize({'postingboard': {'account_id': uid(1)},
                               'moltbook': {'account_id': uid(2)},
                               'custom': {'account_id': 'example', 'adapter': '/unused.py'}})

    def command(self, command, **arguments):
        result, code = commands.outcome(lambda: commands.execute(self.store, command, **arguments))
        self.assertEqual(code, 0, result)
        self.assertFalse(result['history_complete'])
        return result

    def tag(self, tag, source='postingboard', thread=None, **arguments):
        return self.command('tag_add', tag=tag, source=source,
                            thread=thread if thread is not None else None if 'id' in arguments else uid(100),
                            **arguments)

    def cli(self, *arguments):
        run = subprocess.run([sys.executable, '-m', 'boardmail', '--db', str(self.path), *arguments],
                             text=True, capture_output=True, timeout=10)
        return run.returncode, json.loads(run.stdout)

    def rows(self):
        with self.store.connect() as db:
            names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                     if r[0] != 'thread_tags']
            return {name: [tuple(r) for r in db.execute('SELECT * FROM "' + name + '"')]
                    for name in names}

    def test_cross_board_topics_overlap_without_duplicate_messages_or_marks(self):
        self.store.save('postingboard', uid(1), [dict(mail(10), addressing='thread'),
                                                dict(mail(11), thread_id=uid(200))])
        self.store.save('moltbook', uid(2), [mail(10)])
        before = self.rows()
        self.tag('htalk')
        self.tag('htalk', 'moltbook')
        self.tag('agent-memory')
        self.assertEqual(self.rows(), before)
        overview = self.command('tags')
        self.assertEqual([(t['tag'], t['threads'], t['messages'], t['unread']) for t in overview['tags']],
                         [('agent-memory', 1, 1, 1), ('htalk', 2, 2, 2)])
        self.assertEqual(overview['counts'], {'unread': 3, 'tagged_unread': 2, 'untagged_unread': 1})
        self.assertEqual(overview['untagged']['unread'], 1)
        self.assertNotIn('Synthetic text', json.dumps(overview))
        page = self.command('list', **overview['tags'][1]['read']['arguments'])
        self.assertEqual([(m['source'], m['id']) for m in page['messages']],
                         [('postingboard', uid(10)), ('moltbook', uid(10))])
        self.assertEqual(page['messages'][0]['tags'], ['agent-memory', 'htalk'])
        self.assertFalse(page['checkpoint_safe'])
        self.store.mark('postingboard', uid(10), 'read')
        updated = self.command('tags')
        self.assertEqual([t['unread'] for t in updated['tags']], [0, 1])
        self.assertEqual(updated['counts'], {'unread': 2, 'tagged_unread': 1, 'untagged_unread': 1})
        other = self.command('list', **updated['untagged']['read']['arguments'])
        self.assertEqual([m['id'] for m in other['messages']], [uid(11)])
        self.assertIsNone(other['messages'][0]['read_at'])
        self.assertEqual(other['messages'][0]['tags'], [])
        self.assertEqual(self.command('show', source='postingboard', id=uid(10))['message']['tags'],
                         ['agent-memory', 'htalk'])
        self.store.save('postingboard', uid(1), [mail(12)])
        self.assertEqual(self.command('show', source='postingboard', id=uid(12))['message']['tags'],
                         ['agent-memory', 'htalk'])
        self.assertEqual([t['unread'] for t in self.command('tags')['tags']], [1, 2])

    def test_late_tag_filters_before_limit_and_paginates_across_read_marks(self):
        for n in range(10, 15):
            self.store.save('postingboard', uid(1), [dict(mail(n), thread_id=uid(100 if n in (11, 13, 14) else 200))])
        delivery = self.command('list', scope='all')
        self.assertEqual(delivery['next_after'], 5)
        self.tag('late')
        after, seen = 0, []
        for expected, more in ((2, True), (4, True), (5, False)):
            page = self.command('list', tag='late', unread=True, after=after, limit=1, scope='all')
            self.assertEqual((page['next_after'], page['more'], page['checkpoint_safe']), (expected, more, False))
            seen.extend(m['id'] for m in page['messages'])
            self.store.mark('postingboard', page['messages'][0]['id'], 'read')
            after = page['next_after']
        self.assertEqual(seen, [uid(11), uid(13), uid(14)])
        self.assertEqual(len(self.command('list', tag='late', scope='all')['messages']), 3)
        self.assertEqual(len(self.command('list', tag='late', unread=True, scope='all')['messages']), 0)
        self.assertEqual(self.store.status()['counts']['unread'], 2)
        self.assertTrue(self.command('list')['checkpoint_safe'])
        self.assertFalse(self.command('list', tag='missing')['checkpoint_safe'])

    def test_topic_directory_keeps_empty_threads_and_labels_metadata_provenance(self):
        self.store.save('postingboard', uid(1), [dict(mail(100), title='Stored root', body='DO NOT SHOW ROOT BODY')])
        self.store.save('moltbook', uid(2), [dict(mail(10), title='Reply label')])
        self.store.save_collection('moltbook', uid(2), 'moltbook', 0,
            Batch(originals=[dict(mail(100), title='Cached root', body='DO NOT SHOW CACHED BODY')]))
        self.store.save('custom', 'example', [dict(mail(11), thread_id='opaque/root:α', title='Local reply label')])
        self.store.set_subscription('postingboard', uid(100), True)
        self.tag('memory')
        self.tag('memory', 'moltbook')
        self.tag('memory', 'custom', thread='opaque/root:α')
        self.tag('memory', 'custom', thread='unseen-root')
        before = self.path.read_bytes()
        result = self.command('tag_show', tag='memory')
        self.assertTrue(result['exists'])
        self.assertEqual(result['counts'], {'threads': 4, 'messages': 3, 'unread': 3})
        members = {(r['source'], r['thread']): r for r in result['threads']}
        root = members['postingboard', uid(100)]
        self.assertEqual((root['title'], root['title_origin'], root['title_message_id'], root['subscribed']),
                         ('Stored root', 'stored_root', uid(100), True))
        cached = members['moltbook', uid(100)]
        self.assertEqual((cached['title'], cached['title_origin'], cached['url']),
                         ('Cached root', 'cached_root', mail(100)['url']))
        reply = members['custom', 'opaque/root:α']
        self.assertEqual((reply['title'], reply['title_origin'], reply['url_origin']),
                         ('Local reply label', 'stored_message', 'stored_message'))
        empty = members['custom', 'unseen-root']
        self.assertEqual((empty['title'], empty['url'], empty['messages'], empty['unread'], empty['subscribed']),
                         (None, None, 0, 0, False))
        self.assertNotIn('DO NOT SHOW', json.dumps(result))
        self.assertNotIn('Synthetic text', json.dumps(result))
        self.assertEqual(self.path.read_bytes(), before)
        self.store.mark('postingboard', uid(100), 'read')
        reopened = self.command('list', **root['read']['arguments'])
        self.assertEqual([m['id'] for m in reopened['messages']], [uid(100)])

    def test_tags_and_subscriptions_remain_independent_and_repeated_writes_are_safe(self):
        self.store.save('postingboard', uid(1), [mail(10)])
        self.store.mark('postingboard', uid(10), 'needs_reply')
        self.store.mark('postingboard', uid(10), 'replied', ref='https://example.invalid/reply')
        self.store.set_subscription('postingboard', uid(100), True)
        self.store.set_paused('postingboard', True)
        before = self.rows()
        for changed in (True, False):
            result = self.tag('htalk', id=uid(10))
            self.assertEqual((result['thread'], result['tagged'], result['changed']), (uid(100), True, changed))
            self.assertFalse(result['collection_performed'])
            self.assertEqual(self.rows(), before)
        self.store.set_subscription('postingboard', uid(100), False)
        self.assertEqual(self.command('tags')['tags'][0]['threads'], 1)
        self.store.set_subscription('postingboard', uid(100), True)
        before = self.rows()
        for changed in (True, False):
            result = self.command('tag_remove', tag='htalk', source='postingboard', id=uid(10))
            self.assertEqual((result['tagged'], result['changed']), (False, changed))
            self.assertEqual(self.rows(), before)
        self.assertEqual(self.command('tags')['tags'], [])
        self.assertEqual(self.command('tags')['untagged']['unread'], 1)

    def test_old_databases_read_without_migration_and_tag_write_preserves_old_state(self):
        for version in (1, 2):
            path = Path(self.temp.name) / f'v{version}.sqlite3'
            with closing(sqlite3.connect(path)) as db:
                db.executescript((Path(__file__).parent / 'fixtures/v1.sql').read_text())
            self.store = Store(path)
            if version == 2:
                self.store.prepare_collection()
            before = path.read_bytes()
            saved = self.rows()
            self.assertEqual(self.command('tags')['tags'], [])
            self.assertFalse(self.command('tag_show', tag='none')['exists'])
            self.assertEqual(self.command('list', tag='none')['messages'], [])
            self.assertEqual(self.command('list', untagged=True)['scanned'], self.store.status()['counts']['total'])
            self.assertFalse(self.command('tag_remove', tag='none', source='moltbook', thread=uid(100))['changed'])
            self.assertEqual(path.read_bytes(), before)
            self.tag('new', source='moltbook')
            self.assertEqual(self.rows(), saved)
            with self.store.connect() as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], version)

    def test_invalid_selections_and_filters_fail_without_writes_or_collection(self):
        before = self.path.read_bytes()
        bad = [('tag_add', {'tag': name, 'source': 'postingboard', 'thread': uid(100)}, 'invalid_tag_name')
               for name in ('', 'HTalk', 'two words', '-bad', 'x'*65, True)]
        bad += [('tag_add', {'tag': 'ok', 'source': 'typo', 'thread': uid(100)}, 'source_not_found'),
                ('tag_add', {'tag': 'ok', 'source': 'postingboard', 'id': uid(999)}, 'message_not_found'),
                ('tag_add', {'tag': 'ok', 'source': 'postingboard'}, 'invalid_arguments'),
                ('tag_add', {'tag': 'ok', 'source': 'postingboard', 'thread': uid(100), 'id': uid(10)}, 'invalid_arguments'),
                ('list', {'tag': 'ok', 'untagged': True}, 'invalid_arguments'),
                ('check', {'tag': 'ok', 'sources': {}}, 'invalid_arguments'),
                ('list', {'untagged': 1}, 'invalid_arguments')]
        with patch('boardmail.providers.collect_all') as collect:
            for command, arguments, expected in bad:
                result, code = commands.outcome(lambda: commands.execute(self.store, command, **arguments))
                self.assertEqual((result.get('error'), code), (expected, 2), (command, arguments, result))
                self.assertEqual(self.path.read_bytes(), before)
            collect.assert_not_called()

    def test_cli_message_selection_survives_restart_and_topic_summary_has_tags(self):
        self.store.save('postingboard', uid(1), [dict(mail(10), addressing='thread')])
        code, result = self.cli('tag', 'add', 'htalk', 'postingboard', '--message', uid(10))
        self.assertEqual((code, result.get('thread')), (0, uid(100)))
        code, topics = self.cli('tags')
        self.assertEqual((code, topics['tags'][0]['unread']), (0, 1))
        code, selected = self.cli('list', '--tag', 'htalk')
        self.assertEqual(code, 0)
        self.assertEqual((selected['messages'], selected['thread_activity'][0]['tags']), ([], ['htalk']))
        code, membership = self.cli('tag', 'show', 'htalk')
        self.assertEqual((code, len(membership['threads'])), (0, 1))
        self.assertEqual(self.cli('list', '--tag', 'htalk', '--untagged')[0], 2)
        self.assertEqual(self.cli('tag', 'add', 'htalk', 'postingboard', uid(100), '--message', uid(10))[0], 2)
        self.assertEqual(self.cli('tag', 'remove', 'htalk', 'postingboard', uid(100))[0], 0)
        self.assertEqual(self.cli('list', '--untagged', '--scope', 'all')[1]['messages'][0]['tags'], [])

    def test_concurrent_additions_preserve_membership_and_source_isolation(self):
        def add(pair):
            source, tag = pair
            result, code = commands.execute(Store(self.path), 'tag_add', tag=tag, source=source, thread=uid(100))
            self.assertEqual(code, 0)
            return result['changed']
        pairs = [('postingboard', 'one'), ('postingboard', 'two'), ('moltbook', 'one')]*2
        with ThreadPoolExecutor(3) as pool:
            self.assertEqual(sum(pool.map(add, pairs)), 3)
        members = self.command('tag_show', tag='one')['threads']
        self.assertEqual([(t['source'], t['tags']) for t in members],
                         [('moltbook', ['one']), ('postingboard', ['one', 'two'])])

    def test_directory_ignores_mismatched_cache_and_bounds_fallback_titles(self):
        self.store.save('postingboard', uid(1), [dict(mail(10), title=' \t\n'),
                                                dict(mail(11), title='Known title '*30)])
        self.store.save_collection('postingboard', uid(1), 'postingboard', 0,
            Batch(originals=[dict(mail(100), thread_id=uid(999), title='Wrong cached thread')]))
        self.tag('topic')
        member = self.command('tag_show', tag='topic')['threads'][0]
        self.assertEqual(member['title'], ('Known title '*30)[:160])
        self.assertTrue(member['title_truncated'])
        self.assertEqual((member['title_origin'], member['title_message_id']), ('stored_message', uid(11)))
        self.assertNotIn('body', member)

    def test_tag_and_untagged_filters_combine_with_source_thread_and_interval(self):
        self.store.save('postingboard', uid(1), [mail(10), dict(mail(11), thread_id=uid(200)),
                                                dict(mail(12), thread_id=uid(200))])
        self.store.save('moltbook', uid(2), [mail(10)])
        self.tag('one')
        self.tag('one', 'moltbook')
        selected = self.command('list', tag='one', source='moltbook', thread=uid(100), through=4, limit=1)
        self.assertEqual([(m['source'], m['id']) for m in selected['messages']], [('moltbook', uid(10))])
        first = self.command('list', untagged=True, source='postingboard', through=3, limit=1)
        self.assertEqual((first['next_after'], first['more'], first['scanned']), (2, True, 1))
        second = self.command('list', untagged=True, source='postingboard', through=3, after=2, limit=1)
        self.assertEqual((second['next_after'], second['more'], second['scanned']), (3, False, 1))


if __name__ == '__main__':
    unittest.main()
