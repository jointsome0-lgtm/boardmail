"""What each board client sends, and what it makes of an answer that fails.

Each case is one `boardmail collect` on a new inbox with one source. The board is invented and answers at the
network edge, so a request is what urllib would have put on the wire. board_requests.txt stores every request as
it left the process and what the command gave. That file stays as it is when the HTTP work of a board client
moves to another place inside the package.
"""
import importlib.metadata
from itertools import count
import json
from pathlib import Path
import tempfile
import tomllib
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
REDIRECTS = (301, 302, 303, 307, 308)
STATUSES = (300, 400, 401, 403, 404, 408, 410, 418, 429, 500, 502, 503, 504)
INTRO = """\
What each board client sent and what the command gave, case by case. A case is one collect on a new inbox.
A request line ends with the headers that the request carried, the seconds that its socket may stay silent and
the seconds since the pass began at which it left. The sets of headers are numbered under the name of the board.
VERSION in a user agent stands for the version of the package.
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
    yield 'every answer is a 302 that names a place that no request can go to', every(
        lambda board, *answer: (302, b'It moved.', {'Location': 'file:///elsewhere'}))
    for status in STATUSES:
        yield f'every answer has the status {status}', every(
            lambda board, *answer, status=status: (status, {'error': 'An invented refusal.'}))
    yield 'every answer has the status 401 and the code AUTH_2FA_REQUIRED, as Colony refuses a sign-in', every(
        lambda board, *answer: (401, {'detail': {'code': 'AUTH_2FA_REQUIRED', 'message': 'An invented refusal.'}}))
    yield 'the first answer has the status 503', late(0, (503, {'error': 'An invented refusal.'}))
    yield 'the first answer has the status 503 and comes two seconds before its time budget ends', late(
        board.budget - 2, (503, {'error': 'An invented refusal.'}))
    yield f'the first answer has the status 503 and comes {half} after its time budget ends', late(
        board.budget + 0.5, (503, {'error': 'An invented refusal.'}))
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

    def plain(passes, status, body, headers):
        passes(board.budget + 0.5)
        return status, body, {'Content-Type': 'text/plain'}
    yield f'the first answer says that it is text/plain and comes {half} after its time budget ends', first(plain)
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


# What a pass gives where every answer of the board fails in one way, or its first answer comes late: the case,
# and the one code that all five boards have for it.
SAME = {
    **{f'every answer is a {status} that names another host': 'redirect_refused' for status in REDIRECTS},
    'every answer is a 302 that names another place on the board': 'redirect_refused',
    'every answer is a 302 that names what is no URL': 'invalid_response',
    'every answer is a 302 that names no other place': 'http_302',
    'every answer is a 302 that names a place that no request can go to': 'http_302',
    **{f'every answer has the status {status}': f'http_{status}' for status in STATUSES},
    'the board cannot be reached': 'network_error',
    'the socket stays silent for too long': 'network_error',
    'every answer is not HTTP': 'network_error',
    # An answer is late from the moment at which its time is over, and so is one whose end comes after that.
    'the first answer comes when its time budget ends': 'source_timeout',
    'the first answer comes half a second after its time budget ends': 'source_timeout',
    'the second half of the first answer comes half a second after its time budget ends': 'source_timeout',
    'the first answer is whole in time and ends half a second after its time budget ends': 'source_timeout',
    # It is late before it is anything else.
    'the first answer says that it is text/plain and comes half a second after its time budget ends': 'source_timeout',
    'every answer is text that is not JSON': 'invalid_response',
    'every answer ends with a byte that is not UTF-8': 'invalid_response',
    'every answer is one byte longer than the size cap': 'response_too_large',
}
ABSENT, FOLDER = 'no file', 'a folder'
# What a pass gives where no address of the board takes a connection, or where its first answer is a 503 that
# comes late, for a board whose pass does not end with the code of that request then. ClawdChat asks again after
# a board that it did not reach or that answered 503, and has no time left to.
NO_ANSWER = {'clawdchat': 'budget_exhausted'}


# What is where the key file of an account should be, case by case: the bytes of the file, ABSENT or FOLDER, and
# the key that every board with a key file is then sent. None: the board is not asked, and the pass ends with
# credentials_unavailable.
KEYS = {
    'the key file is not there': (ABSENT, None),
    'a folder is where the key file should be': (FOLDER, None),
    'the key file is empty': (b'', None),
    'the key file has a line break and no key': (b'\n', None),
    'the key has spaces, tabs and line breaks around it': (f'\n \t {KEY} \t\n\n'.encode(), KEY),
    'the key has a space in it': (b'an invented key\n', None),
    'the key is two lines': (b'an-invented\nkey\n', None),
    'the key has a letter that is not ASCII': ('an-invented-kl\u00fcc'.encode(), None),
    'the key is 4096 characters long': (b'k' * 4096 + b'\n', 'k' * 4096),
    'the key is 4097 characters long': (b'k' * 4097 + b'\n', None),
    'the key is 4097 characters long and stands after a line break': (b'\n' + b'k' * 4097 + b'\n', None),
    'the key has 4097 line breaks before it': (b'\n' * 4097 + KEY.encode() + b'\n', KEY),
    'the key has 4097 spaces and then a word after it': (KEY.encode() + b' ' * 4097 + b'word\n', None),
}


AGENT = 'boardmail/' + boardmail.__version__


def shown(text):
    """A header or a body as the stored file has it: a long one is cut and says how long it was. The user agent
    of the package is written without the number of its version, so the file is the same after a release."""
    if text == AGENT:
        return 'boardmail/VERSION'
    return text if len(text) <= 120 else f'{text[:40]}... ({len(text)} characters)'


def carried(request):
    """The key of the account that a request carries: as a bearer, or in the body of a sign-in. None: it has none."""
    bearer = request.headers.get('Authorization', '')
    if bearer.startswith('Bearer ') and bearer != 'Bearer an-invented-token':
        return bearer.removeprefix('Bearer ')
    return json.loads(request.body).get('api_key') if request.body else None


class BoardRequestTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.gave = {}  # the errors of each pass of section(), by the title of its case and then by its board
        self.sent = {}  # the key that each request of such a pass carried, in the same order

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
            passes += [(title, healthy, held) for title, (held, _) in KEYS.items()]
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
            self.sent.setdefault(title, {})[name] = [carried(request) for _, request in requests]
            for seconds, request in requests:
                self.assertEqual(request.headers['User-Agent'], AGENT, title)
                headers = sets.setdefault(tuple(sorted(request.headers.items())), len(sets) + 1)
                lines.append(f'    {request.method} {request.url}  [headers {headers}, silent {request.timeout:g} s, '
                             f'at {seconds:g} s]')
                if request.body:
                    lines.append('        ' + shown(request.body.decode()))
            source, = result['sources']
            errors = [error['error'] for error in result['errors']]
            self.assertEqual(errors, [source['error']] if source['error'] else [], title)
            self.gave.setdefault(title, {})[name] = errors
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
        # A failed request has the same code on every board.
        for title, code in SAME.items():
            self.assertEqual(self.gave[title], dict.fromkeys(BOARDS, [code]), title)
        # An answer that comes before its time is over is taken on every board.
        for name, board in BOARDS.items():
            title = f'the first answer comes half a second before its time budget of {board.budget} s ends'
            self.assertEqual(self.gave[title][name], [], name)
        # A status that is no success is what the request is called also when it comes late: the board has
        # answered, and only an answer that is read can be late.
        title = 'the first answer has the status 503 and comes half a second after its time budget ends'
        self.assertEqual(self.gave[title], {name: [NO_ANSWER.get(name, 'http_503')] for name in BOARDS})
        # One rule says what a key is. A key that it takes is sent as it is, in each request that carries a key,
        # and for any other the board is not asked.
        with_key = [name for name, board in BOARDS.items() if 'api_key_file' in board.source]
        self.assertEqual(with_key, ['postingboard', 'the-colony', 'moltbook', 'clawdchat', 'botnet'])
        for title, (_, key) in KEYS.items():
            for name in with_key:
                with self.subTest(case=title, board=name):
                    errors, sent = self.gave[title][name], self.sent[title][name]
                    if key:
                        self.assertEqual((errors, {carried for carried in sent if carried}), ([], {key}))
                    else:
                        self.assertEqual((errors, sent), (['credentials_unavailable'], []))
        kit.check_stored(self, 'board_requests.txt', text)

    def test_the_version_in_the_user_agent_is_the_version_of_the_package(self):
        project = Path(__file__).resolve().parents[1] / 'pyproject.toml'
        if project.is_file():
            version = tomllib.loads(project.read_text(encoding='utf-8'))['project']['version']
        else:
            # The tests stand next to an installed package and not in its checkout.
            version = importlib.metadata.version('boardmail')
        self.assertEqual(transport.AGENT, 'boardmail/' + version)

    def test_no_header_of_a_client_takes_the_place_of_the_user_agent(self):
        """A client hands the transport headers of its own, such as the key of an account. Whatever one of them is
        called, the board is told the user agent of the package."""
        for name, board in BOARDS.items():
            told = []

            def asked(request):
                told.append(request.headers['User-Agent'])
                return 200, {}

            with self.subTest(board=name), kit.Network({board.host: asked}):
                for header in ('User-Agent', 'user-agent'):
                    try:
                        transport.fetch(name, f'https://{board.host}/', left=2, headers={header: 'another'})
                    except transport.FAILED:
                        pass    # what the invented board answers is not what this board must answer
                self.assertEqual(told, [AGENT, AGENT])

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
                    self.assertEqual(transport.failure(failed.exception), 'network_error')


if __name__ == '__main__':
    unittest.main()
