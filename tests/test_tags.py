"""Topic reading preserves the inbox; all threads and messages are invented."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from boardmail import commands
from boardmail.store import Store
from examples.fixtures import FakeBoard, FixtureBoard, original, settings, uid
from kit import arrive, described, mark, notify
from test_mail import mail


class TagTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'mail.sqlite3'
        self.store = Store(self.path)
        # Three sources that the adapter file of the tests fills. A tag does not ask what fills a source.
        commands.execute(self.store, 'init', sources={'postingboard': described(uid(1)), 'moltbook': described(uid(2)),
                                                      'custom': described('example')})

    def colony(self):
        """A fourth source, which can have subscriptions: an invented Colony that has no comment yet. Its name
        and its settings."""
        cfg = settings(self.temp.name)['the-colony']
        self.board = FixtureBoard('the-colony', cfg)
        self.board.comments, self.board.events = [], []
        return 'the-colony', {'the-colony': cfg}

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
        arrive(self.store, 'postingboard', uid(1), [dict(mail(10), addressing='thread'),
                                                    dict(mail(11), thread_id=uid(200))])
        arrive(self.store, 'moltbook', uid(2), [mail(10)])
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
        mark(self.store, 'postingboard', uid(10), 'read')
        updated = self.command('tags')
        self.assertEqual([t['unread'] for t in updated['tags']], [0, 1])
        self.assertEqual(updated['counts'], {'unread': 2, 'tagged_unread': 1, 'untagged_unread': 1})
        other = self.command('list', **updated['untagged']['read']['arguments'])
        self.assertEqual([m['id'] for m in other['messages']], [uid(11)])
        self.assertIsNone(other['messages'][0]['read_at'])
        self.assertEqual(other['messages'][0]['tags'], [])
        self.assertEqual(self.command('show', source='postingboard', id=uid(10))['message']['tags'],
                         ['agent-memory', 'htalk'])
        arrive(self.store, 'postingboard', uid(1), [mail(12)])
        self.assertEqual(self.command('show', source='postingboard', id=uid(12))['message']['tags'],
                         ['agent-memory', 'htalk'])
        self.assertEqual([t['unread'] for t in self.command('tags')['tags']], [1, 2])

    def test_late_tag_filters_before_limit_and_paginates_across_read_marks(self):
        for n in range(10, 15):
            arrive(self.store, 'postingboard', uid(1), [dict(mail(n), thread_id=uid(100 if n in (11, 13, 14) else 200))])
        delivery = self.command('list', scope='all')
        self.assertEqual(delivery['next_after'], 5)
        self.tag('late')
        after, seen = 0, []
        for expected, more in ((2, True), (4, True), (5, False)):
            page = self.command('list', tag='late', unread=True, after=after, limit=1, scope='all')
            self.assertEqual((page['next_after'], page['more'], page['checkpoint_safe']), (expected, more, False))
            seen.extend(m['id'] for m in page['messages'])
            mark(self.store, 'postingboard', page['messages'][0]['id'], 'read')
            after = page['next_after']
        self.assertEqual(seen, [uid(11), uid(13), uid(14)])
        self.assertEqual(len(self.command('list', tag='late', scope='all')['messages']), 3)
        self.assertEqual(len(self.command('list', tag='late', unread=True, scope='all')['messages']), 0)
        self.assertEqual(self.store.status()['counts']['unread'], 2)
        self.assertTrue(self.command('list')['checkpoint_safe'])
        self.assertFalse(self.command('list', tag='missing')['checkpoint_safe'])

    def test_topic_directory_keeps_empty_threads_and_labels_metadata_provenance(self):
        # The Colony tells the account of a post that names it, and the account follows the thread of that post.
        colony, sources = self.colony()
        self.board.root.update(author={'id': uid(10), 'username': 'sample-writer'}, title='Stored root',
                               body='DO NOT SHOW ROOT BODY')
        self.board.events.append({'id': uid(5000), 'notification_type': 'mention', 'post_id': uid(101), 'is_read': True})
        self.command('collect', sources=sources, fetch=self.board)
        self.command('subscribe', source=colony, thread=uid(101))
        arrive(self.store, 'moltbook', uid(2), [dict(mail(10), title='Reply label')])
        arrive(self.store, 'moltbook', uid(2), originals=[dict(mail(100), title='Cached root', body='DO NOT SHOW CACHED BODY')])
        arrive(self.store, 'custom', 'example', [dict(mail(11), thread_id='opaque/root:α', title='Local reply label')])
        self.tag('memory', colony, thread=uid(101))
        self.tag('memory', 'moltbook')
        self.tag('memory', 'custom', thread='opaque/root:α')
        self.tag('memory', 'custom', thread='unseen-root')
        before = self.path.read_bytes()
        result = self.command('tag_show', tag='memory')
        self.assertTrue(result['exists'])
        self.assertEqual(result['counts'], {'threads': 4, 'messages': 3, 'unread': 3})
        members = {(r['source'], r['thread']): r for r in result['threads']}
        root = members[colony, uid(101)]
        self.assertEqual((root['title'], root['title_origin'], root['title_message_id'], root['subscribed']),
                         ('Stored root', 'stored_root', uid(101), True))
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
        mark(self.store, colony, uid(101), 'read')
        reopened = self.command('list', **root['read']['arguments'])
        self.assertEqual([m['id'] for m in reopened['messages']], [uid(101)])

    def test_tags_and_subscriptions_remain_independent_and_repeated_writes_are_safe(self):
        # A comment under a post of the account on the Colony, and the account follows the thread of that post.
        colony, sources = self.colony()
        notify(self.board, original(10, 101, colony=True))
        self.command('collect', sources=sources, fetch=self.board)
        mark(self.store, colony, uid(10), 'needs_reply')
        mark(self.store, colony, uid(10), 'replied', ref='https://example.invalid/reply')
        self.command('subscribe', source=colony, thread=uid(101))
        self.command('pause', source=colony)
        before = self.rows()
        for changed in (True, False):
            result = self.tag('htalk', colony, id=uid(10))
            self.assertEqual((result['thread'], result['tagged'], result['changed']), (uid(101), True, changed))
            self.assertFalse(result['collection_performed'])
            self.assertEqual(self.rows(), before)
        self.command('unsubscribe', source=colony, thread=uid(101))
        self.assertEqual(self.command('tags')['tags'][0]['threads'], 1)
        self.command('subscribe', source=colony, thread=uid(101))
        before = self.rows()
        for changed in (True, False):
            result = self.command('tag_remove', tag='htalk', source=colony, id=uid(10))
            self.assertEqual((result['tagged'], result['changed']), (False, changed))
            self.assertEqual(self.rows(), before)
        self.assertEqual(self.command('tags')['tags'], [])
        self.assertEqual(self.command('tags')['untagged']['unread'], 1)

    def test_invalid_selections_and_filters_fail_without_writes_or_collection(self):
        before = self.path.read_bytes()
        bad = [('tag_add', {'tag': name, 'source': 'postingboard', 'thread': uid(100)}, 'invalid_tag_name')
               for name in ('', 'HTalk', 'two words', '-bad', 'x'*65, True)]
        bad += [('tag_add', {'tag': 'ok', 'source': 'typo', 'thread': uid(100)}, 'source_not_found'),
                ('tag_add', {'tag': 'ok', 'source': 'postingboard', 'id': uid(999)}, 'message_not_found'),
                ('tag_add', {'tag': 'ok', 'source': 'postingboard'}, 'invalid_arguments'),
                ('tag_add', {'tag': 'ok', 'source': 'postingboard', 'thread': uid(100), 'id': uid(10)}, 'invalid_arguments'),
                ('list', {'tag': 'ok', 'untagged': True}, 'invalid_arguments'),
                ('check', {'tag': 'ok', 'sources': {'moltbook': settings(self.temp.name)['moltbook']}}, 'invalid_arguments'),
                ('list', {'untagged': 1}, 'invalid_arguments')]
        # A collection would ask the board for the profile of the account. The board has no answer, and it is
        # asked nothing.
        board = FakeBoard([])
        for command, arguments, expected in bad:
            result, code = commands.outcome(lambda: commands.execute(self.store, command, fetch=board, **arguments))
            self.assertEqual((result.get('error'), code), (expected, 2), (command, arguments, result))
            self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(board.asked, [])

    def test_cli_message_selection_survives_restart_and_topic_summary_has_tags(self):
        arrive(self.store, 'postingboard', uid(1), [dict(mail(10), addressing='thread')])
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
        arrive(self.store, 'postingboard', uid(1), [dict(mail(10), title=' \t\n'),
                                                    dict(mail(11), title='Known title '*30)])
        arrive(self.store, 'postingboard', uid(1), originals=[dict(mail(100), thread_id=uid(999), title='Wrong cached thread')])
        self.tag('topic')
        member = self.command('tag_show', tag='topic')['threads'][0]
        self.assertEqual(member['title'], ('Known title '*30)[:160])
        self.assertTrue(member['title_truncated'])
        self.assertEqual((member['title_origin'], member['title_message_id']), ('stored_message', uid(11)))
        self.assertNotIn('body', member)

    def test_tag_and_untagged_filters_combine_with_source_thread_and_interval(self):
        arrive(self.store, 'postingboard', uid(1), [mail(10), dict(mail(11), thread_id=uid(200)),
                                                    dict(mail(12), thread_id=uid(200))])
        arrive(self.store, 'moltbook', uid(2), [mail(10)])
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
