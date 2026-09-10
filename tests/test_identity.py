"""Credential rotation must not mix mail or checkpoints between accounts."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from boardmail import config, providers
from boardmail.adapters import Batch
from boardmail.store import Store
from examples.fixtures import FixtureClient, settings, uid


class IdentityTests(unittest.TestCase):
    def test_replacing_a_key_rechecks_identity_before_inbox_or_cursor_access(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            key = root / 'key'
            key.write_text('synthetic-A')
            cfg = {'adapter': 'postingboard', 'account_id': uid(1), 'api_key_file': key,
                   'inbox': True, 'threads': []}
            store = Store(root / 'mail.sqlite3'); store.initialize({'account-a': cfg})
            calls = []

            def request(client, path, *, token=None, body=None):
                path, params = urlsplit(path).path, parse_qs(urlsplit(path).query)
                calls.append(path)
                account, seq = (uid(1), 10) if token == 'synthetic-A' else (uid(2), 20)
                if path == '/v1/me': return {'id': account}
                if path == '/v1/inbox':
                    after = int(params['after'][0])
                    return {'items': [] if after >= seq else [
                        {'id': uid(seq), 'root_id': uid(100), 'seq': seq, 'reasons': ['mention']}],
                        'resume_after': max(after, seq), 'next_after': None}
                return {'post': {'id': uid(seq), 'root_id': uid(100), 'seq': seq,
                        'agent_id': uid(3), 'body': 'A public reply.', 'created_at': 1}}

            with patch.object(providers.Client, '_request', request):
                self.assertEqual(providers.collect_all(store, {'account-a': cfg})['added'], 1)
                store.mark('account-a', uid(10), 'needs_reply')
                row = store.show('account-a', uid(10))
                before = store.collection_state('account-a', uid(1), 'postingboard')[:2]
                key.write_text('synthetic-B'); calls.clear()
                result = providers.collect_all(store, {'account-a': cfg})
                self.assertEqual((result['added'], result['errors'][0]['error']), (0, 'account_mismatch'))
                self.assertEqual(calls, ['/v1/me'])
                self.assertEqual(store.collection_state('account-a', uid(1), 'postingboard')[:2], before)
                self.assertEqual(store.show('account-a', uid(10)), row)
                key.write_text('synthetic-A')
                self.assertFalse(providers.collect_all(store, {'account-a': cfg})['failed'])

    def test_every_authenticated_legacy_adapter_fails_closed_and_other_sources_continue(self):
        for source, cfg in settings().items():
            for profile, error in (({'id': uid(99)}, 'account_mismatch'), ({}, 'invalid_response')):
                with self.subTest(source=source, profile=profile), tempfile.TemporaryDirectory() as folder:
                    cfg = {**cfg, 'adapter': source}
                    store = Store(Path(folder) / 'mail.sqlite3'); store.initialize({'alias': cfg})
                    state = {'threads': {uid(301): 20}, 'discovery': {'offset': 100}, 'pending': {}}
                    store.prepare_collection()
                    store.save_collection('alias', cfg['account_id'], source, 0, Batch(state=state))
                    last_ok = store.status()['sources'][0]['last_ok']
                    client = FixtureClient(source, cfg)
                    endpoint = '/v1/me' if source == 'postingboard' else '/agents/me'
                    def get(path, params=None, *, authenticated=False):
                        client.calls.append((path, params, authenticated))
                        self.assertEqual((path, authenticated), (endpoint, True))
                        return {'agent': profile} if source == 'moltbook' else profile
                    client.get = get
                    good = settings()['postingboard']
                    result = providers.collect_all(store, {'alias': cfg, 'good': {**good, 'adapter': 'postingboard'}},
                        client_factory=lambda adapter, conf: client if conf is cfg else FixtureClient(adapter, conf))
                    self.assertEqual(result['errors'], [{'source': 'alias', 'error': error,
                        'next_action': 'restore_source_identity_or_use_a_new_source' if error == 'account_mismatch' else 'retry_collect'}])
                    self.assertGreater(result['added'], 0)
                    self.assertEqual(len(client.calls), 1)
                    self.assertEqual(store.collection_state('alias', cfg['account_id'], source)[1], state)
                    self.assertEqual(next(s for s in store.status()['sources'] if s['source'] == 'alias')['last_ok'], last_ok)


class ConfigFieldsTests(unittest.TestCase):
    def test_builtin_typos_and_settings_for_other_adapters_are_rejected(self):
        for adapter in config.SOURCE_FIELDS:
            base = {'adapter': adapter, 'account_id': uid(1)}
            if adapter in config.LEGACY_ADAPTERS: base['api_key_file'] = 'unused.key'
            if adapter == 'postingboard': base['inbox'] = True
            for key in ('mention_mode', 'unrecognized_setting'):
                with self.subTest(adapter=adapter, key=key), tempfile.TemporaryDirectory() as folder:
                    path = Path(folder) / 'config.json'
                    data = {'database': 'mail.sqlite3', 'sources': {'alias': {**base, key: 'bare'}}}
                    path.write_text(json.dumps(data))
                    with self.assertRaisesRegex(config.MailError, '^invalid_config$'): config.load(path)
                    del data['sources']['alias'][key]
                    path.write_text(json.dumps(data))
                    self.assertEqual(config.load(path)['sources']['alias']['adapter'], adapter)

    def test_custom_adapter_keeps_its_options(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.json'
            path.write_text(json.dumps({'database': 'mail.sqlite3', 'sources': {'fourclaw': {
                'adapter': 'custom.py', 'account_id': 'example', 'mention_mode': 'bare',
                'provider_options': {'limit': 8}}}}))
            cfg = config.load(path)['sources']['fourclaw']
            self.assertEqual(cfg['mention_mode'], 'bare')
            self.assertEqual(cfg['provider_options'], {'limit': 8})


if __name__ == '__main__':
    unittest.main()
