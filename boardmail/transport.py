"""The one HTTP path of the board clients.

A board client asks for a URL in the name of its board. fetch() sends the request, follows no redirect, stops at
the size cap and when the time is over, and reads the answer as what it must be. failure() says what a request
that failed is called there, and key() reads the key of an account from its file. BOARDS holds every difference
between the boards that a board or an agent can see. The module of a board enters the row of its board when it
loads. None of the differences is unified here, and none is decided outside that row.

What a client does around a request stays with its board: its sign-in, its pauses, its retries, the time that
it gives a pass, what it keeps of an answer, and what it expects an answer to hold.

fetch() is also where a test stands in for a board. A board module that takes it as an argument is handed
FakeBoard of examples/fixtures.py instead, which answers with invented data and sends nothing.
"""
from http.client import HTTPException
import json
from pathlib import Path
import time
from typing import NamedTuple
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .errors import MailError


class Board(NamedTuple):
    """What a request to one board carries, what its answer may take, and what a failure is called."""
    accept: str             # the Accept header
    agent: str              # the User-Agent header
    protocol: str | None    # the X-Agent-Protocol header. None: the board gets none.
    key: int | None         # the most characters of the key of an account. Each must be printable ASCII and
                            # no space. None: a key is any text, or the board has no key.
    kind: str | None        # the content type that an answer must have; it is then UTF-8 text.
                            # None: an answer is JSON, whatever type it gives.
    cap: int                # the most bytes of an answer that are read. One more is too large.
    silence: float          # the seconds that the socket may stay silent, and never more than the time has left
    budget: float | None    # the seconds that a request may take. None: the client says how many it has left.
    at_the_end: bool        # an answer is late at the very moment at which the time is over. Else only after it.
    to_the_end: bool        # an answer that has come whole is still late when its end comes after that moment
    late: str               # the code for an answer that is late
    large: str              # the code for an answer over the cap
    network: str            # the code when the board is not reached or does not answer in HTTP
    content: str            # the code for an answer that cannot be read as what it must be
    statuses: tuple | None  # the statuses that are called http_<status>. None: every status.
    status: str | None      # the code for any other status
    redirect: str | None    # the code for a redirect that names where it leads. None: it is called as its status is.


# The row of each board under its name. A board module enters its own, so a board is asked only once its module
# has loaded.
BOARDS = {}
# What fetch() raises when a request fails. failure() names each of them.
FAILED = (MailError, OSError, HTTPException, ValueError)


class NoRedirect(HTTPRedirectHandler):
    """No redirect of a board is followed. It fails with the code that the board has for it, or urllib raises its
    status like any other status that is not a success.

    The answer of a redirect is closed here, whatever becomes of it, so it holds no connection open: when it is
    refused, and when urllib cannot read where it leads."""
    def __init__(self, board):
        self.refused = BOARDS[board].redirect

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if self.refused:
            raise MailError(self.refused)
        return None

    def http_error_302(self, req, fp, code, msg, headers):
        try:
            return super().http_error_302(req, fp, code, msg, headers)
        finally:
            fp.close()

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


def key(board, file):
    """The key of an account as its file gives it, without the spaces and line breaks around it. Where the file
    cannot be read, or holds no key that the board takes, a MailError says that the credentials are not there."""
    most = BOARDS[board].key
    try:
        if most is None:
            key = file.read_text().strip()
        else:
            with Path(file).open() as stream:
                key = stream.read(most + 1).strip()
            if len(key) > most or any(ord(letter) < 33 or ord(letter) > 126 for letter in key):
                raise ValueError('The board takes no such key')
        if not key:
            raise ValueError('The file holds no key')
    except (OSError, ValueError, TypeError):
        raise MailError('credentials_unavailable') from None
    return key


def fetch(board, url, *, left=None, headers=None, body=None):
    """The answer of a board to a request for this URL: text where an answer must be a page, and what the JSON
    says everywhere else.

    left is the seconds that the client has left for this request, where the board has no time budget of its
    own. headers are sent with the headers of the board. A body is sent as JSON, and the request is then a POST.
    Without one it is a GET.

    A request that fails raises one of FAILED: what urllib and http.client raise, a ValueError for an answer
    that cannot be read, and a MailError with the code of the board for an answer that is too large or late."""
    about = BOARDS[board]
    send = {'Accept': about.accept, 'User-Agent': about.agent, **(headers or {})}
    if about.protocol:
        send['X-Agent-Protocol'] = about.protocol
    if body is not None:
        send['Content-Type'] = 'application/json'
    request = Request(url, headers=send, data=None if body is None else json.dumps(body).encode())
    if left is None:
        left = about.budget
    end = time.monotonic() + left

    def in_time():
        now = time.monotonic()
        if now > end or about.at_the_end and now == end:
            raise MailError(about.late)

    with build_opener(NoRedirect(board)).open(request, timeout=min(about.silence, left)) as answer:
        if about.kind and answer.headers.get_content_type() != about.kind:
            raise ValueError('The answer is not ' + about.kind)
        content = bytearray()
        while True:
            in_time()
            chunk = answer.read1(min(65536, about.cap + 1 - len(content)))
            if about.to_the_end:
                in_time()
            if not chunk:
                break
            content += chunk
            if len(content) > about.cap:
                raise MailError(about.large)
    return content.decode('utf-8') if about.kind else json.loads(content)


def failure(board, exc):
    """What a request to this board is called when it failed with exc: one of FAILED, from fetch() or from a
    stand-in for it. Anything else that is handed in is called what an unreadable answer is called."""
    return called(BOARDS[board], exc)


def called(about, exc):
    """failure() for a board of which only its entry is at hand."""
    if isinstance(exc, MailError):
        return str(exc)
    if isinstance(exc, HTTPError):
        exc.close()
        return 'http_' + str(exc.code) if about.statuses is None or exc.code in about.statuses else about.status
    if isinstance(exc, (OSError, HTTPException)):
        return about.network
    return about.content
