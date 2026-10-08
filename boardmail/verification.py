"""Verify a known reply reference with bounded provider reads, never publication."""
from functools import partial
import sqlite3
import time
from urllib.parse import urlsplit

from . import adapter_common, boards, replies, schema, transport
from .config import MailError, uuid


def candidate(about, ref, thread):
    """Parse an allowlisted identity. Never send a caller URL to the HTTP client. about is what the board
    declares of its replies."""
    try:
        replies.reference(ref)
        url = urlsplit(ref)
        if (url.scheme != 'https' or url.hostname not in about.hosts or url.port not in (None, 443)
                or url.query or '%' in ref or '\\' in ref):
            raise ValueError()
        parts = url.path.split('/')
        if about.direct and parts[:-1] == ['', *about.direct] and not url.fragment:
            return uuid(parts[-1])
        if len(parts) == 3 and parts[1] in about.pages and uuid(parts[2]) == thread and url.fragment.startswith('comment-'):
            return uuid(url.fragment.removeprefix('comment-'))
        raise ValueError()
    except (MailError, ValueError, TypeError, AttributeError, KeyError):
        raise MailError('reply_reference_unsupported') from None


def available(original, about):
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
    if about.verified and original.get('verification_status') != 'verified':
        raise MailError('reply_provider_not_verified')
    if any(original.get(field) is not False for field in about.explicit):
        raise MailError('reply_provider_status_unknown')


def read(about, settings, mid, thread, fetch=transport.fetch):
    """Return the original and its observed status using fixed provider endpoints.

    fetch asks the board: the transport, or an invented board in its place. The client of the board is handed
    it. about is what the board declares of its replies, and its read is what knows the endpoints."""
    client = about.client(settings, fetch=fetch)
    original, root, author, parent, body, basis = about.read(client, mid, thread, partial(available, about=about))
    if uuid(original['id']) != mid:
        raise MailError('reply_identity_mismatch')
    if root != thread or mid == thread:
        raise MailError('reply_thread_mismatch')
    available(original, about)
    return {'reply_id': mid, 'thread_id': root, 'author_id': author,
            'target_id': uuid(parent) if parent is not None else root, 'body_sha256': replies.digest(body),
            'availability_basis': basis, 'provider_status': original.get('verification_status', 'available')}, body


def save_failed_check(store, source, message_id, attempt, thread, settings, evidence):
    """Save only a safe failure code; a lost binding or write failure stays explicit."""
    if attempt['state'] != 'unknown':
        return False, False
    key, ref = evidence['idempotency_key'], evidence['reply_ref']
    try:
        with store.connect(write=True) as db:
            current = replies.saved(db, source, message_id, writing=True)
            if (not current or current['state'] != 'unknown' or current['idempotency_key'] != key
                    or current['body_sha256'] != attempt['body_sha256']):
                return False, False
            replies.check_source(db, source, settings, writing=True)
            message = db.execute('SELECT thread_id,reply_ref FROM messages WHERE source=? AND id=?',
                                 (source, message_id)).fetchone()
            if (not message or message['thread_id'] != thread
                    or any(value not in (None, ref) for value in (current['reply_ref'], message['reply_ref']))):
                return False, False
            bound = db.execute('SELECT 1 FROM reply_candidates WHERE source=? AND message_id=? '
                               'AND idempotency_key=? AND reply_ref=? AND adapter=? AND account_id=?',
                               (source, message_id, key, ref, evidence['adapter'], settings['account_id'])).fetchone()
            if not bound:
                return False, False
            # Keep the seven-column candidate table writable by 0.12.0 clients.
            # Commit order defines the last saved check; wall clocks are not an ordering key.
            schema.add(db, 'reply_candidate_checks')
            changed = db.execute('INSERT INTO reply_candidate_checks VALUES (?,?,?,?,?,?) '
                                 'ON CONFLICT(source,message_id,reply_ref) DO UPDATE SET '
                                 'idempotency_key=excluded.idempotency_key,checked_at=excluded.checked_at,reason=excluded.reason '
                                 'WHERE idempotency_key!=excluded.idempotency_key OR checked_at!=excluded.checked_at '
                                 'OR reason!=excluded.reason',
                                 (source, message_id, key, ref, evidence['checked_at'], evidence['reason'])).rowcount > 0
        return True, changed
    except (MailError, sqlite3.Error, OSError):
        return False, False


def execute(store, sources, source, message_id, *, key, ref, fetch=transport.fetch):
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
    adapter = boards.owner(source, settings)
    about = boards.declared(adapter).replies
    if about is None:
        raise MailError('reply_verification_unsupported')
    with store.connect() as db:
        replies.check_source(db, source, settings)
    thread = uuid(shown['message']['thread_id'])
    mid = candidate(about, ref, thread)
    if any(value not in (None, ref) for value in (attempt['reply_ref'], shown['message'].get('reply_ref'))):
        raise MailError('reply_reference_conflict')
    candidate_changed = False
    if attempt['state'] == 'unknown':
        # Commit the pointer before I/O. Never use receipt storage for unverified data:
        # older clients treat that table as evidence of a successful provider check.
        with store.connect(write=True) as db:
            replies.check_source(db, source, settings, writing=True)
            current = replies.saved(db, source, message_id, writing=True)
            if current is None or current['idempotency_key'] != key:
                raise MailError('reply_key_mismatch')
            message = db.execute('SELECT reply_ref FROM messages WHERE source=? AND id=?',
                                 (source, message_id)).fetchone()
            if any(value not in (None, ref) for value in (current['reply_ref'], message['reply_ref'])):
                raise MailError('reply_reference_conflict')
            if current['state'] == 'unknown':
                existing = replies.candidates(db, source, message_id, current, writing=True)
                if not any(item['reply_ref'] == ref for item in existing):
                    if len(existing) >= replies.MAX_CANDIDATES:
                        raise MailError('reply_candidate_limit')
                    schema.add(db, 'reply_candidates')
                    db.execute('INSERT INTO reply_candidates VALUES (?,?,?,?,?,?,?)',
                               (source, message_id, key, ref, adapter, settings['account_id'], int(time.time())))
                    candidate_changed = True
    evidence = {'adapter': adapter, 'reply_ref': ref, 'idempotency_key': key, 'key_scope': 'local',
                'checked_at': int(time.time()), 'status': 'unverified', 'reason': None}
    try:
        observed, body = read(about, settings, mid, thread, fetch)
        evidence.update(observed)
        if observed['author_id'] != uuid(settings['account_id']):
            raise MailError('reply_author_mismatch')
        if observed['target_id'] != message_id or mid == message_id:
            raise MailError('reply_target_mismatch')
        if observed['body_sha256'] != attempt['body_sha256']:
            raise MailError('reply_readback_mismatch')
    except adapter_common.FAILURES as exc:
        evidence['reason'] = adapter_common.error_code(exc)
        evidence['checked_at'] = int(time.time())
        check_saved, check_changed = save_failed_check(store, source, message_id, attempt, thread, settings, evidence)
        # Show the current state if another caller confirmed while this read was in flight.
        result, _ = replies.execute(store, 'show', source, message_id)
        result['changed'] = candidate_changed or check_changed
        result['last_check_saved'] = check_saved
        result['verification'] = evidence
        if result['reply']['state'] == 'unknown':
            result['next_action'] = 'reconcile_publication_before_retry'
        return result, 1
    evidence['status'] = 'verified'
    evidence['checked_at'] = int(time.time())
    return replies.execute(store, 'confirm', source, message_id, key=key, ref=ref, readback_body=body,
                           verification=evidence, settings=settings)
