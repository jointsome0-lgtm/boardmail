-- v1.sql after these commands of 0.15.1, and no collect: subscribe research, subscribe postingboard, pause
-- research, tag add, settings --scope all --context none, reply prepare. The config of that run read research
-- through the postingboard adapter and postingboard through the-colony adapter. The data is invented.
BEGIN TRANSACTION;
CREATE TABLE adapter_state (
    source TEXT PRIMARY KEY, adapter TEXT NOT NULL, revision INTEGER NOT NULL,
    state TEXT NOT NULL, backlog_pending INTEGER NOT NULL DEFAULT 0);
INSERT INTO "adapter_state" VALUES('research','postingboard',0,'{}',0);
INSERT INTO "adapter_state" VALUES('postingboard','the-colony',0,'{}',0);
CREATE TABLE messages (
                arrival_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL, id TEXT NOT NULL, thread_id TEXT NOT NULL,
                parent_id TEXT, provider_seq INTEGER, kind TEXT NOT NULL,
                author TEXT, title TEXT NOT NULL, body TEXT NOT NULL, url TEXT NOT NULL,
                created_at INTEGER NOT NULL, arrived_at INTEGER NOT NULL,
                read_at INTEGER, needs_reply INTEGER NOT NULL DEFAULT 0,
                replied_at INTEGER, reply_ref TEXT, UNIQUE(source, id));
INSERT INTO "messages" VALUES(1,'moltbook','00000000-0000-0000-0000-00000000000a','00000000-0000-0000-0000-000000000064',NULL,NULL,'mention','example-agent','Legacy example','Synthetic v1 message 10','https://example.invalid/posts/00000000-0000-0000-0000-00000000000a',110,200,201,1,202,'https://example.invalid/reply/old');
INSERT INTO "messages" VALUES(2,'moltbook','00000000-0000-0000-0000-00000000000b','00000000-0000-0000-0000-000000000064',NULL,NULL,'mention','example-agent','Legacy example','Synthetic v1 message 11','https://example.invalid/posts/00000000-0000-0000-0000-00000000000b',111,200,NULL,0,NULL,NULL);
CREATE TABLE reader_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO "reader_settings" VALUES('scope','all');
INSERT INTO "reader_settings" VALUES('context','none');
CREATE TABLE reply_attempts (
    source TEXT NOT NULL, message_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE, body TEXT NOT NULL, body_sha256 TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('prepared','unknown','confirmed')),
    prepared_at INTEGER NOT NULL, attempted_at INTEGER, confirmed_at INTEGER,
    reply_ref TEXT, readback_sha256 TEXT,
    PRIMARY KEY (source,message_id));
INSERT INTO "reply_attempts" VALUES('moltbook','00000000-0000-0000-0000-00000000000b','00000000-0000-4000-8000-000000000001','An invented answer.','8ef8cf14fb49cba679fa3cf51757ac04169cf1402deed52cf71c40d9df77b578','prepared',1800000000,NULL,NULL,NULL,NULL);
CREATE TABLE sources (
                source TEXT PRIMARY KEY, account_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'unknown', last_checked INTEGER,
                last_ok INTEGER, error TEXT, unavailable INTEGER NOT NULL DEFAULT 0, paused INTEGER NOT NULL DEFAULT 0);
INSERT INTO "sources" VALUES('moltbook','00000000-0000-0000-0000-000000000002','ok',200,200,NULL,0,0);
INSERT INTO "sources" VALUES('research','00000000-0000-0000-0000-000000000001','unknown',NULL,NULL,NULL,0,1);
INSERT INTO "sources" VALUES('postingboard','00000000-0000-0000-0000-000000000004','unknown',NULL,NULL,NULL,0,0);
CREATE TABLE subscriptions (
    source TEXT NOT NULL, thread_id TEXT NOT NULL, subscribed_at INTEGER NOT NULL,
    PRIMARY KEY (source,thread_id));
INSERT INTO "subscriptions" VALUES('research','00000000-0000-0000-0000-0000000000c8',1800000000);
INSERT INTO "subscriptions" VALUES('postingboard','00000000-0000-0000-0000-0000000000c9',1800000000);
CREATE TABLE thread_tags (
    tag TEXT NOT NULL, source TEXT NOT NULL, thread_id TEXT NOT NULL,
    tagged_at INTEGER NOT NULL, PRIMARY KEY (tag,source,thread_id));
INSERT INTO "thread_tags" VALUES('follow-up','moltbook','00000000-0000-0000-0000-000000000064',1800000000);
CREATE INDEX thread_tags_membership ON thread_tags(source,thread_id,tag);
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('messages',2);
COMMIT;
PRAGMA user_version=1;
