"""What a story test stands on: invented boards at the network edge, a fixed clock and fixed keys, both entry
points, and a look at each connection to the inbox file.

A story is a function that takes one argument, step, and calls it once for each command, in the order an agent
would. told() runs a story through the CLI entry point or through MCP tool calls and returns what each step gave.
Nothing here patches a name inside the package or calls a Store method.
"""
import asyncio
from contextlib import ExitStack, chdir, contextmanager, redirect_stdout
import difflib
import http.client
import importlib.util
import io
from itertools import count
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import sys
import time
from typing import NamedTuple
from unittest.mock import patch
from urllib.parse import urlsplit

from boardmail import cli, config
from boardmail.store import Store
from examples.fixtures import uid


TESTS = Path(__file__).resolve().parent
NO_EXTRA = 'From the source checkout, install .[mcp] to test the optional MCP interface'
UPDATE = 'UPDATE_STORIES'


class Request(NamedTuple):
    """One request as it left the process, and how long its socket may stay silent."""
    method: str
    url: str
    headers: dict
    body: bytes
    timeout: float | None = None

    @property
    def path(self):
        return urlsplit(self.url).path


class Pieces(list):
    """A body that a board sends piece by piece and without its length, so it ends where the connection closes.

    A piece that is a function sends nothing. It is called when the reader has taken every piece before it, so a
    board can let time pass in the middle of its answer or before its end."""


class Reply(io.RawIOBase):
    """An answer that is read one piece at a time."""
    def __init__(self, pieces):
        self.pieces = list(pieces)

    def readable(self):
        return True

    def readinto(self, buffer):
        while self.pieces:
            piece = self.pieces.pop(0)
            if callable(piece):
                piece()
                continue
            taken = min(len(buffer), len(piece))
            buffer[:taken] = piece[:taken]
            if taken < len(piece):
                self.pieces.insert(0, piece[taken:])
            return taken
        return 0


class Wire:
    """The socket of one connection. It keeps what is sent and answers when the reply is read."""
    def __init__(self, answer):
        self.answer, self.sent = answer, b''

    def sendall(self, data):
        self.sent += data

    def makefile(self, *args, **kwargs):
        answer = self.answer(self.sent)
        return io.BytesIO(answer) if isinstance(answer, bytes) else io.BufferedReader(Reply(answer))

    def close(self):
        pass


class Network:
    """Invented answers at the standard-library network edge.

    Inside the with block no connection leaves the process. What urllib or http.client sends to a host goes to
    boards[host](request), which returns (status, value) or (status, value, headers). A value that is not bytes
    is sent as JSON, and one that is Pieces is sent piece by piece. A board that returns bytes or Pieces alone
    sends them as they are, so its answer need not be HTTP. A board that is an exception is a host that cannot be
    reached: every connection to it fails with that exception. A host that has no board fails the test.

    answers has every answer that a client began to read, as http.client made it. The network holds on to
    them, so an answer that its client did not close is still open when the test looks.
    """
    def __init__(self, boards):
        self.boards, self.answers = boards, []

    def answer(self, scheme, host, sent, timeout):
        head, _, body = sent.partition(b'\r\n\r\n')
        first, *lines = head.decode('iso-8859-1').split('\r\n')
        method, target, _ = first.split(' ', 2)
        headers = dict(line.split(': ', 1) for line in lines)
        answer = self.boards[host](Request(method, f'{scheme}://{headers["Host"]}{target}', headers, body, timeout))
        if isinstance(answer, (bytes, Pieces)):
            return answer
        status, value, *more = answer
        pieces = value if isinstance(value, Pieces) else None
        if not isinstance(value, bytes):
            value = b'' if pieces is not None else json.dumps(value).encode()
        given = {'Content-Type': 'application/json', **(more[0] if more else {}),
                 **({} if pieces is not None else {'Content-Length': len(value)}), 'Connection': 'close'}
        lines = [f'HTTP/1.1 {status} {http.client.responses[status]}']
        lines += [f'{name}: {text}' for name, text in given.items()]
        head = '\r\n'.join(lines).encode('iso-8859-1') + b'\r\n\r\n'
        return head + value if pieces is None else [head, *pieces]

    def __enter__(self):
        def offline(scheme):
            def connect(connection):
                host = connection.host
                if host not in self.boards:
                    raise AssertionError(f'No invented board answers for {host}')
                if isinstance(self.boards[host], Exception):
                    raise self.boards[host]
                connection.sock = Wire(lambda sent: self.answer(scheme, host, sent, connection.timeout))
            return connect

        answers = self.answers

        class Kept(http.client.HTTPResponse):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                answers.append(self)

        self.stack = ExitStack()
        self.stack.enter_context(patch.object(http.client.HTTPConnection, 'response_class', Kept))
        self.stack.enter_context(patch.object(http.client.HTTPSConnection, 'connect', offline('https')))
        self.stack.enter_context(patch.object(http.client.HTTPConnection, 'connect', offline('http')))
        # A proxy from the environment would turn every request into one to the proxy.
        self.stack.enter_context(patch.dict('os.environ', {'no_proxy': '*'}))
        return self

    def __exit__(self, *exc):
        self.stack.close()


class Clock:
    """The time inside fixed(): it stands still until the story moves it.

    With a step it also moves by that many seconds each time a package module asks how long something has taken,
    so a wait of the package comes to its end without anyone waiting."""
    def __init__(self, now, step=0):
        self.now, self.step = now, step

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@contextmanager
def fixed(clock):
    """Time and generated keys at the standard-library edge.

    time.time() is the clock for everyone. time.monotonic() is the clock for a package module, so no deadline
    passes while a story runs unless the clock has a step, and stays real for anyone else: the event loop of the
    MCP client needs it. A package module that sleeps moves the clock by that long and does not wait.
    A uuid4() that a package module asks for counts up from 1, so a key is the same on every run and through
    both entry points, and no two keys are equal. Random bytes for anyone else stay random.
    """
    turn, random, elapsed, pause = count(1), os.urandom, time.monotonic, time.sleep

    def package_asks(frames):
        return sys._getframe(frames + 1).f_globals.get('__name__', '').startswith('boardmail.')

    def monotonic():
        if not package_asks(1):
            return elapsed()
        clock.advance(clock.step)
        return clock.now

    def sleep(seconds):
        if package_asks(1):
            clock.advance(seconds)
        else:
            pause(seconds)

    def urandom(size):
        # uuid4() reads os.urandom itself, so the module that asked for the key is one frame further up.
        return next(turn).to_bytes(size, 'big') if package_asks(2) else random(size)

    with patch('time.time', clock), patch('time.monotonic', monotonic), patch('time.sleep', sleep), \
            patch('os.urandom', urandom):
        yield


class Closed(NamedTuple):
    """One connection to the inbox file as it was when it closed."""
    writes: bool      # it was opened to write
    stand_ins: tuple  # the names of its temporary tables and views
    memory: bool      # what is temporary stays in memory


@contextmanager
def connections():
    """Every SQLite connection that a package module opens inside the with block, in the order they closed.

    A temporary table or view belongs to its connection and is gone when the connection closes, so this looks
    at that moment. A connection that the test opens itself is not looked at.
    """
    closed, real = [], sqlite3.connect

    class Watched(sqlite3.Connection):
        def close(self):
            names = self.execute("SELECT name FROM sqlite_temp_master WHERE type IN ('table','view') ORDER BY name")
            closed.append(Closed(self.writes, tuple(row[0] for row in names),
                                 self.execute('PRAGMA temp_store').fetchone()[0] == 2))
            super().close()

    def connect(database, *args, **kwargs):
        if not sys._getframe(1).f_globals.get('__name__', '').startswith('boardmail.'):
            return real(database, *args, **kwargs)
        db = real(database, *args, factory=Watched, **kwargs)
        db.writes = 'mode=ro' not in str(database)
        return db

    with patch('sqlite3.connect', connect):
        yield closed


class Moltbook:
    """An invented Moltbook: posts with their comments, and the notifications of one account."""
    HOST = 'www.moltbook.com'
    URL = 'https://www.moltbook.com/post/'

    def __init__(self, account, name):
        self.account, self.posts, self.comments, self.notifications = {'id': account, 'name': name}, {}, {}, []
        self.down = False

    def post(self, post, author, name, title, content, at):
        self.posts[post] = {'id': post, 'title': title, 'content': content, 'author': {'id': author, 'name': name},
                            'created_at': at, 'is_deleted': False, 'is_spam': False, 'verification_status': 'verified'}
        self.comments[post] = []

    def comment(self, comment, post, author, name, content, at, *, parent=None, notify=None):
        """A comment under a post or under another comment. notify is the type of the notification that the
        account gets for it, if it gets one. The comment as the board keeps it, so a story can change it later."""
        kept = {'id': comment, 'post_id': post, 'parent_id': parent, 'content': content,
                'author': {'id': author, 'name': name}, 'created_at': at, 'is_deleted': False, 'is_spam': False,
                'verification_status': 'verified'}
        self.comments[post].append(kept)
        if notify:
            self.notifications.insert(0, {'id': uid(len(self.notifications) + 9001), 'type': notify, 'isRead': False,
                                          'relatedPostId': post, 'relatedCommentId': comment})
        return kept

    def url(self, post, comment=None):
        return self.URL + post + ('#comment-' + comment if comment else '')

    def tree(self, post, parent=None, depth=0):
        return [{**comment, 'depth': depth, 'replies': self.tree(post, comment['id'], depth + 1)}
                for comment in self.comments[post] if comment['parent_id'] == parent]

    def __call__(self, request):
        path, private = request.path.removeprefix('/api/v1'), 'Authorization' in request.headers
        if self.down:
            return 503, {'success': False}
        if path in ('/agents/me', '/notifications'):
            if not private:
                return 401, {'success': False}
            return 200, ({'agent': self.account} if path == '/agents/me' else
                         {'notifications': self.notifications, 'has_more': False, 'next_cursor': None})
        found = re.fullmatch('/posts/([^/]+)(/comments)?', path)
        if private or not found or found[1] not in self.posts:
            return 404, {'success': False}
        if found[2]:
            return 200, {'comments': self.tree(found[1]), 'has_more': False, 'next_cursor': None}
        return 200, {'success': True, 'post': self.posts[found[1]]}


class Step(NamedTuple):
    """What one step of a story gave: the exit code or the MCP error flag, and the result as it was printed."""
    title: str
    command: str
    outcome: int | bool
    text: str


def mcp_missing():
    return importlib.util.find_spec('mcp') is None


def told(story, home, entry):
    """Run a story on the inbox that the config in home describes, through 'cli' or 'mcp'. The steps it took."""
    steps = []
    if entry == 'mcp':
        from mcp import Client

        from boardmail.mcp import create_server
        # The way boardmail-mcp starts: the operator's config is read once.
        data = config.load(home / 'config.json')
        server = create_server(Store(data['database']), data['sources'])

        async def call(tool, arguments):
            async with Client(server, raise_exceptions=True) as client:
                return await client.call_tool('boardmail_' + tool, arguments)

    def step(title, typed, tool, /, **arguments):
        """One command: typed is what follows boardmail on the command line, tool and arguments are the MCP call.
        The command line runs in home, so it can name a file there."""
        assert all(title != done.title for done in steps), f'Two steps are called {title!r}'
        if entry == 'cli':
            printed = io.StringIO()
            with redirect_stdout(printed), chdir(home):
                code = cli.main(['--config', str(home / 'config.json'), *shlex.split(typed)])
            steps.append(Step(title, f'boardmail {typed}', code, printed.getvalue().rstrip('\n')))
        else:
            result = asyncio.run(call(tool, arguments))
            text, = [part.text for part in result.content]
            assert json.loads(text) == result.structured_content, 'The text and the structured result differ'
            steps.append(Step(title, f'boardmail_{tool} {json.dumps(arguments)}', bool(result.is_error), text))
        return json.loads(steps[-1].text)

    story(step)
    return steps


def transcript(steps):
    """The steps of a CLI telling as the text that is stored."""
    parts = []
    for number, step in enumerate(steps, 1):
        result = json.dumps(json.loads(step.text), indent=2, ensure_ascii=False)
        parts.append(f'== {number}. {step.title}\n$ {step.command}\nexit code {step.outcome}\n{result}\n')
    return '\n'.join(parts)


def check_stored(test, name, text):
    """Fail when text is not the stored file, and show the difference. With UPDATE_STORIES set, write the file."""
    path = TESTS / name
    if os.environ.get(UPDATE):
        path.write_text(text, encoding='utf-8', newline='\n')
    stored = path.read_text(encoding='utf-8')
    if text != stored:
        changes = difflib.unified_diff(stored.splitlines(), text.splitlines(), 'stored', 'now', n=2, lineterm='')
        test.fail(f'This is not what {name} stores. If the change is meant, run the test with {UPDATE}=1 and review '
                  'the difference.\n' + '\n'.join(changes))


def check_both(test, typed, called, named):
    """Fail when an MCP telling does not give what the CLI telling gave.

    MCP has no exit code. Its error flag is set where the exit code is 1, 2 or 5. named maps the title of a step
    that MCP answers differently to the result it gives there.
    """
    titles = [step.title for step in typed]
    test.assertEqual([step.title for step in called], titles)
    test.assertEqual(sorted(set(named) - set(titles)), [], 'A named difference has no step')
    for cli_step, mcp_step in zip(typed, called):
        with test.subTest(step=cli_step.title, call=mcp_step.command):
            if cli_step.title in named:
                test.assertEqual(json.loads(mcp_step.text), named[cli_step.title])
                test.assertNotEqual(mcp_step.text, cli_step.text, 'The named difference is gone')
            else:
                test.assertEqual(mcp_step.text, cli_step.text)
            test.assertEqual(mcp_step.outcome, cli_step.outcome in (1, 2, 5))
