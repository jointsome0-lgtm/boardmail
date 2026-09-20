"""Local thread membership and topic views; independent of collection and marks."""
import json
import re
import time

from .config import MailError, identifier

NAME_PATTERN = r"[a-z0-9][a-z0-9_-]{0,63}"
SCHEMA = """CREATE TABLE IF NOT EXISTS thread_tags (
    tag TEXT NOT NULL, source TEXT NOT NULL, thread_id TEXT NOT NULL,
    tagged_at INTEGER NOT NULL, PRIMARY KEY (tag,source,thread_id))"""


def validate_name(tag):
    if not isinstance(tag, str) or re.fullmatch(NAME_PATTERN, tag) is None:
        raise MailError('invalid_tag_name')


def exists(db):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='thread_tags'").fetchone())


def names(db, source, thread):
    if not exists(db):
        return []
    return [r[0] for r in db.execute(
        'SELECT tag FROM thread_tags WHERE source=? AND thread_id=? ORDER BY tag', (source, thread))]


def predicate(db, tag=None, untagged=False):
    """A message predicate, so filtering precedes LIMIT and membership cannot duplicate rows."""
    if type(untagged) is not bool or tag is not None and untagged:
        raise MailError('invalid_arguments')
    if tag is not None:
        validate_name(tag)
    if tag is None and not untagged:
        return '1', ()
    if not exists(db):
        return ('0' if tag is not None else '1'), ()
    selected = ('EXISTS (SELECT 1 FROM thread_tags t WHERE t.source=messages.source '
                'AND t.thread_id=messages.thread_id' + (' AND t.tag=?' if tag is not None else '') + ')')
    return ('NOT ' if untagged else '') + selected, (tag,) if tag is not None else ()


def read_action(*, unread=True, **filters):
    return {'command': 'list', 'tool': 'boardmail_list',
            'arguments': {'after': 0, 'unread': unread, 'scope': 'all', **filters}}


def overview(db):
    rows = []
    if exists(db):
        rows = [dict(row) for row in db.execute("""WITH counts AS (
            SELECT source,thread_id,COUNT(*) AS messages,SUM(read_at IS NULL) AS unread
            FROM messages GROUP BY source,thread_id)
            SELECT t.tag,COUNT(*) AS threads,COALESCE(SUM(c.messages),0) AS messages,
                   COALESCE(SUM(c.unread),0) AS unread
            FROM thread_tags t LEFT JOIN counts c ON t.source=c.source AND t.thread_id=c.thread_id
            GROUP BY t.tag ORDER BY t.tag""")]
    for row in rows:
        row['read'] = read_action(tag=row['tag'])
        row['show'] = {'command': 'tag show', 'tool': 'boardmail_tag_show', 'arguments': {'tag': row['tag']}}
    clause, values = predicate(db, untagged=True)
    untagged = dict(db.execute("""SELECT COUNT(*) AS threads,COALESCE(SUM(messages),0) AS messages,
        COALESCE(SUM(unread),0) AS unread FROM (
        SELECT COUNT(*) AS messages,SUM(read_at IS NULL) AS unread FROM messages WHERE """ + clause +
        ' GROUP BY source,thread_id)', values).fetchone())
    total_unread = db.execute('SELECT COUNT(*) FROM messages WHERE read_at IS NULL').fetchone()[0]
    return {'event': 'tags', 'tags': rows, 'untagged': {**untagged, 'read': read_action(untagged=True)},
            'counts': {'unread': total_unread, 'tagged_unread': total_unread - untagged['unread'],
                       'untagged_unread': untagged['unread']},
            'collection_performed': False, 'next_action': 'choose_tag_or_untagged'}


def metadata(db, source, thread):
    """Use only observed local labels/links, with their provenance; never synthesize a root URL."""
    candidates = []
    root = db.execute('SELECT id,title,url FROM messages WHERE source=? AND id=? AND thread_id=?',
                      (source, thread, thread)).fetchone()
    if root is not None:
        candidates.append(('stored_root', dict(root)))
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='originals'").fetchone():
        row = db.execute('SELECT value FROM originals WHERE source=? AND id=?', (source, thread)).fetchone()
        if row is not None:
            original = json.loads(row[0])
            if original['id'] == thread and original['thread_id'] == thread:
                candidates.append(('cached_root', original))
    for field in ('title', 'url'):
        for row in db.execute('SELECT id,title,url FROM messages WHERE source=? AND thread_id=? AND '
                              + field + "<>'' ORDER BY arrival_seq", (source, thread)):
            if row[field].strip():
                candidates.append(('stored_message', dict(row)))
                break
    result = {'title': None, 'title_origin': None, 'title_message_id': None, 'title_truncated': False,
              'url': None, 'url_origin': None, 'url_message_id': None}
    for field in ('title', 'url'):
        for origin, candidate in candidates:
            value = candidate.get(field)
            if isinstance(value, str) and value.strip():
                result[field] = value[:160] if field == 'title' else value
                result[field + '_origin'] = origin
                result[field + '_message_id'] = candidate['id']
                if field == 'title':
                    result['title_truncated'] = len(value) > 160
                break
    return result


def show(db, store, tag):
    members = []
    subscriptions = {(r['source'], r['thread']) for r in store._subscriptions(db)}
    if exists(db):
        members = [dict(row) for row in db.execute("""SELECT t.source,t.thread_id AS thread,t.tagged_at,
            COUNT(m.id) AS messages,COALESCE(SUM(m.id IS NOT NULL AND m.read_at IS NULL),0) AS unread
            FROM thread_tags t LEFT JOIN messages m ON t.source=m.source AND t.thread_id=m.thread_id
            WHERE t.tag=? GROUP BY t.source,t.thread_id,t.tagged_at ORDER BY t.source,t.thread_id""", (tag,))]
    for member in members:
        source, thread = member['source'], member['thread']
        member.update(metadata(db, source, thread), tags=names(db, source, thread),
                      subscribed=(source, thread) in subscriptions,
                      read=read_action(source=source, thread=thread, unread=False))
    return {'event': 'tag', 'tag': tag, 'exists': bool(members), 'threads': members,
            'counts': {'threads': len(members), 'messages': sum(r['messages'] for r in members),
                       'unread': sum(r['unread'] for r in members)},
            'read': read_action(tag=tag), 'collection_performed': False,
            'next_action': 'read_selected_thread_or_tag'}


def execute(store, action, *, tag=None, source=None, thread=None, id=None):
    if action not in ('list', 'show', 'add', 'remove'):
        raise MailError('invalid_arguments')
    if action != 'list':
        validate_name(tag)
    if action in ('list', 'show'):
        if any(value is not None for value in (source, thread, id)) or action == 'list' and tag is not None:
            raise MailError('invalid_arguments')
    else:
        if (thread is None) == (id is None):
            raise MailError('invalid_arguments')
        try:
            identifier(source)
            identifier(thread if thread is not None else id)
        except (ValueError, TypeError, AttributeError):
            raise MailError('invalid_arguments') from None
    with store.connect(write=action in ('add', 'remove')) as db:
        if action == 'list':
            return overview(db), 0
        if action == 'show':
            return show(db, store, tag), 0
        # Source identity already belongs to this inbox. Tagging never creates or rebinds it.
        if not db.execute('SELECT 1 FROM sources WHERE source=?', (source,)).fetchone():
            raise MailError('source_not_found')
        if id is not None:
            row = db.execute('SELECT thread_id FROM messages WHERE source=? AND id=?', (source, id)).fetchone()
            if row is None:
                raise MailError('message_not_found')
            thread = row[0]
        changed = False
        if action == 'add':
            db.execute(SCHEMA)
            db.execute('CREATE INDEX IF NOT EXISTS thread_tags_membership ON thread_tags(source,thread_id,tag)')
            changed = bool(db.execute('INSERT OR IGNORE INTO thread_tags VALUES (?,?,?,?)',
                                     (tag, source, thread, int(time.time()))).rowcount)
        elif exists(db):
            changed = bool(db.execute('DELETE FROM thread_tags WHERE tag=? AND source=? AND thread_id=?',
                                     (tag, source, thread)).rowcount)
        return {'event': 'thread_tag', 'tag': tag, 'source': source, 'thread': thread,
                'tagged': action == 'add', 'changed': changed, 'collection_performed': False,
                'next_action': 'show_tag_or_read_saved_mail'}, 0
