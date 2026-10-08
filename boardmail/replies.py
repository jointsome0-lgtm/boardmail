"""Durable reply intentions and atomic receipts; the caller owns publication."""
import hashlib
import json
import time
from urllib.parse import urlsplit
from uuid import uuid4

from . import boards, reader, schema
from .config import MailError, converted, identifier
from .errors import route

MAX_BODY_BYTES = 65536
PAGE_SIZE = 20
MAX_CANDIDATES = 8
NEXT_ACTION = {'prepared': 'begin_before_publishing', 'unknown': 'read_back_before_retry',
               'confirmed': 'do_not_publish_again'}
RECOVERY_GUIDANCE = (
    "This attempt's external outcome remains unresolved. A replied mark or matching publication "
    "does not identify which provider request created it; the saved key identifies this local journal entry. "
    "A lookup limited to retained publications cannot exclude an earlier commit followed by deletion. "
    "A failed verification leaves the attempt unknown. Accepting the published reply's target does not "
    "resolve the earlier request's outcome. Preserve these distinctions and the missing evidence in the "
    "handoff before deciding whether retry or confirmation is justified."
)


def digest(body, argument=None):
    """The SHA-256 of a reply text. argument is the argument that the text was given as, for the error."""
    try:
        if not isinstance(body, str) or not body.strip() or '\0' in body:
            raise ValueError()
        raw = body.encode('utf-8')
        if len(raw) > MAX_BODY_BYTES:
            raise ValueError()
    except (ValueError, UnicodeError):
        raise MailError('invalid_reply_body', argument=argument) from None
    return hashlib.sha256(raw).hexdigest()


def read_body(path, argument=None):
    """Bounded UTF-8 read; preserve line endings and the final newline exactly. argument is the argument that
    names the file, for the error."""
    try:
        with path.open('rb') as stream:
            raw = stream.read(MAX_BODY_BYTES + 1)
        if len(raw) > MAX_BODY_BYTES:
            raise ValueError()
        body = raw.decode('utf-8')
        digest(body, argument)
        return body
    except (OSError, ValueError, UnicodeError):
        raise MailError('invalid_reply_body', argument=argument) from None


def reference(ref):
    try:
        identifier(ref)
        url = urlsplit(ref)
        if url.scheme not in ('http', 'https') or not url.hostname or url.username is not None or url.password is not None:
            raise ValueError()
        url.port
    except (ValueError, TypeError, AttributeError):
        raise MailError('reply_ref_required', argument='ref') from None
    return ref


# These three read for show and for the commands that write. writing says that the connection writes: it has no
# stand-in for a table that the file lacks, so the question goes to the schema module first.
def saved(db, source, message_id, writing=False):
    if writing and not schema.has(db, 'reply_attempts'):
        return None
    row = db.execute('SELECT * FROM reply_attempts WHERE source=? AND message_id=?', (source, message_id)).fetchone()
    return dict(row) if row else None


def receipt(db, source, message_id, attempt, writing=False):
    if not attempt or writing and not schema.has(db, 'reply_verifications'):
        return None
    row = db.execute('SELECT evidence FROM reply_verifications WHERE source=? AND message_id=?', (source, message_id)).fetchone()
    evidence = json.loads(row[0]) if row else None
    # Older receipts used the same local binding; describing it does not refresh their evidence.
    return {**evidence, 'key_scope': 'local'} if evidence and evidence['idempotency_key'] == attempt['idempotency_key'] else None


def candidates(db, source, message_id, attempt, writing=False):
    """Caller-supplied references, separate from receipts and confirmed reply URLs."""
    if not attempt or attempt['state'] != 'unknown' or writing and not schema.has(db, 'reply_candidates'):
        return []
    checks = {}
    if not writing or schema.has(db, 'reply_candidate_checks'):
        checks = {row['reply_ref']: {'checked_at': row['checked_at'], 'reason': row['reason'],
                                     'status': 'unverified'}
                  for row in db.execute('SELECT reply_ref,checked_at,reason FROM reply_candidate_checks '
                                        'WHERE source=? AND message_id=? AND idempotency_key=?',
                                        (source, message_id, attempt['idempotency_key']))}
    rows = db.execute('SELECT reply_ref,adapter,account_id,recorded_at FROM reply_candidates '
                      'WHERE source=? AND message_id=? AND idempotency_key=? ORDER BY recorded_at,reply_ref',
                      (source, message_id, attempt['idempotency_key']))
    return [{**dict(row), 'status': 'unverified', 'identity_basis': 'parsed_reference',
             'last_check': checks.get(row['reply_ref'])} for row in rows]


def summary(source, message_id, attempt):
    if attempt is None:
        return None
    return {'state': attempt['state'], 'next_action': NEXT_ACTION[attempt['state']],
            'show': route('reply_show', source=source, id=message_id)}


def pending(db, after=0, limit=PAGE_SIZE):
    """Bounded discovery in the caller's read transaction; never creates a journal."""
    for name, value, least, most in (('after', after, 0, 2**63-1), ('limit', limit, 1, 100)):
        if type(value) is not int or not least <= value <= most:
            raise MailError('invalid_arguments', argument=name)
    counts = {state: 0 for state in NEXT_ACTION}
    counts.update(db.execute('SELECT state, COUNT(*) FROM reply_attempts GROUP BY state'))
    rows = db.execute("""SELECT m.arrival_seq, a.source, a.message_id id, a.state
        FROM reply_attempts a JOIN messages m ON m.source=a.source AND m.id=a.message_id
        WHERE a.state IN ('prepared','unknown') AND m.arrival_seq>?
        ORDER BY m.arrival_seq LIMIT ?""", (after, limit + 1)).fetchall()
    items = [{'arrival_seq': row['arrival_seq'], 'source': row['source'], 'id': row['id'],
              **summary(row['source'], row['id'], row)} for row in rows[:limit]]
    next_after = items[-1]['arrival_seq'] if items else after
    more = len(rows) > limit
    return {'counts': counts, 'items': items, 'has_more': more, 'next_after': next_after,
            'next': route('reply_list', after=next_after, limit=limit) if more else None}


def check_source(db, source, settings, writing=False):
    """Used before fetching and again inside the confirmation transaction."""
    row = db.execute('SELECT * FROM sources WHERE source=?', (source,)).fetchone()
    if row is None:
        raise MailError('source_not_found')
    if row['account_id'] != settings['account_id']:
        raise MailError('account_mismatch')
    if writing:
        # No stand-ins on a connection that writes: the file may have no pause column and no adapter_state.
        row = schema.whole('sources', row)
    if row['paused']:
        raise MailError('source_paused')
    previous = None
    if not writing or schema.has(db, 'adapter_state'):
        previous = db.execute('SELECT adapter FROM adapter_state WHERE source=?', (source,)).fetchone()
    # Schema v1 had three fixed source names and no adapter aliases or state table.
    expected = previous['adapter'] if previous else source if boards.declared(source).since_v1 else None
    if expected is None:
        raise MailError('reply_adapter_identity_unknown')
    if expected != boards.owner(source, settings):
        raise MailError('adapter_mismatch')


def execute(store, action, source, message_id, *, body=None, key=None, readback_body=None, ref=None, replace_key=None,
            verification=None, settings=None):
    """One SQLite transaction binds each transition to a saved incoming and key."""
    if action not in ('prepare', 'begin', 'show', 'confirm'):
        raise MailError('invalid_arguments')
    for name, value in (('source', source), ('id', message_id), ('key', key), ('replace_key', replace_key)):
        if value is not None or name in ('source', 'id'):
            converted(identifier, value, error='invalid_arguments', argument=name)
    if (action != 'prepare' and (body is not None or replace_key is not None)
            or action not in ('begin', 'confirm') and key is not None
            or action != 'confirm' and (ref is not None or readback_body is not None)
            or action in ('begin', 'confirm') and key is None):
        raise MailError('invalid_arguments')
    body_sha = digest(body, 'body') if action == 'prepare' else None
    readback_sha = digest(readback_body, 'readback_body') if action == 'confirm' else None
    if action == 'confirm':
        reference(ref)

    changed, send_allowed = False, False
    writing = action != 'show'
    with store.connect(write=writing) as db:
        row = db.execute('SELECT * FROM messages WHERE source=? AND id=?', (source, message_id)).fetchone()
        if row is None:
            raise MailError('message_not_found')
        message = store.record(row, db, writing)
        if verification is not None:
            check_source(db, source, settings, writing)
            if (action != 'confirm' or verification['thread_id'] != message['thread_id']
                    or verification['target_id'] != message_id):
                raise MailError('reply_target_mismatch')
        attempt = saved(db, source, message_id, writing)
        now = int(time.time())
        if action == 'prepare':
            if replace_key is not None and (attempt is None or attempt['idempotency_key'] != replace_key):
                raise MailError('reply_key_mismatch')
            if attempt is not None and attempt['body'] == body:
                pass  # A repeated preparation never resets a state or mints a new key.
            else:
                if message['reply_ref'] is not None or message['replied_at'] is not None:
                    raise MailError('reply_already_recorded')
                if attempt is not None:
                    if attempt['state'] != 'prepared':
                        raise MailError('reply_already_started')
                    if replace_key is None:
                        raise MailError('reply_body_conflict')
                schema.add(db, 'reply_attempts')
                values = (str(uuid4()), body, body_sha, now, source, message_id)
                if attempt is None:
                    db.execute("""INSERT INTO reply_attempts
                        (idempotency_key,body,body_sha256,prepared_at,source,message_id,state)
                        VALUES (?,?,?,?,?,?,'prepared')""", values)
                else:
                    db.execute("""UPDATE reply_attempts SET idempotency_key=?,body=?,body_sha256=?,prepared_at=?
                        WHERE source=? AND message_id=?""", values)
                changed = True
        elif action in ('begin', 'confirm'):
            if attempt is None:
                raise MailError('reply_not_prepared')
            if key != attempt['idempotency_key']:
                raise MailError('reply_key_mismatch')
            if action == 'begin' and attempt['state'] == 'prepared':
                if message['reply_ref'] is not None or message['replied_at'] is not None:
                    raise MailError('reply_already_recorded')
                db.execute("UPDATE reply_attempts SET state='unknown',attempted_at=? WHERE source=? AND message_id=?",
                           (now, source, message_id))
                changed = send_allowed = True
            elif action == 'confirm':
                if attempt['state'] == 'prepared':
                    raise MailError('reply_not_started')
                if readback_sha != attempt['body_sha256']:
                    raise MailError('reply_readback_mismatch')
                if (message['reply_ref'] not in (None, ref)
                        or attempt['reply_ref'] not in (None, ref)):
                    raise MailError('reply_reference_conflict')
                if attempt['state'] != 'confirmed':
                    db.execute("""UPDATE reply_attempts SET state='confirmed',confirmed_at=?,reply_ref=?,readback_sha256=?
                        WHERE source=? AND message_id=?""", (now, ref, readback_sha, source, message_id))
                    db.execute('UPDATE messages SET replied_at=?,reply_ref=? WHERE source=? AND id=?',
                               (now, ref, source, message_id))
                    message.update(replied_at=now, reply_ref=ref)
                    changed = True
                if verification is not None:
                    schema.add(db, 'reply_verifications')
                    db.execute('INSERT INTO reply_verifications VALUES (?,?,?) ON CONFLICT(source,message_id) '
                               'DO UPDATE SET evidence=excluded.evidence',
                               (source, message_id, json.dumps(verification, ensure_ascii=True)))
                    changed = True
        attempt = saved(db, source, message_id, writing)
        evidence = receipt(db, source, message_id, attempt, writing)
        references = candidates(db, source, message_id, attempt, writing)

    if attempt is None:
        following = 'inspect_recorded_reply' if message['reply_ref'] else 'prepare_reply'
    else:
        following = NEXT_ACTION[attempt['state']]
    if send_allowed:
        following = 'publish_saved_body_with_saved_key_then_read_back'
    basis = None
    if attempt is not None and attempt['state'] == 'confirmed':
        basis = 'provider_readback' if evidence else 'caller_supplied_readback'
    return {'event': 'reply_attempt', 'message': reader.written(message), 'reply': attempt,
            'changed': changed, 'send_allowed': send_allowed, 'next_action': following,
            'confirmation_basis': basis, 'reply_candidates': references,
            'recovery_guidance': RECOVERY_GUIDANCE if attempt and attempt['state'] == 'unknown' and not send_allowed else None,
            'remote_verified': verification is not None, 'verification': verification, 'verification_receipt': evidence,
            'publication_performed': False, 'collection_performed': False}, 0
