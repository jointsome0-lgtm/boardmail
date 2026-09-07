"""SQLite state and a read-only wait predicate over committed arrival numbers."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import threading
import time
from urllib.parse import urlsplit

from .config import COVERAGE, MailError

STALE_AFTER = 540
SCHEMA_VERSION = 1


class Store:
    def __init__(self, path):
        self.path = Path(path).expanduser()

    @contextmanager
    def connect(self, *, write=False, create=False):
        if not create and not self.path.is_file():
            raise MailError("database_missing")
        uri = self.path.resolve().as_uri() + ("?mode=rw" if write else "?mode=ro")
        db = sqlite3.connect(str(self.path) if create else uri, uri=not create, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            if not create and db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                raise MailError("unsupported_database")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            with db:
                yield db
        finally:
            db.close()

    def initialize(self, sources=None):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.path.touch(mode=0o600, exist_ok=False)
        except FileExistsError:
            raise MailError("database_exists") from None
        with self.connect(write=True, create=True) as db:
            db.execute("""CREATE TABLE messages (
                arrival_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL, id TEXT NOT NULL, thread_id TEXT NOT NULL,
                parent_id TEXT, provider_seq INTEGER, kind TEXT NOT NULL,
                author TEXT, title TEXT NOT NULL, body TEXT NOT NULL, url TEXT NOT NULL,
                created_at INTEGER NOT NULL, arrived_at INTEGER NOT NULL,
                read_at INTEGER, needs_reply INTEGER NOT NULL DEFAULT 0,
                replied_at INTEGER, reply_ref TEXT, UNIQUE(source, id))""")
            db.execute("""CREATE TABLE sources (
                source TEXT PRIMARY KEY, account_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'unknown', last_checked INTEGER,
                last_ok INTEGER, error TEXT, unavailable INTEGER NOT NULL DEFAULT 0)""")
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            for source, settings in (sources or {}).items():
                db.execute("INSERT INTO sources (source, account_id) VALUES (?, ?)",
                           (source, settings["account_id"]))

    def known(self, source, account_id):
        with self.connect() as db:
            self._check_account(db, source, account_id)
            return {r[0] for r in db.execute("SELECT id FROM messages WHERE source=?", (source,))}

    @staticmethod
    def _check_account(db, source, account_id):
        previous = db.execute("SELECT account_id FROM sources WHERE source=?", (source,)).fetchone()
        if previous and previous[0] != account_id:
            raise MailError("account_mismatch")

    def save(self, source, account_id, messages, *, unavailable=0, error=None, now=None):
        stamp = int(time.time()) if now is None else now
        with self.connect(write=True) as db:
            self._check_account(db, source, account_id)
            added = 0
            for item in sorted(messages, key=lambda m: (m["created_at"], m.get("provider_seq") or 0, m["id"])):
                if db.execute("SELECT 1 FROM messages WHERE source=? AND id=?", (source, item["id"])).fetchone():
                    continue
                db.execute("""INSERT INTO messages
                    (source,id,thread_id,parent_id,provider_seq,kind,author,title,body,url,created_at,arrived_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (source, item["id"], item["thread_id"],
                    item.get("parent_id"), item.get("provider_seq"), item["kind"], item.get("author"),
                    item["title"], item["body"], item["url"], item["created_at"], stamp))
                added += 1
            db.execute("""INSERT INTO sources (source,account_id,status,last_checked,last_ok,unavailable,error)
                VALUES (?,?,?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET
                status=excluded.status,last_checked=excluded.last_checked,
                last_ok=COALESCE(excluded.last_ok,sources.last_ok),error=excluded.error,
                unavailable=excluded.unavailable""",
                (source,account_id,"error" if error else "ok",stamp,None if error else stamp,unavailable,error))
            return added

    def failure(self, source, account_id, error, *, now=None):
        stamp = int(time.time()) if now is None else now
        with self.connect(write=True) as db:
            db.execute("""INSERT INTO sources (source,account_id,status,last_checked,error)
                VALUES (?,?,'error',?,?) ON CONFLICT(source) DO UPDATE SET
                status='error',last_checked=excluded.last_checked,error=excluded.error""",
                (source,account_id,stamp,error))

    @staticmethod
    def _health(db):
        result = []
        now = time.time()
        for row in db.execute("SELECT * FROM sources ORDER BY source"):
            value = dict(row)
            if value["status"] == "ok" and now - value["last_ok"] > STALE_AFTER:
                value["status"] = "stale"
            value.update(coverage=COVERAGE[value["source"]], history_complete=False)
            result.append(value)
        return result

    @staticmethod
    def _message(row):
        item = dict(row)
        item["needs_reply"] = bool(item["needs_reply"])
        return item

    def page(self, after=0, limit=100, *, unread=False):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM messages WHERE arrival_seq>?" +
                (" AND read_at IS NULL" if unread else "") + " ORDER BY arrival_seq LIMIT ?",
                (after,limit+1)).fetchall()
            selected = rows[:limit]
            return {"messages": [self._message(r) for r in selected],
                    "next_after": selected[-1]["arrival_seq"] if selected else after,
                    "more": len(rows)>limit, "sources": self._health(db)}

    def status(self):
        with self.connect() as db:
            counts = db.execute("""SELECT COUNT(*) total, COALESCE(MAX(arrival_seq),0) latest_arrival,
                COALESCE(SUM(read_at IS NULL),0) unread, COALESCE(SUM(needs_reply),0) needs_reply,
                COALESCE(SUM(replied_at IS NOT NULL),0) replied FROM messages""").fetchone()
            return {"counts": dict(counts), "sources": self._health(db)}

    def show(self, source, message_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM messages WHERE source=? AND id=?", (source,message_id)).fetchone()
            if row is None:
                raise MailError("message_not_found")
            return self._message(row)

    def mark(self, source, message_id, action, *, ref=None):
        clauses = {"read": ("read_at=COALESCE(read_at,?)", (int(time.time()),)),
                   "unread": ("read_at=NULL", ()), "needs_reply": ("needs_reply=1", ()),
                   "clear_reply": ("needs_reply=0", ())}
        if action == "replied":
            parsed = urlsplit(ref or "")
            if parsed.scheme not in ("http","https") or not parsed.netloc or parsed.username or parsed.password:
                raise MailError("reply_ref_required")
            clauses[action] = ("replied_at=?,reply_ref=?", (int(time.time()),ref))
        if action not in clauses or (ref is not None and action != "replied"):
            raise MailError("invalid_mark")
        clause, values = clauses[action]
        with self.connect(write=True) as db:
            changed = db.execute(f"UPDATE messages SET {clause} WHERE source=? AND id=?",
                                 (*values,source,message_id)).rowcount
            if not changed:
                raise MailError("message_not_found")

    def wait(self, after, timeout, limit=100, *, cancelled=None):
        cancelled = cancelled or threading.Event()
        deadline = time.monotonic()+timeout
        while True:
            result = self.page(after,limit)
            if cancelled.is_set():
                return {"event":"cancelled", "messages":[], "next_after":after,
                        "more":bool(result["messages"]), "sources":result["sources"]}
            if result["messages"]:
                return {"event":"messages", **result}
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                return {"event":"timeout", **result}
            cancelled.wait(min(1,remaining))
