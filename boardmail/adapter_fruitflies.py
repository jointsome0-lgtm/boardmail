"""Public Fruitflies mentions and replies within a documented parent window."""
from datetime import datetime
from http.client import HTTPException
import json
import re
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

from boardmail.adapters import Batch

API_VERSION = 1
BASE = 'https://api.fruitflies.ai/v1/feed'
PAGE = 100
MAX_OFFSET = 100000
MAX_BYTES = 2 * 1024 * 1024


class FetchError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _fetch(params):
    # Public requests never read configured credentials or follow redirects.
    request = Request(BASE + '?' + urlencode(params), headers={'Accept': 'application/json',
                      'User-Agent': 'boardmail/fruitflies'})
    try:
        deadline = monotonic() + 8
        with build_opener(NoRedirect()).open(request, timeout=8) as response:
            chunks = []
            size = 0
            while size <= MAX_BYTES:
                if monotonic() >= deadline:
                    raise FetchError('network_timeout')
                chunk = response.read1(min(65536, MAX_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            raw = b''.join(chunks)
        if len(raw) > MAX_BYTES:
            raise FetchError('response_too_large')
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get('posts'), list):
            raise FetchError('invalid_response')
        if len(data['posts']) > PAGE:
            raise FetchError('invalid_response')
        return data['posts']
    except HTTPError as exc:
        exc.close()
        raise FetchError('http_' + str(exc.code)) from None
    except (URLError, OSError, TimeoutError, HTTPException):
        raise FetchError('network_error') from None
    except (ValueError, UnicodeError):
        raise FetchError('invalid_response') from None


def _uuid(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError()
    return value


def _post(raw):
    if not isinstance(raw, dict):
        raise ValueError()
    ident = _uuid(raw['id'])
    parent = raw.get('parent_id')
    if parent is not None:
        parent = _uuid(parent)
    kind = raw['post_type']
    if kind not in ('post', 'question', 'answer'):
        raise ValueError()
    body = raw['content']
    author = raw['agents']['handle']
    if not isinstance(body, str) or not isinstance(author, str):
        raise ValueError()
    body.encode('utf-8'); author.encode('utf-8')
    date = datetime.fromisoformat(raw['created_at'].replace('Z', '+00:00'))
    if date.tzinfo is None:
        raise ValueError()
    return dict(id=ident, parent_id=parent, post_type=kind, body=body,
                author=author, created_at=int(date.timestamp()))


def collect(settings, state, known):
    """Read newest and one historical page; retry failed history next call."""
    handle = settings.get('account_id')
    if not isinstance(handle, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', handle):
        return Batch(state={}, complete=False, error='invalid_config')
    offset = state.get('offset', PAGE)
    if type(offset) is not int or not PAGE <= offset <= MAX_OFFSET or offset % PAGE:
        offset = PAGE
    result = Batch(state={'offset': offset}, complete=False)
    mention = re.compile(r'(?<![\w@])@' + re.escape(handle) + r'(?![\w-])', re.IGNORECASE)
    own = {}
    own_ok = True
    try:
        for raw in _fetch({'agent': handle, 'limit': PAGE, 'offset': 0}):
            try:
                post = _post(raw)
                if post['author'].casefold() == handle.casefold():
                    own[post['id']] = post['post_type']
            except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
                own_ok = False
                result.error = 'invalid_response'
    except FetchError as exc:
        own_ok = False
        result.error = str(exc)

    emitted = set(known)
    for position in (0, offset):
        try:
            rows = _fetch({'limit': PAGE, 'offset': position})
        except FetchError as exc:
            result.error = str(exc)
            continue
        for raw in rows:
            try:
                post = _post(raw)
            except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
                result.unavailable += 1
                result.error = 'invalid_response'
                continue
            if post['id'] in emitted or post['author'].casefold() == handle.casefold():
                continue
            parent_kind = own.get(post['parent_id'])
            if parent_kind:
                kind = 'reply_to_comment' if parent_kind == 'answer' else 'reply_to_post'
            elif mention.search(post['body']):
                kind = 'mention'
            else:
                continue
            result.messages.append(dict(id=post['id'], parent_id=post['parent_id'],
                thread_id=post['parent_id'] or post['id'], kind=kind, author=post['author'],
                title='', body=post['body'], url='https://fruitflies.ai/feed',
                created_at=post['created_at']))
            emitted.add(post['id'])
        if position == offset and own_ok:
            # Cycle the finite scan window. Re-visits also retry malformed originals.
            end = len(rows) < PAGE or offset >= MAX_OFFSET
            result.state['offset'] = PAGE if end else offset + PAGE
            result.complete = end and result.error is None
    return result
