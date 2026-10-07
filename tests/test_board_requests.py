"""What each board client sends, and what it makes of an answer that fails.

Each case is one `boardmail collect` on a new inbox with one source. The board is invented and answers at the
network edge, so a request is what urllib would have put on the wire. board_requests.txt stores every request as
it left the process and what the command gave. That file stays as it is when the HTTP work of a board client
moves to another place inside the package.
"""
from itertools import count
import json
from pathlib import Path
import tempfile
from typing import NamedTuple
import unittest
from urllib.error import HTTPError
from urllib.parse import parse_qsl, unquote, urlsplit

from examples import fixtures
from examples.fixtures import FixtureClient, uid
import kit


START = 1790000000
ELSEWHERE = 'elsewhere.example'
KEY = 'an-invented-key'
ME, WRITER, THREAD, REPLY = uid(1), uid(2), uid(100), uid(70)
MEMBER = 'participant-' + ME
PAGE = '10000000-0000-4000-8000-000000000001'
REDIRECTS = (301, 302, 303, 307, 308)
STATUSES = (300, 400, 401, 403, 404, 408, 410, 418, 429, 500, 502, 503, 504)
INTRO = """\
What each board client sent and what the command gave, case by case. A case is one collect on a new inbox.
A request line ends with the headers that the request carried, the seconds that its socket may stay silent and
the seconds since the pass began at which it left. The sets of headers are numbered under the name of the board.
"""


class Board(NamedTuple):
    """One board as its client meets it."""
    host: str
    source: dict     # its entry in the config
    healthy: object  # makes the invented board: a function from a request to its answer
    cap: int         # the most bytes of one answer that the client reads
    budget: float    # the seconds that the client gives the first answer


def served(source, prefix=''):
    """The invented board of the examples, at the network edge."""
    def healthy():
        client = FixtureClient(source, fixtures.settings()[source])

        def board(request):
            url = urlsplit(request.url)
            if request.method == 'POST':
                return 200, {'access_token': 'an-invented-token'}
            asked = {name: int(value) if value.isdigit() else value for name, value in parse_qsl(url.query)}
            try:
                return 200, client.get(url.path.removeprefix(prefix), asked,
                                       authenticated='Authorization' in request.headers)
            except HTTPError as absent:
                return absent.code, {}
        return board
    return healthy


def pages(prefix, signed, anonymous, **headers):
    """An invented board: what it answers to a signed request and to an anonymous one, by path."""
    def board(request):
        found = (signed if 'Authorization' in request.headers else anonymous).get(unquote(request.path)[len(prefix):])
        return (404, {}) if found is None else (200, found, headers)
    return lambda: board


def fourclaw_page():
    posts = ''.join(f'<div class="claw-post {kind}"><span class="claw-post-name">writer</span>'
                    f'<time datetime="2026-09-07T12:00:00Z">then</time><div class="claw-post-body">{body}</div></div>'
                    for kind, body in (('op', 'An invented opening.'), ('reply', '@reader, an invented reply.')))
    keys = '["$","div",' + json.dumps(PAGE) + ',{"className":"claw-post reply"}]'
    return ('<div class="claw-section-title">An invented thread</div>' + posts
            + '<script>self.__next_f.push(' + json.dumps([1, keys]) + ')</script>').encode()


def legacy(source):
    return {**fixtures.settings()[source], 'api_key_file': 'board.key'}


BOARDS = {
    'postingboard': Board('getpostingboard.dev', legacy('postingboard'), served('postingboard'), 16 * 1024 * 1024, 45),
    'the-colony': Board('thecolony.ai', {**legacy('the-colony'), 'totp_secret_file': 'board.totp'},
                        served('the-colony', '/api/v1'), 16 * 1024 * 1024, 45),
    'moltbook': Board('www.moltbook.com', legacy('moltbook'), served('moltbook', '/api/v1'), 16 * 1024 * 1024, 45),
    'clawdchat': Board('clawdchat.cn', {'account_id': ME, 'api_key_file': 'board.key'}, pages('/api/v1', {
        '/agents/me': {'id': ME},
        '/notifications': {'success': True, 'total': 1, 'items': [
            {'id': uid(1070), 'type': 'comment', 'post_id': THREAD, 'comment_id': REPLY}]},
    }, {
        '/comments/' + REPLY: {
            'id': REPLY, 'post_id': THREAD, 'parent_id': None, 'content': 'An invented reply.',
            'author': {'id': WRITER, 'name': 'writer'}, 'created_at': '2026-09-07T10:00:00Z',
            'post': {'id': THREAD, 'title': 'An invented thread'}},
    }), 1024 * 1024, 5),
    'botnet': Board('botnet.com', {'account_id': MEMBER, 'api_key_file': 'board.key'}, pages('/api/forum', {
        '/me': {'actor': {'id': MEMBER}},
        '/inbox': {'nextCursor': None, 'items': [
            {'id': 70, 'threadId': THREAD, 'postId': REPLY, 'reason': 'reply', 'readAt': 1}]},
    }, {
        '/topic-messages/post:' + REPLY: {
            'id': 'post:' + REPLY, 'topicId': uid(200), 'parentMessageId': 'thread:' + THREAD, 'title': None,
            'body': 'An invented reply.', 'createdAt': 1790593200000, 'sequence': 70, 'status': None,
            'author': {'id': 'participant-' + WRITER, 'name': 'writer'}},
        '/topic-messages/thread:' + THREAD: {
            'id': 'thread:' + THREAD, 'topicId': uid(200), 'parentMessageId': None, 'title': None,
            'body': 'An invented opening.', 'createdAt': 1790593200000, 'sequence': 1, 'status': None,
            'author': {'id': MEMBER, 'name': 'reader'}},
        '/topics/' + uid(200): {'id': uid(200), 'title': 'An invented topic', 'description': 'It is invented.',
                                'createdAt': 1790593200000},
    }), 1024 * 1024, 10),
    'fruitflies': Board('api.fruitflies.ai', {'account_id': 'reader'}, pages('', {}, {
        '/v1/feed': {'posts': [{'id': REPLY, 'parent_id': None, 'post_type': 'post',
                                'content': '@reader, an invented question.', 'agents': {'handle': 'writer'},
                                'created_at': '2026-09-07T10:00:00Z'}]},
    }), 2 * 1024 * 1024, 8),
    'fourclaw': Board('www.4claw.org', {'account_id': 'reader', 'watched_threads': [PAGE]},
                      pages('', {}, {'/t/' + PAGE: fourclaw_page()}, **{'Content-Type': 'text/html'}), 2_000_000, 10),
}
FILES = {'board.key': KEY + '\n', 'board.totp': 'INVENTEDINVENTED\n'}


def parts(answer):
    """(status, body, headers) of what an invented board returned."""
    status, value, *more = answer
    return status, value if isinstance(value, bytes) else json.dumps(value).encode(), more[0] if more else {}


def every(change):
    """A case: the healthy board, with each of its answers changed. change(board, status, body, headers)."""
    return lambda name, healthy, clock: lambda request: change(BOARDS[name], *parts(healthy(request)))


def first(change):
    """A case: the healthy board, with its first answer changed. change(passes, status, body, headers), where
    passes(seconds) lets that much time go by for the client."""
    def case(name, healthy, clock):
        turn = count()

        def board(request):
            answer = parts(healthy(request))
            return answer if next(turn) else change(clock.advance, *answer)
        return board
    return case


def late(seconds):
    def change(passes, *answer):
        passes(seconds)
        return answer
    return first(change)


def unreachable(name, healthy, clock):
    return ConnectionRefusedError()


def silent(name, healthy, clock):
    def waited():
        raise TimeoutError()
    return lambda request: kit.Pieces([waited])


def cases(board):
    half = 'half a second'
    yield 'a healthy board', every(lambda board, *answer: answer)
    for status in REDIRECTS:
        yield f'every answer is a {status} that names another host', every(
            lambda board, *answer, status=status: (status, b'It moved.', {'Location': f'https://{ELSEWHERE}/caught'}))
    yield 'every answer is a 302 that names another place on the board', every(
        lambda board, *answer: (302, b'It moved.', {'Location': '/elsewhere'}))
    yield 'every answer is a 302 that names no other place', every(lambda board, *answer: (302, b'It moved.'))
    for status in STATUSES:
        yield f'every answer has the status {status}', every(
            lambda board, *answer, status=status: (status, {'error': 'An invented refusal.'}))
    yield 'the board cannot be reached', unreachable
    yield 'the socket stays silent for too long', silent
    yield 'every answer is not HTTP', lambda name, healthy, clock: lambda request: b'An invented line.\r\n\r\n'
    yield f'the first answer comes {half} before its time budget of {board.budget} s ends', late(board.budget - 0.5)
    yield 'the first answer comes when its time budget ends', late(board.budget)
    yield f'the first answer comes {half} after its time budget ends', late(board.budget + 0.5)
    yield f'the second half of the first answer comes {half} after its time budget ends', first(
        lambda passes, status, body, headers: (status, kit.Pieces([
            body[:len(body) // 2], lambda: passes(board.budget + 0.5), body[len(body) // 2:]]), headers))
    yield f'the first answer is whole in time and ends {half} after its time budget ends', first(
        lambda passes, status, body, headers: (status, kit.Pieces([body, lambda: passes(board.budget + 0.5)]), headers))
    yield 'every answer is text that is not JSON', every(
        lambda board, status, body, headers: (status, b'An invented line.', headers))
    yield 'every answer ends with a byte that is not UTF-8', every(
        lambda board, status, body, headers: (status, body + b'\xff', headers))
    yield 'every answer is an empty JSON list', every(lambda board, status, body, headers: (status, b'[]', headers))
    yield 'every answer is a JSON object that has only an invented field', every(
        lambda board, status, body, headers: (status, b'{"invented": ["An invented line."]}', headers))
    yield 'every answer says that it is text/plain', every(
        lambda board, status, body, headers: (status, body, {'Content-Type': 'text/plain'}))
    yield f'every answer is as long as the size cap of {board.cap} bytes', every(
        lambda board, status, body, headers: (status, body.ljust(board.cap), headers))
    yield 'every answer is one byte longer than the size cap', every(
        lambda board, status, body, headers: (status, body.ljust(board.cap + 1), headers))


class BoardRequestTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def home(self, name):
        """A folder with the config of one board and its new inbox, and that inbox as bytes."""
        home = self.root / name
        home.mkdir()
        for file, text in FILES.items():
            (home / file).write_text(text)
        (home / 'config.json').write_text(json.dumps({'database': 'inbox.sqlite3', 'sources': {name: BOARDS[name].source}}))
        made, = kit.told(lambda step: step('a new inbox', 'init', 'init'), home, 'cli')
        self.assertEqual(made.outcome, 0)
        return home, (home / 'inbox.sqlite3').read_bytes()

    def collected(self, name, home, case, clock):
        """One pass over a board in the state that the case describes: (the requests with the seconds at which
        they left, the exit code, the result)."""
        requests, strayed, board = [], [], case(name, BOARDS[name].healthy(), clock)

        def watched(request):
            requests.append((clock.now - START, request))
            return board(request)

        def elsewhere(request):
            strayed.append(request)
            return 200, {}

        clock.now = START
        network = kit.Network({BOARDS[name].host: board if isinstance(board, Exception) else watched,
                               ELSEWHERE: elsewhere})
        with network:
            pass_, = kit.told(lambda step: step('one pass', 'collect', 'collect'), home, 'cli')
        self.assertEqual(strayed, [], 'A request followed a redirect to another host')
        self.assertEqual(len(network.answers), len(requests))
        self.assertEqual([answer for answer in network.answers if not answer.isclosed()], [],
                         'The client left an answer open')
        return requests, pass_.outcome, json.loads(pass_.text)

    def section(self, name, clock):
        board, sets, lines = BOARDS[name], {}, []
        home, new = self.home(name)
        for title, case in cases(board):
            (home / 'inbox.sqlite3').write_bytes(new)
            requests, code, result = self.collected(name, home, case, clock)
            lines += ['', '-- ' + title]
            for seconds, request in requests:
                headers = sets.setdefault(tuple(sorted(request.headers.items())), len(sets) + 1)
                lines.append(f'    {request.method} {request.url}  [headers {headers}, silent {request.timeout:g} s, '
                             f'at {seconds:g} s]')
                if request.body:
                    lines.append('        ' + request.body.decode())
            source, = result['sources']
            errors = [error['error'] for error in result['errors']]
            self.assertEqual(errors, [source['error']] if source['error'] else [], title)
            lines.append(f'    exit code {code}, added {result["added"]}, errors: {", ".join(errors) or "none"}; '
                         f'the source: status {source["status"]}, unavailable {source["unavailable"]}, '
                         f'backlog {"pending" if source["backlog_pending"] else "done"}')
        legend = [f'    headers {number}: ' + '; '.join(f'{header}: {text}' for header, text in headers)
                  for headers, number in sets.items()]
        return '\n'.join([f'== {name}', *legend, *lines, ''])

    def test_what_each_board_is_sent_and_what_a_failed_answer_becomes(self):
        clock = kit.Clock(START)
        with kit.fixed(clock):
            text = '\n'.join([INTRO, *(self.section(name, clock) for name in BOARDS)])
        kit.check_stored(self, 'board_requests.txt', text)


if __name__ == '__main__':
    unittest.main()
