"""Real SDK clients, invented public mail, and the modern HTTP request path."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from boardmail.mcp import create_server
from boardmail.store import Store
from examples.fixtures import FixtureClient, named, original, settings, uid
from test_mail import mail

try:
    from mcp import Client, StdioServerParameters
    import httpx2 as httpx
except ImportError:
    Client = None


@unittest.skipIf(Client is None, 'From the source checkout, install .[mcp] to test the optional MCP interface')
class MCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'mail.sqlite3'
        self.store = Store(self.path)

    async def call(self, client, name, arguments=None, *, error=False):
        result = await client.call_tool('boardmail_' + name, arguments or {})
        self.assertEqual(result.is_error, error, result)
        self.assertEqual(json.loads(result.content[0].text), result.structured_content)
        self.assertIs(result.structured_content['history_complete'], False)
        return result.structured_content

    async def test_discovery_errors_arrivals_and_independent_marks(self):
        async with Client(create_server(self.store), mode='2026-07-28', raise_exceptions=True) as c:
            tools = (await c.list_tools()).tools
            self.assertEqual([t.name for t in tools], sorted('boardmail_' + n for n in ('init','check','collect','status','list','show','wait','mark','context','pause','resume')))
            for t in tools:
                self.assertFalse(t.input_schema['additionalProperties'])
                self.assertIn('event', t.output_schema['required'])
                self.assertEqual(t.annotations.read_only_hint, t.name in ('boardmail_status','boardmail_list','boardmail_show','boardmail_wait','boardmail_context'))
                self.assertEqual(t.annotations.open_world_hint, t.name in ('boardmail_collect','boardmail_check','boardmail_context'))
            missing = await self.call(c, 'status', error=True)
            self.assertEqual((missing['error'],missing['next_action']), ('database_missing','run_init'))
            self.assertFalse(self.path.exists())
            await self.call(c, 'init')
            before = self.path.read_bytes()
            self.assertEqual((await self.call(c, 'init', error=True))['error'], 'database_exists')
            self.assertEqual(before, self.path.read_bytes())
            for name, args in [('list', {'db':'secret/path'}), ('wait', {'timeout':61}), ('list', {'after':True})]:
                self.assertEqual((await self.call(c, name, args, error=True))['error'], 'invalid_arguments')
            self.assertEqual((await self.call(c, 'collect', error=True))['error'], 'config_missing')
            self.store.save('moltbook', uid(2), [mail(10), mail(11), mail(12)])
            page = await self.call(c, 'list', {'limit':2})
            self.assertEqual([m['arrival_seq'] for m in page['messages']], [1,2])
            self.assertEqual(page['next_after'], 2)
            self.assertTrue(page['more'])
            target = {'source':'moltbook', 'id':uid(10)}
            await self.call(c, 'mark', {**target, 'action':'read'})
            await self.call(c, 'mark', {**target, 'action':'needs-reply'})
            await self.call(c, 'mark', {**target, 'action':'replied'}, error=True)
            await self.call(c, 'mark', {**target, 'action':'replied', 'ref':'https://board.example.invalid/reply'})
            message = (await self.call(c, 'show', target))['message']
            self.assertIsNotNone(message['read_at'])
            self.assertTrue(message['needs_reply'])
            self.assertIsNotNone(message['replied_at'])
            page = await self.call(c, 'wait', {'after':2, 'timeout':0})
            self.assertEqual([m['arrival_seq'] for m in page['messages']], [3])
            before = self.path.read_bytes()
            page = await self.call(c, 'wait', {'after':3, 'timeout':0.01})
            self.assertEqual((page['event'],page['next_after']), ('timeout',3))
            self.assertFalse(page['collection_performed'])
            context = await self.call(c, 'context', target, error=True)
            self.assertEqual((context['target']['status'],context['root']['status'],context['fetched']), ('available','unknown',False))
            health = await self.call(c, 'status', {'require_fresh':True})
            self.assertEqual((health['fresh'],health['freshness_required'],health['sources'][0]['last_ok_age']<540), (True,True,True))
            self.assertEqual((await self.call(c, 'status', {'require_fresh':True,'stale_after':-1}, error=True))['error'], 'invalid_arguments')
            self.assertEqual(before, self.path.read_bytes())

    async def test_wait_allows_other_calls_and_cancellation_leaves_checkpoint(self):
        self.store.initialize()
        started, finished = threading.Event(), threading.Event()
        original_wait = self.store.wait
        def observed_wait(*args, **kwargs):
            started.set()
            try:
                return original_wait(*args, **kwargs)
            finally:
                finished.set()
        with patch.object(self.store, 'wait', observed_wait):
            async with Client(create_server(self.store), mode='2026-07-28', raise_exceptions=True) as c:
                waiter = asyncio.create_task(c.call_tool('boardmail_wait', {'after':0,'timeout':60}))
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                # A second request is served while wait is blocked in SQLite polling.
                await asyncio.wait_for(self.call(c, 'status'), 2)
                before = self.path.read_bytes()
                waiter.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await waiter
                self.assertTrue(await asyncio.to_thread(finished.wait, 2))
                self.assertEqual(before, self.path.read_bytes())
                self.store.save('moltbook',uid(2),[mail(10)])
                page = await self.call(c, 'wait', {'after':0,'timeout':0})
                self.assertEqual((page['event'],page['next_after']), ('messages',1))

    async def test_collection_partial_success_replay_and_account_isolation(self):
        cfg = settings()
        self.store.initialize(cfg)
        from boardmail import providers
        actual = providers.collect_all
        def factory(name, config):
            client = FixtureClient(name, config)
            client.fail = name == 'moltbook'
            return client
        def collect(store, sources):
            return actual(store, sources, client_factory=factory)
        with patch.object(providers, 'collect_all', collect):
            async with Client(create_server(self.store,cfg), mode='2026-07-28', raise_exceptions=True) as c:
                result = await self.call(c, 'check', {'after':0,'limit':1}, error=True)
                self.assertGreater(result['collection']['added'], 0)
                self.assertTrue(result['collection']['failed'])
                self.assertTrue(result['collection_performed'])
                self.assertEqual(result['next_after'],1)
                self.assertTrue(result['more'])
                first = await self.call(c, 'list')
                target = {k:first['messages'][0][k] for k in ('source','id')}
                await self.call(c, 'mark', {**target,'action':'needs-reply'})
                self.assertEqual((await self.call(c,'collect',error=True))['added'],0)
                self.assertTrue((await self.call(c,'show',target))['message']['needs_reply'])
                cfg['postingboard']['account_id'] = uid(999)
                result = await self.call(c,'collect',error=True)
                self.assertIn('account_mismatch',[e['error'] for e in result['errors']])

    async def test_context_returns_saved_and_current_text_without_marks(self):
        cfg = {'postingboard': settings()['postingboard']}
        self.store.initialize(cfg)
        self.store.save('postingboard', cfg['postingboard']['account_id'], [{**mail(610), 'thread_id': uid(610)}])
        fixture = FixtureClient('postingboard', cfg['postingboard'])
        fixture.others[uid(610)] = named(610, 610, body='Edited root text')
        before = self.path.read_bytes()
        with patch('boardmail.providers.Client', return_value=fixture):
            async with Client(create_server(self.store, cfg), mode='2026-07-28', raise_exceptions=True) as c:
                target = {'source': 'postingboard', 'id': uid(610)}
                result = await self.call(c, 'context', target)
                self.assertEqual(result['target']['message']['body'], 'Synthetic text')
                self.assertEqual(result['target']['current_message']['body'], 'Edited root text')
                self.assertIs(result['target']['differs_from_saved'], True)
                local = await self.call(c, 'context', {**target, 'local': True})
                self.assertIsNone(local['target']['current_message'])
                self.assertIsNone(local['target']['differs_from_saved'])
        self.assertEqual(len(fixture.calls), 1)
        self.assertEqual(self.path.read_bytes(), before)

    async def test_context_links_recorded_answers_and_reads_our_colony_parent(self):
        from boardmail import providers
        cfg = {'the-colony': settings()['the-colony']}
        self.store.initialize(cfg)
        self.store.save('the-colony', cfg['the-colony']['account_id'], [
            mail(201), {**mail(130), 'thread_id': uid(101), 'parent_id': uid(120)}])
        fixture = FixtureClient('the-colony', cfg['the-colony'])
        fixture.comments += [original(120, 101, 1, colony=True, body='Our previous reply.'),
                             {**original(130, 101, colony=True), 'parent_id': uid(120)}]
        ref = fixture.host + '/posts/' + uid(101) + '#comment-' + uid(120)
        self.store.mark('the-colony', uid(201), 'replied', ref=ref)
        before = self.path.read_bytes()
        with patch.object(providers, 'Client', return_value=fixture), patch.dict(providers.HOSTS, {'the-colony': fixture.host}):
            async with Client(create_server(self.store, cfg), mode='2026-07-28', raise_exceptions=True) as c:
                result = await self.call(c, 'context', {'source': 'the-colony', 'id': uid(130)})
                self.assertEqual(result['parent']['message']['body'], 'Our previous reply.')
                self.assertEqual(result['previous_exchange']['status'], 'linked')
                self.assertEqual([m['id'] for m in result['previous_exchange']['messages']], [uid(201)])
        self.assertEqual(self.path.read_bytes(), before)

    async def test_pause_is_shared_with_cli_without_restarting_server(self):
        from boardmail import providers
        cfg = {'moltbook': settings()['moltbook']}
        self.store.initialize(cfg)
        actual = providers.collect_all
        with patch('boardmail.providers.Client', side_effect=AssertionError('unexpected network client')):
            with patch.object(providers, 'collect_all', side_effect=lambda store, sources:
                              actual(store, sources, client_factory=FixtureClient)):
                async with Client(create_server(self.store, cfg), mode='2026-07-28', raise_exceptions=True) as c:
                    process = await asyncio.create_subprocess_exec(
                        sys.executable, '-m', 'boardmail', '--db', str(self.path), 'pause', 'moltbook',
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                    stdout, stderr = await process.communicate()
                    self.assertEqual(process.returncode, 0, stderr)
                    self.assertTrue(json.loads(stdout)['paused'])
                    result = await self.call(c, 'check')
                    self.assertEqual((result['collection']['added'], result['sources'][0]['status']), (0, 'paused'))
                    await self.call(c, 'status', {'require_fresh': True})
                    self.assertEqual((await self.call(c, 'pause', {'source': 'typo'}, error=True))['error'], 'source_not_found')
                    for changed in (True, False):
                        result = await self.call(c, 'resume', {'source': 'moltbook'})
                        self.assertEqual((result['changed'], result['paused'], result['collection_performed']),
                                         (changed, False, False))
                    self.assertGreater((await self.call(c, 'collect'))['added'], 0)
                    result = await self.call(c, 'pause', {'source': 'moltbook'})
                    self.assertEqual(result['event'], 'paused')
                    self.assertTrue(Store(self.path).is_paused('moltbook'))
                    tool = next(t for t in (await c.list_tools()).tools if t.name == 'boardmail_pause')
                    self.assertTrue(tool.annotations.idempotent_hint)
                    self.assertFalse(tool.annotations.read_only_hint or tool.annotations.open_world_hint)

    async def test_stdio_modern_and_legacy_clients(self):
        self.store.initialize()
        params = StdioServerParameters(command=sys.executable,
            args=['-m','boardmail.mcp','--db',str(self.path)])
        for mode in ('2026-07-28', 'legacy'):
            async with Client(params, mode=mode, read_timeout_seconds=5) as c:
                self.assertEqual(len((await c.list_tools()).tools),11)
                self.assertEqual((await self.call(c,'wait',{'timeout':0}))['event'],'timeout')

    async def test_cancelled_collection_finishes_before_next_collection(self):
        from boardmail import providers
        self.store.initialize()
        started, release, second = threading.Event(), threading.Event(), threading.Event()
        calls = []
        def collect(*args):
            calls.append(1)
            if len(calls) == 1:
                started.set()
                release.wait(5)
            else:
                second.set()
            return {'event':'collected','added':0,'failed':False,'errors':[]}
        with patch.object(providers, 'collect_all', collect):
            async with Client(create_server(self.store, {}), mode='2026-07-28', raise_exceptions=True) as c:
                first = asyncio.create_task(c.call_tool('boardmail_collect'))
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                first.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await first
                other = asyncio.create_task(c.call_tool('boardmail_check'))
                try:
                    await self.call(c, 'status')
                    self.assertFalse(await asyncio.to_thread(second.wait, 0.1))
                finally:
                    release.set()
                    await other
                self.assertTrue(second.is_set())

    async def test_modern_http_without_initialization_or_session(self):
        self.store.initialize()
        app = create_server(self.store).streamable_http_app(json_response=True, stateless_http=True)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8766') as c:
                async def request(method, params=None, **headers):
                    return await c.post('/mcp', headers={'Accept':'application/json, text/event-stream',
                        'MCP-Protocol-Version':'2026-07-28','Mcp-Method':method, **headers},
                        json={'jsonrpc':'2.0','id':1,'method':method,'params':{
                            '_meta':{'io.modelcontextprotocol/clientInfo':{'name':'boardmail-test','version':'1'},
                                     'io.modelcontextprotocol/protocolVersion':'2026-07-28',
                                     'io.modelcontextprotocol/clientCapabilities':{}},
                            **(params or {})}})
                for method in ('server/discover','tools/list'):
                    response = await request(method)
                    self.assertEqual(response.status_code,200,response.text)
                    self.assertNotIn('mcp-session-id',response.headers)
                    self.assertIn('result',response.json())
                response = await request('tools/call',{'name':'boardmail_wait','arguments':{'timeout':0}}, **{'Mcp-Name':'boardmail_wait'})
                self.assertEqual(response.status_code,200,response.text)
                self.assertEqual(response.json()['result']['structuredContent']['event'],'timeout')
                response = await request('tools/list', **{'Host':'evil.example'})
                self.assertEqual(response.status_code,421)
