PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS registry_meta (schema_version TEXT PRIMARY KEY CHECK(schema_version='registry/v2.1'));
INSERT OR IGNORE INTO registry_meta VALUES ('registry/v2.1');
CREATE TABLE IF NOT EXISTS implementation_meta (revision INTEGER PRIMARY KEY CHECK(revision=2));
INSERT OR IGNORE INTO implementation_meta VALUES (2);
CREATE TABLE IF NOT EXISTS assets (
 asset_id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind='raw_upload'),
 filename TEXT NOT NULL, safe_filename TEXT NOT NULL, size_bytes INTEGER,
 sha256 TEXT, md5 TEXT, transport_length_source TEXT, created_at TEXT NOT NULL,
 CHECK ((size_bytes IS NULL AND sha256 IS NULL AND md5 IS NULL) OR
        (size_bytes>=0 AND length(sha256)=64 AND length(md5)=32))
);
CREATE TRIGGER IF NOT EXISTS immutable_asset_bytes BEFORE UPDATE OF size_bytes,sha256,md5 ON assets
 WHEN OLD.sha256 IS NOT NULL AND (NEW.sha256 IS NOT OLD.sha256 OR NEW.md5 IS NOT OLD.md5 OR NEW.size_bytes IS NOT OLD.size_bytes)
 BEGIN SELECT RAISE(ABORT,'asset byte identity is immutable'); END;
CREATE TABLE IF NOT EXISTS intakes (intake_id TEXT PRIMARY KEY, replay_key TEXT NOT NULL UNIQUE, notification_chat_id TEXT);
CREATE TRIGGER IF NOT EXISTS frozen_intake_target BEFORE UPDATE ON intakes
 BEGIN SELECT RAISE(ABORT,'intake identity and notification target are immutable'); END;
CREATE TABLE IF NOT EXISTS source_groups (
 source_group_id TEXT NOT NULL, membership_revision INTEGER NOT NULL CHECK(membership_revision>0),
 membership_sealed INTEGER NOT NULL DEFAULT 0 CHECK(membership_sealed IN (0,1)),
 intake_id TEXT REFERENCES intakes(intake_id),
 CHECK(membership_sealed=0 OR intake_id IS NOT NULL),
 PRIMARY KEY(source_group_id,membership_revision)
);
CREATE TRIGGER IF NOT EXISTS frozen_group_intake BEFORE UPDATE OF intake_id ON source_groups
 WHEN OLD.intake_id IS NOT NULL AND NEW.intake_id IS NOT OLD.intake_id
 BEGIN SELECT RAISE(ABORT,'group intake binding is immutable'); END;
CREATE TABLE IF NOT EXISTS intake_entries (
 entry_id TEXT PRIMARY KEY, intake_id TEXT NOT NULL REFERENCES intakes(intake_id),
 replay_key TEXT NOT NULL, contract_digest TEXT NOT NULL, asset_id TEXT NOT NULL REFERENCES assets(asset_id),
 received_filename TEXT NOT NULL, lineage_json TEXT NOT NULL, expected_json TEXT NOT NULL,
 UNIQUE(intake_id,replay_key), UNIQUE(intake_id,entry_id)
);
CREATE TABLE IF NOT EXISTS source_group_memberships (
 source_group_id TEXT NOT NULL, membership_revision INTEGER NOT NULL,
 entry_id TEXT NOT NULL UNIQUE REFERENCES intake_entries(entry_id), required INTEGER NOT NULL CHECK(required IN (0,1)),
 PRIMARY KEY(source_group_id,membership_revision,entry_id),
 FOREIGN KEY(source_group_id,membership_revision) REFERENCES source_groups(source_group_id,membership_revision)
);
CREATE TRIGGER IF NOT EXISTS sealed_membership_insert BEFORE INSERT ON source_group_memberships
 WHEN (SELECT membership_sealed FROM source_groups WHERE source_group_id=NEW.source_group_id AND membership_revision=NEW.membership_revision)=1
 BEGIN SELECT RAISE(ABORT,'sealed membership: use a new revision'); END;
CREATE TRIGGER IF NOT EXISTS same_intake_membership BEFORE INSERT ON source_group_memberships
 BEGIN
  SELECT CASE WHEN (SELECT intake_id FROM source_groups WHERE source_group_id=NEW.source_group_id AND membership_revision=NEW.membership_revision) IS NOT NULL
    AND (SELECT intake_id FROM source_groups WHERE source_group_id=NEW.source_group_id AND membership_revision=NEW.membership_revision)
        IS NOT (SELECT intake_id FROM intake_entries WHERE entry_id=NEW.entry_id)
    THEN RAISE(ABORT,'cross-intake group membership forbidden') END;
  UPDATE source_groups SET intake_id=(SELECT intake_id FROM intake_entries WHERE entry_id=NEW.entry_id)
    WHERE source_group_id=NEW.source_group_id AND membership_revision=NEW.membership_revision AND intake_id IS NULL;
 END;
CREATE TRIGGER IF NOT EXISTS immutable_membership_update BEFORE UPDATE ON source_group_memberships
 BEGIN SELECT RAISE(ABORT,'membership is immutable; use a new scope'); END;
CREATE TRIGGER IF NOT EXISTS immutable_membership_delete BEFORE DELETE ON source_group_memberships
 BEGIN SELECT RAISE(ABORT,'membership is immutable; use a new scope'); END;
CREATE TRIGGER IF NOT EXISTS sealed_group BEFORE UPDATE OF membership_sealed ON source_groups
 WHEN OLD.membership_sealed=1 AND NEW.membership_sealed!=1
 BEGIN SELECT RAISE(ABORT,'cannot unseal a group'); END;
CREATE TABLE IF NOT EXISTS staging_files (
 asset_id TEXT PRIMARY KEY REFERENCES assets(asset_id), relative_path TEXT NOT NULL UNIQUE,
 custody_health TEXT NOT NULL DEFAULT 'missing' CHECK(custody_health IN ('healthy','suspect','missing')),
 health_revision INTEGER NOT NULL DEFAULT 0, cleanup_hold INTEGER NOT NULL DEFAULT 1 CHECK(cleanup_hold IN (0,1))
);
CREATE TABLE IF NOT EXISTS staging_receipts (
 acceptance_id TEXT PRIMARY KEY, intake_id TEXT NOT NULL, entry_id TEXT NOT NULL UNIQUE,
 asset_id TEXT NOT NULL REFERENCES assets(asset_id),
 status TEXT NOT NULL CHECK(status IN ('pending','copying','verifying','accepted','failed')),
 accepted_at TEXT, validation_json TEXT, attempt_id TEXT, failure_episode_id TEXT, last_error_json TEXT,
 UNIQUE(intake_id,entry_id), FOREIGN KEY(intake_id,entry_id) REFERENCES intake_entries(intake_id,entry_id),
 CHECK(status!='accepted' OR (accepted_at IS NOT NULL AND validation_json IS NOT NULL))
);
CREATE TRIGGER IF NOT EXISTS accepted_history BEFORE UPDATE ON staging_receipts
 WHEN OLD.status='accepted' AND (NEW.status!='accepted' OR NEW.accepted_at IS NOT OLD.accepted_at OR NEW.validation_json IS NOT OLD.validation_json)
 BEGIN SELECT RAISE(ABORT,'accepted history is immutable'); END;
CREATE TABLE IF NOT EXISTS staging_attempts (
 attempt_id TEXT PRIMARY KEY, acceptance_id TEXT NOT NULL REFERENCES staging_receipts(acceptance_id),
 intake_id TEXT NOT NULL, entry_id TEXT NOT NULL, asset_id TEXT NOT NULL REFERENCES assets(asset_id),
 started_at TEXT NOT NULL, finished_at TEXT, outcome TEXT, phase TEXT NOT NULL,
 error_code TEXT, error_summary TEXT,
 FOREIGN KEY(intake_id,entry_id) REFERENCES intake_entries(intake_id,entry_id),
 CHECK(outcome IS NULL OR outcome IN ('succeeded','failed','interrupted')),
 CHECK((finished_at IS NULL AND outcome IS NULL) OR (finished_at IS NOT NULL AND outcome IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS staging_attempt_receipt ON staging_attempts(acceptance_id,started_at);
CREATE TRIGGER IF NOT EXISTS immutable_completed_attempt BEFORE UPDATE ON staging_attempts
 WHEN OLD.finished_at IS NOT NULL OR NEW.attempt_id IS NOT OLD.attempt_id
 OR NEW.acceptance_id IS NOT OLD.acceptance_id OR NEW.intake_id IS NOT OLD.intake_id
 OR NEW.entry_id IS NOT OLD.entry_id OR NEW.asset_id IS NOT OLD.asset_id OR NEW.started_at IS NOT OLD.started_at
 BEGIN SELECT RAISE(ABORT,'completed attempt and attempt identity are immutable'); END;
CREATE TRIGGER IF NOT EXISTS no_delete_staging_attempt BEFORE DELETE ON staging_attempts
 BEGIN SELECT RAISE(ABORT,'staging attempts cannot be deleted'); END;
CREATE TABLE IF NOT EXISTS asset_leases (
 lease_id TEXT PRIMARY KEY, asset_id TEXT NOT NULL REFERENCES assets(asset_id),
 purpose TEXT NOT NULL, acquired_at TEXT NOT NULL, released_at TEXT
);
CREATE TABLE IF NOT EXISTS custody_events (
 event_id TEXT PRIMARY KEY, dedupe_key TEXT NOT NULL UNIQUE, event_type TEXT NOT NULL,
 aggregate_id TEXT NOT NULL, occurred_at TEXT NOT NULL, payload_version TEXT NOT NULL,
 payload_json TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS append_only_events_update BEFORE UPDATE ON custody_events
 BEGIN SELECT RAISE(ABORT,'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS append_only_events_delete BEFORE DELETE ON custody_events
 BEGIN SELECT RAISE(ABORT,'events are append-only'); END;
CREATE TABLE IF NOT EXISTS notification_outbox (
 notification_id TEXT PRIMARY KEY, dedupe_key TEXT NOT NULL UNIQUE, scope_type TEXT NOT NULL,
 scope_id TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('success','failure')),
 template_version TEXT NOT NULL, payload TEXT NOT NULL, provider_request_key TEXT NOT NULL UNIQUE,
 delivery_state TEXT NOT NULL DEFAULT 'pending' CHECK(delivery_state IN ('pending','sending','delivered','failed_retryable','delivery_unknown','cancelled')),
 provider_message_id TEXT, lease_token TEXT, lease_until REAL, created_at TEXT NOT NULL,
 last_error TEXT
 ,intake_id TEXT NOT NULL REFERENCES intakes(intake_id), target_chat_id TEXT
);
CREATE TRIGGER IF NOT EXISTS frozen_notification_identity BEFORE UPDATE ON notification_outbox
 WHEN NEW.intake_id IS NOT OLD.intake_id OR NEW.target_chat_id IS NOT OLD.target_chat_id
 OR NEW.provider_request_key IS NOT OLD.provider_request_key OR NEW.scope_id IS NOT OLD.scope_id
 OR NEW.scope_type IS NOT OLD.scope_type OR NEW.dedupe_key IS NOT OLD.dedupe_key
 OR NEW.payload IS NOT OLD.payload OR NEW.kind IS NOT OLD.kind
 BEGIN SELECT RAISE(ABORT,'notification identity, payload and target are immutable'); END;
CREATE TABLE IF NOT EXISTS notification_attempts (
 attempt_id TEXT PRIMARY KEY, notification_id TEXT NOT NULL REFERENCES notification_outbox(notification_id),
 provider_request_key TEXT NOT NULL, operation TEXT NOT NULL CHECK(operation IN ('send','lookup')),
 started_at TEXT NOT NULL, finished_at TEXT, result TEXT NOT NULL, provider_message_id TEXT
);
CREATE INDEX IF NOT EXISTS outbox_ready ON notification_outbox(delivery_state,created_at);
CREATE INDEX IF NOT EXISTS receipt_status ON staging_receipts(status);
CREATE INDEX IF NOT EXISTS event_aggregate ON custody_events(aggregate_id,occurred_at);
CREATE INDEX IF NOT EXISTS lease_asset ON asset_leases(asset_id,released_at);
CREATE TABLE IF NOT EXISTS conversion_bindings (
 task_id TEXT PRIMARY KEY, entry_id TEXT NOT NULL REFERENCES intake_entries(entry_id),
 asset_id TEXT NOT NULL REFERENCES assets(asset_id), inspection_id TEXT NOT NULL,
 released_at TEXT
);
CREATE TABLE IF NOT EXISTS conversion_lease_owners (
 lease_id TEXT PRIMARY KEY REFERENCES asset_leases(lease_id),
 task_id TEXT NOT NULL REFERENCES conversion_bindings(task_id)
);
CREATE TABLE IF NOT EXISTS extraction_snapshots (
 extraction_id TEXT PRIMARY KEY, conversion_task_id TEXT NOT NULL UNIQUE,
 source_asset_id TEXT NOT NULL REFERENCES assets(asset_id), output_asset_id TEXT NOT NULL UNIQUE,
 relative_root TEXT NOT NULL UNIQUE, map_json TEXT NOT NULL, inventory_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS image_preferences (
 preference_version TEXT PRIMARY KEY, material_type TEXT NOT NULL,
 strategy INTEGER NOT NULL CHECK(strategy BETWEEN 1 AND 6), confirmation_ref TEXT NOT NULL, confirmed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS expression_snapshots (
 extraction_id TEXT PRIMARY KEY, parent_extraction_id TEXT NOT NULL REFERENCES extraction_snapshots(extraction_id),
 source_asset_id TEXT NOT NULL REFERENCES assets(asset_id), output_asset_id TEXT NOT NULL UNIQUE,
 relative_root TEXT NOT NULL UNIQUE, map_json TEXT NOT NULL, inventory_json TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS immutable_expression_update BEFORE UPDATE ON expression_snapshots
 BEGIN SELECT RAISE(ABORT,'expression is immutable; create a new version'); END;
CREATE TRIGGER IF NOT EXISTS immutable_expression_delete BEFORE DELETE ON expression_snapshots
 BEGIN SELECT RAISE(ABORT,'expression history cannot be deleted'); END;
-- Additive DEV tables: keep the inherited raw asset/receipt schema intact.
-- A key references exactly one authoritative byte identity, never a copied hash.
CREATE TABLE IF NOT EXISTS derived_assets (
 asset_id TEXT PRIMARY KEY, source_asset_id TEXT NOT NULL REFERENCES assets(asset_id),
 extraction_id TEXT NOT NULL REFERENCES extraction_snapshots(extraction_id),
 kind TEXT NOT NULL CHECK(kind IN ('extracted_image','extracted_text')),
 filename TEXT NOT NULL, relative_path TEXT NOT NULL UNIQUE, size_bytes INTEGER NOT NULL,
 sha256 TEXT NOT NULL, md5 TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS immutable_derived BEFORE UPDATE ON derived_assets
 BEGIN SELECT RAISE(ABORT,'derived byte identity is immutable'); END;
CREATE TABLE IF NOT EXISTS cloud_asset_keys (
 asset_id TEXT PRIMARY KEY, raw_id TEXT UNIQUE REFERENCES assets(asset_id),
 derived_id TEXT UNIQUE REFERENCES derived_assets(asset_id),
 CHECK((raw_id IS NULL) != (derived_id IS NULL)),
 CHECK(asset_id=coalesce(raw_id,derived_id))
);
CREATE VIEW IF NOT EXISTS archive_asset_identity AS
 SELECT k.asset_id,a.kind,a.safe_filename AS filename,a.size_bytes,a.sha256,a.md5,f.relative_path,e.entry_id
 FROM cloud_asset_keys k JOIN assets a ON a.asset_id=k.raw_id
 JOIN staging_files f ON f.asset_id=a.asset_id JOIN intake_entries e ON e.asset_id=a.asset_id
 UNION ALL
 SELECT k.asset_id,d.kind,d.filename,d.size_bytes,d.sha256,d.md5,d.relative_path,NULL
 FROM cloud_asset_keys k JOIN derived_assets d ON d.asset_id=k.derived_id;
CREATE TABLE IF NOT EXISTS asset_locations (
 asset_id TEXT PRIMARY KEY REFERENCES cloud_asset_keys(asset_id), provider TEXT NOT NULL,
 account_ref TEXT NOT NULL, root_path TEXT NOT NULL, fs_id TEXT, path_snapshot TEXT,
 current_path TEXT, planned_path TEXT, remote_size_bytes INTEGER, cloud_md5 TEXT,
 cloud_md5_kind TEXT, cloud_md5_endpoint TEXT, semantic_evidence TEXT,
 archive_status TEXT NOT NULL DEFAULT 'pending_cloud_archive', availability TEXT NOT NULL DEFAULT 'unknown',
 verification_level TEXT NOT NULL DEFAULT 'none', archived_at TEXT, last_verified_at TEXT,
 last_seen_at TEXT, first_content_verified_at TEXT, integrity_hold INTEGER NOT NULL DEFAULT 0,
 registry_revision INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
 validation_mode TEXT NOT NULL DEFAULT 'offline_mock'
);
CREATE TABLE IF NOT EXISTS archive_operations (
 operation_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, plan_json TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'planned', started_at TEXT, finished_at TEXT,
 result_json TEXT, error_code TEXT
);
CREATE TABLE IF NOT EXISTS archive_attempts (
 attempt_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL REFERENCES archive_operations(operation_id),
 method TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, outcome TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS archive_windows (
 confirmation_ref TEXT PRIMARY KEY, scope_json TEXT NOT NULL, expires_at TEXT NOT NULL,
 max_calls INTEGER NOT NULL, used_calls INTEGER NOT NULL DEFAULT 0, stopped INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS asset_references (
 asset_id TEXT NOT NULL REFERENCES cloud_asset_keys(asset_id), reference_id TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('publication','staging_dependency')), released_at TEXT,
 PRIMARY KEY(asset_id,reference_id,kind)
);
CREATE TABLE IF NOT EXISTS derived_leases (
 lease_id TEXT PRIMARY KEY, asset_id TEXT NOT NULL REFERENCES derived_assets(asset_id),
 purpose TEXT NOT NULL, acquired_at TEXT NOT NULL, released_at TEXT
);
CREATE TABLE IF NOT EXISTS index_backups (
 backup_id TEXT PRIMARY KEY, relative_path TEXT NOT NULL, sha256 TEXT NOT NULL,
 export_path TEXT NOT NULL, export_sha256 TEXT NOT NULL, created_at TEXT NOT NULL,
 revisions_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS retention_policies (
 version TEXT PRIMARY KEY, days INTEGER NOT NULL CHECK(days>=0), confirmation_ref TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS staging_cleanup (
 asset_id TEXT PRIMARY KEY REFERENCES assets(asset_id), state TEXT NOT NULL,
 plan_json TEXT NOT NULL, confirmation_ref TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS asset_cache (
 asset_id TEXT PRIMARY KEY REFERENCES cloud_asset_keys(asset_id), relative_path TEXT NOT NULL,
 sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL, expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cache_leases (
 lease_id TEXT PRIMARY KEY, asset_id TEXT NOT NULL REFERENCES cloud_asset_keys(asset_id),
 acquired_at TEXT NOT NULL, released_at TEXT
);
CREATE TRIGGER IF NOT EXISTS immutable_preference BEFORE UPDATE ON image_preferences
 BEGIN SELECT RAISE(ABORT,'preferences are versioned'); END;
CREATE TABLE IF NOT EXISTS active_image_preferences (
 material_type TEXT PRIMARY KEY, preference_version TEXT NOT NULL REFERENCES image_preferences(preference_version)
);
CREATE TABLE IF NOT EXISTS publications_v21 (
 publication_id TEXT PRIMARY KEY,entry_id TEXT NOT NULL REFERENCES intake_entries(entry_id),source_asset_id TEXT NOT NULL REFERENCES assets(asset_id),
 source_id TEXT NOT NULL,state TEXT NOT NULL CHECK(state IN ('prepared','confirmed','publishing','published','withdrawn')),
 preview_sha256 TEXT NOT NULL,manifest_sha256 TEXT NOT NULL,files_json TEXT NOT NULL,
 confirmation_event_id TEXT NOT NULL UNIQUE,revision_of TEXT REFERENCES publications_v21(publication_id)
);
CREATE TABLE IF NOT EXISTS active_publications_v21 (
 source_asset_id TEXT PRIMARY KEY REFERENCES assets(asset_id),publication_id TEXT NOT NULL UNIQUE REFERENCES publications_v21(publication_id)
);
CREATE TABLE IF NOT EXISTS publication_confirmations (
 confirmation_event_id TEXT PRIMARY KEY,publication_id TEXT NOT NULL UNIQUE REFERENCES publications_v21(publication_id),
 preview_sha256 TEXT NOT NULL,actor TEXT NOT NULL,confirmation_ref TEXT NOT NULL,confirmed_at TEXT NOT NULL
);
-- Personal Pilot: additive local control records; no cloud/retention state changes.
CREATE TABLE IF NOT EXISTS pilot_intake_scope (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),app_id TEXT NOT NULL,chat_id TEXT NOT NULL,user_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pilot_intake_messages (
 message_key TEXT PRIMARY KEY,envelope_json TEXT NOT NULL,registered_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pilot_conversion_jobs (
 entry_id TEXT PRIMARY KEY REFERENCES intake_entries(entry_id),
 message_key TEXT NOT NULL REFERENCES pilot_intake_messages(message_key),resource_index INTEGER NOT NULL,
 request_key TEXT NOT NULL UNIQUE,inspection_id TEXT,task_id TEXT UNIQUE,stop_reason TEXT,
 UNIQUE(message_key,resource_index)
);
CREATE TABLE IF NOT EXISTS pilot_candidates (
 publication_id TEXT PRIMARY KEY REFERENCES publications_v21(publication_id),
 app_id TEXT NOT NULL,chat_id TEXT NOT NULL,user_id TEXT NOT NULL,
 version INTEGER NOT NULL,staged_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pilot_current_candidates (
 source_asset_id TEXT PRIMARY KEY REFERENCES assets(asset_id),
 publication_id TEXT NOT NULL UNIQUE REFERENCES pilot_candidates(publication_id)
);
CREATE TABLE IF NOT EXISTS pilot_preview_deliveries (
 publication_id TEXT PRIMARY KEY REFERENCES pilot_candidates(publication_id),
 app_id TEXT NOT NULL,chat_id TEXT NOT NULL,message_id TEXT NOT NULL,
 pdf_sha256 TEXT NOT NULL,receipt_ref TEXT NOT NULL,delivered_at TEXT NOT NULL,
 UNIQUE(app_id,chat_id,message_id)
);
CREATE TABLE IF NOT EXISTS pilot_preview_decisions (
 publication_id TEXT PRIMARY KEY REFERENCES pilot_preview_deliveries(publication_id),
 app_id TEXT NOT NULL,chat_id TEXT NOT NULL,message_id TEXT NOT NULL,
 user_id TEXT NOT NULL,reply_to TEXT NOT NULL,command TEXT NOT NULL,decided_at TEXT NOT NULL,
 UNIQUE(app_id,chat_id,message_id)
);
CREATE TRIGGER IF NOT EXISTS immutable_extraction_update BEFORE UPDATE ON extraction_snapshots
 BEGIN SELECT RAISE(ABORT,'extraction is immutable; create a new version'); END;
CREATE TRIGGER IF NOT EXISTS immutable_extraction_delete BEFORE DELETE ON extraction_snapshots
 BEGIN SELECT RAISE(ABORT,'extraction history cannot be deleted'); END;
-- Refresh only this derived view in the initialization transaction. Existing DEV
-- revision-2 tables, receipt facts and historical events are left unchanged.
DROP VIEW IF EXISTS intake_custody;
CREATE VIEW intake_custody AS
 SELECT e.entry_id,e.intake_id,e.asset_id AS source_staging_asset_id,r.acceptance_id,
 r.status AS source_staging_status,r.accepted_at AS source_staging_accepted_at,f.custody_health,
 CASE WHEN sc.state='cleaned' THEN 'cleaned' WHEN sc.state='cleaning' THEN 'cleanup_pending'
   WHEN f.custody_health='missing' THEN 'missing' ELSE 'present' END AS local_presence,
 CASE WHEN sc.state='cleaned' THEN NULL ELSE f.relative_path END AS local_staging_path,
 CASE WHEN m.entry_id IS NOT NULL AND m.required=1 THEN
  CASE WHEN EXISTS(SELECT 1 FROM custody_events c WHERE c.event_type='source_group_staging_accepted'
    AND c.dedupe_key=e.intake_id||':'||m.source_group_id||':'||m.membership_revision||':accepted')
   THEN 'cleanup_eligible' ELSE 'retain_required' END
 ELSE CASE WHEN r.status='accepted' THEN 'cleanup_eligible' ELSE 'retain_required' END END AS feishu_attachment_retention
 FROM intake_entries e JOIN staging_receipts r ON r.entry_id=e.entry_id JOIN staging_files f ON f.asset_id=e.asset_id
 LEFT JOIN source_group_memberships m ON m.entry_id=e.entry_id
 LEFT JOIN staging_cleanup sc ON sc.asset_id=e.asset_id;
