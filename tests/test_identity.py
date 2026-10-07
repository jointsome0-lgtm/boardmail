"""Credential rotation must not mix mail or checkpoints between accounts."""
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlsplit

from boardmail import config, providers
from boardmail.store import Store
from examples.fixtures import FakeBoard, FixtureBoard, settings, uid
from kit import Clock, fixed, mark, new_inbox


class IdentityTests(unittest.TestCase):
    def test_replacing_a_key_rechecks_identity_before_inbox_or_cursor_access(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            key = root / 'key'
            key.write_text('synthetic-A')
            cfg = {'adapter': 'postingboard', 'account_id': uid(1), 'api_key_file': key,
                   'inbox': True, 'threads': []}
            store = new_inbox(root / 'mail.sqlite3', {'account-a': cfg})
            calls, meanwhile = [], []

            def answer(asked):
                """A board that knows two accounts, each by its key."""
                path = urlsplit(asked.url).path
                calls.append(path)
                if path == '/v1/inbox' and meanwhile:
                    meanwhile.pop()()
                account, seq = (uid(1), 10) if asked.headers == {'Authorization': 'Bearer synthetic-A'} else (uid(2), 20)
                if path == '/v1/me': return {'id': account}
                if path == '/v1/inbox':
                    after = int(asked.params['after'])
                    return {'items': [] if after >= seq else [
                        {'id': uid(seq), 'root_id': uid(100), 'seq': seq, 'reasons': ['mention']}],
                        'resume_after': max(after, seq), 'next_after': None}
                return {'post': {'id': uid(seq), 'root_id': uid(100), 'seq': seq,
                        'agent_id': uid(3), 'body': 'A public reply.', 'created_at': 1}}

            board = FakeBoard(answer)
            with fixed(Clock(1_000_000)):  # The wait of the client between two requests only moves the clock.
                self.assertEqual(providers.collect_all(store, {'account-a': cfg}, fetch=board)['added'], 1)
                mark(store, 'account-a', uid(10), 'needs_reply')
                row = store.show('account-a', uid(10))
                before = store.collection_state('account-a', uid(1), 'postingboard')
                key.write_text('synthetic-B'); calls.clear()
                result = providers.collect_all(store, {'account-a': cfg}, fetch=board)
                self.assertEqual((result['added'], result['errors'][0]['error']), (0, 'account_mismatch'))
                self.assertEqual(calls, ['/v1/me'])
                self.assertEqual(store.collection_state('account-a', uid(1), 'postingboard'), before)
                self.assertEqual(store.show('account-a', uid(10)), row)
                # A valid pass started before the key failed still owns its checkpoint revision.
                key.write_text('synthetic-A')
                def key_fails():
                    key.write_text('synthetic-B')
                    failed.append(providers.collect_all(Store(store.path), {'account-a': cfg}, fetch=board))
                    key.write_text('synthetic-A')
                failed = []
                meanwhile.append(key_fails)
                self.assertFalse(providers.collect_all(store, {'account-a': cfg}, fetch=board)['failed'])
                self.assertEqual(failed[0]['errors'][0]['error'], 'account_mismatch')
                self.assertEqual(store.collection_state('account-a', uid(1), 'postingboard')[2], before[2] + 1)
                self.assertFalse(providers.collect_all(store, {'account-a': cfg}, fetch=board)['failed'])

    def test_every_authenticated_legacy_adapter_fails_closed_and_other_sources_continue(self):
        for source, cfg in settings().items():
            for profile, error in (({'id': uid(99)}, 'account_mismatch'), ({}, 'invalid_response')):
                with self.subTest(source=source, profile=profile), tempfile.TemporaryDirectory() as folder:
                    cfg = {**cfg, 'adapter': source, 'api_key_file': Path(folder) / 'alias.key'}
                    cfg['api_key_file'].write_text('the-key-of-the-alias')
                    store = new_inbox(Path(folder) / 'mail.sqlite3', {'alias': cfg})
                    board = FixtureBoard(source, cfg)
                    board.key = 'the-key-of-the-alias'
                    with fixed(Clock(900_000)):  # A pass that went well leaves its position and its time.
                        self.assertFalse(providers.collect_all(store, {'alias': cfg}, fetch=board)['failed'])
                    state, revision = store.collection_state('alias', cfg['account_id'], source)[1:]
                    last_ok = store.status()['sources'][0]['last_ok']
                    board.calls.clear()
                    endpoint = '/v1/me' if source == 'postingboard' else '/agents/me'
                    def get(path, params=None, *, authenticated=False):
                        board.calls.append((path, params, authenticated))
                        self.assertEqual((path, authenticated), (endpoint, True))
                        return {'agent': profile} if source == 'moltbook' else profile
                    board.get = get
                    good = settings(folder)['postingboard']
                    other = FixtureBoard('postingboard', good)
                    def fetch(name, url, *, headers=None, **asks):
                        # Two accounts can be on one board. It tells them apart by the key.
                        ours = name == source and (name != 'postingboard' or headers == {'Authorization': 'Bearer the-key-of-the-alias'})
                        return (board if ours else other)(name, url, headers=headers, **asks)
                    with fixed(Clock(1_000_000)):  # The wait of the client of Postingboard only moves the clock.
                        result = providers.collect_all(store, {'alias': cfg, 'good': {**good, 'adapter': 'postingboard'}}, fetch=fetch)
                    self.assertEqual(result['errors'], [{'source': 'alias', 'error': error,
                        'next_action': 'restore_source_identity_or_use_a_new_source' if error == 'account_mismatch' else 'retry_collect'}])
                    self.assertGreater(result['added'], 0)
                    self.assertEqual(len(board.calls), 1)
                    self.assertEqual(store.collection_state('alias', cfg['account_id'], source)[1:], (state, revision))
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
