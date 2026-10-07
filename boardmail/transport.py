"""The one HTTP path of the board clients.

A board client asks for a URL in the name of its board. fetch() sends the request, follows no redirect, stops at
the size cap and at the time budget of that board, and reads the answer as what it must be. failure() says what
a request that failed is called there. BOARDS holds every difference between the boards that a board or an agent
can see. None of them is unified here, and none is decided in another module.

Fruitflies and 4claw read through this module. The other board clients still carry their own copy of the rest,
and refuse a redirect with the NoRedirect of this module under the name that they give it.
"""
from http.client import HTTPException
import json
import time
from typing import NamedTuple
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import MailError


class Board(NamedTuple):
    """What a request to one board carries, what its answer may take, and what a failure is called."""
    accept: str             # the Accept header
    agent: str              # the User-Agent header
    kind: str | None        # the content type that an answer must have; it is then UTF-8 text.
                            # None: an answer is JSON, whatever type it gives.
    cap: int                # the most bytes of an answer that are read. One more is too large.
    silence: float          # the seconds that the socket may stay silent
    budget: float           # the seconds that a request may take
    to_the_end: bool        # an answer that has come whole is still late when its end comes after the budget
    late: str               # the code when the budget is spent
    large: str              # the code for an answer over the cap
    network: str            # the code when the board is not reached or does not answer in HTTP
    content: str            # the code for an answer that cannot be read as what it must be
    statuses: tuple | None  # the statuses that are called http_<status>. None: every status.
    status: str | None      # the code for any other status


BOARDS = {
    'fruitflies': Board(
        accept='application/json', agent='boardmail/fruitflies', kind=None, cap=2 * 1024 * 1024,
        silence=8, budget=8, to_the_end=False,
        late='network_timeout', large='response_too_large', network='network_error', content='invalid_response',
        statuses=None, status=None),
    'fourclaw': Board(
        accept='text/html', agent='boardmail/1', kind='text/html', cap=2_000_000,
        silence=5, budget=10, to_the_end=True,
        late='fourclaw_network_error', large='fourclaw_invalid_public_page', network='fourclaw_network_error',
        content='fourclaw_invalid_public_page',
        statuses=(401, 403, 404, 429, 500, 502, 503, 504), status='fourclaw_http_error'),
}
# What fetch() raises when a request fails. failure() names each of them.
FAILED = (MailError, OSError, HTTPException, ValueError)


class NoRedirect(HTTPRedirectHandler):
    """No redirect is followed, and urllib then raises its status like any other status that is not a success.

    The answer of a redirect is closed here, whatever becomes of it, so it holds no connection open: when it is
    refused, when a board client refuses it under a name of its own, and when urllib cannot read where it leads."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

    def http_error_302(self, req, fp, code, msg, headers):
        try:
            return super().http_error_302(req, fp, code, msg, headers)
        finally:
            fp.close()

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


def fetch(board, url):
    """The answer of a board to a GET of this URL: text where an answer must be a page, and what the JSON says
    everywhere else.

    A request that fails raises one of FAILED: what urllib and http.client raise, a ValueError for an answer
    that cannot be read, and a MailError with the code of the board for an answer that is too large or late."""
    about = BOARDS[board]
    request = Request(url, headers={'Accept': about.accept, 'User-Agent': about.agent})
    end = time.monotonic() + about.budget

    def in_time():
        if time.monotonic() >= end:
            raise MailError(about.late)

    with build_opener(NoRedirect()).open(request, timeout=about.silence) as answer:
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
    about = BOARDS[board]
    if isinstance(exc, MailError):
        return str(exc)
    if isinstance(exc, HTTPError):
        exc.close()
        return 'http_' + str(exc.code) if about.statuses is None or exc.code in about.statuses else about.status
    if isinstance(exc, (OSError, HTTPException)):
        return about.network
    return about.content
