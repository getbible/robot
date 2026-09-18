-- Synthetic records generated with the deployed store at
-- 6ec7081bcb4683011151437298891226539e3aa2. No real user information.
BEGIN TRANSACTION;
CREATE TABLE contribution_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                contributor_id INTEGER,
                subject_type TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                detail_json TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
INSERT INTO "contribution_audit" VALUES(1,'telegram:42','application_submitted',42,'application','42','{}',1700000000000000000);
INSERT INTO "contribution_audit" VALUES(2,'fixture-reviewer','application_decided',42,'application','42','{"previous_state":"pending","state":"approved"}',1700000000000000000);
INSERT INTO "contribution_audit" VALUES(3,'telegram:42','disclosure_acknowledged',42,'application','42','{}',1700000000000000000);
INSERT INTO "contribution_audit" VALUES(4,'telegram:42','events_submitted',42,'event_batch','1700000000000000000:3','{"accepted":3,"replayed":0}',1700000000000000000);
INSERT INTO "contribution_audit" VALUES(5,'fixture-reviewer','topic_decided',42,'topic','local.mercy','{"canonical_topic_id":"mercy","state":"mapped"}',1700000000000000000);
INSERT INTO "contribution_audit" VALUES(6,'fixture-reviewer','event_decided',42,'event','1','{"canonical_topic_id":"mercy","state":"approved"}',1700000000000000000);
INSERT INTO "contribution_audit" VALUES(7,'fixture-reviewer','event_decided',42,'event','2','{"canonical_topic_id":"mercy","state":"approved"}',1700000000000000000);
INSERT INTO "contribution_audit" VALUES(8,'fixture-reviewer','catalog_published',NULL,'catalog_revision','1','{"checksum":"10c12e6451910b92aad77b1a66f56beaa42ba080949a5bf1d3c51c7374092d90"}',1700000000000000000); -- pragma: allowlist secret (synthetic catalogue checksum)
INSERT INTO "contribution_audit" VALUES(9,'fixture-reviewer','events_applied',NULL,'catalog_revision','1','{"count":2}',1700000000000000000);
INSERT INTO "contribution_audit" VALUES(10,'telegram:84','application_submitted',84,'application','84','{}',1700000000000000000);
INSERT INTO "contribution_audit" VALUES(11,'telegram:126','application_submitted',126,'application','126','{}',1700000000000000000);
INSERT INTO "contribution_audit" VALUES(12,'fixture-reviewer','application_decided',126,'application','126','{"previous_state":"pending","state":"approved"}',1700000000000000000);
INSERT INTO "contribution_audit" VALUES(13,'fixture-reviewer','application_decided',126,'application','126','{"previous_state":"approved","state":"revoked"}',1700000000000000000);
CREATE TABLE contribution_canonical_topics (
                topic_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                color TEXT NOT NULL,
                aliases TEXT NOT NULL,
                definition_json TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                actor TEXT NOT NULL
            );
INSERT INTO "contribution_canonical_topics" VALUES('mercy','Mercy','#123456','[]','{"aliases":[],"color":"#123456","id":"mercy","name":"Mercy"}',1700000000000000000,1700000000000000000,'fixture-reviewer');
CREATE TABLE contribution_catalog_revisions (
                revision INTEGER PRIMARY KEY,
                checksum TEXT NOT NULL,
                catalog_json TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                actor TEXT NOT NULL
            );
INSERT INTO "contribution_catalog_revisions" VALUES(0,'29881d08798cf5314abe901d419c6eaf29d0cb2422d6dd9ac2544bd1b94fad7d','{"associations":{"add":[],"remove":[]},"schema_version":1,"topics":[]}',1700000000000000000,'system'); -- pragma: allowlist secret (synthetic catalogue checksum)
INSERT INTO "contribution_catalog_revisions" VALUES(1,'10c12e6451910b92aad77b1a66f56beaa42ba080949a5bf1d3c51c7374092d90','{"associations":{"add":[{"book":43,"chapter":3,"topic_id":"mercy","verse":16}],"remove":[]},"schema_version":1,"topics":[{"aliases":[],"color":"#123456","id":"mercy","name":"Mercy"}]}',1700000000000000000,'fixture-reviewer'); -- pragma: allowlist secret (synthetic catalogue checksum)
CREATE TABLE contribution_client_snapshots (
                contributor_id INTEGER NOT NULL,
                client_id TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,
                snapshot_digest BLOB NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (contributor_id, client_id),
                FOREIGN KEY (contributor_id)
                    REFERENCES contributor_applications(user_id)
            );
CREATE TABLE contribution_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contributor_id INTEGER,
                subject_type TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                previous_state TEXT NOT NULL,
                decision TEXT NOT NULL,
                canonical_topic_id TEXT,
                note TEXT NOT NULL,
                actor TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
INSERT INTO "contribution_decisions" VALUES(1,42,'application','42','pending','approved',NULL,'','fixture-reviewer',1700000000000000000);
INSERT INTO "contribution_decisions" VALUES(2,42,'topic','local.mercy','pending','mapped','mercy','','fixture-reviewer',1700000000000000000);
INSERT INTO "contribution_decisions" VALUES(3,42,'event','1','pending','approved','mercy','','fixture-reviewer',1700000000000000000);
INSERT INTO "contribution_decisions" VALUES(4,42,'event','2','pending','approved','mercy','','fixture-reviewer',1700000000000000000);
INSERT INTO "contribution_decisions" VALUES(5,126,'application','126','pending','approved',NULL,'','fixture-reviewer',1700000000000000000);
INSERT INTO "contribution_decisions" VALUES(6,126,'application','126','approved','revoked',NULL,'','fixture-reviewer',1700000000000000000);
CREATE TABLE contribution_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contributor_id INTEGER NOT NULL,
                client_event_id TEXT NOT NULL,
                event_type TEXT NOT NULL CHECK (
                    event_type IN ('topic_upsert','topic_delete','verse_add','verse_remove')
                ),
                local_topic_id TEXT NOT NULL,
                topic_name TEXT,
                topic_color TEXT,
                book INTEGER,
                chapter INTEGER,
                verse INTEGER,
                payload_json TEXT NOT NULL,
                payload_digest BLOB NOT NULL,
                state TEXT NOT NULL CHECK (
                    state IN ('pending','approved','rejected','deferred','applied')
                ),
                canonical_topic_id TEXT,
                replay_count INTEGER NOT NULL DEFAULT 0,
                submitted_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                decided_at INTEGER,
                UNIQUE (contributor_id, client_event_id),
                FOREIGN KEY (contributor_id)
                    REFERENCES contributor_applications(user_id),
                FOREIGN KEY (contributor_id, local_topic_id)
                    REFERENCES contributor_source_topics(contributor_id, local_topic_id)
            );
INSERT INTO "contribution_events" VALUES(1,42,'topic.mercy','topic_upsert','local.mercy','Mercy','#123456',NULL,NULL,NULL,'{"client_event_id":"topic.mercy","topic":{"color":"#123456","local_topic_id":"local.mercy","name":"Mercy"},"type":"topic_upsert"}',X'5E63D5D86305E1E0E449377085A2797D9480F4096C8B7EB7DAFC3096106021B2','applied','mercy',0,1700000000000000000,1700000000000000000,1700000000000000000);
INSERT INTO "contribution_events" VALUES(2,42,'verse.mercy.1','verse_add','local.mercy',NULL,NULL,43,3,16,'{"client_event_id":"verse.mercy.1","topic":{"local_topic_id":"local.mercy"},"type":"verse_add","verse":{"book":43,"chapter":3,"verse":16}}',X'100E50A98D4F1D3B433042CDBCD3072318600FB3251082A999CE41A77B120F13','applied','mercy',0,1700000000000000000,1700000000000000000,1700000000000000000);
INSERT INTO "contribution_events" VALUES(3,42,'verse.mercy.pending','verse_add','local.mercy',NULL,NULL,43,3,17,'{"client_event_id":"verse.mercy.pending","topic":{"local_topic_id":"local.mercy"},"type":"verse_add","verse":{"book":43,"chapter":3,"verse":17}}',X'A70A01F0DE7562BCD78EC68CA7512CAED489C5C64194C1D8ED282D8F17193C89','pending','mercy',0,1700000000000000000,1700000000000000000,NULL);
CREATE TABLE contribution_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contributor_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                message TEXT NOT NULL,
                state TEXT NOT NULL CHECK (
                    state IN ('pending','sending','failed','sent')
                ),
                attempts INTEGER NOT NULL,
                available_at INTEGER NOT NULL,
                lease_until INTEGER NOT NULL DEFAULT 0,
                claim_token TEXT,
                last_error TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                FOREIGN KEY (contributor_id)
                    REFERENCES contributor_applications(user_id)
            );
INSERT INTO "contribution_notifications" VALUES(1,42,'application_approved','You are now enrolled as a GetBible topic contributor. New topics and verse-tag changes you make in the Mini App will be shared with the administrators for review. Approved changes become part of the core catalogue for everyone using the project.','pending',0,1700000000000000000,0,NULL,NULL,1700000000000000000,1700000000000000000);
INSERT INTO "contribution_notifications" VALUES(2,126,'application_approved','You are now enrolled as a GetBible topic contributor. New topics and verse-tag changes you make in the Mini App will be shared with the administrators for review. Approved changes become part of the core catalogue for everyone using the project.','pending',0,1700000000000000000,0,NULL,NULL,1700000000000000000,1700000000000000000);
INSERT INTO "contribution_notifications" VALUES(3,126,'application_revoked','Your GetBible contributor enrolment has ended. Your personal topics and verse markings remain available on your devices, but new changes will not be submitted for project review.','pending',0,1700000000000000000,0,NULL,NULL,1700000000000000000,1700000000000000000);
CREATE TABLE contribution_publication_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                live_revision INTEGER NOT NULL,
                live_checksum TEXT NOT NULL,
                live_updated_at INTEGER NOT NULL,
                repo_revision INTEGER,
                repo_state TEXT,
                repo_checksum TEXT,
                repo_token TEXT,
                repo_lease_until INTEGER NOT NULL DEFAULT 0,
                repo_branch TEXT,
                repo_commit TEXT,
                repo_error TEXT,
                updated_at INTEGER NOT NULL
            );
INSERT INTO "contribution_publication_state" VALUES(1,1,'10c12e6451910b92aad77b1a66f56beaa42ba080949a5bf1d3c51c7374092d90',1700000000000000000,NULL,NULL,NULL,NULL,0,NULL,NULL,NULL,1700000000000000000); -- pragma: allowlist secret (synthetic fixture digest)
CREATE TABLE contribution_sync_receipts (
                contributor_id INTEGER NOT NULL,
                sync_id TEXT NOT NULL,
                client_id TEXT NOT NULL,
                request_digest BLOB NOT NULL,
                accepted INTEGER NOT NULL,
                replayed INTEGER NOT NULL,
                event_ids_json TEXT NOT NULL,
                snapshot_digest TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (contributor_id, sync_id),
                FOREIGN KEY (contributor_id)
                    REFERENCES contributor_applications(user_id)
            );
CREATE TABLE contributor_applications (
                user_id INTEGER PRIMARY KEY,
                state TEXT NOT NULL CHECK (
                    state IN ('pending','approved','rejected','deferred','revoked')
                ),
                first_name TEXT,
                last_name TEXT,
                username TEXT,
                language_code TEXT,
                requested_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                decided_at INTEGER,
                disclosure_acknowledged_at INTEGER
            );
INSERT INTO "contributor_applications" VALUES(42,'approved','Fixture Contributor',NULL,'fixture_reader','af',1700000000000000000,1700000000000000000,1700000000000000000,1700000000000000000);
INSERT INTO "contributor_applications" VALUES(84,'pending','Pending Fixture',NULL,NULL,NULL,1700000000000000000,1700000000000000000,NULL,NULL);
INSERT INTO "contributor_applications" VALUES(126,'revoked','Revoked Fixture',NULL,NULL,NULL,1700000000000000000,1700000000000000000,1700000000000000000,NULL);
CREATE TABLE contributor_capabilities (
                token_digest BLOB PRIMARY KEY,
                contributor_id INTEGER NOT NULL,
                issued_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                last_used_at INTEGER NOT NULL,
                FOREIGN KEY (contributor_id)
                    REFERENCES contributor_applications(user_id)
            );
CREATE TABLE contributor_source_topics (
                contributor_id INTEGER NOT NULL,
                local_topic_id TEXT NOT NULL,
                name TEXT,
                color TEXT,
                aliases TEXT NOT NULL DEFAULT '[]',
                state TEXT NOT NULL CHECK (
                    state IN ('pending','mapped','rejected','deferred')
                ),
                canonical_topic_id TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (contributor_id, local_topic_id),
                FOREIGN KEY (contributor_id)
                    REFERENCES contributor_applications(user_id)
            );
INSERT INTO "contributor_source_topics" VALUES(42,'local.mercy','Mercy','#123456','[]','mapped','mercy',1700000000000000000,1700000000000000000);
CREATE INDEX contributor_applications_state
                ON contributor_applications (state, requested_at, user_id);
CREATE INDEX contributor_source_topics_state
                ON contributor_source_topics (state, updated_at);
CREATE INDEX contribution_events_review
                ON contribution_events (state, submitted_at, id);
CREATE INDEX contribution_events_topic
                ON contribution_events (
                    contributor_id, local_topic_id, state, submitted_at
                );
CREATE INDEX contribution_decisions_subject
                ON contribution_decisions (subject_type, subject_id, created_at);
CREATE INDEX contribution_audit_created
                ON contribution_audit (created_at, id);
CREATE INDEX contribution_notifications_delivery
                ON contribution_notifications (state, available_at, id);
CREATE INDEX contribution_sync_receipts_created
                ON contribution_sync_receipts (created_at, contributor_id);
CREATE INDEX contributor_capabilities_owner
                ON contributor_capabilities (contributor_id, expires_at, last_used_at);
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('contribution_audit',13);
INSERT INTO "sqlite_sequence" VALUES('contribution_decisions',6);
INSERT INTO "sqlite_sequence" VALUES('contribution_notifications',3);
INSERT INTO "sqlite_sequence" VALUES('contribution_events',3);
COMMIT;
PRAGMA user_version=5;
