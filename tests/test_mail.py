"""Mail, pagination, replay and wait contracts. All provider data is invented."""
from contextlib import redirect_stdout
from copy import deepcopy
from http.client import IncompleteRead
from urllib.error import HTTPError
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

from boardmail import cli, commands, table
from boardmail.boards import collect_all
from boardmail.config import MailError
from boardmail.store import Store
from examples.fixtures import FixtureBoard, named, original, settings, together, uid
from kit import Clock, arrive, described, fixed, mark, new_inbox


def mail(n, *, created=100):
    return {"id":uid(n),"thread_id":uid(100),"kind":"mention","author":"example-agent",
            "title":"Example","body":"Synthetic text","url":"https://board.example.invalid/posts/"+uid(n),
            "created_at":created}


class MailTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'inbox.sqlite3'
        self.store = new_inbox(self.path)

    def boards(self):
        """The three invented boards and one fetch for them. The client of Postingboard waits between two
        requests, so the clock is fixed: its wait only moves the clock."""
        self.enterContext(fixed(Clock(1_000_000)))
        boards = {s:FixtureBoard(s,cfg) for s,cfg in settings(self.temp.name).items()}
        return boards, together(boards)

    def save(self,*numbers):
        """A pass that gives these messages. How many of them are new."""
        return arrive(self.store,'moltbook',uid(2),[mail(n) for n in numbers])['added']

    def went_well(self,store,source,cfg,board,at=123):
        """An earlier pass over the source that went well and brought nothing: the board had nothing for the
        account then, and the account watched no thread. The time that the source has for it."""
        quiet = {**cfg,'threads':[],'inbox':True} if source=='postingboard' else cfg
        events = getattr(board,'events',None)
        if events is not None: board.events = []
        with fixed(Clock(at)):
            result = collect_all(store,{source:quiet},fetch=board)
        if events is not None: board.events = events
        self.assertEqual((result['added'],result['failed']),(0,False),result)
        board.calls.clear()
        return next(s['last_ok'] for s in result['sources'] if s['source']==source)

    def test_bounded_pages_and_race_between_list_and_wait(self):
        self.save(10,11,12,13,14)
        first = self.store.page(0,2)
        self.assertEqual([m['arrival_seq'] for m in first['messages']],[1,2])
        self.assertEqual(first['next_after'],2);self.assertTrue(first['more'])
        # Insert after list, before wait; even an old creation date must wake.
        arrive(self.store,'moltbook',uid(2),[mail(15,created=1)])
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
        # Two passes that overlap give the same two messages. Each message arrives once.
        inner = []
        outer = arrive(self.store,'moltbook',uid(2),[mail(10),mail(11)],meanwhile=lambda:inner.append(self.save(10,11)))
        self.assertEqual((inner,outer['added'],[e['error'] for e in outer['errors']]),([2],0,['collection_conflict']))
        mark(self.store, 'moltbook',uid(10),'read')
        mark(self.store, 'moltbook',uid(10),'needs_reply')
        with self.assertRaises(MailError):mark(self.store, 'moltbook',uid(10),'replied')
        mark(self.store, 'moltbook',uid(10),'replied',ref='https://board.example.invalid/reply/1')
        before = self.store.show('moltbook',uid(10))
        self.assertEqual(self.save(10,11),0)
        self.assertEqual(self.store.show('moltbook',uid(10)),before)
        arrive(self.store,'the-colony',uid(1),[mail(10)])
        self.assertIsNone(self.store.show('the-colony',uid(10))['read_at'])
        mark(self.store, 'moltbook',uid(10),'unread')
        updated = self.store.show('moltbook',uid(10))
        self.assertTrue(updated['needs_reply']);self.assertIsNotNone(updated['replied_at'])
        with self.assertRaises(MailError):self.store.known('moltbook',uid(999))
        result,_ = commands.execute(self.store,'collect',sources={'moltbook':described(uid(999),messages=[mail(12)])})
        self.assertEqual((result['added'],[e['error'] for e in result['errors']]),(0,['account_mismatch']))

    def test_partial_source_transaction_has_no_visible_arrival(self):
        broken = mail(11);del broken['body']
        result,_ = commands.execute(self.store,'collect',sources={'moltbook':described(uid(2),messages=[mail(10),broken])})
        self.assertEqual((result['added'],[e['error'] for e in result['errors']]),(0,['invalid_adapter_result']))
        self.assertEqual(self.store.wait(0,0)['event'],'timeout')
        self.assertEqual([s['status'] for s in self.store.status()['sources']],['error'])
        self.save(10);self.assertEqual(self.store.page()['next_after'],1)

    def test_public_shapes_pages_nested_replies_and_source_isolation(self):
        boards, fetch = self.boards()
        c = boards['moltbook']
        child = original(212,201);child['parent_id']=uid(211)
        c.comments[0]['replies']=[child]
        c.events += [deepcopy(c.events[0])]
        for board in boards.values():
            board.per_page = 1  # A page holds one notification or comment, however many the client asks for.
        added = 0
        for _ in range(3):
            result = collect_all(self.store,settings(self.temp.name),fetch=fetch)
            added += result['added']
            self.assertFalse(result['failed'])
        for board in boards.values():
            board.per_page = None
        self.assertEqual(added,7)
        self.assertEqual(self.store.show('moltbook',uid(212))['parent_id'],uid(211))
        self.assertEqual(self.store.show('moltbook',uid(211))['body'],'A synthetic public reply.')
        self.assertEqual(self.store.show('the-colony',uid(111))['url'],
                         'https://thecolony.ai/posts/'+uid(101)+'#comment-'+uid(111))
        self.assertEqual(self.store.show('postingboard',uid(312))['provider_seq'],312)
        self.assertEqual(self.store.show('postingboard',uid(314))['kind'],'mention')
        self.assertNotIn(uid(315),self.store.known('postingboard',uid(3)))
        before = next(s['last_ok'] for s in result['sources'] if s['source']=='the-colony')
        boards['the-colony'].fail=True
        c.comments.append(original(213,201))
        extra=deepcopy(c.events[0]);extra.update(id=uid(999),relatedCommentId=uid(213));c.events.append(extra)
        result=collect_all(self.store,settings(self.temp.name),fetch=fetch)
        self.assertEqual(result['added'],1)
        health=next(s for s in result['sources'] if s['source']=='the-colony')
        self.assertEqual(health['last_ok'],before);self.assertEqual(health['error'],'http_503')
        self.assertNotIn('secret-token',json.dumps(result));self.assertNotIn('provider prose',json.dumps(result))

    def test_partial_notification_page_and_late_public_body(self):
        sources={'moltbook':settings(self.temp.name)['moltbook']}
        c=FixtureBoard('moltbook',sources['moltbook']);get=c.get
        def fail_later(path,params=None,**kw):
            if path=='/notifications' and params.get('cursor'):raise OSError('sensitive path or token')
            return get(path,params,**kw)
        c.get=fail_later
        c.per_page=1  # A page holds one notification, however many the client asks for.
        result=collect_all(self.store,sources,fetch=c)
        self.assertEqual(result['added'],1)
        result=collect_all(self.store,sources,fetch=c)
        c.per_page=None
        self.assertTrue(result['failed']);self.assertEqual(len(self.store.page()['messages']),1)
        c.get=get
        result=collect_all(self.store,sources,fetch=c)
        self.assertEqual(result['added'],0)
        after=self.store.page()['next_after'];self.assertEqual(result['sources'][0]['unavailable'],1)
        c.comments.append(original(212,201,body='Late public confirmation.'))
        c.comments[-1]['created_at']='2020-01-01T00:00:00Z'
        result=collect_all(self.store,sources,fetch=c)
        self.assertEqual(result['added'],1)
        self.assertEqual(self.store.wait(after,0)['messages'][0]['body'],'Late public confirmation.')

    def test_confirmed_prefix_survives_a_repeated_small_provider_budget(self):
        cfg = settings(self.temp.name)['moltbook']
        events = [{'id':uid(n+1000),'type':'post_comment','relatedPostId':uid(n),
                   'relatedCommentId':uid(n+10)} for n in (501,502,503)]
        board = FixtureBoard('moltbook',cfg)
        base_get = board.get
        used = 0
        def get(path,params=None,**kw):
            nonlocal used
            if path == '/agents/me':
                used = 0  # A pass begins with the profile. The board gives one post in each pass.
                return base_get(path, params, **kw)
            if path=='/notifications':return {'notifications':events,'has_more':False}
            root = int(UUID(path.split('/')[2]))
            if path.endswith('/comments'):
                return {'comments':[original(root+10,root)],'has_more':False}
            used += 1
            if used>1:
                raise HTTPError('https://example.invalid',429,'quota',{},io.BytesIO())
            return {'post':{**original(root,root,2),'title':'Example'}}
        board.get = get
        for expected in (1,2,3):
            result = collect_all(self.store,{'moltbook':cfg},fetch=board)
            self.assertEqual(result['added'],1)
            self.assertEqual(self.store.status()['counts']['total'],expected)
            if expected<3:
                self.assertEqual(result['sources'][0]['status'],'error')
                self.assertIsNone(result['sources'][0]['last_ok'])
                self.assertEqual(self.store.wait(expected-1,0)['event'],'messages')
        self.assertFalse(result['failed']);self.assertIsNotNone(result['sources'][0]['last_ok'])

    def test_non_post_mentions_unknown_authors_and_http_stream_errors(self):
        boards, fetch = self.boards()
        c = boards['moltbook']
        c.events.insert(0,{'id':uid(900),'type':'mention','relatedPostId':None})
        c.events.insert(0,{'id':uid(901),'type':'comment_reply','relatedPostId':None})
        c.comments[0]['author'] = None
        result = collect_all(self.store,settings(self.temp.name),fetch=fetch)
        self.assertFalse(result['failed'])
        self.assertIsNone(self.store.show('moltbook',uid(211))['author'])
        def broken(*_,**__):raise IncompleteRead(b'sensitive partial payload')
        boards['the-colony'].get = broken
        result = collect_all(self.store,settings(self.temp.name),fetch=fetch)
        self.assertTrue(result['failed'])
        self.assertEqual(next(s['status'] for s in result['sources'] if s['source']=='moltbook'),'ok')
        self.assertNotIn('sensitive',json.dumps(result))

    def test_second_page_failure_keeps_confirmed_messages_from_one_thread(self):
        for source in ('moltbook','postingboard'):
            with self.subTest(source=source):
                cfg = deepcopy(settings(self.temp.name)[source])
                cfg['threads'] = [uid(301)]
                board = FixtureBoard(source,cfg)
                if source == 'moltbook':
                    board.comments.append(original(212,201))
                    first_count, total = 1,2
                else:
                    board.comments[uid(301)] = [named(n,301) for n in range(400,431)]
                    first_count, total = 30,31
                self.went_well(self.store,source,cfg,board)
                get = board.get
                def broken(path,params=None,**kw):
                    if source=='moltbook' and path=='/notifications':
                        return {'notifications':board.events,'has_more':False}
                    if params and (('cursor' in params and path.endswith('/comments')) or 'before' in params):
                        raise HTTPError('https://example.invalid',429,'quota',{},io.BytesIO())
                    return get(path,params,**kw)
                board.get = broken
                board.per_page = 1  # A page of Moltbook holds one comment, however many the client asks for.
                # The client of Postingboard waits between two requests. Here its wait only moves the clock.
                with fixed(Clock(1_000_000)):
                    for expected_added in (first_count,0):
                        result = collect_all(self.store,{source:cfg},fetch=board)
                        self.assertEqual(result['added'],expected_added)
                        health = next(s for s in result['sources'] if s['source']==source)
                        if expected_added==0:
                            self.assertEqual(health['status'],'error');self.assertEqual(health['last_ok'],last_ok)
                        last_ok=health['last_ok']
                        self.assertEqual(len(self.store.known(source,cfg['account_id'])),first_count)
                    board.get = get
                    result = collect_all(self.store,{source:cfg},fetch=board)
                self.assertFalse(result['failed']);self.assertEqual(result['added'],total-first_count)
                self.assertEqual(len(self.store.known(source,cfg['account_id'])),total)

    def test_postingboard_all_selected_pages_and_summary_hydration(self):
        c=self.boards()[0]['postingboard']
        c.comments[uid(301)]=[named(n,301) for n in [311,312,*range(400,433)]]
        result=collect_all(self.store,{'postingboard':c.settings},fetch=c)
        self.assertEqual(result['added'],36)
        self.assertEqual(self.store.show('postingboard',uid(312))['body'],'A synthetic named-board reply.')
        self.assertTrue(any('before' in p for _,p,_ in c.calls))

    def test_postingboard_slow_or_broken_root_does_not_starve_later_roots(self):
        cfg = settings(self.temp.name)['postingboard']
        board = FixtureBoard('postingboard',cfg)
        board.comments[uid(301)] = [named(n,301) for n in range(400,1700)]
        get = board.get
        for failure in ('source_timeout','http_503'):
            with self.subTest(failure=failure):
                store = new_inbox(Path(self.temp.name)/(failure+'.sqlite3'))
                earlier = self.went_well(store,'postingboard',cfg,board)
                clock, calls = Clock(1_000_000), []
                def respond(path,params=None,**kw):
                    calls.append((path,clock.now))
                    if failure=='http_503' and path.endswith(uid(301)):
                        raise HTTPError('https://example.invalid',503,'private provider prose',{},io.BytesIO())
                    return get(path,params,**kw)
                board.get = respond
                # The wait of the client between two requests moves the clock, and the time of a root ends by it.
                with fixed(clock):
                    for attempt in range(2):
                        result = collect_all(store,{'postingboard':cfg},fetch=board)
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
                            self.assertEqual(result['sources'][0]['last_ok'],earlier)
                            self.assertEqual(count,1)
                            if attempt:self.assertEqual(result['added'],0)
                # Actual HTTP requests stay paced across both configured roots.
                for previous,current in zip(calls,calls[1:]):
                    if current[0].endswith(uid(302)):
                        self.assertGreaterEqual(current[1]-previous[1],1.1-1e-8)

    def test_discovery_progress_and_repeated_cursor_recovery(self):
        sources={'moltbook':settings(self.temp.name)['moltbook']}
        c=FixtureBoard('moltbook',sources['moltbook'])
        c.per_page=1  # A page holds one notification or comment, however many the client asks for.
        result=collect_all(self.store,sources,fetch=c)
        self.assertFalse(result['failed']);self.assertTrue(result['sources'][0]['backlog_pending'])
        self.assertEqual(result['added'],1)
        get=c.get
        def repeat(path,params=None,**kw):
            result=get(path,{**(params or {}),'cursor':'0'},**kw)
            if path=='/notifications':result.update(has_more=True,next_cursor='same')
            return result
        c.get=repeat
        for _ in range(2):
            result=collect_all(self.store,sources,fetch=c)
        self.assertEqual(result['sources'][0]['error'],'pagination_no_progress')
        c.get=get
        result=collect_all(self.store,sources,fetch=c)
        self.assertFalse(result['failed']);self.assertEqual(self.store.status()['counts']['total'],1)

    def test_wait_timeout_cancellation_and_outage_never_write(self):
        def down(): raise OSError('The board is not reached')
        # A healthy pass, then one that fails as a whole: its adapter file raises.
        self.save(10);self.assertEqual(arrive(self.store,'moltbook',uid(2),meanwhile=down)['errors'][0]['error'],'adapter_failed')
        before=self.path.read_bytes();result=self.store.wait(1,0.02)
        self.assertEqual(result['event'],'timeout');self.assertEqual(result['sources'][0]['status'],'error')
        stopped=threading.Event();stopped.set();result=self.store.wait(0,10,cancelled=stopped)
        self.assertEqual(result['event'],'cancelled');self.assertEqual(result['next_after'],0)
        self.assertEqual(result['messages'],[]);self.assertEqual(self.path.read_bytes(),before)

    def test_auth_hosts_and_public_confirmation(self):
        boards, fetch = self.boards()
        self.assertFalse(collect_all(self.store,settings(self.temp.name),fetch=fetch)['failed'])
        # What each board was asked, and with which sign that the request is the one of the account. Colony signs
        # the account in for a token. A public original is asked without any sign, on every board that has one.
        asked = {s:[(a.url.partition('?')[0],a.headers.get('Authorization')) for a in board.asked] for s,board in boards.items()}
        colony, molt = 'https://thecolony.ai/api/v1', 'https://www.moltbook.com/api/v1'
        self.assertEqual(asked['the-colony'], [(colony+'/auth/token',None),(colony+'/agents/me','Bearer invented-token'),
            (colony+'/notifications','Bearer invented-token'),(colony+'/comments/'+uid(111),None),(colony+'/comments/'+uid(112),None)])
        self.assertEqual(boards['the-colony'].asked[0].body, {'api_key':'invented-key'})
        self.assertTrue(all(a.body is None for board in boards.values() for a in board.asked[board.source=='the-colony':]))
        self.assertEqual(asked['moltbook'], [(molt+'/agents/me','Bearer invented-key'),(molt+'/notifications','Bearer invented-key'),
            (molt+'/posts/'+uid(201),None),(molt+'/posts/'+uid(201)+'/comments',None)])
        self.assertEqual(set(asked['postingboard']), {('https://getpostingboard.dev/v1/'+path,'Bearer invented-key')
            for path in ('me','posts/'+uid(301),'posts/'+uid(302),'posts/'+uid(312))})


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.db=Path(self.temp.name)/'mail.sqlite3'
        self.command=[sys.executable,'-m','boardmail','--db',str(self.db)]

    def invoke(self,*args,database=None):
        command = self.command if database is None else [*self.command[:-1], str(database)]
        result=subprocess.run([*command,*args],capture_output=True,text=True,timeout=5)
        return result.returncode,json.loads(result.stdout)

    def init_config(self):
        config = Path(self.temp.name)/'config.json'
        config.write_text(json.dumps({'database': 'configured.sqlite3', 'sources': {
            'research': {'adapter': 'moltbook', 'account_id': uid(2),
                         'api_key_file': 'missing-key'}}}))
        return config

    def test_init_explicit_config_seeds_the_override_database(self):
        config = self.init_config()
        code, result = self.invoke('--config', str(config), 'init')
        self.assertEqual(code, 0, result)
        self.assertEqual([(s['source'], s['account_id']) for s in result['sources']],
                         [('research', uid(2))])
        with Store(self.db).connect() as db:
            self.assertEqual(tuple(db.execute('SELECT source, adapter FROM adapter_state').fetchone()),
                             ('research', 'moltbook'))
        self.assertFalse((config.parent/'configured.sqlite3').exists())
        self.assertFalse((config.parent/'missing-key').exists())

    def test_init_missing_or_invalid_explicit_config_creates_no_database(self):
        config = Path(self.temp.name)/'config.json'
        cases = ((None, 5, 'config_missing'), ('{broken', 2, 'invalid_config'),
                 ('{"database":"configured.sqlite3","sources":{}}', 2, 'invalid_config'))
        for index, (contents, code, error) in enumerate(cases):
            with self.subTest(contents=contents):
                if contents is not None:
                    config.write_text(contents)
                database = config.parent/f'case-{index}.sqlite3'
                actual, result = self.invoke('--config', str(config), 'init', database=database)
                self.assertEqual((actual, result.get('error')), (code, error), result)
                self.assertFalse(database.exists())
                self.assertFalse((config.parent/'configured.sqlite3').exists())

    def test_db_only_init_and_local_status_do_not_load_config(self):
        code, result = self.invoke('init')
        self.assertEqual((code, result['sources']), (0, []))
        before = self.db.read_bytes()
        config = Path(self.temp.name)/'config.json'
        for contents in (None, '{broken'):
            with self.subTest(contents=contents):
                if contents is not None:
                    config.write_text(contents)
                code, result = self.invoke('--config', str(config), 'status')
                self.assertEqual((code, result['sources']), (0, []))
                self.assertEqual(self.db.read_bytes(), before)

    def test_init_with_explicit_config_preserves_an_existing_database(self):
        self.assertEqual(self.invoke('init')[0], 0)
        arrive(Store(self.db), 'moltbook', uid(2), [mail(10)])
        before = self.db.read_bytes()
        config = self.init_config()
        code, result = self.invoke('--config', str(config), 'init')
        self.assertEqual((code, result['error']), (2, 'database_exists'))
        self.assertEqual(self.db.read_bytes(), before)
        self.assertFalse((config.parent/'configured.sqlite3').exists())

    def test_which_commands_read_the_config_when_db_names_the_inbox(self):
        home = Path(self.temp.name)
        broken = home/'broken.json'
        usual = home/'.config/boardmail/config.json'
        usual.parent.mkdir(parents=True)
        for config in (broken, usual):
            config.write_text('{broken')

        def reading(*before, named=True, local=False):
            """The commands that read a config when they are typed after these words: they say that it is broken."""
            found = set()
            for name, command in table.COMMANDS.items():
                group = name.partition('_')[0]
                words = [group, name.removeprefix(group + '_')] if group in table.GROUPS else [name]
                for argument in command.arguments:
                    if argument.required:
                        # A word that the parser takes for it. What it names need not be there.
                        option = argument.typed.split()[0]
                        word = argument.kind['enum'][0] if 'enum' in argument.kind else '1'
                        words += [option, word] if option.startswith('-') else [word]
                    elif local and argument.name == 'local':
                        words.append('--local')
                # An inbox of its own, so that no command finds the one that init has made.
                inbox = ['--db', str(home/(name + '.sqlite3'))] if named else []
                printed = io.StringIO()
                with redirect_stdout(printed):
                    cli.main([*before, *inbox, *words])
                if json.loads(printed.getvalue()).get('error') == 'invalid_config':
                    found.add(name)
            return found

        needed = {'collect', 'check', 'reply_verify'}
        used = {'init', 'subscribe', 'unsubscribe', 'pause', 'resume', 'context', 'expand'}
        with patch.dict(os.environ, {'HOME': str(home), 'USERPROFILE': str(home)}):
            # Without --db the config says where the inbox is.
            self.assertEqual(reading(named=False), set(table.COMMANDS))
            # With --db, a command that cannot run without the sources reads the config where it usually is.
            self.assertEqual(reading(), needed)
            # A command that only uses the sources reads the config that --config names, unless it is told --local.
            self.assertEqual(reading('--config', str(broken)), needed | used)
            self.assertEqual(reading('--config', str(broken), local=True), needed | used - {'context', 'expand'})

    def test_missing_state_config_bad_input_and_zero_timeout(self):
        self.assertEqual(self.invoke('wait','--timeout','0'),(5,{'event':'error','error':'database_missing','next_action':'run_init',
            'next':{'tool':'boardmail_init','arguments':{}},'history_complete':False}))
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
        arrive(Store(self.db),'moltbook',uid(2),[item])
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
