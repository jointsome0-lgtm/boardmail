"""The shape of the inbox file: every statement that gives it a table, a column or an index, and every question
about what it has. No other module writes one of these, and only the store calls this one, where it opens a
file and where init creates one.

An inbox file has every part. init creates it whole. A file that an older release left is short of some, and
complete gives it those when a command first opens it. So no command asks whether a part is there.
tests/file_shape.txt has the shape of a file, and what each kind of older file gets.
The file keeps the text of a statement as it is written here, line breaks and spaces included.
"""
from .config import MailError

VERSION = 2

# Brief context looks up one reply reference for each message it shows.
REPLY_INDEX = "CREATE INDEX IF NOT EXISTS messages_reply_ref ON messages (source,reply_ref)"

# The tables that an older release gave a file when a command first needed them, each with its index if it has
# one. So an older file can be short of any of them.
LATER = {
    "adapter_state": ("""CREATE TABLE IF NOT EXISTS adapter_state (
    source TEXT PRIMARY KEY, adapter TEXT NOT NULL, revision INTEGER NOT NULL,
    state TEXT NOT NULL, backlog_pending INTEGER NOT NULL DEFAULT 0)""",),
    "originals": ("""CREATE TABLE IF NOT EXISTS originals (
    source TEXT NOT NULL, id TEXT NOT NULL, value TEXT NOT NULL,
    fetched_at INTEGER NOT NULL, PRIMARY KEY (source,id))""",),
    "subscriptions": ("""CREATE TABLE IF NOT EXISTS subscriptions (
    source TEXT NOT NULL, thread_id TEXT NOT NULL, subscribed_at INTEGER NOT NULL,
    PRIMARY KEY (source,thread_id))""",),
    "reader_settings": ("CREATE TABLE IF NOT EXISTS reader_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)",),
    "thread_tags": ("""CREATE TABLE IF NOT EXISTS thread_tags (
    tag TEXT NOT NULL, source TEXT NOT NULL, thread_id TEXT NOT NULL,
    tagged_at INTEGER NOT NULL, PRIMARY KEY (tag,source,thread_id))""",
                    "CREATE INDEX IF NOT EXISTS thread_tags_membership ON thread_tags(source,thread_id,tag)"),
    "reply_attempts": ("""CREATE TABLE IF NOT EXISTS reply_attempts (
    source TEXT NOT NULL, message_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE, body TEXT NOT NULL, body_sha256 TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('prepared','unknown','confirmed')),
    prepared_at INTEGER NOT NULL, attempted_at INTEGER, confirmed_at INTEGER,
    reply_ref TEXT, readback_sha256 TEXT,
    PRIMARY KEY (source,message_id))""",),
    "reply_verifications": ("""CREATE TABLE IF NOT EXISTS reply_verifications (
    source TEXT NOT NULL, message_id TEXT NOT NULL, evidence TEXT NOT NULL,
    PRIMARY KEY (source,message_id))""",),
    "reply_candidates": ("""CREATE TABLE IF NOT EXISTS reply_candidates (
    source TEXT NOT NULL, message_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
    reply_ref TEXT NOT NULL, adapter TEXT NOT NULL, account_id TEXT NOT NULL,
    recorded_at INTEGER NOT NULL, PRIMARY KEY (source,message_id,reply_ref))""",),
    "reply_candidate_checks": ("""CREATE TABLE IF NOT EXISTS reply_candidate_checks (
    source TEXT NOT NULL, message_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
    reply_ref TEXT NOT NULL, checked_at INTEGER NOT NULL, reason TEXT NOT NULL,
    PRIMARY KEY (source,message_id,reply_ref))""",),
}

# The columns that an older file can be short of, each as it is added. A row from before has NULL in one of
# messages, and is not paused.
LATER_COLUMNS = {"messages": {"discovery": "TEXT", "addressing": "TEXT"},
                 "sources": {"paused": "INTEGER NOT NULL DEFAULT 0"}}

# Every table and index that an older file can be short of, by name.
PARTS = {"messages_reply_ref", "thread_tags_membership", *LATER}


def version(db):
    return db.execute("PRAGMA user_version").fetchone()[0]


def check_version(db):
    if version(db) not in (1, VERSION):
        raise MailError("unsupported_database")


def columns(db, table):
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


def lacks(db):
    there = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
    return (version(db) != VERSION or not PARTS <= there
            or any(not added.keys() <= columns(db, table) for table, added in LATER_COLUMNS.items()))


def create(db):
    db.execute("""CREATE TABLE messages (
                arrival_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL, id TEXT NOT NULL, thread_id TEXT NOT NULL,
                parent_id TEXT, provider_seq INTEGER, kind TEXT NOT NULL,
                author TEXT, title TEXT NOT NULL, body TEXT NOT NULL, url TEXT NOT NULL,
                created_at INTEGER NOT NULL, arrived_at INTEGER NOT NULL,
                read_at INTEGER, needs_reply INTEGER NOT NULL DEFAULT 0,
                replied_at INTEGER, reply_ref TEXT, discovery TEXT, addressing TEXT, UNIQUE(source, id))""")
    db.execute(REPLY_INDEX)
    db.execute("""CREATE TABLE sources (
                source TEXT PRIMARY KEY, account_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'unknown', last_checked INTEGER,
                last_ok INTEGER, error TEXT, unavailable INTEGER NOT NULL DEFAULT 0,
                paused INTEGER NOT NULL DEFAULT 0)""")
    db.execute(f"PRAGMA user_version={VERSION}")
    later(db)


def later(db):
    for statements in LATER.values():
        for statement in statements:
            db.execute(statement)


def complete(db, since_v1):
    """Give a file that an older release left each part that it is short of, and the version number of this
    release. Every row stays as it is. A file that has every part is left alone.

    since_v1 names the boards that version 1 had. It knew a source by the name of its board and kept no row
    that says so. A version-1 file gets that row for each such source, unless a later release has written one."""
    if not lacks(db):
        return
    older = version(db) == 1
    for table, added in LATER_COLUMNS.items():
        had = columns(db, table)
        for column, kind in added.items():
            if column not in had:
                # Additive, and nullable or with a default, so that an older release can go on with the file.
                # docs/reference.md names the releases that were checked.
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
    # An index changes no row and no version: 0.14.2 and earlier still read and write this file.
    db.execute(REPLY_INDEX)
    later(db)
    if older:
        for source in since_v1:
            db.execute("""INSERT OR IGNORE INTO adapter_state
                SELECT source,source,0,'{}',0 FROM sources WHERE source=?""", (source,))
    db.execute(f"PRAGMA user_version={VERSION}")
