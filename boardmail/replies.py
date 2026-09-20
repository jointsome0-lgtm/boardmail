"""Durable reply intentions and atomic receipts; the caller owns publication."""
import hashlib
import json
import time
from urllib.parse import urlsplit
from uuid import uuid4

from .config import MailError, identifier

MAX_BODY_BYTES = 65536
SCHEMA = """CREATE TABLE IF NOT EXISTS reply_attempts (
    source TEXT NOT NULL, message_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE, body TEXT NOT NULL, body_sha256 TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('prepared','unknown','confirmed')),
    prepared_at INTEGER NOT NULL, attempted_at INTEGER, confirmed_at INTEGER,
    reply_ref TEXT, readback_sha256 TEXT,
    PRIMARY KEY (source,message_id))"""

VERIFICATION_SCHEMA = """CREATE TABLE IF NOT EXISTS reply_verifications (
    source TEXT NOT NULL, message_id TEXT NOT NULL, evidence TEXT NOT NULL,
    PRIMARY KEY (source,message_id))"""


def digest(body):
    try:
        if not isinstance(body, str) or not body.strip() or '\0' in body:
            raise ValueError()
        raw = body.encode('utf-8')
        if len(raw) > MAX_BODY_BYTES:
            raise ValueError()
    except (ValueError, UnicodeError):
        raise MailError('invalid_reply_body') from None
    return hashlib.sha256(raw).hexdigest()


def read_body(path):
    """Bounded UTF-8 read; preserve line endings and the final newline exactly."""
    try:
        with path.open('rb') as stream:
            raw = stream.read(MAX_BODY_BYTES + 1)
        if len(raw) > MAX_BODY_BYTES:
            raise ValueError()
        body = raw.decode('utf-8')
        digest(body)
        return body
    except (OSError, ValueError, UnicodeError):
        raise MailError('invalid_reply_body') from None


def reference(ref):
    try:
        identifier(ref)
        url = urlsplit(ref)
        if url.scheme not in ('http', 'https') or not url.hostname or url.username is not None or url.password is not None:
            raise ValueError()
        url.port
    except (ValueError, TypeError, AttributeError):
        raise MailError('reply_ref_required') from None
    return ref


def saved(db, source, message_id):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='reply_attempts'").fetchone():
        return None
    row = db.execute('SELECT * FROM reply_attempts WHERE source=? AND message_id=?', (source, message_id)).fetchone()
    return dict(row) if row else None


def receipt(db, source, message_id, attempt):
    if not attempt or not db.execute("SELECT 1 FROM sqlite_master WHERE name='reply_verifications'").fetchone():
        return None
    row = db.execute('SELECT evidence FROM reply_verifications WHERE source=? AND message_id=?', (source, message_id)).fetchone()
    evidence = json.loads(row[0]) if row else None
    return evidence if evidence and evidence['idempotency_key'] == attempt['idempotency_key'] else None


def execute(store, action, source, message_id, *, body=None, key=None, readback_body=None, ref=None, replace_key=None,
            verification=None, settings=None):
    """One SQLite transaction binds each transition to a saved incoming and key."""
    if action not in ('prepare', 'begin', 'show', 'confirm'):
        raise MailError('invalid_arguments')
    try:
        identifier(source)
        identifier(message_id)
        for value in (key, replace_key):
            if value is not None:
                identifier(value)
    except (ValueError, TypeError, AttributeError):
        raise MailError('invalid_arguments') from None
    if (action != 'prepare' and (body is not None or replace_key is not None)
            or action not in ('begin', 'confirm') and key is not None
            or action != 'confirm' and (ref is not None or readback_body is not None)
            or action in ('begin', 'confirm') and key is None):
        raise MailError('invalid_arguments')
    body_sha = digest(body) if action == 'prepare' else None
    readback_sha = digest(readback_body) if action == 'confirm' else None
    if action == 'confirm':
        reference(ref)

    changed, send_allowed = False, False
    with store.connect(write=action != 'show') as db:
        row = db.execute('SELECT * FROM messages WHERE source=? AND id=?', (source, message_id)).fetchone()
        if row is None:
            raise MailError('message_not_found')
        message = store._message(row)
        if verification is not None:
            from .verification import check_source
            check_source(db, source, settings)
            if (action != 'confirm' or verification['thread_id'] != message['thread_id']
                    or verification['target_id'] != message_id):
                raise MailError('reply_target_mismatch')
        attempt = saved(db, source, message_id)
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
                db.execute(SCHEMA)
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
                    db.execute(VERIFICATION_SCHEMA)
                    db.execute('INSERT INTO reply_verifications VALUES (?,?,?) ON CONFLICT(source,message_id) '
                               'DO UPDATE SET evidence=excluded.evidence',
                               (source, message_id, json.dumps(verification, ensure_ascii=True)))
                    changed = True
        attempt = saved(db, source, message_id)
        evidence = receipt(db, source, message_id, attempt)

    if attempt is None:
        following = 'inspect_recorded_reply' if message['reply_ref'] else 'prepare_reply'
    else:
        following = {'prepared': 'begin_before_publishing', 'unknown': 'read_back_before_retry',
                     'confirmed': 'do_not_publish_again'}[attempt['state']]
    if send_allowed:
        following = 'publish_saved_body_with_saved_key_then_read_back'
    basis = None
    if attempt is not None and attempt['state'] == 'confirmed':
        basis = 'provider_readback' if evidence else 'caller_supplied_readback'
    return {'event': 'reply_attempt', 'message': message, 'reply': attempt,
            'changed': changed, 'send_allowed': send_allowed, 'next_action': following,
            'confirmation_basis': basis,
            'remote_verified': verification is not None, 'verification': verification, 'verification_receipt': evidence,
            'publication_performed': False, 'collection_performed': False}, 0
