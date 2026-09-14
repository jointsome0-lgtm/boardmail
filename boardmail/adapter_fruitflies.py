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

from boardmail import addressing, subscriptions
from boardmail.adapters import Batch
from boardmail.config import MailError

API_VERSION = 1
BASE = 'https://api.fruitflies.ai/v1/feed'
PAGE = 100
MAX_OFFSET = 100000
MAX_BYTES = 2 * 1024 * 1024
MAX_MEMBERS = 200  # Root plus the newest recognized members by creation time.


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
    handle = handle.lower()
    try:
        selected = subscriptions.selected(settings)
    except MailError:
        return Batch(state={}, complete=False, error='invalid_config')
    offset = state.get('offset', PAGE)
    if type(offset) is not int or not PAGE <= offset <= MAX_OFFSET or offset % PAGE:
        offset = PAGE
    result = Batch(state={'offset': offset}, complete=False)
    if isinstance(state.get('subscriptions'), dict):
        result.state['subscriptions'] = json.loads(json.dumps(state['subscriptions']))
    entry = subscriptions.progress(result.state, selected)
    seen_posts = {}  # Every parsed row of this pass, own and foreign, as parent evidence.
    explicit = addressing.mention_pattern(addressing.aliases({'handle': handle}, settings.get('mention_aliases')))
    own = {}
    own_ok = True
    try:
        for raw in _fetch({'agent': handle, 'limit': PAGE, 'offset': 0}):
            try:
                post = _post(raw)
                seen_posts[post['id']] = post
                if post['author'].casefold() == handle.casefold():
                    own[post['id']] = post['post_type']
                    # This adapter anchors each incoming reply at its immediate
                    # parent, even when that parent is itself an answer.
                    addressing.cache_original(result, dict(id=post['id'], thread_id=post['id'],
                        parent_id=post['parent_id'], author=post['author'], title='', body=post['body'],
                        url='https://fruitflies.ai/feed', created_at=post['created_at']))
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
            seen_posts[post['id']] = post
            if post['id'] in emitted or post['author'].casefold() == handle.casefold():
                continue
            parent_kind = own.get(post['parent_id'])
            mentioned = addressing.mentions(explicit, post['body'])
            if parent_kind:
                kind = 'reply_to_comment' if parent_kind == 'answer' else 'reply_to_post'
            elif mentioned:
                kind = 'mention'
            else:
                continue
            # A verified own parent proves the reply is to us; the handle mention is
            # explicit text. Both can hold at once.
            result.messages.append(dict(id=post['id'], parent_id=post['parent_id'],
                thread_id=post['parent_id'] or post['id'], kind=kind, author=post['author'],
                title='', body=post['body'], url='https://fruitflies.ai/feed',
                created_at=post['created_at'],
                addressing=addressing.resolve(direct=bool(parent_kind), mention=mentioned)))
            emitted.add(post['id'])
        if position == offset and own_ok:
            # Cycle the finite scan window. Re-visits also retry malformed originals.
            end = len(rows) < PAGE or offset >= MAX_OFFSET
            result.state['offset'] = PAGE if end else offset + PAGE
            result.complete = end and result.error is None
    if selected:
        _subscribed(result, entry, selected, seen_posts, emitted, handle, explicit)
    return result


def _subscribed(result, entry, selected, seen_posts, emitted, handle, explicit):
    """Thread activity for subscribed roots, inferred from immediate parents only.

    The feed is newest-first and gives no thread lookup, so ancestry is rebuilt
    from every row of this pass plus a bounded memory of members found earlier.
    A post whose parent was never seen stays outside the subscription."""
    for root in selected:
        progress = entry['roots'].setdefault(root, {})
        members = {mid: value for mid, value in progress.get('members', {}).items()
                   if isinstance(mid, str) and isinstance(value, list) and len(value) == 2
                   and value[0] in (True, False, None)
                   and (value[1] is None or type(value[1]) is int)} if isinstance(progress.get('members'), dict) else {}
        root_post = seen_posts.get(root)
        if root_post is not None:
            members[root] = [root_post['author'].casefold() == handle.casefold(), root_post['created_at']]
        elif root not in members:
            members[root] = [None, None]  # Root author unknown until its row is seen.
        changed = True
        while changed:
            changed = False
            for post in seen_posts.values():
                parent = post['parent_id']
                if parent in members and post['id'] not in members:
                    members[post['id']] = [post['author'].casefold() == handle.casefold(), post['created_at']]
                    changed = True
        for post in seen_posts.values():
            mid = post['id']
            if mid == root or mid not in members or mid in emitted or members[mid][0]:
                continue
            ownership = members.get(post['parent_id'], [None, None])[0]
            result.messages.append(dict(id=mid, parent_id=post['parent_id'], thread_id=root, kind='thread_activity',
                author=post['author'], title='', body=post['body'], url='https://fruitflies.ai/feed',
                created_at=post['created_at'], discovery='subscription',
                addressing=addressing.resolve(direct=ownership is True,
                                              mention=addressing.mentions(explicit, post['body']), thread=ownership is False)))
            emitted.add(mid)
        if len(members) > MAX_MEMBERS:
            keep = sorted((mid for mid in members if mid != root),
                          key=lambda mid: (members[mid][1] is not None, members[mid][1] or 0, mid),
                          reverse=True)[:MAX_MEMBERS - 1]
            members = {root: members[root], **{mid: members[mid] for mid in keep}}
        progress['members'] = members
