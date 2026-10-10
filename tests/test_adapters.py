"""Public extension, durable progress and 0.1 database compatibility contracts."""
from contextlib import closing, redirect_stdout
from pathlib import Path
from urllib.error import HTTPError
import io
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from boardmail import cli, commands, reader
from boardmail.adapters import Batch
from boardmail.boards import collect_all
from boardmail.config import MailError
from boardmail.store import Store
from examples.fixtures import FixtureBoard, named, original, settings, uid
from kit import DESCRIBED, Clock, arrive, described, fixed, mark, new_inbox
from test_mail import mail


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root/'mail.sqlite3'
        self.store = new_inbox(self.db)

    def cli(self, *args):
        result = subprocess.run([sys.executable, '-m', 'boardmail', *args], capture_output=True, text=True, timeout=10)
        return result.returncode, json.loads(result.stdout)

    def test_a_source_that_names_no_adapter_is_collected_by_the_file_of_its_name_in_the_adapters_folder(self):
        examples = Path(__file__).resolve().parents[1]/'examples'
        shutil.copyfile(examples/'custom_feed.json', self.root/'custom_feed.json')
        folder = self.root/'adapters'; folder.mkdir()
        ran = self.root/'ran'
        config = self.root/'config.json'
        sources = {'my-board': {'account_id': 'agent', 'feed_file': 'custom_feed.json', 'batch_size': 1}}
        config.write_text(json.dumps({'database': 'picked.sqlite3', 'sources': sources}))
        # Without its file the source is no source, as before.
        code, result = self.cli('--config', str(config), 'init')
        self.assertEqual((code, result['error']), (2, 'invalid_config'))
        shutil.copyfile(examples/'custom_board.py', folder/'my-board.py')
        # A file that no source names is not run, and neither is one with the name of a board of the package.
        for name in ('other.py', 'moltbook.py'):
            (folder/name).write_text(f'from pathlib import Path\nPath({str(ran)!r}).touch()\n')
        sources['moltbook'] = {'account_id': uid(1), 'api_key_file': 'no-key'}
        config.write_text(json.dumps({'database': 'picked.sqlite3', 'sources': sources}))
        self.assertEqual(self.cli('--config', str(config), 'init')[0], 0)
        code, result = self.cli('--config', str(config), 'collect')
        mine = [row for row in result['sources'] if row['source'] == 'my-board']
        self.assertEqual([(row['status'], row['error']) for row in mine], [('ok', None)])
        self.assertEqual(result['added'], 1)
        self.assertEqual(Store(self.root/'picked.sqlite3').show('my-board', '1')['id'], '1')
        self.assertFalse(ran.exists())

    def test_separately_supplied_adapter_and_copyable_consumer_loop(self):
        examples = Path(__file__).resolve().parents[1]/'examples'
        for name in ('custom_board.py', 'custom_feed.json', 'custom_config.json'):
            shutil.copyfile(examples/name, self.root/name)
        adapter = self.root/'custom_board.py'
        adapter.write_text(adapter.read_text()+'\nprint("synthetic diagnostic must not corrupt JSON")\n')
        config = self.root/'custom_config.json'
        db = self.root/'custom.sqlite3'
        base = ('--config', str(config))
        self.assertEqual(self.cli(*base, 'init')[0], 0)
        for expected in (1, 2):
            code, result = self.cli(*base, 'collect')
            self.assertEqual(code, 0); self.assertEqual(result['added'], 1)
            code, result = self.cli('--db', str(db), 'wait', '--after', str(expected-1), '--timeout', '0')
            self.assertEqual(code, 0); self.assertEqual(result['messages'][0]['id'], str(expected))
            self.assertFalse(result['history_complete'])
        self.cli('--db', str(db), 'mark', 'read', 'example', '1')
        self.cli('--db', str(db), 'mark', 'needs-reply', 'example', '1')
        self.cli('--db', str(db), 'mark', 'replied', 'example', '1', '--ref', 'https://example.invalid/reply')
        before = Store(db).show('example', '1')
        self.assertEqual(self.cli(*base, 'collect')[1]['added'], 0)
        self.assertEqual(Store(db).show('example', '1'), before)
        # Local commands do not need the adapter code or its configuration.
        adapter.unlink(); config.unlink()
        checkpoint = self.root/'after.txt'
        ledger = self.root/'delivery.jsonl'
        loop = [sys.executable, str(examples/'agent_loop.py'), '--db', str(db), '--checkpoint', str(checkpoint),
                '--ledger', str(ledger), '--once']
        result = subprocess.run(loop, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([json.loads(line)['id'] for line in result.stdout.splitlines()], ['1', '2'])
        self.assertEqual(checkpoint.read_text().strip(), '2')
        entries = [json.loads(line) for line in ledger.read_text().splitlines()]
        self.assertEqual([(entry['id'], entry['attempt'], entry['outcome']) for entry in entries],
                         [('1', 1, 'unrecorded'), ('2', 1, 'unrecorded')])
        self.assertFalse(any('body' in entry or 'title' in entry for entry in entries))
        self.assertEqual(subprocess.run(loop, capture_output=True, text=True, timeout=10).stdout, '')
        self.assertEqual(self.cli('--db', str(db), 'show', 'example', '1')[1]['message'], reader.written(before))
        self.assertEqual(self.cli('--db', str(db), 'mark', 'unread', 'example', '1')[0], 0)

    def test_v1_read_then_additive_migration_preserves_arrivals_and_marks(self):
        legacy = self.root/'legacy.sqlite3'
        with closing(sqlite3.connect(legacy)) as db:
            db.executescript((Path(__file__).parent/'fixtures/v1.sql').read_text())
        store = Store(legacy)
        before = store.show('moltbook', uid(10)); raw = legacy.read_bytes()
        self.assertEqual(store.wait(1, 0)['messages'][0]['arrival_seq'], 2)
        self.assertEqual(legacy.read_bytes(), raw)
        cfg = settings(self.root)['moltbook']
        board = FixtureBoard('moltbook', cfg)
        result = collect_all(store, {'moltbook': cfg}, fetch=board)
        self.assertFalse(result['failed']); self.assertEqual(result['added'], 1)
        self.assertEqual(store.show('moltbook', uid(10)), before)
        self.assertEqual(store.wait(2, 0)['messages'][0]['arrival_seq'], 3)
        collect_all(Store(legacy), {'moltbook': cfg}, fetch=board)
        self.assertEqual(Store(legacy).show('moltbook', uid(10)), before)
        with closing(sqlite3.connect(legacy)) as db:
            self.assertEqual(db.execute('SELECT seq FROM sqlite_sequence WHERE name="messages"').fetchone()[0], 3)

    def test_a_board_that_left_the_package_is_a_name_like_any_other_and_its_mail_stays(self):
        gone = ('fourclaw', 'fruitflies')
        # 0.16.1 was the last release with these two boards. The file is an inbox as it left one: each board has a
        # source there, with the name of the board as its adapter, and one message.
        left = str(self.root/'left.sqlite3')
        with closing(sqlite3.connect(left)) as db:
            db.executescript((Path(__file__).parent/'fixtures/left-0.16.1.sql').read_text())
        # No config names them, and the local commands read what the inbox holds. A source that no pass reads any
        # more is stale, until it is paused.
        code, status = self.cli('--db', left, 'status')
        self.assertEqual((code, status['fresh'], [(row['source'], row['status']) for row in status['sources']]),
                         (0, False, [(source, 'stale') for source in gone]))
        code, page = self.cli('--db', left, 'list', '--scope', 'all')
        self.assertEqual((code, [message['source'] for message in page['messages']]), (0, list(gone)))
        # The parent that the inbox holds for a message of 4claw is read as the parent of any other message.
        reply = page['messages'][0]
        self.assertEqual((reply['parent_id'], reply['brief']['parent']),
                         (reply['thread_id'], {'id': reply['thread_id'], 'status': 'same_as_root'}))
        code, shown = self.cli('--db', left, 'show', gone[0], reply['id'])
        self.assertEqual((code, shown['message']['body']), (0, 'An invented reply in the thread.'))
        for source in gone:
            self.assertEqual(self.cli('--db', left, 'pause', source)[1]['event'], 'paused')
        code, status = self.cli('--db', left, 'status')
        self.assertEqual((status['fresh'], [row['status'] for row in status['sources']]), (True, ['paused', 'paused']))
        # In a config either name is no board. Without an adapter file it is no source, and with one in the folder
        # for adapter files it is the source of that file.
        examples = Path(__file__).resolve().parents[1]/'examples'
        shutil.copyfile(examples/'custom_feed.json', self.root/'custom_feed.json')
        folder = self.root/'adapters'; folder.mkdir()
        config = self.root/'config.json'
        for source in gone:
            with self.subTest(source=source):
                about = {'account_id': 'agent', 'feed_file': 'custom_feed.json', 'batch_size': 1}
                config.write_text(json.dumps({'database': f'{source}.sqlite3', 'sources': {source: about}}))
                code, result = self.cli('--config', str(config), 'init')
                self.assertEqual((code, result['error']), (2, 'invalid_config'))
                shutil.copyfile(examples/'custom_board.py', folder/f'{source}.py')
                self.assertEqual(self.cli('--config', str(config), 'init')[0], 0)
                code, result = self.cli('--config', str(config), 'collect')
                self.assertEqual((code, result['added'], [row['status'] for row in result['sources']]), (0, 1, ['ok']))
                self.assertEqual(Store(self.root/f'{source}.sqlite3').adapter(source), str(folder/f'{source}.py'))
                # The name as the adapter of a source is the path of a file, as any name that is no board is.
                config.write_text(json.dumps({'database': f'{source}.sqlite3', 'sources': {
                    source: about, 'other': {'account_id': 'agent', 'adapter': source}}}))
                code, result = self.cli('--config', str(config), 'collect')
                self.assertEqual((code, [(error['source'], error['error']) for error in result['errors']]),
                                 (1, [('other', 'adapter_load_failed')]))

    def test_a_later_source_of_a_version_1_file_is_read_by_the_adapter_of_its_config(self):
        legacy = self.root/'later.sqlite3'
        with closing(sqlite3.connect(legacy)) as db:
            db.executescript((Path(__file__).parent/'fixtures/v1.sql').read_text())
        store = Store(legacy)
        # Version 1 had no board of this name. A later release put the source into the file, here with a pause,
        # which names no adapter there. So only the config says which adapter reads it.
        sources = {'later-board': described('an-invented-account')}
        for command in ('pause', 'resume'):
            commands.execute(store, command, source='later-board', sources=sources)
        result = arrive(store, 'later-board', 'an-invented-account', [mail(10)])
        self.assertEqual((result['added'], result['errors']), (1, []))
        self.assertEqual((store.adapter('later-board'), store.adapter('moltbook')), (str(DESCRIBED), 'moltbook'))

    def test_stale_collector_keeps_mail_but_cannot_rewind_progress(self):
        def another_pass():
            # It starts after the pass that is under way and ends before it.
            other = arrive(self.store, 'moltbook', uid(2), [mail(10)], state={'cursor': 'new'})
            self.assertEqual((other['added'], other['failed']), (1, False))
            mark(self.store, 'moltbook', uid(10), 'needs_reply')
        late = arrive(self.store, 'moltbook', uid(2), [mail(11)], state={'cursor': 'old'}, meanwhile=another_pass)
        self.assertEqual((late['added'], [error['error'] for error in late['errors']]), (1, ['collection_conflict']))
        known, state, revision = self.store.collection_state('moltbook', uid(2), str(DESCRIBED))
        self.assertEqual(state, {'cursor': 'new'}); self.assertEqual(revision, 1)
        self.assertEqual(known, {uid(10), uid(11)})
        self.assertTrue(self.store.show('moltbook', uid(10))['needs_reply'])
        with self.assertRaises(MailError): self.store.collection_state('moltbook', uid(999), str(DESCRIBED))
        with self.assertRaises(MailError): self.store.collection_state('moltbook', uid(2), 'different-adapter')

    def test_failing_original_cannot_starve_later_or_late_public_originals(self):
        for source in ('the-colony', 'moltbook'):
            with self.subTest(source=source):
                cfg = settings(self.root)[source]
                store = new_inbox(self.root/(source+'.sqlite3'))
                colony = source == 'the-colony'
                events = [{'id': uid(n+1000), 'notification_type' if colony else 'type': 'comment_on_post' if colony else 'post_comment',
                           'post_id' if colony else 'relatedPostId': uid(n),
                           'comment_id' if colony else 'relatedCommentId': uid(n+10)} for n in range(601,605)]
                clock, late, retained = Clock(1_000_000), [False], [True]
                board = FixtureBoard(source, cfg)
                base_get = board.get
                def get(path, params=None, **kw):
                    if path == '/agents/me': return base_get(path, params, **kw)
                    # An answer takes a sixth of the time that a pass has for a source. One that the client has
                    # no time left for is late, as the transport says it.
                    left = board.asked[-1].left
                    clock.advance(min(left, 7.5))
                    if left < 7.5: raise MailError('source_timeout')
                    if path == '/notifications':
                        rows = events if retained[0] else []
                        if colony: return rows[(params or {}).get('offset',0):]
                        return {'notifications': rows, 'has_more': False}
                    number = int(path.split('/')[2].replace('-',''),16)
                    root = number-10 if path.startswith('/comments/') else number
                    if root == 601: raise HTTPError('https://example.invalid',503,'private body',{},io.BytesIO())
                    if root == 604 and not late[0]: raise HTTPError('https://example.invalid',404,'missing',{},io.BytesIO())
                    if colony: return original(root+10,root,colony=True)
                    if path.endswith('/comments'):
                        return {'comments':[original(root+10,root)], 'has_more':False}
                    return {'post':{**original(root,root), 'title':'Example'}}
                board.get = get
                with fixed(clock):
                    for _ in range(6): collect_all(store,{source:cfg},fetch=board)
                    self.assertTrue({uid(612),uid(613)} <= store.known(source,cfg['account_id']))
                    self.assertNotIn(uid(614), store.known(source,cfg['account_id']))
                    retained[0] = False; late[0] = True
                    for _ in range(6): collect_all(Store(store.path),{source:cfg},fetch=board)
                    self.assertIn(uid(614),store.known(source,cfg['account_id']))
                    self.assertNotIn(uid(611),store.known(source,cfg['account_id']))

    def test_large_known_root_finds_new_head_while_backfill_resumes(self):
        cfg = settings(self.root)['postingboard']; cfg['threads'] = [uid(301)]
        board = FixtureBoard('postingboard',cfg)
        board.comments[uid(301)] = [named(n,301) for n in range(400,10400)]
        # The client waits 1.1 seconds between two requests, and a root has 45 seconds. Here a wait moves the clock.
        self.enterContext(fixed(Clock(1_000_000)))
        position = lambda: self.store.collection_state('postingboard',cfg['account_id'],'postingboard')[1]['threads'][uid(301)]
        # Pass after pass delivers the whole root. The pass after them finds nothing new: it starts the sweep
        # again and stops deep in mail that is delivered.
        while collect_all(self.store,{'postingboard':cfg},fetch=board)['added']: pass
        self.assertEqual(len(self.store.known('postingboard',cfg['account_id'])),10000)
        self.assertEqual(position(),9200)
        board.comments[uid(301)].append(named(10401,301))
        board.calls.clear()
        result=collect_all(self.store,{'postingboard':cfg},fetch=board)
        self.assertEqual(result['added'],1)
        self.assertEqual(self.store.show('postingboard',uid(10401))['body'],'A synthetic named-board reply.')
        self.assertEqual(board.calls[0],('/v1/me', {}, True))
        self.assertEqual(board.calls[1][1],{'limit':30})
        # The sweep goes on where it was, a page of 30 after the other, until the time of the root is over.
        self.assertEqual([call[1] for call in board.calls[2:]],[{'limit':30,'before':before} for before in range(9200,8030,-30)])
        self.assertEqual(position(),8030)
        self.assertTrue(result['sources'][0]['backlog_pending'])

    def test_moltbook_comment_cursor_progress_and_expired_cursor_recovery(self):
        cfg=settings(self.root)['moltbook']; board=FixtureBoard('moltbook',cfg)
        board.events=[{'id':uid(999), 'type':'mention', 'relatedPostId':uid(201), 'relatedCommentId':uid(229)}]
        board.comments=[original(n,201) for n in range(220,230)]
        board.per_page=1  # A page holds one comment, however many the client asks for.
        for _ in range(3): collect_all(self.store,{'moltbook':cfg},fetch=board)
        get=board.get
        def expired(path,params=None,**kw):
            if path.endswith('/comments') and (params or {}).get('cursor'):
                raise HTTPError('https://example.invalid',400,'expired',{},io.BytesIO())
            return get(path,params,**kw)
        board.get=expired
        self.assertTrue(collect_all(self.store,{'moltbook':cfg},fetch=board)['failed'])
        board.get=get
        for _ in range(10): collect_all(Store(self.db),{'moltbook':cfg},fetch=board)
        self.assertEqual(self.store.show('moltbook',uid(229))['body'],'A synthetic public reply.')
        self.assertEqual(self.store.status()['counts']['total'],1)

    def test_rate_limited_hydration_keeps_its_backfill_position(self):
        cfg=settings(self.root)['postingboard']; cfg['threads']=[uid(301)]
        board=FixtureBoard('postingboard',cfg)
        board.comments[uid(301)]=[named(n,301) for n in range(400,460)]
        board.summaries={uid(405)}
        get=board.get
        def limited(path,params=None,**kw):
            if path.endswith(uid(405)):
                raise HTTPError('https://example.invalid',429,'quota',{},io.BytesIO())
            return get(path,params,**kw)
        board.get=limited
        self.enterContext(fixed(Clock(1_000_000)))  # The wait of the client between two requests moves the clock.
        for _ in range(2):
            result=collect_all(self.store,{'postingboard':cfg},fetch=board)
            self.assertEqual(result['sources'][0]['error'],'http_429')
            self.assertTrue(result['sources'][0]['backlog_pending'])
            # The sweep stays before the post that it could not read.
            self.assertEqual(self.store.collection_state('postingboard',cfg['account_id'],'postingboard')[1]['threads'][uid(301)],406)
        board.get=get
        collect_all(self.store,{'postingboard':cfg},fetch=board)
        self.assertIn(uid(405),self.store.known('postingboard',cfg['account_id']))

    def test_invalid_extension_batch_is_rejected_without_state_or_mail(self):
        adapter=self.root/'bad.py'
        adapter.write_text('from boardmail.adapters import Batch\nAPI_VERSION=1\ndef collect(settings,state,known):\n    return Batch(messages=[{"id":42}],state={"skip":True})\n')
        cfg={'account_id':'demo-agent','adapter':adapter}
        result=collect_all(self.store,{'custom':cfg})
        self.assertEqual(result['errors'][0]['error'],'invalid_adapter_result')
        self.assertEqual(result['errors'][0]['next_action'],'check_trusted_adapter_code')
        self.assertEqual(self.store.page()['messages'],[])
        self.assertEqual(self.store.collection_state('custom','demo-agent',str(adapter))[1],{})

    def adapter_file(self, name, body='return settings["gives"]()', version=1):
        """An adapter file in the folder of the test. Its collect() is this Python text: without one it gives what
        the function under 'gives' in the settings of its source returns. It says this version of the interface,
        or none."""
        path = self.root/name
        path.write_text('from boardmail.adapters import Batch\n' + ('' if version is None else f'API_VERSION = {version!r}\n')
                        + 'def collect(settings, state, known):\n' + ''.join('    ' + line + '\n' for line in body.splitlines()))
        return path

    def extension_checkpoint(self):
        cfg = {'account_id': 'demo-agent', 'adapter': self.adapter_file('custom.py')}
        first = Batch(messages=[mail(10)], state={'cursor': 'saved'}, originals=[mail(20)])
        self.assertFalse(collect_all(self.store, {'custom': {**cfg, 'gives': lambda: first}})['failed'])
        mark(self.store, 'custom', uid(10), 'read')
        mark(self.store, 'custom', uid(10), 'needs_reply')
        with self.store.connect() as db:
            originals = [tuple(row) for row in db.execute('SELECT * FROM originals WHERE source=?', ('custom',))]
        return cfg, (self.store.collection_state('custom', cfg['account_id'], str(cfg['adapter'])),
                     self.store.show('custom', uid(10)), originals)

    def assert_extension_checkpoint(self, cfg, before):
        self.assertEqual(self.store.collection_state('custom', cfg['account_id'], str(cfg['adapter'])), before[0])
        self.assertEqual(self.store.show('custom', uid(10)), before[1])
        with self.store.connect() as db:
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM originals WHERE source=?', ('custom',))], before[2])

    def test_state_serializer_failure_returns_json_preserves_progress_and_continues(self):
        cfg, before = self.extension_checkpoint()
        # The state that the pass gives is nested deeper than the serializer of JSON goes.
        self.adapter_file('custom.py', 'state = {"cursor": "next"}\nfor _ in range(100_000):\n    state = {"deeper": state}\n'
                          f'return Batch(messages=[{mail(11)!r}], state=state, originals={[{**mail(20), "body": "replacement"}, mail(21)]!r})')
        later = {**cfg, 'adapter': self.adapter_file(
            'later.py', f'return Batch(messages=[{mail(30)!r}], state={{"cursor": "independent"}})')}
        config = self.root/'config.json'
        config.write_text(json.dumps({'database': str(self.db), 'sources': {
            'custom': {**cfg, 'adapter': str(cfg['adapter'])},
            'later': {**later, 'adapter': str(later['adapter'])}}}))
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.main(['--config', str(config), 'collect'])
        result = json.loads(output.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(result['errors'], [{'source': 'custom', 'error': 'invalid_adapter_result',
                                            'next_action': 'check_trusted_adapter_code'}])
        self.assertEqual(result['added'], 1)
        self.assertTrue(result['failed'])
        self.assertFalse(result['history_complete'])
        self.assertNotIn('recursion', output.getvalue().lower())
        self.assertEqual({s['source']: s['error'] for s in result['sources']},
                         {'custom': 'invalid_adapter_result', 'later': None})
        self.assert_extension_checkpoint(cfg, before)
        self.assertEqual(self.store.collection_state('later', later['account_id'], str(later['adapter'])),
                         ({uid(30)}, {'cursor': 'independent'}, 1))

    def test_extension_versions_are_rejected_before_collection(self):
        cfg, before = self.extension_checkpoint()
        calls = []
        for version in (None, 2, True, '1'):
            with self.subTest(version=version):
                self.adapter_file('custom.py', version=version)
                result = collect_all(self.store, {'custom': {**cfg, 'gives': lambda: calls.append('collected')}})
                self.assertEqual(result['errors'][0]['error'], 'adapter_version_unsupported')
                self.assertEqual(result['errors'][0]['next_action'], 'check_trusted_adapter_code')
                self.assertEqual(result['added'], 0)
                self.assert_extension_checkpoint(cfg, before)
        self.assertEqual(calls, [])

    def test_invalid_extension_contract_matrix_preserves_committed_data(self):
        cfg, before = self.extension_checkpoint()
        # Each ordinary batch has new mail and cache changes that must not commit.
        cases = {
            'not_batch': lambda b: {'messages': b.messages, 'state': b.state},
            'messages_not_list': lambda b: Batch(messages=tuple(b.messages), state=b.state),
            'state_not_object': lambda b: Batch(messages=b.messages, state=[]),
            'complete_not_bool': lambda b: Batch(messages=b.messages, complete=1),
            'unavailable_not_count': lambda b: Batch(messages=b.messages, unavailable=True),
            'error_prose': lambda b: Batch(messages=b.messages, error='provider failed'),
            'state_not_json': lambda b: Batch(messages=b.messages, state={'cursor': object()}),
            'state_nonfinite': lambda b: Batch(messages=b.messages, state={'cursor': float('nan')}),
            'numeric_id': lambda b: Batch(messages=[{**b.messages[0], 'id': 11}], state=b.state),
            'missing_title': lambda b: Batch(messages=[{k: v for k, v in b.messages[0].items() if k != 'title'}], state=b.state),
            'non_utf8_body': lambda b: Batch(messages=[{**b.messages[0], 'body': '\ud800'}], state=b.state),
            'bool_timestamp': lambda b: Batch(messages=[{**b.messages[0], 'created_at': True}], state=b.state),
            'sequence_out_of_range': lambda b: Batch(messages=[{**b.messages[0], 'provider_seq': 2**63}], state=b.state),
            'non_http_url': lambda b: Batch(messages=[{**b.messages[0], 'url': 'ftp://example.invalid/item'}], state=b.state),
            'url_credentials': lambda b: Batch(messages=[{**b.messages[0], 'url': 'https://user:pass@example.invalid/item'}], state=b.state),
            'unknown_kind': lambda b: Batch(messages=[{**b.messages[0], 'kind': 'notification'}], state=b.state),
            'unknown_addressing': lambda b: Batch(messages=[{**b.messages[0], 'addressing': 'inferred'}], state=b.state),
            'invalid_cached_original': lambda b: Batch(messages=b.messages, state=b.state, originals=[{**mail(20), 'created_at': None}]),
            'cached_original_url_credentials': lambda b: Batch(messages=b.messages, state=b.state, originals=[{**mail(20), 'url': 'https://user:pass@example.invalid/item'}]),
        }
        for name, malformed in cases.items():
            with self.subTest(contract=name):
                batch = malformed(Batch(messages=[mail(11)], state={'cursor': 'next'}))
                # Preserve invalid originals/container fields; otherwise exercise cache atomicity too.
                if isinstance(batch, Batch) and 'cached_original' not in name:
                    batch.originals = [{**mail(20), 'body': 'replacement'}, mail(21)]
                result = collect_all(self.store, {'custom': {**cfg, 'gives': lambda: batch}})
                self.assertEqual(result['errors'][0]['error'], 'invalid_adapter_result')
                self.assertEqual(result['errors'][0]['next_action'], 'check_trusted_adapter_code')
                self.assertEqual(result['added'], 0)
                self.assert_extension_checkpoint(cfg, before)

    def test_valid_extension_optional_fields_bounds_and_replay(self):
        cfg, _ = self.extension_checkpoint()
        item = {**mail(11), 'author': None, 'parent_id': None, 'provider_seq': None, 'created_at': -(2**63)}
        cached = {**mail(21), 'created_at': 2**63-1}
        del cached['kind']
        batch = Batch(messages=[mail(10), item], state={'cursor': 'next'}, originals=[cached])
        first = collect_all(self.store, {'custom': {**cfg, 'gives': lambda: batch}})
        replay = collect_all(self.store, {'custom': {**cfg, 'gives': lambda: batch}})
        self.assertFalse(first['failed'])
        self.assertEqual(first['added'], 1)
        self.assertFalse(replay['failed'])
        self.assertEqual(replay['added'], 0)
        self.assertEqual(self.store.collection_state('custom', cfg['account_id'], str(cfg['adapter']))[1], {'cursor': 'next'})
        self.assertIsNotNone(self.store.show('custom', uid(10))['read_at'])
        self.assertTrue(self.store.show('custom', uid(10))['needs_reply'])
        with self.store.connect() as db:
            self.assertIsNotNone(db.execute('SELECT 1 FROM originals WHERE source=? AND id=?', ('custom', uid(21))).fetchone())

    def test_bad_original_does_not_block_a_sibling_and_remains_retryable(self):
        self.enterContext(fixed(Clock(1_000_000)))  # The wait of the client of Postingboard moves the clock.
        for source, good, bad in (('postingboard',311,312),('moltbook',211,212)):
            with self.subTest(source=source):
                cfg=settings(self.root)[source]; board=FixtureBoard(source,cfg)
                get=board.get
                if source=='postingboard':
                    def fail(path,params=None,**kw):
                        if path.endswith(uid(312)):
                            raise HTTPError('https://example.invalid',503,'failed hydration',{},io.BytesIO())
                        return get(path,params,**kw)
                    board.get=fail
                else:
                    board.comments.append({**original(212,201),'created_at':None})
                for _ in range(3):
                    result=collect_all(self.store,{source:cfg},fetch=board)
                    self.assertTrue(result['failed'])
                self.assertIn(uid(good),self.store.known(source,cfg['account_id']))
                self.assertNotIn(uid(bad),self.store.known(source,cfg['account_id']))
                board.get=get
                if source=='moltbook':
                    board.comments[-1]=original(212,201)
                    board.events=[]  # The retained reference still gets another chance.
                collect_all(Store(self.db),{source:cfg},fetch=board)
                self.assertIn(uid(bad),self.store.known(source,cfg['account_id']))

    def test_source_coverage_uses_adapter_identity(self):
        cfg=settings(self.root)['moltbook']
        collect_all(self.store,{'alias':{**cfg,'adapter':'moltbook'}},fetch=FixtureBoard('moltbook',cfg))
        arrive(self.store,'moltbook','handle')
        sources={s['source']:s for s in self.store.status()['sources']}
        self.assertIn('anonymous public originals',sources['alias']['coverage'])
        self.assertIn('Configured adapter',sources['moltbook']['coverage'])

    def test_adapter_system_exit_still_returns_json_and_source_health(self):
        adapter=self.root/'exits.py'
        config=self.root/'config.json'
        config.write_text(json.dumps({'database':str(self.db),'sources':{
            'custom':{'account_id':'demo-agent','adapter':str(adapter)}}}))
        for source,expected in [('import sys\nsys.exit(2)\n','adapter_load_failed'),
                                ('import sys\nAPI_VERSION=1\ndef collect(settings,state,known):\n    sys.exit(7)\n','adapter_failed')]:
            adapter.write_text(source)
            code,result=self.cli('--config',str(config),'collect')
            self.assertEqual(code,1)
            self.assertEqual(result['errors'][0]['error'],expected)
            self.assertEqual(result['sources'][0]['error'],expected)
            self.assertEqual(result['errors'][0]['next_action'],'check_trusted_adapter_code')


if __name__ == '__main__': unittest.main()
