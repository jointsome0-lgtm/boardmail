"""Public extension, durable progress and 0.1 database compatibility contracts."""
from contextlib import closing
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch
import io
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from boardmail import providers
from boardmail.adapters import Batch, collect_all
from boardmail.config import MailError
from boardmail.store import Store
from examples.fixtures import FixtureClient, named, original, settings, uid
from test_mail import mail


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root/'mail.sqlite3'
        self.store = Store(self.db); self.store.initialize()

    def cli(self, *args):
        result = subprocess.run([sys.executable, '-m', 'boardmail', *args], capture_output=True, text=True, timeout=10)
        return result.returncode, json.loads(result.stdout)

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
        loop = [sys.executable, str(examples/'agent_loop.py'), '--db', str(db), '--checkpoint', str(checkpoint), '--once']
        result = subprocess.run(loop, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([json.loads(line)['id'] for line in result.stdout.splitlines()], ['1', '2'])
        self.assertEqual(checkpoint.read_text().strip(), '2')
        self.assertEqual(subprocess.run(loop, capture_output=True, text=True, timeout=10).stdout, '')
        self.assertEqual(self.cli('--db', str(db), 'show', 'example', '1')[1]['message'], before)
        self.assertEqual(self.cli('--db', str(db), 'mark', 'unread', 'example', '1')[0], 0)

    def test_v1_read_then_additive_migration_preserves_arrivals_and_marks(self):
        legacy = self.root/'legacy.sqlite3'
        with closing(sqlite3.connect(legacy)) as db:
            db.executescript((Path(__file__).parent/'fixtures/v1.sql').read_text())
        store = Store(legacy)
        before = store.show('moltbook', uid(10)); raw = legacy.read_bytes()
        self.assertEqual(store.wait(1, 0)['messages'][0]['arrival_seq'], 2)
        self.assertEqual(legacy.read_bytes(), raw)
        cfg = settings()['moltbook']
        result = collect_all(store, {'moltbook': cfg}, client_factory=FixtureClient)
        self.assertFalse(result['failed']); self.assertEqual(result['added'], 1)
        self.assertEqual(store.show('moltbook', uid(10)), before)
        self.assertEqual(store.wait(2, 0)['messages'][0]['arrival_seq'], 3)
        collect_all(Store(legacy), {'moltbook': cfg}, client_factory=FixtureClient)
        self.assertEqual(Store(legacy).show('moltbook', uid(10)), before)
        with closing(sqlite3.connect(legacy)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT seq FROM sqlite_sequence WHERE name="messages"').fetchone()[0], 3)

    def test_stale_collector_keeps_mail_but_cannot_rewind_progress(self):
        self.store.prepare_collection()
        first = self.store.collection_state('moltbook', uid(2), 'moltbook')
        second = self.store.collection_state('moltbook', uid(2), 'moltbook')
        a = Batch(messages=[mail(10)], state={'cursor': 'new'})
        b = Batch(messages=[mail(11)], state={'cursor': 'old'})
        self.assertEqual(self.store.save_collection('moltbook', uid(2), 'moltbook', first[2], a), (1, False))
        self.store.mark('moltbook', uid(10), 'needs_reply')
        self.assertEqual(self.store.save_collection('moltbook', uid(2), 'moltbook', second[2], b), (1, True))
        known, state, revision = self.store.collection_state('moltbook', uid(2), 'moltbook')
        self.assertEqual(state, {'cursor': 'new'}); self.assertEqual(revision, 1)
        self.assertEqual(known, {uid(10), uid(11)})
        self.assertTrue(self.store.show('moltbook', uid(10))['needs_reply'])
        with self.assertRaises(MailError): self.store.collection_state('moltbook', uid(999), 'moltbook')
        with self.assertRaises(MailError): self.store.collection_state('moltbook', uid(2), 'different-adapter')

    def test_failing_original_cannot_starve_later_or_late_public_originals(self):
        for source in ('the-colony', 'moltbook'):
            with self.subTest(source=source):
                cfg = settings()[source]
                store = Store(self.root/(source+'.sqlite3')); store.initialize()
                colony = source == 'the-colony'
                events = [{'id': uid(n+1000), 'notification_type' if colony else 'type': 'comment_on_post' if colony else 'post_comment',
                           'post_id' if colony else 'relatedPostId': uid(n),
                           'comment_id' if colony else 'relatedCommentId': uid(n+10)} for n in range(601,605)]
                clock, late, retained = [0.0], [False], [True]
                def factory(*args):
                    client = FixtureClient(*args)
                    def get(path, params=None, **kw):
                        if clock[0]+1 > client.deadline: raise MailError('source_timeout')
                        clock[0] += 1
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
                    client.get = get
                    return client
                with patch.object(providers,'SOURCE_SECONDS',6), patch.object(providers.time,'monotonic',side_effect=lambda:clock[0]):
                    for _ in range(6): collect_all(store,{source:cfg},client_factory=factory)
                    self.assertTrue({uid(612),uid(613)} <= store.known(source,cfg['account_id']))
                    self.assertNotIn(uid(614), store.known(source,cfg['account_id']))
                    retained[0] = False; late[0] = True
                    for _ in range(6): collect_all(Store(store.path),{source:cfg},client_factory=factory)
                    self.assertIn(uid(614),store.known(source,cfg['account_id']))
                    self.assertNotIn(uid(611),store.known(source,cfg['account_id']))

    def test_large_known_root_finds_new_head_while_backfill_resumes(self):
        cfg = settings()['postingboard']; cfg['threads'] = [uid(301)]
        client = FixtureClient('postingboard',cfg)
        client.comments[uid(301)] = [named(n,301) for n in range(400,10400)]
        # Seed a deep unfinished sweep and previously delivered mail.
        self.store.save_collection('postingboard',cfg['account_id'],'postingboard',0,
                                   Batch(state={'threads':{uid(301):5000}}))
        for item in client.comments[uid(301)]:
            item.update(thread_id=uid(301),kind='reply_to_post',url='https://example.invalid/'+item['id'])
        self.store.save('postingboard',cfg['account_id'],client.comments[uid(301)])
        client.comments[uid(301)].append(named(10401,301))
        with patch.object(providers,'MAX_PAGES',1):
            result=collect_all(self.store,{'postingboard':cfg},client_factory=lambda *_:client)
        self.assertEqual(result['added'],1)
        self.assertEqual(self.store.show('postingboard',uid(10401))['body'],'A synthetic named-board reply.')
        self.assertEqual(client.calls[0][1],{'limit':30})
        self.assertEqual(client.calls[1][1],{'limit':30,'before':5000})
        self.assertEqual(len(client.calls),2)
        self.assertTrue(result['sources'][0]['backlog_pending'])

    def test_moltbook_comment_cursor_progress_and_expired_cursor_recovery(self):
        cfg=settings()['moltbook']; client=FixtureClient('moltbook',cfg)
        client.events=[{'id':uid(999), 'type':'mention', 'relatedPostId':uid(201), 'relatedCommentId':uid(229)}]
        client.comments=[original(n,201) for n in range(220,230)]
        with patch.object(providers,'PAGE_SIZE',1):
            for _ in range(3): collect_all(self.store,{'moltbook':cfg},client_factory=lambda *_:client)
            get=client.get
            def expired(path,params=None,**kw):
                if path.endswith('/comments') and (params or {}).get('cursor'):
                    raise HTTPError('https://example.invalid',400,'expired',{},io.BytesIO())
                return get(path,params,**kw)
            client.get=expired
            self.assertTrue(collect_all(self.store,{'moltbook':cfg},client_factory=lambda *_:client)['failed'])
            client.get=get
            for _ in range(10): collect_all(Store(self.db),{'moltbook':cfg},client_factory=lambda *_:client)
        self.assertEqual(self.store.show('moltbook',uid(229))['body'],'A synthetic public reply.')
        self.assertEqual(self.store.status()['counts']['total'],1)

    def test_rate_limited_hydration_keeps_its_backfill_position(self):
        cfg=settings()['postingboard']; cfg['threads']=[uid(301)]
        client=FixtureClient('postingboard',cfg)
        client.comments[uid(301)]=[named(n,301) for n in range(400,460)]
        client.summaries={uid(405)}
        self.store.save_collection('postingboard',cfg['account_id'],'postingboard',0,
                                   Batch(state={'threads':{uid(301):406}}))
        get=client.get
        def limited(path,params=None,**kw):
            if path.endswith(uid(405)):
                raise HTTPError('https://example.invalid',429,'quota',{},io.BytesIO())
            return get(path,params,**kw)
        client.get=limited
        for _ in range(2):
            result=collect_all(self.store,{'postingboard':cfg},client_factory=lambda *_:client)
            self.assertEqual(result['sources'][0]['error'],'http_429')
            self.assertTrue(result['sources'][0]['backlog_pending'])
            self.assertEqual(self.store.collection_state('postingboard',cfg['account_id'],'postingboard')[1]['threads'][uid(301)],406)
        client.get=get
        collect_all(self.store,{'postingboard':cfg},client_factory=lambda *_:client)
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

    def test_bad_original_does_not_block_a_sibling_and_remains_retryable(self):
        for source, good, bad in (('postingboard',311,312),('moltbook',211,212)):
            with self.subTest(source=source):
                cfg=settings()[source]; client=FixtureClient(source,cfg)
                get=client.get
                if source=='postingboard':
                    def fail(path,params=None,**kw):
                        if path.endswith(uid(312)):
                            raise HTTPError('https://example.invalid',503,'failed hydration',{},io.BytesIO())
                        return get(path,params,**kw)
                    client.get=fail
                else:
                    client.comments.append({**original(212,201),'created_at':None})
                for _ in range(3):
                    result=collect_all(self.store,{source:cfg},client_factory=lambda *_:client)
                    self.assertTrue(result['failed'])
                self.assertIn(uid(good),self.store.known(source,cfg['account_id']))
                self.assertNotIn(uid(bad),self.store.known(source,cfg['account_id']))
                client.get=get
                if source=='moltbook':
                    client.comments[-1]=original(212,201)
                    client.events=[]  # The retained reference still gets another chance.
                collect_all(Store(self.db),{source:cfg},client_factory=lambda *_:client)
                self.assertIn(uid(bad),self.store.known(source,cfg['account_id']))

    def test_source_coverage_uses_adapter_identity(self):
        self.store.save_collection('alias',uid(2),'moltbook',0,Batch())
        self.store.save_collection('moltbook','handle','/example/custom.py',0,Batch())
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
