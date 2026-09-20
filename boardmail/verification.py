"""Verify a known reply reference with bounded provider reads, never publication."""
import time
from urllib.parse import urlsplit

from . import adapter_clawdchat, providers, replies
from .config import LEGACY_ADAPTERS, MailError, uuid

ADAPTERS = ('postingboard', 'the-colony', 'moltbook', 'clawdchat')


def candidate(adapter, ref, thread):
    """Parse an allowlisted identity. Never send a caller URL to the HTTP client."""
    try:
        replies.reference(ref)
        url = urlsplit(ref)
        hosts = {'postingboard': ('getpostingboard.dev',), 'the-colony': ('thecolony.ai',),
                 'moltbook': ('www.moltbook.com', 'moltbook.com'), 'clawdchat': ('clawdchat.cn',)}
        if (url.scheme != 'https' or url.hostname not in hosts[adapter] or url.port not in (None, 443)
                or url.query or '%' in ref or '\\' in ref):
            raise ValueError()
        parts = url.path.split('/')
        if adapter == 'postingboard' and parts[:3] == ['', 'v1', 'posts'] and len(parts) == 4 and not url.fragment:
            return uuid(parts[3])
        if adapter == 'clawdchat' and parts[:4] == ['', 'api', 'v1', 'comments'] and len(parts) == 5 and not url.fragment:
            return uuid(parts[4])
        paths = {'the-colony': ('post', 'posts'), 'moltbook': ('post',), 'clawdchat': ('post',)}
        if len(parts) == 3 and parts[1] in paths.get(adapter, ()) and uuid(parts[2]) == thread and url.fragment.startswith('comment-'):
            return uuid(url.fragment.removeprefix('comment-'))
        raise ValueError()
    except (MailError, ValueError, TypeError, AttributeError, KeyError):
        raise MailError('reply_reference_unsupported') from None


def check_source(db, source, settings):
    """Used before fetching and again inside the confirmation transaction."""
    row = db.execute('SELECT * FROM sources WHERE source=?', (source,)).fetchone()
    if row is None:
        raise MailError('source_not_found')
    if row['account_id'] != settings['account_id']:
        raise MailError('account_mismatch')
    if dict(row).get('paused'):
        raise MailError('source_paused')
    previous = None
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='adapter_state'").fetchone():
        previous = db.execute('SELECT adapter FROM adapter_state WHERE source=?', (source,)).fetchone()
    # Schema v1 had three fixed source names and no adapter aliases or state table.
    expected = previous['adapter'] if previous else source if source in LEGACY_ADAPTERS else None
    if expected is None:
        raise MailError('reply_adapter_identity_unknown')
    if expected != settings.get('adapter', source):
        raise MailError('adapter_mismatch')


def available(original, *, adapter):
    """Availability is a provider observation, not a promise about future visibility."""
    if any(original.get(k) not in (None, False, 0) for k in ('is_deleted', 'is_spam', 'is_hidden', 'held')):
        raise MailError('reply_not_visible')
    if original.get('visibility', 'public') != 'public':
        raise MailError('reply_not_visible')
    if original.get('is_truncated') or original.get('truncated'):
        raise MailError('reply_incomplete')
    for field in ('status', 'state'):
        if field in original and original[field] not in ('published', 'public', 'active', 'visible', 'verified'):
            raise MailError('reply_provider_status_unknown')
    if 'verification_status' in original and original['verification_status'] != 'verified':
        raise MailError('reply_provider_not_verified')
    if adapter == 'moltbook':
        if original.get('verification_status') != 'verified':
            raise MailError('reply_provider_not_verified')
        if original.get('is_deleted') is not False or original.get('is_spam') is not False:
            raise MailError('reply_provider_status_unknown')
    if adapter == 'the-colony' and original.get('held') is not False:
        raise MailError('reply_provider_status_unknown')


def read(adapter, settings, mid, thread):
    """Return the original and its observed status using fixed provider endpoints."""
    client = adapter_clawdchat.Client(settings) if adapter == 'clawdchat' else providers.Client(adapter, settings)
    if adapter == 'postingboard':
        original = client.get('/v1/posts/' + mid, authenticated=True)['post']
        root = uuid(original['root_id'])
        if original.get('thread_id') is not None and uuid(original['thread_id']) != root:
            raise MailError('reply_thread_mismatch')
        author = uuid(original['agent_id'])
        parent = original['reply_to_id']
        body = original['body']
        basis = 'authenticated_original'
    else:
        if adapter == 'moltbook':
            originals = {}
            status, error, _ = providers.moltbook_lookup(client, mid, thread, originals=originals)
            if status != 'available':
                raise MailError(error or 'reply_' + status)
            original = originals[thread, mid]
            available(originals[thread, thread], adapter=adapter)
        elif adapter == 'clawdchat':
            _, _, original, context = adapter_clawdchat._fetch(client, {
                'id': mid, 'post': thread, 'is_post': False, 'kind': 'reply_to_comment'})
            available(context, adapter=adapter)
        else:
            original = client.get('/comments/' + mid)
        root = uuid(original['post_id'])
        author = uuid(original['author']['id'])
        if original.get('author_id') is not None and uuid(original['author_id']) != author:
            raise MailError('reply_author_mismatch')
        # Moltbook omits parent_id for top-level comments and explicitly reports depth 0.
        if adapter == 'moltbook' and 'parent_id' not in original and type(original.get('depth')) is int and original['depth'] == 0:
            parent = None
        else:
            parent = original['parent_id']  # Missing relationship evidence must fail closed.
        body = original['body' if adapter == 'the-colony' else 'content']
        basis = 'anonymous_original'
    if uuid(original['id']) != mid:
        raise MailError('reply_identity_mismatch')
    if root != thread or mid == thread:
        raise MailError('reply_thread_mismatch')
    available(original, adapter=adapter)
    return {'reply_id': mid, 'thread_id': root, 'author_id': author,
            'target_id': uuid(parent) if parent is not None else root, 'body_sha256': replies.digest(body),
            'availability_basis': basis, 'provider_status': original.get('verification_status', 'available')}, body


def execute(store, sources, source, message_id, *, key, ref):
    shown, _ = replies.execute(store, 'show', source, message_id)
    attempt = shown['reply']
    if attempt is None:
        raise MailError('reply_not_prepared')
    if key != attempt['idempotency_key']:
        raise MailError('reply_key_mismatch')
    if attempt['state'] == 'prepared':
        raise MailError('reply_not_started')
    if not sources:
        raise MailError('config_missing')
    if source not in sources:
        raise MailError('source_not_found')
    settings = dict(sources[source])
    adapter = settings.get('adapter', source)
    if adapter not in ADAPTERS:
        raise MailError('reply_verification_unsupported')
    with store.connect() as db:
        check_source(db, source, settings)
    thread = uuid(shown['message']['thread_id'])
    mid = candidate(adapter, ref, thread)
    if any(value not in (None, ref) for value in (attempt['reply_ref'], shown['message']['reply_ref'])):
        raise MailError('reply_reference_conflict')
    evidence = {'adapter': adapter, 'reply_ref': ref, 'idempotency_key': key, 'key_scope': 'local',
                'checked_at': int(time.time()), 'status': 'unverified', 'reason': None}
    try:
        observed, body = read(adapter, settings, mid, thread)
        evidence.update(observed)
        if observed['author_id'] != uuid(settings['account_id']):
            raise MailError('reply_author_mismatch')
        if observed['target_id'] != message_id or mid == message_id:
            raise MailError('reply_target_mismatch')
        if observed['body_sha256'] != attempt['body_sha256']:
            raise MailError('reply_readback_mismatch')
    except providers.FAILURES as exc:
        evidence['reason'] = providers.error_code(exc)
        evidence['checked_at'] = int(time.time())
        # Show the current state if another caller confirmed while this read was in flight.
        result, _ = replies.execute(store, 'show', source, message_id)
        result['verification'] = evidence
        if result['reply']['state'] == 'unknown':
            result['next_action'] = 'reconcile_publication_before_retry'
        return result, 1
    evidence['status'] = 'verified'
    evidence['checked_at'] = int(time.time())
    return replies.execute(store, 'confirm', source, message_id, key=key, ref=ref, readback_body=body,
                           verification=evidence, settings=settings)
