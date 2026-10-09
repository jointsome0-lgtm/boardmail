-- v1.sql after the first collect of 0.15.1, from an invented board that had nothing new. The data is invented.
BEGIN TRANSACTION;
CREATE TABLE adapter_state (
    source TEXT PRIMARY KEY, adapter TEXT NOT NULL, revision INTEGER NOT NULL,
    state TEXT NOT NULL, backlog_pending INTEGER NOT NULL DEFAULT 0);
INSERT INTO "adapter_state" VALUES('moltbook','moltbook',1,'{"pending": {}, "discovery": null}',0);
CREATE TABLE messages (
                arrival_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL, id TEXT NOT NULL, thread_id TEXT NOT NULL,
                parent_id TEXT, provider_seq INTEGER, kind TEXT NOT NULL,
                author TEXT, title TEXT NOT NULL, body TEXT NOT NULL, url TEXT NOT NULL,
                created_at INTEGER NOT NULL, arrived_at INTEGER NOT NULL,
                read_at INTEGER, needs_reply INTEGER NOT NULL DEFAULT 0,
                replied_at INTEGER, reply_ref TEXT, discovery TEXT, addressing TEXT, UNIQUE(source, id));
INSERT INTO "messages" VALUES(1,'moltbook','00000000-0000-0000-0000-00000000000a','00000000-0000-0000-0000-000000000064',NULL,NULL,'mention','example-agent','Legacy example','Synthetic v1 message 10','https://example.invalid/posts/00000000-0000-0000-0000-00000000000a',110,200,201,1,202,'https://example.invalid/reply/old',NULL,NULL);
INSERT INTO "messages" VALUES(2,'moltbook','00000000-0000-0000-0000-00000000000b','00000000-0000-0000-0000-000000000064',NULL,NULL,'mention','example-agent','Legacy example','Synthetic v1 message 11','https://example.invalid/posts/00000000-0000-0000-0000-00000000000b',111,200,NULL,0,NULL,NULL,NULL,NULL);
CREATE TABLE originals (
    source TEXT NOT NULL, id TEXT NOT NULL, value TEXT NOT NULL,
    fetched_at INTEGER NOT NULL, PRIMARY KEY (source,id));
CREATE TABLE sources (
                source TEXT PRIMARY KEY, account_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'unknown', last_checked INTEGER,
                last_ok INTEGER, error TEXT, unavailable INTEGER NOT NULL DEFAULT 0);
INSERT INTO "sources" VALUES('moltbook','00000000-0000-0000-0000-000000000002','ok',1800000000,1800000000,NULL,0);
CREATE TABLE subscriptions (
    source TEXT NOT NULL, thread_id TEXT NOT NULL, subscribed_at INTEGER NOT NULL,
    PRIMARY KEY (source,thread_id));
CREATE INDEX messages_reply_ref ON messages (source,reply_ref);
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('messages',2);
COMMIT;
PRAGMA user_version=2;
