BEGIN TRANSACTION;
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
CREATE TABLE sources (
                source TEXT PRIMARY KEY, account_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'unknown', last_checked INTEGER,
                last_ok INTEGER, error TEXT, unavailable INTEGER NOT NULL DEFAULT 0);
INSERT INTO "sources" VALUES('moltbook','00000000-0000-0000-0000-000000000002','ok',200,200,NULL,0);
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('messages',2);
COMMIT;
PRAGMA user_version=1;
