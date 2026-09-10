"""Colony auth through the normal client and collector, without network access."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from boardmail import config, providers
from boardmail.adapters import collect_all, next_action
from boardmail.store import Store
from examples.fixtures import FixtureClient, settings, uid


class ColonyAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.key = self.root / 'api.key'
        self.key.write_text('synthetic-api-key')
        self.secret = self.root / 'totp.key'
        self.secret.write_text('GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ\n')
        self.settings = {**settings()['the-colony'], 'api_key_file': self.key}

    def client(self, *, totp=False):
        cfg = {**self.settings}
        if totp:
            cfg['totp_secret_file'] = self.secret
        return providers.Client('the-colony', cfg)

    def test_auth_body_uses_rfc_vectors_and_reuses_only_the_jwt_in_memory(self):
        # RFC 6238 Appendix B SHA-1 results, truncated to six digits.
        for stamp, code in ((59, '287082'), (1111111109, '081804'), (20000000000, '353130'), (59, None)):
            with self.subTest(stamp=stamp, code=code):
                client, calls = self.client(totp=code is not None), []
                def respond(request, timeout):
                    calls.append(request)
                    return io.BytesIO(b'{"access_token":"synthetic-jwt"}' if request.data else b'{}')
                with patch.object(client.opener, 'open', side_effect=respond), patch.object(providers.time, 'time', return_value=stamp):
                    client.get('/notifications', authenticated=True)
                    client.get('/notifications', {'offset': 100}, authenticated=True)
                    client.get('/posts/' + uid(101))
                expected = {'api_key': 'synthetic-api-key'}
                if code is not None:
                    expected['totp_code'] = code
                self.assertEqual([json.loads(r.data) for r in calls if r.data], [expected])
                self.assertTrue(calls[0].full_url.endswith('/api/v1/auth/token'))
                self.assertEqual([r.get_header('Authorization') for r in calls],
                                 [None, 'Bearer synthetic-jwt', 'Bearer synthetic-jwt', None])
                self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['api.key', 'totp.key'])

    def test_required_factor_preserves_health_and_other_sources_continue(self):
        for status in (400, 401, 403):
            with self.subTest(status=status):
                store = Store(self.root / f'health-{status}.sqlite3')
                sources = {'the-colony': self.settings, 'moltbook': settings()['moltbook']}
                store.initialize(sources)
                store.save('the-colony', uid(1), [], now=123)
                requests = []
                def factory(source, cfg):
                    if source != 'the-colony':
                        return FixtureClient(source, cfg)
                    client = providers.Client(source, cfg)
                    def reject(request, timeout):
                        requests.append(request)
                        raise HTTPError(request.full_url, status, 'SYNTHETIC-SECRET', {}, io.BytesIO(
                            b'{"detail":{"code":"AUTH_2FA_REQUIRED","message":"SYNTHETIC-SECRET"}}'))
                    client.opener.open = reject
                    return client
                result = collect_all(store, sources, client_factory=factory)
                self.assertEqual(result['errors'], [{'source': 'the-colony', 'error': 'auth_2fa_required',
                                                    'next_action': 'configure_colony_totp_secret_file'}])
                colony = next(s for s in result['sources'] if s['source'] == 'the-colony')
                self.assertEqual(colony['last_ok'], 123)
                self.assertGreater(result['added'], 0)
                self.assertEqual(len(requests), 1)
                self.assertTrue(requests[0].full_url.endswith('/auth/token'))
                self.assertNotIn('SYNTHETIC-SECRET', json.dumps(result))
                self.assertNotIn(b'SYNTHETIC-SECRET', store.path.read_bytes())

    def test_allowlist_classifies_auth_responses_without_retry_or_prose(self):
        cases = [
            ({'detail': {'code': 'AUTH_2FA_INVALID', 'message': 'SYNTHETIC-SECRET'}}, 'auth_2fa_invalid'),
            ({'detail': {'code': 'AUTH_INVALID_TOKEN'}}, 'auth_invalid_token'),
            ({'detail': {'code': 'AUTH_TOKEN_REVOKED'}}, 'auth_token_revoked'),
            ({'detail': {'code': 'AUTH_PENDING_ACTIVATION'}}, 'auth_pending_activation'),
            ({'detail': {'code': 'AUTH_UNKNOWN_SYNTHETIC_SECRET'}}, 'http_401'),
            ({'detail': {'code': ['AUTH_2FA_REQUIRED']}}, 'http_401'),
            ({'detail': 'SYNTHETIC-SECRET'}, 'http_401'),
            (['SYNTHETIC-SECRET'], 'http_401'),
            (b'<html>SYNTHETIC-SECRET</html>', 'http_401'),
        ]
        for data, expected in cases:
            with self.subTest(expected=expected, data=data):
                client = self.client(totp=True)
                raw = data if isinstance(data, bytes) else json.dumps(data).encode()
                body = io.BytesIO(raw)
                exc = HTTPError('https://thecolony.ai/api/v1/auth/token', 401, 'SYNTHETIC-SECRET', {}, body)
                with patch.object(client.opener, 'open', side_effect=exc) as request:
                    try:
                        client.get('/notifications', authenticated=True)
                    except Exception as error:
                        code = providers.error_code(error)
                    else:
                        self.fail('Failed authentication was accepted')
                self.assertEqual(code, expected)
                self.assertEqual(request.call_count, 1)
                self.assertTrue(body.closed)
                self.assertNotIn('SYNTHETIC-SECRET', code)
        self.assertEqual(next_action('auth_2fa_invalid'), 'check_totp_secret_and_system_clock')
        self.assertEqual(next_action('auth_token_revoked'), 'check_config_and_credentials')

    def test_error_read_is_bounded_and_applies_only_to_colony_auth(self):
        raw = json.dumps({'detail': {'code': 'AUTH_2FA_REQUIRED', 'message': 'x' * 10000}}).encode()
        body = io.BytesIO(raw)
        client = self.client()
        with patch.object(body, 'read', wraps=body.read) as read, patch.object(client.opener, 'open', side_effect=
                HTTPError('https://thecolony.ai/api/v1/auth/token', 401, '', {}, body)):
            with self.assertRaises(HTTPError) as raised:
                client.get('/notifications', authenticated=True)
            self.assertEqual(providers.error_code(raised.exception), 'http_401')
            read.assert_called_once_with(providers.MAX_AUTH_ERROR_BYTES + 1)
        raw = b'{"detail":{"code":"AUTH_2FA_REQUIRED"}}'
        for source, authenticated, status in (('moltbook', True, 401), ('the-colony', False, 401), ('the-colony', True, 429)):
            with self.subTest(source=source, authenticated=authenticated, status=status):
                client = providers.Client(source, self.settings)
                with patch.object(client.opener, 'open', side_effect=HTTPError('https://example.invalid', status, '', {}, io.BytesIO(raw))):
                    with self.assertRaises(HTTPError) as raised:
                        client.get('/posts/' + uid(101), authenticated=authenticated)
                    self.assertEqual(providers.error_code(raised.exception), 'http_' + str(status))
        client = self.client()
        client.token = 'synthetic-jwt'
        with patch.object(client.opener, 'open', side_effect=HTTPError('https://thecolony.ai', 403, '', {}, io.BytesIO(raw))):
            with self.assertRaisesRegex(config.MailError, '^auth_2fa_required$'):
                client.get('/notifications', authenticated=True)

    def test_unavailable_and_malformed_secrets_fail_before_network(self):
        for raw, expected in ((None, 'credentials_unavailable'), (b'\n', 'credentials_unavailable'),
                              (b'bad-BASE32-secret', 'invalid_totp_secret'), (b'\xff', 'invalid_totp_secret'),
                              (b'A' * 200, 'invalid_totp_secret')):
            with self.subTest(raw=raw):
                self.secret.unlink(missing_ok=True)
                if raw is not None:
                    self.secret.write_bytes(raw)
                client = self.client(totp=True)
                with patch.object(client.opener, 'open') as network:
                    with self.assertRaisesRegex(config.MailError, '^' + expected + '$'):
                        client.get('/notifications', authenticated=True)
                    network.assert_not_called()

    def test_configuration_resolves_secret_for_renamed_colony_source(self):
        path = self.root / 'config.json'
        def write(adapter='the-colony', secret='totp.key'):
            path.write_text(json.dumps({'database': 'mail.sqlite3', 'sources': {'mail': {
                'adapter': adapter, 'account_id': uid(1), 'api_key_file': 'api.key', 'totp_secret_file': secret}}}))
        write()
        self.assertEqual(config.load(path)['sources']['mail']['totp_secret_file'], self.secret)
        for adapter, secret in (('the-colony', ''), ('the-colony', True), ('the-colony', None), ('moltbook', 'totp.key')):
            with self.subTest(adapter=adapter, secret=secret):
                write(adapter, secret)
                with self.assertRaisesRegex(config.MailError, '^invalid_config$'):
                    config.load(path)


if __name__ == '__main__':
    unittest.main()
