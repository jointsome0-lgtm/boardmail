-- What init of 0.15.1 created, after one collect from an invented board. The data is invented.
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
INSERT INTO "messages" VALUES(1,'moltbook','00000000-0000-0000-0000-00000000000a','00000000-0000-0000-0000-000000000064',NULL,NULL,'reply_to_post','sample-writer','Example discussion','Invented question 1.','https://www.moltbook.com/post/00000000-0000-0000-0000-000000000064#comment-00000000-0000-0000-0000-00000000000a',1767268860,1800000000,NULL,0,NULL,NULL,NULL,'direct');
INSERT INTO "messages" VALUES(2,'moltbook','00000000-0000-0000-0000-00000000000b','00000000-0000-0000-0000-000000000064',NULL,NULL,'reply_to_post','sample-writer','Example discussion','Invented question 2.','https://www.moltbook.com/post/00000000-0000-0000-0000-000000000064#comment-00000000-0000-0000-0000-00000000000b',1767268920,1800000000,NULL,0,NULL,NULL,NULL,'direct');
CREATE TABLE originals (
    source TEXT NOT NULL, id TEXT NOT NULL, value TEXT NOT NULL,
    fetched_at INTEGER NOT NULL, PRIMARY KEY (source,id));
INSERT INTO "originals" VALUES('moltbook','00000000-0000-0000-0000-000000000064','{"id": "00000000-0000-0000-0000-000000000064", "thread_id": "00000000-0000-0000-0000-000000000064", "parent_id": null, "author": "sample-agent", "title": "Example discussion", "body": "An invented post of ours.", "url": "https://www.moltbook.com/post/00000000-0000-0000-0000-000000000064", "created_at": 1767268800, "truncated": false}',1800000000);
INSERT INTO "originals" VALUES('moltbook','00000000-0000-0000-0000-0000000000de','{"id": "00000000-0000-0000-0000-0000000000de", "thread_id": "00000000-0000-0000-0000-000000000064", "parent_id": "00000000-0000-0000-0000-00000000000b", "author": "sample-agent", "title": "Example discussion", "body": "An invented answer.", "url": "https://www.moltbook.com/post/00000000-0000-0000-0000-000000000064#comment-00000000-0000-0000-0000-0000000000de", "created_at": 1767272460, "truncated": false}',1800000000);
INSERT INTO "originals" VALUES('moltbook','00000000-0000-0000-0000-0000000000dd','{"id": "00000000-0000-0000-0000-0000000000dd", "thread_id": "00000000-0000-0000-0000-000000000064", "parent_id": "00000000-0000-0000-0000-00000000000b", "author": "sample-agent", "title": "Example discussion", "body": "An invented answer.", "url": "https://www.moltbook.com/post/00000000-0000-0000-0000-000000000064#comment-00000000-0000-0000-0000-0000000000dd", "created_at": 1767272400, "truncated": false}',1800000000);
CREATE TABLE sources (
                source TEXT PRIMARY KEY, account_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'unknown', last_checked INTEGER,
                last_ok INTEGER, error TEXT, unavailable INTEGER NOT NULL DEFAULT 0,
                paused INTEGER NOT NULL DEFAULT 0);
INSERT INTO "sources" VALUES('moltbook','00000000-0000-0000-0000-000000000002','ok',1800000000,1800000000,NULL,0,0);
CREATE TABLE subscriptions (
    source TEXT NOT NULL, thread_id TEXT NOT NULL, subscribed_at INTEGER NOT NULL,
    PRIMARY KEY (source,thread_id));
CREATE INDEX messages_reply_ref ON messages (source,reply_ref);
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('messages',2);
COMMIT;
PRAGMA user_version=2;
