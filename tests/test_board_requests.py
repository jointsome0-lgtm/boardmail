"""What each board client sends, and what it makes of an answer that fails.

Each case is one `boardmail collect` on a new inbox with one source. The board is invented and answers at the
network edge, so a request is what urllib would have put on the wire. board_requests.txt stores every request as
it left the process and what the command gave. That file stays as it is when the HTTP work of a board client
moves to another place inside the package.

That place is boardmail/transport.py. No other module of the package names what sends a request.
"""
import ast
from itertools import count
import json
from pathlib import Path
import tempfile
from typing import NamedTuple
import unittest
from urllib.error import HTTPError
from urllib.parse import parse_qsl, unquote, urlsplit

import boardmail
from boardmail import transport
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
A board that has a key file has cases for what the file holds. Its board is healthy in them.
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


def late(seconds, instead=None):
    """A case: the first answer comes after this many seconds. It is the healthy one, or the one given instead."""
    def change(passes, *answer):
        passes(seconds)
        return instead or answer
    return first(change)


def slow(seconds):
    """A case: the healthy board, and each of its answers takes this long to come."""
    def case(name, healthy, clock):
        def board(request):
            clock.advance(seconds)
            return healthy(request)
        return board
    return case


def unreachable(name, healthy, clock):
    return ConnectionRefusedError()


def waited():
    raise TimeoutError()


def silent(name, healthy, clock):
    return lambda request: kit.Pieces([waited])


def cases(board):
    half = 'half a second'
    yield 'a healthy board', every(lambda board, *answer: answer)
    for status in REDIRECTS:
        yield f'every answer is a {status} that names another host', every(
            lambda board, *answer, status=status: (status, b'It moved.', {'Location': f'https://{ELSEWHERE}/caught'}))
    yield 'every answer is a 302 that names another place on the board', every(
        lambda board, *answer: (302, b'It moved.', {'Location': '/elsewhere'}))
    yield 'every answer is a 302 that names what is no URL', every(
        lambda board, *answer: (302, b'It moved.', {'Location': 'http://[no-url'}))
    yield 'every answer is a 302 that names no other place', every(lambda board, *answer: (302, b'It moved.'))
    for status in STATUSES:
        yield f'every answer has the status {status}', every(
            lambda board, *answer, status=status: (status, {'error': 'An invented refusal.'}))
    yield 'every answer has the status 401 and the code AUTH_2FA_REQUIRED, as Colony refuses a sign-in', every(
        lambda board, *answer: (401, {'detail': {'code': 'AUTH_2FA_REQUIRED', 'message': 'An invented refusal.'}}))
    yield 'the first answer has the status 503', late(0, (503, {'error': 'An invented refusal.'}))
    yield 'the first answer has the status 503 and comes two seconds before its time budget ends', late(
        board.budget - 2, (503, {'error': 'An invented refusal.'}))
    yield 'the board cannot be reached', unreachable
    yield 'the socket stays silent for too long', silent
    yield 'the socket stays silent for too long after the first request', first(
        lambda passes, *answer: kit.Pieces([waited]))
    yield 'every answer is not HTTP', lambda name, healthy, clock: lambda request: b'An invented line.\r\n\r\n'
    yield f'the first answer comes {half} before its time budget of {board.budget} s ends', late(board.budget - 0.5)
    yield 'the first answer comes when its time budget ends', late(board.budget)
    yield f'the first answer comes {half} after its time budget ends', late(board.budget + 0.5)
    yield f'the second half of the first answer comes {half} after its time budget ends', first(
        lambda passes, status, body, headers: (status, kit.Pieces([
            body[:len(body) // 2], lambda: passes(board.budget + 0.5), body[len(body) // 2:]]), headers))
    yield f'the first answer is whole in time and ends {half} after its time budget ends', first(
        lambda passes, status, body, headers: (status, kit.Pieces([body, lambda: passes(board.budget + 0.5)]), headers))
    yield 'every answer takes three seconds to come', slow(3)
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


ABSENT, FOLDER = 'no file', 'a folder'
# What a pass gives where no address of the board takes a connection, for a board that does not call it network_error.
NO_ANSWER = {'clawdchat': 'budget_exhausted', 'fourclaw': 'fourclaw_network_error'}


def keys():
    """What is where the key file of an account should be, case by case: the bytes of the file, ABSENT or FOLDER."""
    yield 'the key file is not there', ABSENT
    yield 'a folder is where the key file should be', FOLDER
    yield 'the key file is empty', b''
    yield 'the key file has a line break and no key', b'\n'
    yield 'the key has spaces and line breaks around it', f'\n  {KEY} \n\n'.encode()
    yield 'the key has a space in it', b'an invented key\n'
    yield 'the key is two lines', b'an-invented\nkey\n'
    yield 'the key is 4096 characters long', b'k' * 4096 + b'\n'
    yield 'the key is 4097 characters long', b'k' * 4097 + b'\n'


def shown(text):
    """A header or a body as the stored file has it: a long one is cut and says how long it was."""
    return text if len(text) <= 120 else f'{text[:40]}... ({len(text)} characters)'


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

    def collected(self, name, home, case, clock, addresses=None):
        """One pass over a board in the state that the case describes: (the requests with the seconds at which
        they left, the exit code, the result). With addresses the host of the board has those, as kit.Network
        takes them, and self.attempts has the address and the seconds of each connection attempt of the pass."""
        requests, strayed, board = [], [], case(name, BOARDS[name].healthy(), clock)

        def watched(request):
            requests.append((clock.now - START, request))
            return board(request)

        def elsewhere(request):
            strayed.append(request)
            return 200, {}

        clock.now = START
        network = kit.Network({BOARDS[name].host: board if isinstance(board, Exception) else watched,
                               ELSEWHERE: elsewhere}, addresses and {BOARDS[name].host: addresses}, clock)
        with network:
            pass_, = kit.told(lambda step: step('one pass', 'collect', 'collect'), home, 'cli')
        self.attempts = [(number, seconds) for _, number, seconds in network.attempts]
        self.assertEqual(strayed, [], 'A request followed a redirect to another host')
        self.assertEqual(len(network.answers), len(requests))
        self.assertEqual([answer for answer in network.answers if not answer.isclosed()], [],
                         'The client left an answer open')
        return requests, pass_.outcome, json.loads(pass_.text)

    def section(self, name, clock):
        board, sets, lines = BOARDS[name], {}, []
        home, new = self.home(name)
        healthy = every(lambda board, *answer: answer)
        passes = [(title, case, FILES['board.key'].encode()) for title, case in cases(board)]
        if 'api_key_file' in board.source:
            passes += [(title, healthy, held) for title, held in keys()]
        for title, case, held in passes:
            (home / 'inbox.sqlite3').write_bytes(new)
            key = home / 'board.key'
            key.rmdir() if key.is_dir() else key.unlink(missing_ok=True)
            if held is FOLDER:
                key.mkdir()
            elif held is not ABSENT:
                key.write_bytes(held)
            requests, code, result = self.collected(name, home, case, clock)
            lines += ['', '-- ' + title]
            for seconds, request in requests:
                headers = sets.setdefault(tuple(sorted(request.headers.items())), len(sets) + 1)
                lines.append(f'    {request.method} {request.url}  [headers {headers}, silent {request.timeout:g} s, '
                             f'at {seconds:g} s]')
                if request.body:
                    lines.append('        ' + shown(request.body.decode()))
            source, = result['sources']
            errors = [error['error'] for error in result['errors']]
            self.assertEqual(errors, [source['error']] if source['error'] else [], title)
            lines.append(f'    exit code {code}, added {result["added"]}, errors: {", ".join(errors) or "none"}; '
                         f'the source: status {source["status"]}, unavailable {source["unavailable"]}, '
                         f'backlog {"pending" if source["backlog_pending"] else "done"}')
        legend = [f'    headers {number}: ' + '; '.join(f'{header}: {shown(text)}' for header, text in headers)
                  for headers, number in sets.items()]
        return '\n'.join([f'== {name}', *legend, *lines, ''])

    def test_what_each_board_is_sent_and_what_a_failed_answer_becomes(self):
        clock = kit.Clock(START)
        with kit.fixed(clock):
            text = '\n'.join([INTRO, *(self.section(name, clock) for name in BOARDS)])
        kit.check_stored(self, 'board_requests.txt', text)

    def test_an_address_that_takes_no_connection_costs_three_seconds_on_every_board(self):
        """The host of a board has several addresses. One that takes no connection held a request for as long as
        the socket of the board may stay silent. It has three seconds, and then the next address is tried."""
        clock = kit.Clock(START)
        healthy = every(lambda board, *answer: answer)

        def gave(code, result):
            source, = result['sources']
            return code, result['added'], [error['error'] for error in result['errors']], source['status']

        def one_pass(name, home, new, case, addresses=None):
            (home / 'inbox.sqlite3').write_bytes(new)
            requests, code, result = self.collected(name, home, case, clock, addresses)
            return requests, gave(code, result)

        with kit.fixed(clock):
            for name in BOARDS:
                with self.subTest(board=name):
                    home, new = self.home(name)
                    usual, well = one_pass(name, home, new, healthy)
                    silent = [request.timeout for _, request in usual]

                    # The first address never answers, and the second takes the connection at once.
                    requests, outcome = one_pass(name, home, new, healthy, [None, 0])
                    self.assertEqual(outcome, well)
                    self.assertEqual(self.attempts, [(0, 3), (1, 3)] * len(usual))
                    self.assertEqual(requests[0][0], 3, 'The first request leaves after three seconds')
                    self.assertEqual([request.timeout for _, request in requests], silent,
                                     'A socket that is reached may stay silent for as long as before')

                    # The only address takes the connection after two seconds.
                    requests, outcome = one_pass(name, home, new, healthy, [2])
                    self.assertEqual(outcome, well)
                    self.assertEqual((requests[0][0], self.attempts), (2, [(0, 3)] * len(usual)))

                    # No address answers. The pass ends with the code that it had for this before. ClawdChat
                    # has less time for the first answer than the two attempts take.
                    requests, outcome = one_pass(name, home, new, healthy, [None, None])
                    self.assertEqual((requests, outcome), ([], (1, 0, [NO_ANSWER.get(name, 'network_error')], 'error')))
                    self.assertEqual(set(self.attempts), {(0, 3), (1, 3)}, 'Each attempt ends sooner than it did')

    def test_a_connection_attempt_has_no_more_time_than_its_request(self):
        clock = kit.Clock(START)
        with kit.fixed(clock):
            for name, board in BOARDS.items():
                network = kit.Network({board.host: lambda request: (200, {})}, {board.host: [None, None]}, clock)
                with self.subTest(board=name), network:
                    with self.assertRaises(transport.FAILED) as failed:
                        transport.fetch(name, f'https://{board.host}/', left=2)
                    self.assertEqual(network.attempts, [(board.host, 0, 2), (board.host, 1, 2)])
                    self.assertIn(transport.failure(name, failed.exception), ('network_error', 'fourclaw_network_error'))


SENDERS = ('urllib.request', 'http.client', 'socket', 'ssl')


def sends(tree):
    """Each name in a module that is what can send a request, or a part of it: one that the module imports, and
    one that it reaches through a package, as urllib.request.urlopen after a plain `import urllib`. HTTPException
    is what a failed request raises, and a module may name it."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [f'{node.module}.{alias.name}' for alias in node.names]
        elif isinstance(node, ast.Attribute):
            names = [ast.unparse(node)]
        else:
            continue
        found += [name for name in names if name != 'http.client.HTTPException'
                  and any(name == sender or name.startswith(sender + '.') for sender in SENDERS)]
    return found


class OnePathTests(unittest.TestCase):
    def test_only_the_transport_module_names_what_sends_a_request(self):
        package = Path(boardmail.__file__).resolve().parent
        found = [f'{path.name}: {name}' for path in sorted(package.rglob('*.py'))
                 for name in sends(ast.parse(path.read_text(encoding='utf-8')))]
        self.assertEqual([line for line in found if not line.startswith('transport.py: ')], [],
                         'These belong in boardmail/transport.py')
        # If it saw nothing, the line above would say nothing.
        self.assertIn('transport.py: urllib.request.build_opener', found)
        self.assertIn('urllib.request.urlopen', sends(ast.parse('import urllib\nurllib.request.urlopen(url)')))


if __name__ == '__main__':
    unittest.main()
