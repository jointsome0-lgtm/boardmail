"""Collection admission and HTTP completion are separate, on invented boards and clocks only."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlsplit

from boardmail.boards import BOARDS
from boardmail.errors import MailError
from examples.fixtures import FakeBoard, FixtureBoard, named, original, settings, uid
from kit import Clock, Network, Pieces, fixed


class PhaseDeadlineTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.clock = Clock(1_790_000_000)
        self.enterContext(fixed(self.clock))

    def collect(self, source, cfg, fetch=None, state=None, known=()):
        return BOARDS[source].collect(cfg, deepcopy(state or {}), set(known),
                                      **({'fetch': fetch} if fetch is not None else {}))

    def test_last_admitted_notification_answer_finishes_and_next_original_waits(self):
        cfg = settings(self.folder)['moltbook']
        roots = [uid(n) for n in range(600, 604)]
        requests = []
        turn = [0]

        def answer(asked):
            path = urlsplit(asked.url).path.removeprefix('/api/v1')
            if path == '/agents/me':
                return {'agent': {'id': cfg['account_id']}}
            if path == '/notifications':
                return {'notifications': [
                    {'type': 'post_comment', 'relatedPostId': root, 'relatedCommentId': uid(n + 10)}
                    for n, root in zip(range(600, 604), roots)], 'has_more': False}
            root = path.split('/')[2]
            requests.append((root, path.endswith('/comments'), self.clock.now, asked.left))
            turn[0] += 1
            # Five ordinary answers leave four milliseconds in the phase. The sixth arrives 600 ms later.
            self.clock.advance(8.9992 if turn[0] <= 5 else 0.6)
            if path.endswith('/comments'):
                return {'comments': [original(int(root.replace('-', ''), 16) + 10,
                                              int(root.replace('-', ''), 16))], 'has_more': False}
            return {'post': {**original(int(root.replace('-', ''), 16), int(root.replace('-', ''), 16)),
                             'title': 'Synthetic root'}}

        board = FakeBoard(answer)
        first = self.collect('moltbook', cfg, board)
        self.assertIsNone(first.error)
        self.assertFalse(first.complete)
        self.assertEqual({item['id'] for item in first.messages}, {uid(n) for n in (610, 611, 612)})
        self.assertEqual(list(first.state['pending']), [roots[-1]])
        self.assertAlmostEqual(requests[-1][2] - requests[0][2], 44.996, places=3)
        # A request has what is left of its phase, and never under ten seconds.
        self.assertEqual([round(request[3], 3) for request in requests], [45, 36.001, 27.002, 18.002, 10, 10])
        self.assertEqual({item['id'] for item in first.originals}, set(roots[:3]),
                         'The completed roots stay verified alongside the delivered replies')
        second = self.collect('moltbook', cfg, board, first.state, {item['id'] for item in first.messages})
        self.assertIsNone(second.error)
        self.assertEqual([item['id'] for item in second.messages], [uid(613)])
        self.assertEqual(second.state['pending'], {})
        self.assertEqual(requests[6][0], roots[-1], 'The deferred original gets the next turn')

    def test_subscription_keeps_completed_page_and_rotates_before_resuming_cursor(self):
        cfg = {**settings(self.folder)['moltbook'], 'subscriptions': [uid(700), uid(701)]}
        slow = [True]
        calls = []

        def answer(asked):
            path = urlsplit(asked.url).path.removeprefix('/api/v1')
            if path == '/agents/me':
                return {'agent': {'id': cfg['account_id']}}
            if path == '/notifications':
                return {'notifications': [], 'has_more': False}
            root = path.split('/')[2]
            calls.append((root, dict(asked.params)))
            if path.endswith('/comments'):
                cursor = int(asked.params.get('cursor', '0'))
                if slow[0]:
                    self.clock.advance(9)
                return {'comments': [original(710 + cursor, 700)], 'has_more': cursor < 4,
                        'next_cursor': str(cursor + 1)} if root == uid(700) else {
                            'comments': [original(720, 701)], 'has_more': False}
            if slow[0]:
                self.clock.advance(9)
            return {'post': {**original(int(root.replace('-', ''), 16), int(root.replace('-', ''), 16)),
                             'title': 'Synthetic root'}}

        board = FakeBoard(answer)
        first = self.collect('moltbook', cfg, board)
        self.assertFalse(first.complete)
        self.assertIsNone(first.error)
        self.assertEqual({item['id'] for item in first.messages}, {uid(n) for n in range(710, 714)})
        self.assertEqual(first.state['subscriptions']['roots'][uid(700)]['cursor'], '4')
        self.assertEqual(first.state['subscriptions']['next'], uid(701))
        slow[0] = False
        calls.clear()
        second = self.collect('moltbook', cfg, board, first.state, {item['id'] for item in first.messages})
        self.assertTrue(second.complete)
        self.assertIsNone(second.error)
        self.assertEqual(calls[0][0], uid(701), 'A long thread does not starve the next subscribed root')
        self.assertIn((uid(700), {'limit': '100', 'cursor': '4', 'sort': 'old'}), calls)
        self.assertEqual({item['id'] for item in second.messages}, {uid(714), uid(720)})
        self.assertNotIn('cursor', second.state['subscriptions']['roots'][uid(700)])

    def test_postingboard_discovery_keeps_cursor_and_completed_originals_at_cutoff(self):
        cfg = {**settings(self.folder)['postingboard'], 'threads': [], 'inbox': True}
        board = FixtureBoard('postingboard', cfg)
        board.inbox = [(n, named(n + 400, 301), ['reply_to_your_thread']) for n in range(1, 8)]
        board.others.update({uid(n + 400): named(n + 400, 301) for n in range(1, 8)})
        reads = []
        delay = [True]

        def fetch(asked):
            path = urlsplit(asked.url).path
            if path.startswith('/v1/posts/'):
                reads.append(path)
                if delay[0]:
                    self.clock.advance(9)
            return board.answer(asked)

        network = FakeBoard(fetch)
        # The discovery checkpoint already belongs to a completed page, independent of original resolution.
        first = self.collect('postingboard', cfg, network)
        self.assertIsNone(first.error)
        self.assertFalse(first.complete)
        self.assertEqual(first.state['inbox_after'], 7)
        saved = {item['id'] for item in first.messages}
        self.assertTrue(saved)
        self.assertTrue(first.state['pending'])
        pending = list(first.state['pending'])
        delay[0] = False
        reads.clear()
        second = self.collect('postingboard', cfg, network, first.state, saved)
        self.assertIsNone(second.error)
        self.assertEqual(second.state['inbox_after'], 7)
        self.assertEqual(second.state['pending'], {})
        self.assertEqual(reads[0], '/v1/posts/' + pending[0])
        self.assertEqual(saved | {item['id'] for item in second.messages}, {uid(n) for n in range(401, 408)})

    def test_real_early_network_http_and_request_deadline_failures_remain_errors(self):
        cfg = settings(self.folder)['moltbook']

        def late(request):
            self.clock.advance(45.5)
            return 200, {'agent': {'id': cfg['account_id']}}

        for board, error in ((ConnectionRefusedError(), 'network_error'),
                             (lambda request: (503, {}), 'http_503'), (late, 'source_timeout')):
            with self.subTest(error=error), Network({'www.moltbook.com': board}):
                batch = self.collect('moltbook', cfg)
            self.assertFalse(batch.complete)
            self.assertEqual(batch.error, error)
            self.assertEqual(batch.messages, [])

    def test_slow_answer_early_in_a_phase_is_taken(self):
        cfg = settings(self.folder)['moltbook']
        profile = {'agent': {'id': cfg['account_id']}}

        def late(request):
            self.clock.advance(20)
            return 200, profile

        def late_end(request):
            return 200, Pieces([json.dumps(profile).encode(), lambda: self.clock.advance(20)])

        for slow in (late, late_end):
            def board(request):
                if request.path.endswith('/notifications'):
                    return 200, {'notifications': [], 'has_more': False}
                return slow(request)

            with self.subTest(slow=slow.__name__):
                with Network({'www.moltbook.com': board}):
                    batch = self.collect('moltbook', cfg)
                self.assertIsNone(batch.error, 'Twenty seconds of a phase of 45 are no failure')
                self.assertTrue(batch.complete)

    def test_request_at_the_end_of_a_phase_is_taken_past_an_address_that_does_not_answer(self):
        cfg = settings(self.folder)['moltbook']
        asked = []

        def board(request):
            path = request.path.removeprefix('/api/v1')
            asked.append((path, self.clock.now - 1_790_000_000, request.timeout))
            if path == '/agents/me':
                return 200, {'agent': {'id': cfg['account_id']}}
            if path == '/notifications':
                return 200, {'notifications': [
                    {'type': 'post_comment', 'relatedPostId': uid(600), 'relatedCommentId': uid(610)}], 'has_more': False}
            if path.endswith('/comments'):
                return 200, {'comments': [original(610, 600)], 'has_more': False}
            # The thread comes after 38 seconds, so the request for its comments starts with 7 seconds of the
            # phase left and has 10.
            self.clock.advance(38)
            return 200, {'post': {**original(600, 600), 'title': 'Synthetic root'}}

        # The first address of the host takes no connection for that request. The second takes it after half a
        # second. With ten seconds for the first address, the answer came half a second after the request's end.
        addresses = {'www.moltbook.com': [lambda: None if len(asked) == 3 else 0, 0.5]}
        with Network({'www.moltbook.com': board}, addresses, self.clock) as network:
            batch = self.collect('moltbook', cfg)
        self.assertEqual((batch.error, batch.complete, [item['id'] for item in batch.messages]), (None, True, [uid(610)]))
        self.assertEqual(asked[3], ('/posts/' + uid(600) + '/comments', 41.5, 10))
        self.assertEqual([(number, seconds) for _, number, seconds in network.attempts[3:]], [(0, 3), (1, 3)])

    def test_first_request_of_each_board_has_the_whole_phase(self):
        for source in ('postingboard', 'the-colony', 'moltbook'):
            cfg = settings(self.folder)[source]
            board = FixtureBoard(source, cfg)
            with self.subTest(source=source):
                self.assertIsNone(self.collect(source, cfg, board).error)
                self.assertEqual(board.asked[0].left, 45)
                self.assertGreaterEqual(min(asked.left for asked in board.asked), 10)

    def test_json_prefix_needs_timely_eof_and_complete_declared_body(self):
        cfg = settings(self.folder)['moltbook']
        body = json.dumps({'agent': {'id': cfg['account_id']}}).encode()

        def timeout():
            raise TimeoutError('An invented stalled body')

        cases = [
            (lambda request: (200, Pieces([body, lambda: self.clock.advance(45.5)])), 'source_timeout'),
            (lambda request: (200, Pieces([body, timeout])), 'network_error'),
            (lambda request: b'HTTP/1.1 200 OK\r\nContent-Length: 1000\r\nConnection: close\r\n\r\n' + body,
             'network_error'),
            (lambda request: (200, b'{"agent":'), 'invalid_response'),
        ]
        for board, error in cases:
            with self.subTest(error=error), Network({'www.moltbook.com': board}) as network:
                batch = self.collect('moltbook', cfg)
            self.assertEqual(batch.error, error)
            self.assertFalse(batch.complete)
            self.assertEqual(batch.messages, [])
            self.assertTrue(all(answer.closed for answer in network.answers))

    def test_lookup_keeps_shared_admission_budget_but_rejects_late_eof(self):
        cfg = settings(self.folder)['moltbook']
        board = FakeBoard([{'ok': True}, {'ok': True}])
        client = BOARDS['moltbook'].originals.client(cfg, fetch=board)
        client.get('/posts/' + uid(100))
        self.clock.advance(44.9)
        client.get('/posts/' + uid(101))
        self.assertEqual(board.asked[0].left, 45)
        self.assertAlmostEqual(board.asked[1].left, 0.1, places=5)
        self.clock.advance(0.1)
        with self.assertRaisesRegex(MailError, '^budget_exhausted$'):
            client.get('/posts/' + uid(102))
        self.assertEqual(len(board.asked), 2)

        def answer(request):
            return 200, Pieces([b'{"ok":true}', lambda: self.clock.advance(45.5)])

        client = BOARDS['moltbook'].originals.client(cfg)
        with Network({'www.moltbook.com': answer}) as network:
            with self.assertRaisesRegex(MailError, '^source_timeout$'):
                client.get('/posts/' + uid(100))
        self.assertTrue(all(answer.closed for answer in network.answers))


if __name__ == '__main__':
    unittest.main()
