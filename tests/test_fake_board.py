"""The invented board that a test of a board hands in where the transport would ask."""
import unittest
from urllib.error import HTTPError

from examples.fixtures import FakeBoard, status

FEED = 'https://board.example.invalid/feed'


class FakeBoardTests(unittest.TestCase):
    def test_answers_come_in_order_and_each_request_is_kept(self):
        page = {'posts': [{'id': 1}]}
        board = FakeBoard([page, 'a page of text'])
        first = board('moltbook', FEED + '?limit=100&offset=0')
        self.assertEqual(first, page)
        first['posts'].clear()
        self.assertEqual(page, {'posts': [{'id': 1}]}, 'What a caller does to an answer stays with the caller')
        second = board('botnet', FEED, left=3, headers={'Authorization': 'Bearer invented'}, body={'code': '1'})
        self.assertEqual(second, 'a page of text')
        self.assertEqual([tuple(asked) for asked in board.asked],
                         [('moltbook', FEED + '?limit=100&offset=0', {}, None, None),
                          ('botnet', FEED, {'Authorization': 'Bearer invented'}, {'code': '1'}, 3)])
        self.assertEqual(board.asked[0].params, {'limit': '100', 'offset': '0'})
        with self.assertRaisesRegex(AssertionError, 'no answer left'):
            board('moltbook', FEED)
        self.assertEqual(len(board.asked), 3, 'The request that got no answer is kept as well')

    def test_an_answer_that_is_an_exception_is_raised(self):
        board = FakeBoard(lambda asked: status(503, b'busy') if 'offset' in asked.params else OSError('An invented outage'))
        with self.assertRaises(HTTPError) as raised:
            board('moltbook', FEED + '?offset=0')
        self.assertEqual((raised.exception.code, raised.exception.read()), (503, b'busy'))
        with self.assertRaises(OSError):
            board('moltbook', FEED)
        self.assertEqual(len(board.asked), 2)


if __name__ == '__main__':
    unittest.main()
