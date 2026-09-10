"""SQLite state and a read-only wait predicate over committed arrival numbers."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import threading
import time
from urllib.parse import urlsplit

from .config import COVERAGE, MailError

STALE_AFTER = 540
SCHEMA_VERSION = 2

PROGRESS_SCHEMA = """CREATE TABLE IF NOT EXISTS adapter_state (
    source TEXT PRIMARY KEY, adapter TEXT NOT NULL, revision INTEGER NOT NULL,
    state TEXT NOT NULL, backlog_pending INTEGER NOT NULL DEFAULT 0)"""


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
            if not create and db.execute("PRAGMA user_version").fetchone()[0] not in (1, SCHEMA_VERSION):
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
                last_ok INTEGER, error TEXT, unavailable INTEGER NOT NULL DEFAULT 0,
                paused INTEGER NOT NULL DEFAULT 0)""")
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            db.execute(PROGRESS_SCHEMA)
            for source, settings in (sources or {}).items():
                db.execute("INSERT INTO sources (source, account_id) VALUES (?, ?)",
                           (source, settings["account_id"]))
                db.execute("INSERT INTO adapter_state VALUES (?,?,0,'{}',0)",
                           (source, str(settings.get("adapter", source))))

    def known(self, source, account_id):
        with self.connect() as db:
            self._check_account(db, source, account_id)
            return {r[0] for r in db.execute("SELECT id FROM messages WHERE source=?", (source,))}

    def is_paused(self, source):
        with self.connect() as db:
            row = db.execute("SELECT * FROM sources WHERE source=?", (source,)).fetchone()
            return bool(dict(row).get("paused", False)) if row else False

    def set_paused(self, source, paused, settings=None):
        with self.connect(write=True) as db:
            row = db.execute("SELECT * FROM sources WHERE source=?", (source,)).fetchone()
            if row is None and settings is None:
                raise MailError("source_not_found")
            if settings is not None:
                self._check_account(db, source, settings["account_id"])
            previous = bool(dict(row).get("paused", False)) if row else False
            if "paused" not in {r[1] for r in db.execute("PRAGMA table_info(sources)")}:
                # Local readers keep supporting older databases without writing.
                db.execute("ALTER TABLE sources ADD COLUMN paused INTEGER NOT NULL DEFAULT 0")
            if row is None:
                db.execute("INSERT INTO sources (source, account_id) VALUES (?, ?)",
                           (source, settings["account_id"]))
            if previous != paused:
                db.execute("UPDATE sources SET paused=? WHERE source=?", (paused, source))
            return previous != paused

    def prepare_collection(self):
        with self.connect(write=True) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            db.execute(PROGRESS_SCHEMA)
            if version == 1:
                for source in COVERAGE:
                    db.execute("""INSERT OR IGNORE INTO adapter_state
                        SELECT source,source,0,'{}',0 FROM sources WHERE source=?""", (source,))
            if "discovery" not in self._columns(db):
                # Additive and nullable: earlier 0.2.0+ readers still open this file.
                db.execute("ALTER TABLE messages ADD COLUMN discovery TEXT")
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @staticmethod
    def _columns(db):
        return {r[1] for r in db.execute("PRAGMA table_info(messages)")}

    def collection_state(self, source, account_id, adapter):
        with self.connect() as db:
            self._check_account(db, source, account_id)
            row = db.execute("SELECT * FROM adapter_state WHERE source=?", (source,)).fetchone()
            if row and row["adapter"] != adapter:
                raise MailError("adapter_mismatch")
            known = {r[0] for r in db.execute("SELECT id FROM messages WHERE source=?", (source,))}
            return known, json.loads(row["state"]) if row else {}, row["revision"] if row else 0

    def save_collection(self, source, account_id, adapter, revision, batch):
        with self.connect(write=True) as db:
            self._check_account(db, source, account_id)
            row = db.execute("SELECT adapter,revision,state FROM adapter_state WHERE source=?", (source,)).fetchone()
            if row and row["adapter"] != adapter:
                raise MailError("adapter_mismatch")
            stale = (row["revision"] if row else 0) != revision
            added = self._insert_messages(db, source, batch.messages, int(time.time()))
            if not stale:
                self._save_health(db, source, account_id, batch.unavailable, batch.error, int(time.time()))
                state = json.dumps(batch.state, allow_nan=False)
                unchanged = (json.dumps(json.loads(state), sort_keys=True) ==
                             json.dumps(json.loads(row["state"]) if row else {}, sort_keys=True))
                # A failed preflight has health to report, but cannot invalidate
                # a concurrent collector's checkpoint when it made no progress.
                advance = not (batch.error and not batch.messages and unchanged)
                db.execute("""INSERT INTO adapter_state VALUES (?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET
                    revision=excluded.revision,state=excluded.state,backlog_pending=excluded.backlog_pending""",
                    (source, adapter, revision+int(advance), state, not batch.complete))
            return added, stale

    @staticmethod
    def _check_account(db, source, account_id):
        previous = db.execute("SELECT account_id FROM sources WHERE source=?", (source,)).fetchone()
        if previous and previous[0] != account_id:
            raise MailError("account_mismatch")

    def save(self, source, account_id, messages, *, unavailable=0, error=None, now=None):
        stamp = int(time.time()) if now is None else now
        with self.connect(write=True) as db:
            self._check_account(db, source, account_id)
            added = self._insert_messages(db, source, messages, stamp)
            self._save_health(db, source, account_id, unavailable, error, stamp)
            return added

    @staticmethod
    def _insert_messages(db, source, messages, stamp):
        added = 0
        for item in sorted(messages, key=lambda m: (m["created_at"], m.get("provider_seq") or 0, m["id"])):
            if db.execute("SELECT 1 FROM messages WHERE source=? AND id=?", (source, item["id"])).fetchone():
                continue
            columns, values = "", ()
            if item.get("discovery") is not None:
                # Only collection supplies this; its migration added the column.
                columns, values = ",discovery", (item["discovery"],)
            db.execute(f"""INSERT INTO messages
                (source,id,thread_id,parent_id,provider_seq,kind,author,title,body,url,created_at,arrived_at{columns})
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?{",?"*len(values)})""", (source, item["id"], item["thread_id"],
                item.get("parent_id"), item.get("provider_seq"), item["kind"], item.get("author"),
                item["title"], item["body"], item["url"], item["created_at"], stamp, *values))
            added += 1
        return added

    @staticmethod
    def _save_health(db, source, account_id, unavailable, error, stamp):
        db.execute("""INSERT INTO sources (source,account_id,status,last_checked,last_ok,unavailable,error)
            VALUES (?,?,?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET
            status=excluded.status,last_checked=excluded.last_checked,
            last_ok=COALESCE(excluded.last_ok,sources.last_ok),error=excluded.error,
            unavailable=excluded.unavailable""",
            (source,account_id,"error" if error else "ok",stamp,None if error else stamp,unavailable,error))

    def failure(self, source, account_id, error, *, now=None):
        stamp = int(time.time()) if now is None else now
        with self.connect(write=True) as db:
            db.execute("""INSERT INTO sources (source,account_id,status,last_checked,error)
                VALUES (?,?,'error',?,?) ON CONFLICT(source) DO UPDATE SET
                status='error',last_checked=excluded.last_checked,error=excluded.error""",
                (source,account_id,stamp,error))

    @staticmethod
    def _health(db, stale_after=STALE_AFTER):
        result = []
        now = time.time()
        progress = {}
        if db.execute("PRAGMA user_version").fetchone()[0] >= 2:
            progress = {r[0]: (r[1], bool(r[2])) for r in db.execute("SELECT source,adapter,backlog_pending FROM adapter_state")}
        from .adapters import next_action
        for row in db.execute("SELECT * FROM sources ORDER BY source"):
            value = dict(row)
            value["paused"] = bool(value.get("paused", False))
            # Age of the last successful poll; it proves nothing about a consumer.
            elapsed = None if value["last_ok"] is None else max(0.0, now - value["last_ok"])
            value["last_ok_age"] = None if elapsed is None else int(elapsed)
            value["stale_after"] = stale_after
            if value["status"] == "ok" and elapsed > stale_after:
                value["status"] = "stale"
            adapter, pending = progress.get(value["source"], (value["source"], False))
            value.update(coverage=COVERAGE.get(adapter, "Configured adapter scope; consult its instructions."), history_complete=False)
            value.update(backlog_pending=pending,
                         next_action=next_action(value["error"]) if value["error"] else "collect_periodically")
            if value["paused"]:
                value.update(status="paused", next_action="resume_source")
            result.append(value)
        return result

    @staticmethod
    def _message(row):
        item = dict(row)
        item["needs_reply"] = bool(item["needs_reply"])
        item.setdefault("discovery", None)
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

    def status(self, stale_after=None):
        stale_after = STALE_AFTER if stale_after is None else stale_after
        with self.connect() as db:
            counts = db.execute("""SELECT COUNT(*) total, COALESCE(MAX(arrival_seq),0) latest_arrival,
                COALESCE(SUM(read_at IS NULL),0) unread, COALESCE(SUM(needs_reply),0) needs_reply,
                COALESCE(SUM(replied_at IS NOT NULL),0) replied FROM messages""").fetchone()
            sources = self._health(db, stale_after)
            # Freshness is only the last successful poll's age. Backlog is separate.
            return {"counts": dict(counts), "sources": sources, "stale_after": stale_after,
                    "fresh": bool(sources) and all(s["status"] in ("ok", "paused") for s in sources)}

    def adapter(self, source):
        with self.connect() as db:
            row = None
            if db.execute("PRAGMA user_version").fetchone()[0] >= 2:
                row = db.execute("SELECT adapter FROM adapter_state WHERE source=?", (source,)).fetchone()
            return row[0] if row else source

    def find(self, source, message_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM messages WHERE source=? AND id=?", (source,message_id)).fetchone()
            return None if row is None else self._message(row)

    def show(self, source, message_id):
        message = self.find(source, message_id)
        if message is None:
            raise MailError("message_not_found")
        return message

    def replied_with(self, source, reply_ref, *, exclude_id):
        """All local records linked to one exact published reply, within this source."""
        with self.connect() as db:
            return [self._message(row) for row in db.execute(
                "SELECT * FROM messages WHERE source=? AND reply_ref=? AND id<>? ORDER BY arrival_seq",
                (source, reply_ref, exclude_id))]

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
