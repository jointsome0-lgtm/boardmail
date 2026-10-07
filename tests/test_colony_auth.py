"""Colony sign-in through the collector, at the transport seam. The board is invented."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from boardmail import config, providers
from boardmail.adapters import collect_all, next_action
from examples.fixtures import FakeBoard, FixtureBoard, settings, together, uid
from kit import Clock, fixed, new_inbox


def refused(status, raw=b'{"detail":{"code":"AUTH_2FA_REQUIRED"}}'):
    return HTTPError('https://thecolony.ai/api/v1/auth/token', status, 'SYNTHETIC-SECRET', {}, io.BytesIO(raw))


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

    def collect(self, board, *, totp=False):
        """One pass over Colony, which is the board that is handed in."""
        cfg = {**self.settings, **({'totp_secret_file': self.secret} if totp else {})}
        return providers.collect('the-colony', cfg, {}, set(), fetch=board)

    def test_auth_body_uses_rfc_vectors_and_reuses_only_the_jwt_in_memory(self):
        # RFC 6238 Appendix B SHA-1 results, truncated to six digits.
        for stamp, code in ((59, '287082'), (1111111109, '081804'), (20000000000, '353130'), (59, None)):
            with self.subTest(stamp=stamp, code=code):
                board = FixtureBoard('the-colony', self.settings)
                board.key, board.totp = 'synthetic-api-key', code
                with fixed(Clock(stamp)):
                    batch = self.collect(board, totp=code is not None)
                self.assertEqual((batch.error, len(batch.messages)), (None, 2))
                expected = {'api_key': 'synthetic-api-key'}
                if code is not None:
                    expected['totp_code'] = code
                self.assertEqual([asked.body for asked in board.asked if asked.body], [expected])
                # The pass signs in once, asks twice as the account, and reads the two public originals as anyone.
                self.assertEqual([(asked.url.removeprefix('https://thecolony.ai/api/v1'), asked.headers.get('Authorization'))
                                  for asked in board.asked],
                                 [('/auth/token', None), ('/agents/me', 'Bearer invented-token'),
                                  ('/notifications?limit=100', 'Bearer invented-token'),
                                  ('/comments/' + uid(111), None), ('/comments/' + uid(112), None)])
                self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['api.key', 'totp.key'])

    def test_required_factor_preserves_health_and_other_sources_continue(self):
        for status in (400, 401, 403):
            with self.subTest(status=status):
                sources = {'the-colony': self.settings, 'moltbook': settings(self.root)['moltbook']}
                store = new_inbox(self.root / f'health-{status}.sqlite3', sources)
                healthy = FixtureBoard('the-colony', self.settings)
                healthy.key = 'synthetic-api-key'
                with fixed(Clock(123)):  # The last pass over Colony that went well.
                    self.assertFalse(collect_all(store, {'the-colony': self.settings}, fetch=healthy)['failed'])
                colony = FakeBoard(lambda asked: refused(
                    status, b'{"detail":{"code":"AUTH_2FA_REQUIRED","message":"SYNTHETIC-SECRET"}}'))
                boards = {'the-colony': colony, 'moltbook': FixtureBoard('moltbook', sources['moltbook'])}
                result = collect_all(store, sources, fetch=together(boards))
                self.assertEqual(result['errors'], [{'source': 'the-colony', 'error': 'auth_2fa_required',
                                                    'next_action': 'configure_colony_totp_secret_file'}])
                health = next(s for s in result['sources'] if s['source'] == 'the-colony')
                self.assertEqual(health['last_ok'], 123)
                self.assertGreater(result['added'], 0)
                self.assertEqual([asked.url for asked in colony.asked], ['https://thecolony.ai/api/v1/auth/token'])
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
                exc = refused(401, data if isinstance(data, bytes) else json.dumps(data).encode())
                board = FakeBoard([exc])  # The board has one answer. A second request would fail the test.
                batch = self.collect(board, totp=True)
                self.assertEqual((batch.error, batch.messages, batch.complete), (expected, [], False))
                self.assertEqual(len(board.asked), 1)
                self.assertTrue(exc.fp.closed)
        self.assertEqual(next_action('auth_2fa_invalid'), 'check_totp_secret_and_system_clock')
        self.assertEqual(next_action('auth_token_revoked'), 'check_config_and_credentials')

    def test_error_read_is_bounded_and_applies_only_to_colony_auth(self):
        body = io.BytesIO(json.dumps({'detail': {'code': 'AUTH_2FA_REQUIRED', 'message': 'x' * 10000}}).encode())
        with patch.object(body, 'read', wraps=body.read) as read:
            batch = self.collect(FakeBoard([HTTPError('https://thecolony.ai/api/v1/auth/token', 401, '', {}, body)]))
            self.assertEqual(batch.error, 'http_401')
            read.assert_called_once_with(providers.MAX_AUTH_ERROR_BYTES + 1)
        # What Colony says of a sign-in names no failure of another board, of a request that is not the one of
        # the account, or of a status that is no refusal.
        moltbook = settings(self.root)['moltbook']
        self.assertEqual(providers.collect('moltbook', moltbook, {}, set(), fetch=FakeBoard([refused(401)])).error, 'http_401')
        board = FixtureBoard('the-colony', self.settings)
        board.key, get = 'synthetic-api-key', board.get
        def public(path, params=None, **asks):
            if path.startswith('/comments/'): raise refused(401)
            return get(path, params, **asks)
        board.get = public
        self.assertEqual(self.collect(board).error, 'http_401')
        self.assertEqual(self.collect(FakeBoard([{'access_token': 'synthetic-jwt'}, refused(429)])).error, 'http_429')
        # A request with the token of the account is refused as the sign-in is.
        self.assertEqual(self.collect(FakeBoard([{'access_token': 'synthetic-jwt'}, refused(403)])).error, 'auth_2fa_required')

    def test_unavailable_and_malformed_secrets_fail_before_network(self):
        for raw, expected in ((None, 'credentials_unavailable'), (b'\n', 'credentials_unavailable'),
                              (b'bad-BASE32-secret', 'invalid_totp_secret'), (b'\xff', 'invalid_totp_secret'),
                              (b'A' * 200, 'invalid_totp_secret')):
            with self.subTest(raw=raw):
                self.secret.unlink(missing_ok=True)
                if raw is not None:
                    self.secret.write_bytes(raw)
                board = FakeBoard([])
                self.assertEqual(self.collect(board, totp=True).error, expected)
                self.assertEqual(board.asked, [])

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
