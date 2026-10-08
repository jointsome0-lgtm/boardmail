"""Consumer preferences, lossless scope changes, and bounded local context."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest

from boardmail import commands
from boardmail.adapters import Batch, validate
from boardmail.boards import BOARDS
from boardmail.config import MailError
from boardmail.store import Store
from examples.fixtures import FakeBoard, FixtureBoard, original, settings, uid
from kit import DESCRIBED, Clock, arrive, described, fixed, mark, notify
from test_mail import mail


class ReadingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'inbox.sqlite3'
        self.store = Store(self.path)
        commands.execute(self.store, 'init')

    def run_command(self, command='list', **options):
        result, code = commands.execute(self.store, command, **options)
        self.assertEqual(code, 0, result)
        return result

    def save(self, *kinds):
        return arrive(self.store, 'moltbook', uid(2), [dict(mail(10+i), addressing=kind) for i, kind in enumerate(kinds)])

    def colony(self):
        """An invented Colony that has no comment yet, and the settings of its source. A pass over it gives each
        comment as the board has it alone, so the inbox has a message and not what the message answers."""
        cfg = settings(self.temp.name)['the-colony']
        board = FixtureBoard('the-colony', cfg)
        board.comments, board.events = [], []
        return board, {'the-colony': cfg}

    def test_defaults_readonly_persistence_override_reset_and_other_database(self):
        before = self.path.read_bytes()
        self.assertEqual(self.run_command('settings')['settings'], {
            'scope': 'addressed', 'context': 'brief', 'origin': {'scope': 'default', 'context': 'default'}})
        self.assertEqual(self.path.read_bytes(), before)
        other = Store(Path(self.temp.name) / 'other.sqlite3')
        commands.execute(other, 'init')
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(lambda opts: self.run_command('settings', **opts), [{'scope': 'all'}, {'context': 'none'}]))
        self.assertEqual(self.run_command()['reading'], {'scope': 'all', 'context': 'none'})
        self.assertEqual(self.run_command(scope='addressed')['reading'], {'scope': 'addressed', 'context': 'none'})
        self.assertEqual(self.store.settings()['scope'], 'all')
        self.assertEqual(other.settings()['scope'], 'addressed')
        self.run_command('settings', reset=True)
        self.assertEqual(self.store.settings()['origin'], {'scope': 'default', 'context': 'default'})

    def test_invalid_preferences_fail_before_collection_and_can_be_reset(self):
        before = self.path.read_bytes()
        # A collection would ask the board for the profile of the account. The board has no answer, and it is
        # asked nothing.
        board, sources = FakeBoard([]), {'moltbook': settings(self.temp.name)['moltbook']}
        for args in ({'scope': 'guess'}, {'context': 'full'}, {'through': 2}):
            result, code = commands.outcome(lambda: commands.execute(self.store, 'check', sources=sources, fetch=board, **args))
            self.assertEqual((result['error'], code), ('invalid_arguments', 2))
        self.assertEqual(board.asked, [])
        self.assertEqual(self.path.read_bytes(), before)
        self.run_command('settings', scope='all')
        # No command saves a value like this one. The file has it from somewhere else.
        with closing(sqlite3.connect(self.path)) as db, db:
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
        self.assertEqual([m['shown_because'] for m in next_page['messages']], [
            'direct_reply_to_your_message', 'recipient_unconfirmed_shown_by_default',
            'mention_detected_may_be_quoted', 'direct_reply_and_mention_detected'])
        self.assertEqual(next_page['messages'][1]['kind'], 'mention')
        all_page = self.run_command(scope='all', context='none')
        self.assertEqual(len(all_page['messages']), 6)
        self.assertEqual(all_page['messages'][0]['shown_because'],
                         'thread_activity_without_confirmed_direct_reply_or_mention')
        self.assertFalse(any('brief' in m for m in all_page['messages']))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.store.status()['counts']['unread'], 6)

    def test_page_without_a_limit_holds_twenty_and_its_pages_give_a_backlog_once_in_order(self):
        backlog = [dict(mail(10 + n), addressing='direct') for n in range(45)]
        # check collects the backlog and reads its first page. list and wait read that page from the inbox.
        for command, options in (('check', {'sources': {'moltbook': described(uid(2), messages=backlog)}}),
                                 ('list', {}), ('wait', {'timeout': 0})):
            with self.subTest(command=command):
                page = self.run_command(command, **options)
                self.assertEqual((len(page['messages']), page['scanned'], page['next_after'], page['more']),
                                 (20, 20, 20, True))
        pages = [self.run_command()]
        while pages[-1]['more'] and len(pages) < 5:
            pages.append(self.run_command(after=pages[-1]['next_after']))
        self.assertEqual([len(page['messages']) for page in pages], [20, 20, 5])
        self.assertEqual([m['id'] for page in pages for m in page['messages']], [m['id'] for m in backlog])
        # A caller that names a limit gets that many.
        for limit, most in ((1, 1), (21, 21), (100, 45), (500, 45)):
            page = self.run_command(limit=limit)
            self.assertEqual((len(page['messages']), page['more']), (most, most < 45))

    def test_replay_of_a_summary_names_no_limit_and_its_pages_give_the_thread_once(self):
        self.save('direct', *['thread'] * 45)
        summary, = self.run_command(limit=500)['thread_activity']
        self.assertEqual((summary['count'], summary['first_seq'], summary['last_seq']), (45, 2, 46))
        replay = summary['replay']['arguments']
        self.assertNotIn('limit', replay)
        pages = [self.run_command(**replay)]
        while pages[-1]['more'] and len(pages) < 5:
            pages.append(self.run_command(**{**replay, 'after': pages[-1]['next_after']}))
        self.assertEqual([len(page['messages']) for page in pages], [20, 20, 5])
        self.assertEqual([m['id'] for page in pages for m in page['messages']], [uid(11 + n) for n in range(45)])

    def test_page_names_only_the_sources_that_need_attention(self):
        well = {'well': described(uid(2), messages=[dict(mail(10), addressing='direct')])}
        commands.execute(self.store, 'collect', sources=well)
        self.assertEqual(self.run_command()['sources'], [])
        store = Store(Path(self.temp.name) / 'health.sqlite3')
        # A source that the inbox knows and that no pass has read.
        commands.execute(store, 'init', sources={'unread': described(uid(2))})
        commands.execute(store, 'collect', sources=well)
        with fixed(Clock(1000)):
            arrive(store, 'stale', uid(2))  # A pass long ago that went well.
        arrive(store, 'behind', uid(2), complete=False)
        arrive(store, 'failing', uid(2), error='http_503')
        arrive(store, 'resting', uid(2))
        commands.execute(store, 'pause', source='resting')
        ailing = {'behind': ('ok', True), 'failing': ('error', False), 'resting': ('paused', False),
                  'stale': ('stale', False), 'unread': ('unknown', False)}
        for command, options in (('list', {}), ('wait', {'timeout': 0}), ('check', {'sources': well})):
            with self.subTest(command=command):
                page, code = commands.execute(store, command, **options)
                self.assertEqual(code, 0, page)
                self.assertEqual({source['source']: (source['status'], source['backlog_pending'])
                                  for source in page['sources']}, ailing)
        # status and collect name every source.
        for command, options in (('status', {}), ('collect', {'sources': well})):
            with self.subTest(command=command):
                result, code = commands.execute(store, command, **options)
                self.assertEqual({source['source'] for source in result['sources']}, {'well', *ailing})

    def test_message_in_a_result_has_a_field_only_where_it_holds_something(self):
        eight = ('parent_id', 'provider_seq', 'read_at', 'needs_reply', 'replied_at', 'reply_ref', 'discovery', 'tags')
        ref = 'https://board.example.invalid/posts/' + uid(91)
        # The first message holds each of the eight fields and the second holds none. The third answers the first.
        arrive(self.store, 'moltbook', uid(2), [
            dict(mail(10), parent_id=uid(90), provider_seq=7, discovery='subscription', addressing='direct'),
            dict(mail(200), thread_id=uid(200), addressing='direct'),
            dict(mail(14), parent_id=uid(10), addressing='direct')])
        with fixed(Clock(1000)):
            for action in ('read', 'needs_reply'):
                mark(self.store, 'moltbook', uid(10), action)
            mark(self.store, 'moltbook', uid(10), 'replied', ref=ref)
        self.run_command('tag_add', tag='kept', source='moltbook', thread=uid(100))
        holds = {uid(10): {'parent_id': uid(90), 'provider_seq': 7, 'read_at': 1000, 'needs_reply': True,
                           'replied_at': 1000, 'reply_ref': ref, 'discovery': 'subscription', 'tags': ['kept']},
                 uid(200): {}, uid(14): {'parent_id': uid(10), 'tags': ['kept']}}

        def held(*carried):
            """What each of these messages of a result has of the eight fields."""
            return {message['id']: {field: message[field] for field in eight if field in message}
                    for message in carried}

        def of(*ids):
            return {mid: holds[mid] for mid in ids}

        pages = (('list', {}), ('list', {'scope': 'all', 'context': 'none'}), ('wait', {'timeout': 0}),
                 ('check', {'sources': {'moltbook': described(uid(2))}}))
        for command, options in pages:
            with self.subTest(command=command, options=sorted(options)):
                self.assertEqual(held(*self.run_command(command, **options)['messages']), holds)
        for mid in holds:
            for command in ('show', 'reply_show'):
                with self.subTest(command=command, id=mid):
                    self.assertEqual(held(self.run_command(command, source='moltbook', id=mid)['message']), of(mid))
        # Each of these marks is one that the message has already, so the message is as it was.
        self.assertEqual(held(mark(self.store, 'moltbook', uid(10), 'needs_reply')['message']), of(uid(10)))
        self.assertEqual(held(mark(self.store, 'moltbook', uid(200), 'clear_reply')['message']), of(uid(200)))
        prepared = self.run_command('reply_prepare', source='moltbook', id=uid(200), body='An invented answer.')
        self.assertEqual(held(prepared['message']), of(uid(200)))

        def context(mid):
            return commands.execute(self.store, 'context', source='moltbook', id=mid, local=True)[0]

        answer, root = context(uid(14)), context(uid(200))
        self.assertEqual(held(answer['target']['message'], answer['parent']['message']), of(uid(14), uid(10)))
        self.assertEqual(held(root['target']['message'], root['root']['message']), of(uid(200)))
        for thread, ids in ((uid(100), (uid(10), uid(14))), (uid(200), (uid(200),))):
            expanded = commands.execute(self.store, 'expand', source='moltbook', thread=thread, through=3, local=True)[0]
            self.assertEqual(held(*(item['target']['message'] for item in expanded['items'])), of(*ids))
        self.assertEqual(held(expanded['root']['message']), of(uid(200)))
        # What a page and a thread summary have of their own stays, also where it holds nothing.
        arrive(self.store, 'moltbook', uid(2), [dict(mail(15), thread_id=uid(300), addressing='thread')])
        page = self.run_command(after=3)
        self.assertEqual((page['messages'], page['more'], page['sources']), ([], False, []))
        self.assertEqual((page['thread_activity'][0]['tags'], page['thread_activity'][0]['unread']), ([], 1))

    def test_interleaved_summaries_follow_first_arrival_and_share_the_page_checkpoint(self):
        arrive(self.store, 'moltbook', uid(2), [dict(mail(9), addressing='direct')])
        arrive(self.store, 'moltbook', uid(2), [dict(mail(10, created=900), addressing='thread')])
        arrive(self.store, 'moltbook', uid(2), [dict(mail(11, created=800), addressing='direct')])
        arrive(self.store, 'the-colony', uid(1), [dict(mail(10, created=700),
                                                       thread_id=uid(200), addressing='thread')])
        arrive(self.store, 'moltbook', uid(2), [dict(mail(12, created=600), addressing='thread')])
        arrive(self.store, 'moltbook', uid(2), [dict(mail(13, created=500), addressing='direct')])
        page = self.run_command(after=1, limit=4)
        self.assertEqual([m['arrival_seq'] for m in page['messages']], [3])
        self.assertEqual([(s['source'], s['first_seq'], s['last_seq'], s['count'])
                          for s in page['thread_activity']], [('moltbook', 2, 5, 2), ('the-colony', 4, 4, 1)])
        self.assertEqual((page['scanned'], page['next_after'], page['more']), (4, 5, True))
        self.assertEqual(page['scanned'], len(page['messages']) + sum(s['count'] for s in page['thread_activity']))
        following = self.run_command(after=page['next_after'])
        self.assertEqual([m['arrival_seq'] for m in following['messages']], [6])
        self.assertFalse(following['more'])

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
        mark(self.store, 'moltbook', uid(10), 'read')
        arrive(self.store, 'moltbook', uid(2), [dict(mail(99), addressing='thread')])
        arrive(self.store, 'the-colony', uid(1), [dict(mail(10), addressing='thread')])
        replay = self.run_command(**summary['replay']['arguments'])
        self.assertEqual([m['id'] for m in replay['messages']], [uid(10), uid(11)])
        self.assertFalse(replay['more'])
        self.assertFalse(replay['checkpoint_safe'])
        expanded, code = commands.execute(self.store, summary['expand']['command'],
                                           local=True, **summary['expand']['arguments'])
        self.assertEqual(code, 1)  # Saved messages survive unavailable local context.
        self.assertEqual([item['id'] for item in expanded['items']], [uid(10), uid(11)])
        self.assertEqual(expanded['through'], summary['last_seq'])
        self.assertFalse(expanded['checkpoint_safe'])
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

    def test_brief_uses_fetched_public_originals_without_inbox_pollution_or_network(self):
        root = dict(mail(100), body='r' * 5000)
        parent = dict(mail(90), body='our public parent')
        incoming = dict(mail(10), parent_id=uid(90), addressing='direct')
        arrive(self.store, 'moltbook', uid(2), [incoming], originals=[root, parent])
        self.assertEqual(self.store.status()['counts']['total'], 1)
        before = self.path.read_bytes()
        board = FakeBoard([])  # It has no answer, and it is asked nothing.
        brief = self.run_command(fetch=board)['messages'][0]['brief']
        self.assertEqual(board.asked, [])
        self.assertEqual(brief['root']['status'], 'cached')
        self.assertEqual(len(brief['root']['body']), 600)
        self.assertTrue(brief['root']['truncated'])
        self.assertEqual(brief['parent']['body'], 'our public parent')
        self.assertEqual(self.path.read_bytes(), before)
        with self.store.connect() as db:
            cached = json.loads(db.execute('SELECT value FROM originals WHERE id=?', (uid(100),)).fetchone()[0])
        self.assertEqual(len(cached['body']), 4096)
        self.assertTrue(cached['truncated'])

    def test_brief_says_of_root_and_parent_only_what_the_message_does_not(self):
        root = dict(mail(100), title='Another title', body='the root')
        parent = dict(mail(90), body='our public parent')  # It has the title of the message.
        arrive(self.store, 'moltbook', uid(2), [dict(mail(10), parent_id=uid(90), addressing='direct')],
               originals=[root, parent])
        message, = self.run_command()['messages']
        self.assertEqual(message['brief']['root'], {
            'id': uid(100), 'status': 'cached', 'author': 'example-agent', 'title': 'Another title',
            'body': 'the root'})
        self.assertEqual(message['brief']['parent'], {
            'id': uid(90), 'status': 'cached', 'author': 'example-agent', 'body': 'our public parent'})
        self.assertEqual(message['brief']['previous_exchange'], {'status': 'unknown', 'messages': []})
        self.assertEqual(set(message['brief']), {'root', 'parent', 'previous_exchange'})

    def test_missing_context_and_exact_previous_exchange_are_visible_and_bounded(self):
        # Three comments under a post of the account, which answers them with one comment of its own. A fourth
        # comment then answers that one.
        board, sources = self.colony()
        for n in (20, 21, 22):
            notify(board, original(n, 101, colony=True))
        self.run_command('collect', sources=sources, fetch=board)
        ref = BOARDS['the-colony'].reference(uid(101), uid(90))
        for n in (20, 21, 22):
            mark(self.store, 'the-colony', uid(n), 'replied', ref=ref)
        notify(board, {**original(10, 101, colony=True), 'parent_id': uid(90)}, 'reply_to_comment')
        self.run_command('collect', sources=sources, fetch=board)
        brief = self.run_command(after=3)['messages'][0]['brief']
        self.assertEqual(brief['parent']['status'], 'not_available_locally')
        self.assertEqual(brief['previous_exchange']['status'], 'linked')
        self.assertEqual(len(brief['previous_exchange']['messages']), 2)
        self.assertTrue(brief['previous_exchange']['more'])
        # An earlier message of the exchange is under the same post, and has its title.
        self.assertEqual([set(earlier) for earlier in brief['previous_exchange']['messages']],
                         [{'id', 'author', 'body'}] * 2)
        self.assertEqual(set(brief), {'root', 'parent', 'previous_exchange'})
        self.assertEqual(set(brief['previous_exchange']), {'status', 'messages', 'more'})

    def test_stale_collector_keeps_newer_cache_but_retains_unique_arrivals(self):
        root = dict(mail(100), body='new context')
        # A pass gives a message and old context. While it is under way, another pass gives newer context and is
        # saved first.
        stale = arrive(self.store, 'moltbook', uid(2), [dict(mail(10), parent_id=uid(100), addressing='direct')],
                       originals=[dict(root, body='stale context')],
                       meanwhile=lambda: arrive(self.store, 'moltbook', uid(2), originals=[root]))
        self.assertEqual((stale['added'], [error['error'] for error in stale['errors']]), (1, ['collection_conflict']))
        result = self.run_command()['messages'][0]
        self.assertEqual(result['brief']['root']['body'], 'new context')
        self.assertEqual(result['brief']['parent']['status'], 'same_as_root')

    def test_failed_cache_only_collector_rejects_stale_context_and_preserves_marks(self):
        for existing_progress in (False, True):
            with self.subTest(existing_progress=existing_progress):
                store = Store(Path(self.temp.name) / f'cache-{existing_progress}.sqlite3')
                commands.execute(store, 'init')
                if existing_progress:
                    arrive(store, 'moltbook', uid(2), state={'cursor': 'saved'})
                arrive(store, 'moltbook', uid(2), [mail(20)])
                for action in ('read', 'needs_reply'):
                    mark(store, 'moltbook', uid(20), action)
                mark(store, 'moltbook', uid(20), 'replied', ref='https://example.invalid/reply')
                saved = store.show('moltbook', uid(20))
                _, state, revision = store.collection_state('moltbook', uid(2), str(DESCRIBED))
                self.assertEqual(state, {'cursor': 'saved'} if existing_progress else {})
                root = dict(mail(100), body='new context')

                def partial():
                    # A pass that fails after it got newer context, and keeps the progress that it found.
                    result = arrive(store, 'moltbook', uid(2), originals=[root], complete=False, error='source_timeout')
                    self.assertEqual((result['added'], [error['error'] for error in result['errors']]),
                                     (0, ['source_timeout']))
                    self.assertEqual(store.collection_state('moltbook', uid(2), str(DESCRIBED))[2], revision + 1)

                # A pass gives a saved message, a new one, old context and progress of its own. While it is under
                # way, the failing pass is saved first.
                stale = arrive(store, 'moltbook', uid(2), [mail(20), dict(mail(10), parent_id=uid(100), addressing='direct')],
                               originals=[dict(root, body='stale context')], state={'cursor': 'stale'}, meanwhile=partial)
                self.assertEqual((stale['added'], [error['error'] for error in stale['errors']]),
                                 (1, ['collection_conflict']))
                _, current_state, current_revision = store.collection_state('moltbook', uid(2), str(DESCRIBED))
                self.assertEqual((current_state, current_revision), (state, revision + 1))
                self.assertEqual(store.show('moltbook', uid(20)), saved)
                incoming = next(m for m in store.page(context='brief')['messages'] if m['id'] == uid(10))
                self.assertEqual(incoming['brief']['root']['body'], 'new context')
                health = store.status()['sources'][0]
                self.assertEqual((health['error'], health['backlog_pending']), ('source_timeout', True))

    def test_invalid_adapter_metadata_is_rejected_but_omitted_metadata_works(self):
        validate(Batch(messages=[mail(10)]))
        for batch in (Batch(messages=[dict(mail(10), addressing='probably-direct')]),
                      Batch(originals=[dict(mail(100), url='https://user:secret@example.invalid')])):
            with self.assertRaisesRegex(MailError, '^invalid_adapter_result$'):
                validate(batch)

    def test_conflicting_parent_cannot_supply_context_or_previous_exchange(self):
        # The board says of a comment under one post that it answers a comment under another post.
        board, sources = self.colony()
        notify(board, original(90, 99, colony=True))
        notify(board, original(20, 101, colony=True))
        self.run_command('collect', sources=sources, fetch=board)
        mark(self.store, 'the-colony', uid(20), 'replied', ref=BOARDS['the-colony'].reference(uid(101), uid(90)))
        notify(board, {**original(10, 101, colony=True), 'parent_id': uid(90)}, 'reply_to_comment')
        self.run_command('collect', sources=sources, fetch=board)
        brief = self.run_command(after=2)['messages'][0]['brief']
        self.assertEqual(brief['parent'], {'id': uid(90), 'status': 'unavailable', 'reason': 'thread_mismatch'})
        self.assertEqual(brief['previous_exchange'], {'status': 'unknown', 'messages': []})

    def test_fourclaw_synthesized_parent_remains_unknown_in_brief(self):
        from test_fourclaw import THREAD, page, post, threads
        # A reply in a thread that the account opened. The adapter gives the thread as what it answers.
        board = threads({THREAD: page('Reader', [post('Other', 'A reply in the thread.')])})
        self.run_command('collect', sources={'fourclaw': {'account_id': 'Reader', 'watched_threads': [THREAD]}},
                         fetch=board)
        message = self.run_command(scope='all')['messages'][0]
        self.assertEqual((message['parent_id'], message['addressing']), (THREAD, 'thread'))
        self.assertEqual(message['brief']['parent'], {'id': None, 'status': 'unknown'})

    def test_fruitflies_reply_to_our_answer_keeps_that_answer_as_context(self):
        from test_fruitflies import feed, post
        board = feed([post(2, 'our answer', author='alice', parent=1, kind='answer')],
                     [post(3, 'follow-up', parent=2, kind='answer')], [])
        self.run_command('collect', sources={'fly': {'account_id': 'alice', 'adapter': 'fruitflies'}}, fetch=board)
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
        args = dict(summary['expand']['arguments'])
        source, thread = args.pop('source'), args.pop('thread')
        process = subprocess.run([sys.executable, '-m', 'boardmail', '--db', str(self.path),
            summary['expand']['command'], source, thread, '--local',
            *(part for key, value in args.items() for part in ('--'+key, str(value)))],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(process.returncode, 1)  # The fixture has no saved root or parent.
        expanded = json.loads(process.stdout)
        self.assertEqual([item['id'] for item in expanded['items']], [uid(10)])
        self.assertFalse(expanded['checkpoint_safe'])
        self.assertEqual(cli('settings')['settings']['scope'], 'all')


if __name__ == '__main__':
    unittest.main()
