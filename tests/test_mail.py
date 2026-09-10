"""Mail, pagination, replay and wait contracts. All provider data is invented."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from http.client import IncompleteRead
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from uuid import UUID
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from boardmail import providers
from boardmail.config import MailError
from boardmail.store import Store
from examples.fixtures import FixtureClient, named, original, settings, uid


def mail(n, *, created=100):
    return {"id":uid(n),"thread_id":uid(100),"kind":"mention","author":"example-agent",
            "title":"Example","body":"Synthetic text","url":"https://board.example.invalid/posts/"+uid(n),
            "created_at":created}


class MailTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'inbox.sqlite3'
        self.store = Store(self.path)
        self.store.initialize()

    def save(self,*numbers):
        return self.store.save('moltbook',uid(2),[mail(n) for n in numbers])

    def test_bounded_pages_and_race_between_list_and_wait(self):
        self.save(10,11,12,13,14)
        first = self.store.page(0,2)
        self.assertEqual([m['arrival_seq'] for m in first['messages']],[1,2])
        self.assertEqual(first['next_after'],2);self.assertTrue(first['more'])
        # Insert after list, before wait; even an old creation date must wake.
        self.store.save('moltbook',uid(2),[mail(15,created=1)])
        arrivals,after = [],first['next_after']
        for _ in range(4):
            result = self.store.wait(after,0,2)
            arrivals += [m['arrival_seq'] for m in result['messages']]
            after = result['next_after']
            if result['event']=='timeout':break
        self.assertEqual(arrivals,[3,4,5,6])
        self.assertEqual(result['event'],'timeout');self.assertEqual(after,6)
        self.assertEqual(self.store.status()['counts']['unread'],6)

    def test_concurrent_replay_preserves_identity_and_independent_marks(self):
        with ThreadPoolExecutor(2) as pool:
            self.assertEqual(sum(pool.map(lambda _:self.save(10,11),range(2))),2)
        self.store.mark('moltbook',uid(10),'read')
        self.store.mark('moltbook',uid(10),'needs_reply')
        with self.assertRaises(MailError):self.store.mark('moltbook',uid(10),'replied')
        self.store.mark('moltbook',uid(10),'replied',ref='https://board.example.invalid/reply/1')
        before = self.store.show('moltbook',uid(10))
        self.assertEqual(self.save(10,11),0)
        self.assertEqual(self.store.show('moltbook',uid(10)),before)
        self.store.save('the-colony',uid(1),[mail(10)])
        self.assertIsNone(self.store.show('the-colony',uid(10))['read_at'])
        self.store.mark('moltbook',uid(10),'unread')
        updated = self.store.show('moltbook',uid(10))
        self.assertTrue(updated['needs_reply']);self.assertIsNotNone(updated['replied_at'])
        with self.assertRaises(MailError):self.store.known('moltbook',uid(999))
        with self.assertRaises(MailError):self.store.save('moltbook',uid(999),[mail(12)])

    def test_partial_source_transaction_has_no_visible_arrival(self):
        broken = mail(11);del broken['body']
        with self.assertRaises(KeyError):self.store.save('moltbook',uid(2),[mail(10),broken])
        self.assertEqual(self.store.wait(0,0)['event'],'timeout')
        self.assertEqual(self.store.status()['sources'],[])
        self.save(10);self.assertEqual(self.store.page()['next_after'],1)

    def test_public_shapes_pages_nested_replies_and_source_isolation(self):
        clients = {s:FixtureClient(s,cfg) for s,cfg in settings().items()}
        c = clients['moltbook']
        child = original(212,201);child['parent_id']=uid(211)
        c.comments[0]['replies']=[child]
        c.events += [deepcopy(c.events[0])]
        with patch.object(providers,'PAGE_SIZE',1):
            added = 0
            for _ in range(3):
                result = providers.collect_all(self.store,settings(),client_factory=lambda s,_:clients[s])
                added += result['added']
                self.assertFalse(result['failed'])
        self.assertEqual(added,7)
        self.assertEqual(self.store.show('moltbook',uid(212))['parent_id'],uid(211))
        self.assertEqual(self.store.show('moltbook',uid(211))['body'],'A synthetic public reply.')
        self.assertEqual(self.store.show('the-colony',uid(111))['url'],
                         'https://the-colony.example.invalid/posts/'+uid(101)+'#comment-'+uid(111))
        self.assertEqual(self.store.show('postingboard',uid(312))['provider_seq'],312)
        self.assertEqual(self.store.show('postingboard',uid(314))['kind'],'mention')
        self.assertNotIn(uid(315),self.store.known('postingboard',uid(3)))
        before = next(s['last_ok'] for s in result['sources'] if s['source']=='the-colony')
        clients['the-colony'].fail=True
        c.comments.append(original(213,201))
        extra=deepcopy(c.events[0]);extra.update(id=uid(999),relatedCommentId=uid(213));c.events.append(extra)
        result=providers.collect_all(self.store,settings(),client_factory=lambda s,_:clients[s])
        self.assertEqual(result['added'],1)
        health=next(s for s in result['sources'] if s['source']=='the-colony')
        self.assertEqual(health['last_ok'],before);self.assertEqual(health['error'],'http_503')
        self.assertNotIn('secret-token',json.dumps(result));self.assertNotIn('provider prose',json.dumps(result))

    def test_partial_notification_page_and_late_public_body(self):
        c=FixtureClient('moltbook',settings()['moltbook']);get=c.get
        def fail_later(path,params=None,**kw):
            if path=='/notifications' and params.get('cursor'):raise OSError('sensitive path or token')
            return get(path,params,**kw)
        c.get=fail_later
        with patch.object(providers,'PAGE_SIZE',1):
            result=providers.collect_all(self.store,{'moltbook':settings()['moltbook']},client_factory=lambda *_:c)
            self.assertEqual(result['added'],1)
            result=providers.collect_all(self.store,{'moltbook':settings()['moltbook']},client_factory=lambda *_:c)
        self.assertTrue(result['failed']);self.assertEqual(len(self.store.page()['messages']),1)
        c.get=get
        result=providers.collect_all(self.store,{'moltbook':settings()['moltbook']},client_factory=lambda *_:c)
        self.assertEqual(result['added'],0)
        after=self.store.page()['next_after'];self.assertEqual(result['sources'][0]['unavailable'],1)
        c.comments.append(original(212,201,body='Late public confirmation.'))
        c.comments[-1]['created_at']='2020-01-01T00:00:00Z'
        result=providers.collect_all(self.store,{'moltbook':settings()['moltbook']},client_factory=lambda *_:c)
        self.assertEqual(result['added'],1)
        self.assertEqual(self.store.wait(after,0)['messages'][0]['body'],'Late public confirmation.')

    def test_confirmed_prefix_survives_a_repeated_small_provider_budget(self):
        cfg = settings()['moltbook']
        events = [{'id':uid(n+1000),'type':'post_comment','relatedPostId':uid(n),
                   'relatedCommentId':uid(n+10)} for n in (501,502,503)]
        def factory(*_):
            client = FixtureClient('moltbook',cfg)
            base_get = client.get
            used = 0
            def get(path,params=None,**kw):
                nonlocal used
                if path == '/agents/me': return base_get(path, params, **kw)
                if path=='/notifications':return {'notifications':events,'has_more':False}
                root = int(UUID(path.split('/')[2]))
                if path.endswith('/comments'):
                    return {'comments':[original(root+10,root)],'has_more':False}
                used += 1
                if used>1:
                    raise HTTPError('https://example.invalid',429,'quota',{},io.BytesIO())
                return {'post':{**original(root,root,2),'title':'Example'}}
            client.get = get
            return client
        for expected in (1,2,3):
            result = providers.collect_all(self.store,{'moltbook':cfg},client_factory=factory)
            self.assertEqual(result['added'],1)
            self.assertEqual(self.store.status()['counts']['total'],expected)
            if expected<3:
                self.assertEqual(result['sources'][0]['status'],'error')
                self.assertIsNone(result['sources'][0]['last_ok'])
                self.assertEqual(self.store.wait(expected-1,0)['event'],'messages')
        self.assertFalse(result['failed']);self.assertIsNotNone(result['sources'][0]['last_ok'])

    def test_non_post_mentions_unknown_authors_and_http_stream_errors(self):
        clients = {s:FixtureClient(s,cfg) for s,cfg in settings().items()}
        c = clients['moltbook']
        c.events.insert(0,{'id':uid(900),'type':'mention','relatedPostId':None})
        c.events.insert(0,{'id':uid(901),'type':'comment_reply','relatedPostId':None})
        c.comments[0]['author'] = None
        result = providers.collect_all(self.store,settings(),client_factory=lambda s,_:clients[s])
        self.assertFalse(result['failed'])
        self.assertIsNone(self.store.show('moltbook',uid(211))['author'])
        def broken(*_,**__):raise IncompleteRead(b'sensitive partial payload')
        clients['the-colony'].get = broken
        result = providers.collect_all(self.store,settings(),client_factory=lambda s,_:clients[s])
        self.assertTrue(result['failed'])
        self.assertEqual(next(s['status'] for s in result['sources'] if s['source']=='moltbook'),'ok')
        self.assertNotIn('sensitive',json.dumps(result))

    def test_second_page_failure_keeps_confirmed_messages_from_one_thread(self):
        for source in ('moltbook','postingboard'):
            with self.subTest(source=source):
                cfg = deepcopy(settings()[source])
                cfg['threads'] = [uid(301)]
                client = FixtureClient(source,cfg)
                if source == 'moltbook':
                    client.comments.append(original(212,201))
                    first_count, total = 1,2
                else:
                    client.comments[uid(301)] = [named(n,301) for n in range(400,431)]
                    first_count, total = 30,31
                self.store.save(source,cfg['account_id'],[],now=123)
                get = client.get
                def broken(path,params=None,**kw):
                    if source=='moltbook' and path=='/notifications':
                        return {'notifications':client.events,'has_more':False}
                    if params and (('cursor' in params and path.endswith('/comments')) or 'before' in params):
                        raise HTTPError('https://example.invalid',429,'quota',{},io.BytesIO())
                    return get(path,params,**kw)
                client.get = broken
                with patch.object(providers,'PAGE_SIZE',1):
                    for expected_added in (first_count,0):
                        result = providers.collect_all(self.store,{source:cfg},client_factory=lambda *_:client)
                        self.assertEqual(result['added'],expected_added)
                        health = next(s for s in result['sources'] if s['source']==source)
                        if expected_added==0:
                            self.assertEqual(health['status'],'error');self.assertEqual(health['last_ok'],last_ok)
                        last_ok=health['last_ok']
                        self.assertEqual(len(self.store.known(source,cfg['account_id'])),first_count)
                    client.get = get
                    result = providers.collect_all(self.store,{source:cfg},client_factory=lambda *_:client)
                self.assertFalse(result['failed']);self.assertEqual(result['added'],total-first_count)
                self.assertEqual(len(self.store.known(source,cfg['account_id'])),total)

    def test_postingboard_all_selected_pages_and_summary_hydration(self):
        c=FixtureClient('postingboard',settings()['postingboard'])
        c.comments[uid(301)]=[named(n,301) for n in [311,312,*range(400,433)]]
        result=providers.collect_all(self.store,{'postingboard':settings()['postingboard']},client_factory=lambda *_:c)
        self.assertEqual(result['added'],36)
        self.assertEqual(self.store.show('postingboard',uid(312))['body'],'A synthetic named-board reply.')
        self.assertTrue(any('before' in p for _,p,_ in c.calls))

    def test_postingboard_slow_or_broken_root_does_not_starve_later_roots(self):
        cfg = settings()['postingboard']
        key = Path(self.temp.name)/'example.key';key.write_text('synthetic-key')
        cfg['api_key_file'] = key
        fixture = FixtureClient('postingboard',cfg)
        fixture.comments[uid(301)] = [named(n,301) for n in range(400,1700)]
        for failure in ('source_timeout','http_503'):
            with self.subTest(failure=failure):
                store = Store(Path(self.temp.name)/(failure+'.sqlite3'));store.initialize()
                store.save('postingboard',cfg['account_id'],[],now=123)
                clock, calls = [0.0], []
                def sleep(seconds):clock[0] += seconds
                def respond(request,timeout):
                    url = urlsplit(request.full_url)
                    calls.append((url.path,clock[0]))
                    if failure=='http_503' and url.path.endswith(uid(301)):
                        raise HTTPError(request.full_url,503,'private provider prose',{},io.BytesIO())
                    params = {k:int(v[0]) for k,v in parse_qs(url.query).items()}
                    return io.BytesIO(json.dumps(fixture.get(url.path,params,authenticated=True)).encode())
                def factory(*args):
                    client = providers.Client(*args)
                    client.opener.open = respond
                    return client
                with patch.object(providers.time,'monotonic',side_effect=lambda:clock[0]), \
                     patch.object(providers.time,'sleep',side_effect=sleep):
                    for attempt in range(2):
                        result = providers.collect_all(store,{'postingboard':cfg},client_factory=factory)
                        self.assertEqual(store.show('postingboard',uid(314))['kind'],'mention')
                        count = store.status()['counts']['total']
                        if failure=='source_timeout':
                            self.assertFalse(result['failed'])
                            self.assertGreater(result['added'],0)
                            if attempt:self.assertEqual(count,1301)
                            else:
                                self.assertTrue(1<count<1301)
                                self.assertTrue(result['sources'][0]['backlog_pending'])
                        else:
                            self.assertTrue(result['failed'])
                            self.assertEqual(result['sources'][0]['error'],failure)
                            self.assertEqual(result['sources'][0]['last_ok'],123)
                            self.assertEqual(count,1)
                            if attempt:self.assertEqual(result['added'],0)
                # Actual HTTP requests stay paced across both configured roots.
                for previous,current in zip(calls,calls[1:]):
                    if current[0].endswith(uid(302)):
                        self.assertGreaterEqual(current[1]-previous[1],1.1-1e-8)

    def test_discovery_progress_and_repeated_cursor_recovery(self):
        c=FixtureClient('moltbook',settings()['moltbook'])
        with patch.object(providers,'PAGE_SIZE',1):
            result=providers.collect_all(self.store,{'moltbook':settings()['moltbook']},client_factory=lambda *_:c)
            self.assertFalse(result['failed']);self.assertTrue(result['sources'][0]['backlog_pending'])
            self.assertEqual(result['added'],1)
            get=c.get
            def repeat(path,params=None,**kw):
                result=get(path,{**(params or {}),'cursor':'0'},**kw)
                if path=='/notifications':result.update(has_more=True,next_cursor='same')
                return result
            c.get=repeat
            for _ in range(2):
                result=providers.collect_all(self.store,{'moltbook':settings()['moltbook']},client_factory=lambda *_:c)
            self.assertEqual(result['sources'][0]['error'],'pagination_no_progress')
            c.get=get
            result=providers.collect_all(self.store,{'moltbook':settings()['moltbook']},client_factory=lambda *_:c)
            self.assertFalse(result['failed']);self.assertEqual(self.store.status()['counts']['total'],1)

    def test_wait_timeout_cancellation_and_outage_never_write(self):
        self.save(10);self.store.failure('moltbook',uid(2),'http_503')
        before=self.path.read_bytes();result=self.store.wait(1,0.02)
        self.assertEqual(result['event'],'timeout');self.assertEqual(result['sources'][0]['status'],'error')
        stopped=threading.Event();stopped.set();result=self.store.wait(0,10,cancelled=stopped)
        self.assertEqual(result['event'],'cancelled');self.assertEqual(result['next_after'],0)
        self.assertEqual(result['messages'],[]);self.assertEqual(self.path.read_bytes(),before)

    def test_auth_hosts_public_confirmation_and_redirect_refusal(self):
        for source,cfg in settings().items():
            key=Path(self.temp.name)/(source+'.key');key.write_text('synthetic-key')
            client=providers.Client(source,{**cfg,'api_key_file':key});calls=[]
            def respond(request,timeout):
                calls.append(request)
                return io.BytesIO(b'{"access_token":"synthetic-token"}' if request.data else b'{}')
            with patch.object(client.opener,'open',side_effect=respond),patch.object(providers.time,'sleep'):
                client.get('/notifications',authenticated=True);client.get('/posts/'+uid(100))
            self.assertTrue(all(r.full_url.startswith(providers.HOSTS[source]+'/') for r in calls))
            self.assertTrue(all(r.data is None or r.full_url.endswith('/auth/token') for r in calls))
            self.assertIsNone(calls[-1].get_header('Authorization'))
        with self.assertRaises(MailError):providers.NoRedirect().redirect_request(None,None,307,'',{},'https://other.example.invalid')


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.db=Path(self.temp.name)/'mail.sqlite3'
        self.command=[sys.executable,'-m','boardmail','--db',str(self.db)]

    def invoke(self,*args):
        result=subprocess.run([*self.command,*args],capture_output=True,text=True,timeout=5)
        return result.returncode,json.loads(result.stdout)

    def test_missing_state_config_bad_input_and_zero_timeout(self):
        self.assertEqual(self.invoke('wait','--timeout','0'),(5,{'event':'error','error':'database_missing','next_action':'run_init','history_complete':False}))
        self.assertFalse(self.db.exists());self.assertEqual(self.invoke('init')[0],0)
        self.assertEqual(self.invoke('init')[1]['error'],'database_exists')
        self.assertEqual(self.invoke('wait','--timeout','0')[0],3)
        self.assertEqual(self.invoke('wait','--timeout','nan')[1]['error'],'invalid_arguments')
        self.assertEqual(self.invoke('list','--limit','0')[1]['error'],'invalid_arguments')
        missing=Path(self.temp.name)/'private-name.json'
        result=subprocess.run([sys.executable,'-m','boardmail','--config',str(missing),'collect'],capture_output=True,text=True)
        self.assertEqual(json.loads(result.stdout),{'event':'error','error':'config_missing','next_action':'check_config_and_credentials','history_complete':False})
        missing.write_text('{secret_token_goes_here')
        result=subprocess.run([sys.executable,'-m','boardmail','--config',str(missing),'status'],capture_output=True,text=True)
        self.assertEqual(json.loads(result.stdout),{'event':'error','error':'invalid_config','next_action':'check_config_and_credentials','history_complete':False});self.assertNotIn('secret',result.stdout)

    def test_latin1_stdout_preserves_unicode_messages_as_json(self):
        self.invoke('init')
        item = mail(10);item['body'] = 'Привет, мир 🌍'
        Store(self.db).save('moltbook',uid(2),[item])
        for args in (('list',),('show','moltbook',uid(10)),('wait','--timeout','0')):
            result = subprocess.run([*self.command,*args],capture_output=True,timeout=5,
                                    env={**os.environ,'PYTHONIOENCODING':'latin-1'})
            self.assertEqual(result.returncode,0,result.stderr)
            value = json.loads(result.stdout.decode('ascii'))
            message = value['message'] if args[0]=='show' else value['messages'][0]
            self.assertEqual(message['body'],item['body'])

    def test_signal_cancels_wait_process_without_writing(self):
        self.invoke('init');before=self.db.read_bytes()
        script='''import threading
from boardmail.cli import main
original = threading.Event.wait
def ready(self, timeout=None):
    print("ready",flush=True)
    threading.Event.wait = original
    return original(self,timeout)
threading.Event.wait = ready
raise SystemExit(main())
'''
        process=subprocess.Popen([sys.executable,'-c',script,'--db',str(self.db),'wait','--timeout','30'],stdout=subprocess.PIPE,text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(),'ready');process.send_signal(signal.SIGTERM)
            output=process.communicate(timeout=5)[0]
            self.assertEqual(process.returncode,4);self.assertEqual(json.loads(output)['event'],'cancelled')
            self.assertEqual(self.db.read_bytes(),before)
        finally:
            if process.poll() is None:process.kill();process.communicate()


if __name__=='__main__':unittest.main()
