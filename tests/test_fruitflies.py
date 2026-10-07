import json
import unittest
from urllib.error import URLError
from uuid import UUID

from boardmail import adapter_fruitflies as fruit
from boardmail.adapters import validate
from examples.fixtures import FakeBoard, status


def post(n, body='hello', author='other', parent=None, kind='post'):
    return {'id': str(UUID(int=n)), 'content': body, 'agents': {'handle': author},
            'parent_id': str(UUID(int=parent)) if parent else None,
            'post_type': kind, 'created_at': '2026-09-07T10:00:00Z'}


def feed(*pages):
    """An invented board that answers each request with the next of these pages of posts. A page that is an
    exception is how that request fails."""
    return FakeBoard([page if isinstance(page, Exception) else {'posts': page} for page in pages])


class FruitfliesTests(unittest.TestCase):
    def collect(self, pages, state=None, known=frozenset()):
        board = feed(*pages)
        batch = fruit.collect({'account_id': 'alice'}, state or {}, known, fetch=board)
        validate(batch)
        return batch, board.asked

    def test_only_public_personal_originals_and_no_key(self):
        own = [post(1, author='alice', kind='question'), post(2, author='alice', kind='answer')]
        rows = [post(3, '@ALICE hello'), post(4, '@alice-more'), post(5, 'general feed'),
                post(6, parent=1, kind='answer'), post(7, parent=2, kind='answer'),
                post(8, '@alice', author='alice'), post(9, parent=999)]
        result, asked = self.collect([own, rows, []])
        self.assertEqual([m['kind'] for m in result.messages], ['mention', 'reply_to_post', 'reply_to_comment'])
        self.assertEqual([m['body'] for m in result.messages], ['@ALICE hello', 'hello', 'hello'])
        self.assertEqual({m['created_at'] for m in result.messages}, {1788775200})  # 2026-09-07T10:00:00Z
        self.assertEqual(result.state, {'offset': 100})
        self.assertTrue(result.complete)
        self.assertEqual({request.board for request in asked}, {'fruitflies'})
        self.assertEqual([request.url for request in asked], [fruit.BASE + '?' + query for query in (
            'agent=alice&limit=100&offset=0', 'limit=100&offset=0', 'limit=100&offset=100')])
        self.assertEqual([(request.headers, request.body) for request in asked], [({}, None)] * 3)

    def test_uppercase_account_uses_canonical_handle_for_parent_discovery(self):
        def answer(asked):
            if 'agent' in asked.params:
                return {'posts': [post(1, author='alice', kind='question')] if asked.params['agent'] == 'alice' else []}
            return {'posts': [post(2, parent=1, kind='answer')]}
        result = fruit.collect({'account_id': 'ALICE'}, {}, frozenset(), fetch=FakeBoard(answer))
        validate(result)
        self.assertEqual([m['kind'] for m in result.messages], ['reply_to_post'])

    def test_history_failure_keeps_position_and_fresh_discovery(self):
        result, _ = self.collect([[], [post(3, '@alice')], status(503)], {'offset': 400})
        self.assertEqual(len(result.messages), 1)
        self.assertEqual(result.state, {'offset': 400})
        self.assertFalse(result.complete)
        self.assertEqual(result.error, 'http_503')
        retry, _ = self.collect([[], [], [post(4, '@alice')]], result.state)
        self.assertEqual(retry.messages[0]['id'], post(4)['id'])
        self.assertTrue(retry.complete)

    def test_fresh_failure_does_not_starve_history(self):
        result, _ = self.collect([[], URLError('An invented outage'), [post(n, '@alice') for n in range(1,101)]])
        self.assertEqual(result.error, 'network_error')
        self.assertEqual(len(result.messages), 100)
        self.assertEqual(result.state, {'offset': 200})
        self.assertFalse(result.complete)

    def test_parent_failure_retains_reply_scan_and_mentions_survive(self):
        result, _ = self.collect([status(403), [post(3, '@alice')], [post(n) for n in range(100,200)]])
        self.assertEqual(result.error, 'http_403')
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
        result, asked = self.collect([[], [], []], {'offset': 'private notification'})
        self.assertEqual(asked[2].params['offset'], '100')
        # A full page moves the scan on. At the cap it starts over, and that is its end.
        result, _ = self.collect([[], [], [post(n) for n in range(1,101)]], {'offset': 300})
        self.assertEqual((result.state, result.complete), ({'offset': 400}, False))
        result, _ = self.collect([[], [], [post(n) for n in range(1,101)]], {'offset': 100000})
        self.assertEqual(result.state, {'offset': 100})
        self.assertTrue(result.complete)

    def test_an_answer_that_is_no_page_of_posts_is_no_mail(self):
        crowd = [post(n, '@alice') for n in range(1, 102)]  # One more than a page holds.
        for answer in ([], {}, {'posts': None}, {'posts': {'0': post(1, '@alice')}}, {'posts': crowd}, ValueError()):
            with self.subTest(answer=str(answer)[:40]):
                board = FakeBoard([{'posts': []}, answer, {'posts': [post(200, '@alice')]}])
                result = fruit.collect({'account_id': 'alice'}, {}, frozenset(), fetch=board)
                validate(result)
                self.assertEqual((result.error, result.complete), ('invalid_response', False))
                self.assertEqual([m['id'] for m in result.messages], [post(200)['id']], 'The other page is still read')

    def test_a_row_that_is_no_post_is_counted_and_the_rest_is_read(self):
        def bad(**change):
            return {**post(9, '@alice'), **change}
        rows = ['a row of text', bad(id=post(10)['id'].upper()), bad(parent_id=7), bad(post_type='poll'), bad(content=7),
                bad(agents={'handle': None}), bad(created_at='2026-09-07T10:00:00'), post(2, '@alice')]
        result, _ = self.collect([[], rows, []])
        self.assertEqual([m['id'] for m in result.messages], [post(2)['id']])
        self.assertEqual((result.unavailable, result.error, result.complete), (7, 'invalid_response', False))
        # In the page of our own posts such a row also keeps the place in the history, so that it is read again.
        result, _ = self.collect([['a row of text'], [post(2, '@alice')], []], {'offset': 400})
        self.assertEqual([m['id'] for m in result.messages], [post(2)['id']])
        self.assertEqual((result.unavailable, result.error, result.complete), (0, 'invalid_response', False))
        self.assertEqual(result.state, {'offset': 400})

    def test_a_bad_handle_or_subscription_is_a_config_error_and_asks_the_board_nothing(self):
        for settings in ({}, {'account_id': 'a handle'}, {'account_id': 'a' * 65},
                         {'account_id': 'alice', 'subscriptions': ['no id']}):
            with self.subTest(settings=settings):
                board = feed()
                result = fruit.collect(settings, {}, frozenset(), fetch=board)
                validate(result)
                self.assertEqual((result.error, result.complete, result.state, board.asked), ('invalid_config', False, {}, []))
