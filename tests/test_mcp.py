"""Real SDK clients, invented public mail, and the modern HTTP request path."""
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from boardmail import providers
from boardmail.mcp import create_server
from boardmail.store import Store
from examples.fixtures import FixtureClient, named, settings, uid
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
            self.assertEqual([t.name for t in tools], sorted('boardmail_' + n for n in ('init','check','collect','status','settings','subscribe','unsubscribe','subscriptions','list','show','wait','mark','context','expand','pause','resume','reply_prepare','reply_begin','reply_show','reply_confirm','reply_verify')))
            for t in tools:
                self.assertFalse(t.input_schema['additionalProperties'])
                self.assertIn('event', t.output_schema['required'])
                self.assertEqual(t.annotations.destructive_hint, t.name == 'boardmail_reply_prepare')
                self.assertEqual(t.annotations.read_only_hint, t.name in ('boardmail_status','boardmail_list','boardmail_show','boardmail_wait','boardmail_context','boardmail_expand','boardmail_subscriptions','boardmail_reply_show'))
                self.assertEqual(t.annotations.open_world_hint, t.name in ('boardmail_collect','boardmail_check','boardmail_context','boardmail_expand','boardmail_reply_verify'))
            missing = await self.call(c, 'status', error=True)
            self.assertEqual((missing['error'],missing['next_action']), ('database_missing','run_init'))
            self.assertFalse(self.path.exists())
            await self.call(c, 'init')
            before = self.path.read_bytes()
            self.assertEqual((await self.call(c, 'init', error=True))['error'], 'database_exists')
            self.assertEqual(before, self.path.read_bytes())
            for name, args in [('list', {'db':'secret/path'}), ('wait', {'timeout':61}), ('list', {'after':True}),
                               ('expand', {'source':'moltbook', 'thread':uid(100)}), ('expand', {'source':'moltbook', 'thread':uid(100), 'through':3, 'after':4}),
                               ('expand', {'source':'moltbook', 'thread':uid(100), 'through':3, 'limit':101})]:
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

    async def test_reply_attempt_survives_cli_mcp_handoffs_without_reset_or_implicit_marks(self):
        self.store.initialize()
        self.store.save('moltbook', uid(2), [mail(10)])
        self.store.mark('moltbook', uid(10), 'needs_reply')
        target = {'source':'moltbook', 'id':uid(10)}
        body = 'Exact synthetic reply.\r\nКириллица.\n'
        path = Path(self.temp.name) / 'reply.txt'; path.write_bytes(body.encode('utf-8'))
        async with Client(create_server(self.store), mode='2026-07-28', raise_exceptions=True) as c:
            self.assertIsNone((await self.call(c, 'reply_show', target))['reply'])
            run = await asyncio.to_thread(subprocess.run,
                [sys.executable, '-m', 'boardmail', '--db', str(self.path), 'reply', 'prepare',
                 target['source'], target['id'], '--body-file', str(path)], capture_output=True, text=True, timeout=10)
            self.assertEqual(run.returncode, 0, run.stderr)
            prepared = json.loads(run.stdout)
            key = prepared['reply']['idempotency_key']
            begun = await self.call(c, 'reply_begin', {**target, 'key':key})
            self.assertTrue(begun['send_allowed'])
            repeat = await self.call(c, 'reply_prepare', {**target, 'body':body})
            self.assertEqual((repeat['reply']['state'], repeat['reply']['idempotency_key']), ('unknown', key))
            self.assertFalse(repeat['send_allowed'])
            repeat = await self.call(c, 'reply_begin', {**target, 'key':key})
            self.assertFalse(repeat['send_allowed'])
            for args in ({**target, 'body':body, 'replace_key':key, 'unexpected':True},
                         {**target, 'body':123}, {**target, 'body':body, 'replace_key':False}):
                self.assertEqual((await self.call(c, 'reply_prepare', args, error=True))['error'], 'invalid_arguments')
            receipt = {**target, 'key':key, 'ref':'https://example.invalid/reply', 'readback_body':body}
            mismatch = await self.call(c, 'reply_confirm', {**receipt, 'readback_body':body.rstrip()}, error=True)
            self.assertEqual(mismatch['error'], 'reply_readback_mismatch')
            confirmed = await self.call(c, 'reply_confirm', receipt)
            self.assertEqual(confirmed['reply']['state'], 'confirmed')
            self.assertFalse(confirmed['remote_verified'])
            self.assertEqual(confirmed['confirmation_basis'], 'caller_supplied_readback')
            self.assertIsNone(confirmed['message']['read_at'])
            self.assertTrue(confirmed['message']['needs_reply'])
            self.assertEqual(confirmed['message']['reply_ref'], receipt['ref'])
            run = await asyncio.to_thread(subprocess.run,
                [sys.executable, '-m', 'boardmail', '--db', str(self.path), 'reply', 'show',
                 target['source'], target['id']], capture_output=True, text=True, timeout=10)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(json.loads(run.stdout)['reply'], confirmed['reply'])

    async def test_reply_verify_checks_provider_and_exposes_durable_evidence(self):
        cfg = {'postingboard': settings()['postingboard']}
        self.store.initialize(cfg)
        self.store.save('postingboard', cfg['postingboard']['account_id'], [
            {**mail(610), 'thread_id': uid(600)}])
        fixture = FixtureClient('postingboard', cfg['postingboard'])
        reply = named(620, 600, 3, body='Exact reply.\r\n', reply_to=610)
        fixture.others = {uid(620): reply}
        target = {'source': 'postingboard', 'id': uid(610)}
        with patch('boardmail.providers.Client', return_value=fixture):
            async with Client(create_server(self.store, cfg), mode='2026-07-28', raise_exceptions=True) as c:
                prepared = await self.call(c, 'reply_prepare', {**target, 'body': reply['body']})
                args = {**target, 'key': prepared['reply']['idempotency_key']}
                await self.call(c, 'reply_begin', args)
                args['ref'] = providers.parent_reference('postingboard', uid(600), uid(620))
                # A lookalike author must produce an MCP error with the unchanged attempt.
                reply['agent_id'] = uid(99)
                missed = await self.call(c, 'reply_verify', args, error=True)
                self.assertEqual(missed['verification']['reason'], 'reply_author_mismatch')
                self.assertEqual(missed['reply']['state'], 'unknown')
                self.assertFalse(missed['send_allowed'])
                reply['agent_id'] = cfg['postingboard']['account_id']
                verified = await self.call(c, 'reply_verify', args)
                self.assertTrue(verified['remote_verified'])
                self.assertEqual(verified['confirmation_basis'], 'provider_readback')
                shown = await self.call(c, 'reply_show', target)
                self.assertFalse(shown['remote_verified'])
                self.assertEqual(shown['verification_receipt'], verified['verification'])
                self.assertEqual(shown['reply']['state'], 'confirmed')

    async def test_reading_settings_thread_summary_and_replay(self):
        self.store.initialize()
        self.store.save('moltbook', uid(2), [dict(mail(10), addressing='thread'), dict(mail(11), addressing='direct')])
        async with Client(create_server(self.store), mode='2026-07-28', raise_exceptions=True) as c:
            self.assertEqual((await self.call(c, 'settings'))['settings']['scope'], 'addressed')
            page = await self.call(c, 'wait', {'timeout': 0, 'limit': 1})
            self.assertEqual((page['messages'], page['next_after']), ([], 1))
            replay = page['thread_activity'][0]['replay']
            full = await self.call(c, replay['command'], replay['arguments'])
            self.assertEqual([m['id'] for m in full['messages']], [uid(10)])
            self.assertFalse(full['checkpoint_safe'])
            await self.call(c, 'list', {'thread': uid(100)}, error=True)
            await self.call(c, 'settings', {'scope': 'all', 'context': 'none'})
            self.assertEqual(self.store.settings()['scope'], 'all')
            self.assertEqual((await self.call(c, 'list', {'scope': 'addressed'}))['reading'],
                             {'scope': 'addressed', 'context': 'none'})
            await self.call(c, 'settings', {'scope': 'all', 'reset': True}, error=True)
            await self.call(c, 'list', {'after': 2, 'through': 1}, error=True)
            await self.call(c, 'settings', {'reset': True})
            self.assertEqual(self.store.settings()['context'], 'brief')

    async def test_cli_and_live_mcp_share_subscriptions_without_restart(self):
        cfg = {'postingboard': {**settings()['postingboard'], 'threads': []}}
        self.store.initialize(cfg)
        snapshots = []

        def client(adapter, runtime):
            snapshots.append(list(runtime['subscriptions']))
            return FixtureClient(adapter, runtime)

        def cli(command):
            result = subprocess.run([sys.executable, '-m', 'boardmail', '--db', str(self.path),
                                     command, 'postingboard', uid(302)], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return json.loads(result.stdout)

        with patch.object(providers, 'Client', side_effect=client):
            async with Client(create_server(self.store, cfg), mode='2026-07-28', raise_exceptions=True) as c:
                self.assertEqual((await self.call(c, 'subscriptions'))['subscriptions'], [])
                self.assertTrue((await asyncio.to_thread(cli, 'subscribe'))['changed'])
                selected = await self.call(c, 'subscriptions', {'source': 'postingboard'})
                self.assertEqual(selected['subscriptions'][0]['thread'], uid(302))
                target = {'source': 'postingboard', 'thread': uid(302)}
                self.assertFalse((await self.call(c, 'subscribe', target))['changed'])
                page = await self.call(c, 'check')
                self.assertEqual(([m['id'] for m in page['messages']], page['thread_activity'][0]['count']), ([uid(314)], 1))
                self.assertFalse((await self.call(c, 'unsubscribe', target))['subscribed'])
                self.assertEqual(self.store.subscriptions(), [])
                self.assertEqual((await self.call(c, 'collect'))['added'], 0)
                self.assertEqual([m['id'] for m in (await self.call(c, 'list', {'scope': 'all'}))['messages']], [uid(314), uid(315)])
        self.assertEqual(snapshots, [[uid(302)], []])

    async def test_collection_partial_success_replay_and_account_isolation(self):
        cfg = settings()
        self.store.initialize(cfg)
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
        self.store.save('postingboard', cfg['postingboard']['account_id'], [
            {**mail(610), 'thread_id': uid(600), 'parent_id': uid(601)}, mail(611)])
        self.store.mark('postingboard', uid(611), 'replied', ref=providers.HOSTS['postingboard'] + '/v1/posts/' + uid(601))
        fixture = FixtureClient('postingboard', cfg['postingboard'])
        fixture.others = {uid(600): named(600, 600), uid(601): named(601, 600, 3, body='Our previous reply.'),
                          uid(610): named(610, 600, reply_to=601, body='Edited reply text')}
        before = self.path.read_bytes()
        with patch('boardmail.providers.Client', return_value=fixture):
            async with Client(create_server(self.store, cfg), mode='2026-07-28', raise_exceptions=True) as c:
                target = {'source': 'postingboard', 'id': uid(610)}
                result = await self.call(c, 'context', target)
                self.assertEqual(result['target']['message']['body'], 'Synthetic text')
                self.assertEqual(result['target']['current_message']['body'], 'Edited reply text')
                self.assertIs(result['target']['differs_from_saved'], True)
                self.assertEqual(result['parent']['message']['body'], 'Our previous reply.')
                self.assertEqual(result['previous_exchange']['status'], 'linked')
                self.assertEqual([m['id'] for m in result['previous_exchange']['messages']], [uid(611)])
                local = await self.call(c, 'context', {**target, 'local': True}, error=True)
                self.assertEqual((local['fetched'], local['complete'], local['parent']['status']), (False, False, 'unknown'))
                self.assertIsNone(local['target']['current_message'])
                self.assertIsNone(local['target']['differs_from_saved'])
                self.assertEqual(len(fixture.calls), 3)
                expanded = await self.call(c, 'expand', {'source': 'postingboard', 'thread': uid(600), 'through': 1})
                self.assertEqual((expanded['event'], expanded['complete'], expanded['fetched'], expanded['checkpoint_safe']),
                                 ('expanded', True, True, False))
                self.assertEqual((expanded['after'], expanded['through'], expanded['next_after'], expanded['more']), (0, 1, 1, False))
                self.assertEqual((expanded['next_action'], expanded['collection_performed'], expanded['budget_exhausted']),
                                 ('process_filtered_page_keep_delivery_checkpoint', False, False))
                self.assertEqual(expanded['root']['id'], uid(600))
                item, = expanded['items']
                self.assertEqual((item['id'], item['arrival_seq'], item['complete']), (uid(610), 1, True))
                self.assertEqual(item['target'], result['target'])
                self.assertEqual(item['parent']['message']['body'], 'Our previous reply.')
                self.assertEqual(item['previous_exchange'], result['previous_exchange'])
                self.assertEqual(len(fixture.calls), 6)
                partial = await self.call(c, 'expand', {'source': 'postingboard', 'thread': uid(600), 'through': 1, 'local': True}, error=True)
                self.assertEqual((partial['fetched'], partial['complete'], partial['items'][0]['parent']['status']), (False, False, 'unknown'))
        self.assertEqual(len(fixture.calls), 6)
        self.assertEqual(self.path.read_bytes(), before)

    async def test_pause_is_shared_with_cli_without_restarting_server(self):
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
                self.assertEqual(len((await c.list_tools()).tools),21)
                self.assertEqual((await self.call(c,'wait',{'timeout':0}))['event'],'timeout')

    async def test_cancelled_collection_finishes_before_next_collection(self):
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
