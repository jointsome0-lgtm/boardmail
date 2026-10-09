-- An inbox file as 0.16.1 left it, with a source of 4claw and one of Fruitflies. Each has one message.
-- The data is invented.
BEGIN TRANSACTION;
CREATE TABLE adapter_state (
    source TEXT PRIMARY KEY, adapter TEXT NOT NULL, revision INTEGER NOT NULL,
    state TEXT NOT NULL, backlog_pending INTEGER NOT NULL DEFAULT 0);
INSERT INTO "adapter_state" VALUES('fourclaw','fourclaw',1,'{"next_thread": 0}',0);
INSERT INTO "adapter_state" VALUES('fruitflies','fruitflies',1,'{"offset": 100}',0);
CREATE TABLE messages (
                arrival_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL, id TEXT NOT NULL, thread_id TEXT NOT NULL,
                parent_id TEXT, provider_seq INTEGER, kind TEXT NOT NULL,
                author TEXT, title TEXT NOT NULL, body TEXT NOT NULL, url TEXT NOT NULL,
                created_at INTEGER NOT NULL, arrived_at INTEGER NOT NULL,
                read_at INTEGER, needs_reply INTEGER NOT NULL DEFAULT 0,
                replied_at INTEGER, reply_ref TEXT, discovery TEXT, addressing TEXT, UNIQUE(source, id));
INSERT INTO "messages" VALUES(1,'fourclaw','10000000-0000-4000-8000-000000000000','00000000-0000-4000-8000-000000000001','00000000-0000-4000-8000-000000000001',NULL,'reply_to_post','Other','A title','An invented reply in the thread.','https://www.4claw.org/t/00000000-0000-4000-8000-000000000001',1788782400,1790000000,NULL,0,NULL,NULL,NULL,'thread');
INSERT INTO "messages" VALUES(2,'fruitflies','00000000-0000-0000-0000-000000000003','00000000-0000-0000-0000-000000000003',NULL,NULL,'mention','other','','@alice an invented question','https://fruitflies.ai/feed',1788775200,1790000000,NULL,0,NULL,NULL,NULL,'mention');
CREATE TABLE originals (
    source TEXT NOT NULL, id TEXT NOT NULL, value TEXT NOT NULL,
    fetched_at INTEGER NOT NULL, PRIMARY KEY (source,id));
INSERT INTO "originals" VALUES('fourclaw','00000000-0000-4000-8000-000000000001','{"id": "00000000-0000-4000-8000-000000000001", "thread_id": "00000000-0000-4000-8000-000000000001", "parent_id": null, "author": "Reader", "title": "A title", "body": "Opening", "url": "https://www.4claw.org/t/00000000-0000-4000-8000-000000000001", "created_at": 1788782400, "truncated": false}',1790000000);
CREATE TABLE reader_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE reply_attempts (
    source TEXT NOT NULL, message_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE, body TEXT NOT NULL, body_sha256 TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('prepared','unknown','confirmed')),
    prepared_at INTEGER NOT NULL, attempted_at INTEGER, confirmed_at INTEGER,
    reply_ref TEXT, readback_sha256 TEXT,
    PRIMARY KEY (source,message_id));
CREATE TABLE reply_candidate_checks (
    source TEXT NOT NULL, message_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
    reply_ref TEXT NOT NULL, checked_at INTEGER NOT NULL, reason TEXT NOT NULL,
    PRIMARY KEY (source,message_id,reply_ref));
CREATE TABLE reply_candidates (
    source TEXT NOT NULL, message_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
    reply_ref TEXT NOT NULL, adapter TEXT NOT NULL, account_id TEXT NOT NULL,
    recorded_at INTEGER NOT NULL, PRIMARY KEY (source,message_id,reply_ref));
CREATE TABLE reply_verifications (
    source TEXT NOT NULL, message_id TEXT NOT NULL, evidence TEXT NOT NULL,
    PRIMARY KEY (source,message_id));
CREATE TABLE sources (
                source TEXT PRIMARY KEY, account_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'unknown', last_checked INTEGER,
                last_ok INTEGER, error TEXT, unavailable INTEGER NOT NULL DEFAULT 0,
                paused INTEGER NOT NULL DEFAULT 0);
INSERT INTO "sources" VALUES('fourclaw','Reader','ok',1790000000,1790000000,NULL,0,0);
INSERT INTO "sources" VALUES('fruitflies','alice','ok',1790000000,1790000000,NULL,0,0);
CREATE TABLE subscriptions (
    source TEXT NOT NULL, thread_id TEXT NOT NULL, subscribed_at INTEGER NOT NULL,
    PRIMARY KEY (source,thread_id));
CREATE TABLE thread_tags (
    tag TEXT NOT NULL, source TEXT NOT NULL, thread_id TEXT NOT NULL,
    tagged_at INTEGER NOT NULL, PRIMARY KEY (tag,source,thread_id));
CREATE INDEX messages_reply_ref ON messages (source,reply_ref);
CREATE INDEX thread_tags_membership ON thread_tags(source,thread_id,tag);
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('messages',2);
COMMIT;
PRAGMA user_version=2;
