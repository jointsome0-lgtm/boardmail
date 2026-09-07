import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from uuid import UUID

from boardmail import adapter_fruitflies as fruit
from boardmail.adapters import validate


def post(n, body='hello', author='other', parent=None, kind='post'):
    return {'id': str(UUID(int=n)), 'content': body, 'agents': {'handle': author},
            'parent_id': str(UUID(int=parent)) if parent else None,
            'post_type': kind, 'created_at': '2026-09-07T10:00:00Z'}


class FruitfliesTests(unittest.TestCase):
    def collect(self, pages, state=None, known=frozenset()):
        with patch.object(fruit, '_fetch', side_effect=pages) as fetch:
            batch = fruit.collect({'account_id': 'alice'}, state or {}, known)
        validate(batch)
        return batch, fetch.call_args_list

    def test_only_public_personal_originals_and_no_key(self):
        own = [post(1, author='alice', kind='question'), post(2, author='alice', kind='answer')]
        rows = [post(3, '@ALICE hello'), post(4, '@alice-more'), post(5, 'general feed'),
                post(6, parent=1, kind='answer'), post(7, parent=2, kind='answer'),
                post(8, '@alice', author='alice'), post(9, parent=999)]
        result, calls = self.collect([own, rows, []])
        self.assertEqual([m['kind'] for m in result.messages], ['mention', 'reply_to_post', 'reply_to_comment'])
        self.assertEqual([m['body'] for m in result.messages], ['@ALICE hello', 'hello', 'hello'])
        self.assertEqual(result.state, {'offset': 100})
        self.assertTrue(result.complete)
        self.assertEqual(calls[0].args[0], {'agent': 'alice', 'limit': 100, 'offset': 0})

    def test_history_failure_keeps_position_and_fresh_discovery(self):
        result, _ = self.collect([[], [post(3, '@alice')], fruit.FetchError('http_503')], {'offset': 400})
        self.assertEqual(len(result.messages), 1)
        self.assertEqual(result.state, {'offset': 400})
        self.assertFalse(result.complete)
        self.assertEqual(result.error, 'http_503')
        retry, _ = self.collect([[], [], [post(4, '@alice')]], result.state)
        self.assertEqual(retry.messages[0]['id'], post(4)['id'])
        self.assertTrue(retry.complete)

    def test_fresh_failure_does_not_starve_history(self):
        result, _ = self.collect([[], fruit.FetchError('network_error'), [post(n, '@alice') for n in range(1,101)]])
        self.assertEqual(len(result.messages), 100)
        self.assertEqual(result.state, {'offset': 200})
        self.assertFalse(result.complete)

    def test_parent_failure_retains_reply_scan_and_mentions_survive(self):
        result, _ = self.collect([fruit.FetchError('http_403'), [post(3, '@alice')], [post(n) for n in range(100,200)]])
        self.assertEqual(result.state, {'offset': 100})
        self.assertEqual(len(result.messages), 1)

    def test_replay_known_and_malformed_rows(self):
        bad = post(9, '@alice'); bad['created_at'] = 'bad'
        result, _ = self.collect([[], [post(1, '@alice'), post(2, '@alice'), bad], [post(2, '@alice')]],
                                 known={post(1)['id']})
        self.assertEqual([m['id'] for m in result.messages], [post(2)['id']])
        self.assertEqual(result.unavailable, 1)
        self.assertEqual(result.error, 'invalid_response')
        self.assertNotIn('content', json.dumps(result.state))

    def test_invalid_cursor_resets_and_scan_cap_wraps(self):
        result, calls = self.collect([[], [], []], {'offset': 'private notification'})
        self.assertEqual(calls[2].args[0]['offset'], 100)
        result, _ = self.collect([[], [], [post(n) for n in range(1,101)]], {'offset': fruit.MAX_OFFSET})
        self.assertEqual(result.state, {'offset': 100})
        self.assertTrue(result.complete)

    def test_transport_fixed_origin_no_credentials_and_redirect_rejected(self):
        class Response(io.BytesIO):
            pass
        with patch.object(fruit, 'build_opener') as build:
            build.return_value.open.return_value = Response(b'{"posts": []}')
            self.assertEqual(fruit._fetch({'limit': 100}), [])
            req = build.return_value.open.call_args.args[0]
            self.assertEqual(req.full_url, 'https://api.fruitflies.ai/v1/feed?limit=100')
            self.assertNotIn('Authorization', req.headers)
            self.assertIsNone(fruit.NoRedirect().redirect_request(req, None, 302, '', {}, 'https://evil.invalid'))
            build.return_value.open.side_effect = HTTPError(req.full_url, 403, 'secret', {}, None)
            with self.assertRaisesRegex(fruit.FetchError, '^http_403$'):
                fruit._fetch({})

    def test_transport_size_and_schema_limits(self):
        for body, code in [(b' ' * (fruit.MAX_BYTES + 1), 'response_too_large'),
                           (b'{"messages": ["private"]}', 'invalid_response')]:
            with patch.object(fruit, 'build_opener') as build:
                build.return_value.open.return_value = io.BytesIO(body)
                with self.assertRaisesRegex(fruit.FetchError, '^' + code + '$'):
                    fruit._fetch({})
