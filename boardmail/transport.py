"""The one HTTP path of the board clients.

A board client asks for a URL in the name of its board. fetch() sends the request, follows no redirect, stops at
the size cap and when the time is over, and reads the answer as what it must be. It gives each address of the
host ATTEMPT seconds to take the connection. Four things are the same on every board: the user agent that a
board is told, AGENT; the moment from which an answer is late; what a request that failed is called, which
failure() says; and what the key of an account is, which key() reads from its file. BOARDS holds what differs
between the boards: what a board is asked for, and how large and how slow its answer may be. The module of a
board enters the row of its board when it loads. None of those differences is unified here, and none is decided
outside that row.

What a client does around a request stays with its board: its sign-in, its pauses, its retries, the time that
it gives a pass, what it keeps of an answer, and what it expects an answer to hold.

fetch() is also where a test stands in for a board. A board module that takes it as an argument is handed
FakeBoard of examples/fixtures.py instead, which answers with invented data and sends nothing.
"""
from http.client import HTTPException, HTTPSConnection
import json
from pathlib import Path
import socket
import time
from typing import NamedTuple
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from . import __version__
from .errors import MailError


class Board(NamedTuple):
    """What a request to one board carries and what its answer may take."""
    accept: str             # the Accept header
    protocol: str | None    # the X-Agent-Protocol header. None: the board gets none.
    kind: str | None        # the content type that an answer must have; it is then UTF-8 text.
                            # None: an answer is JSON, whatever type it gives.
    cap: int                # the most bytes of an answer that are read. One more is too large.
    silence: float          # the seconds that the socket may stay silent, and never more than the time has left
    budget: float | None    # the seconds that a request may take. None: the client says how many it has left.


# The User-Agent header of every request: the name and the version of the package.
AGENT = 'boardmail/' + __version__
# The most characters of the key of an account.
KEY = 4096
# The row of each board under its name. A board module enters its own, so a board is asked only once its module
# has loaded.
BOARDS = {}
# What fetch() raises when a request fails. failure() names each of them.
FAILED = (MailError, OSError, HTTPException, ValueError)
# The seconds that one address of a host has to take a connection, on every board. The next address is tried
# after them. An address that takes none would else hold the request for as long as its socket may stay silent.
ATTEMPT = 3


def reach(address, timeout, source_address=None):
    """A connection to a host as socket.create_connection() makes it, where each address of the host has ATTEMPT
    seconds to take it, and the timeout if that is less. The socket that is reached has the timeout again, so the
    TLS handshake and the answer may stay silent for as long as the board allows."""
    reached = socket.create_connection(address, min(ATTEMPT, timeout), source_address)
    reached.settimeout(timeout)
    return reached


class Reached(HTTPSConnection):
    """An HTTPS connection whose socket reach() makes."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # http.client has no public name for the time of a connection attempt. Its connect() makes the socket
        # with this attribute, which is socket.create_connection unless it is set.
        self._create_connection = reach


class Attempts(HTTPSHandler):
    """HTTPS over connections that reach() makes."""
    def do_open(self, http_class, req, **http_conn_args):
        return super().do_open(Reached, req, **http_conn_args)


class NoRedirect(HTTPRedirectHandler):
    """No redirect of a board is followed. One that names a place that urllib would ask is refused. urllib raises
    the status of any other, like a status that is not a success: of one that names no place, and of one that
    names what is no http, https or ftp address.

    The answer of a redirect is closed here, whatever becomes of it, so it holds no connection open: when it is
    refused, and when urllib cannot read where it leads."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise MailError('redirect_refused')

    def http_error_302(self, req, fp, code, msg, headers):
        try:
            return super().http_error_302(req, fp, code, msg, headers)
        finally:
            fp.close()

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


def key(file):
    """The key of an account as its file gives it, without the white space around it: spaces, tabs, line breaks
    and whatever else str.strip() takes for it. A key is up to KEY characters, each printable ASCII and none a
    space, whichever board the account is on. Where the file cannot be read, holds no such key or holds more
    than the key, a MailError says that the credentials are not there."""
    try:
        with Path(file).open() as stream:
            # What stands before the key does not count, and one character more than a key may have is enough
            # to know that it is too long.
            key = stream.read(KEY + 1).lstrip()
            while len(key) <= KEY and (more := stream.read(KEY + 1 - len(key))):
                key = (key + more).lstrip()
            key = key.rstrip()
            if not key or len(key) > KEY or any(ord(letter) < 33 or ord(letter) > 126 for letter in key):
                raise ValueError('The file holds no key')
            # The rest of the file is read only to see that it is white space, however far after the key.
            while more := stream.read(KEY + 1):
                if more.strip():
                    raise ValueError('The file holds more than a key')
    except (OSError, ValueError, TypeError):
        raise MailError('credentials_unavailable') from None
    return key


def fetch(board, url, *, left=None, headers=None, body=None):
    """The answer of a board to a request for this URL: text where an answer must be a page, and what the JSON
    says everywhere else.

    left is the completion window of this request, where the board has no time budget of its own. A collection
    client checks admission separately; its phase's remaining time need not be this window. headers are sent
    with the headers of the board, and none of them takes the place of one of those. A body is sent as JSON, and
    the request is then a POST. Without one it is a GET.

    A request that fails raises one of FAILED: what urllib and http.client raise, a ValueError for an answer
    that cannot be read, and a MailError for a redirect that is refused and for an answer that is too large or
    late. Only an answer that is read can be late: a status that is no success and a redirect are raised as what
    they are, whenever they come."""
    about = BOARDS[board]
    # What the board is told comes last, so a header of the client under the same name does not replace it.
    send = {**(headers or {}), 'Accept': about.accept, 'User-Agent': AGENT}
    if about.protocol:
        send['X-Agent-Protocol'] = about.protocol
    if body is not None:
        send['Content-Type'] = 'application/json'
    request = Request(url, headers=send, data=None if body is None else json.dumps(body).encode())
    if left is None:
        left = about.budget
    end = time.monotonic() + left

    def in_time():
        # The time is over from its last moment on, as it is for a request that a client may no longer send.
        if time.monotonic() >= end:
            raise MailError('source_timeout')

    with build_opener(NoRedirect(), Attempts()).open(request, timeout=min(about.silence, left)) as answer:
        # Before anything is asked of the answer: one that begins late is late, whatever else it is.
        in_time()
        if about.kind and answer.headers.get_content_type() != about.kind:
            raise ValueError('The answer is not ' + about.kind)
        content = bytearray()
        while True:
            chunk = answer.read1(min(65536, about.cap + 1 - len(content)))
            # After each read, the last one too: an answer that has come whole is late when its end comes late.
            in_time()
            if not chunk:
                # read1() can reach the socket's EOF before Content-Length is satisfied without raising.
                # A JSON-shaped prefix is not a complete HTTP answer.
                if answer.length:
                    raise HTTPException('The HTTP answer ended before its declared length')
                break
            content += chunk
            if len(content) > about.cap:
                raise MailError('response_too_large')
    return content.decode('utf-8') if about.kind else json.loads(content)


def failure(exc):
    """What a request is called when it failed with exc, whichever board it was sent to: exc is one of FAILED,
    from fetch() or from a stand-in for it. Anything else that is handed in is called what an unreadable answer
    is called."""
    if isinstance(exc, MailError):
        return str(exc)
    if isinstance(exc, HTTPError):
        exc.close()
        return 'http_' + str(exc.code)
    if isinstance(exc, (OSError, HTTPException)):
        return 'network_error'
    return 'invalid_response'
