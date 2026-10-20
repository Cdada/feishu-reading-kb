"""One SQLite authority. Each write uses BEGIN IMMEDIATE and FULL durability."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from .contracts import (SCHEMA, EVENT_VERSION, TEMPLATE_VERSION, SUCCESS, GROUP_SUCCESS,
                        FAILURE, LINEAGE_FIELDS, ContractError, new_id, stable_key,
                        safe_filename, validate_entry, validate_id)


def now():
    return datetime.now(timezone.utc).isoformat()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class Registry:
    def __init__(self, runtime):
        self.runtime = runtime
        runtime.initialize()
        with runtime.initializing("registry"), self.connect() as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and "registry_meta" not in tables:
                raise ContractError("refusing an unversioned/V1 database")
            if "registry_meta" in tables:
                versions = [r[0] for r in db.execute("SELECT schema_version FROM registry_meta")]
                if versions != [SCHEMA]:
                    raise ContractError("unsupported registry version")
                if "implementation_meta" not in tables or db.execute("SELECT revision FROM implementation_meta").fetchone()[0] != 2:
                    raise ContractError("prior DEV schema: use a new runtime under DEV test-output; no automatic migration")
            # Run DDL in one transaction; executescript alone auto-commits and
            # can expose a partially created schema to a second fresh process.
            db.execute("BEGIN IMMEDIATE")
            try:
                statement = ""
                for line in Path(__file__).with_name("schema.sql").read_text().splitlines(keepends=True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        db.execute(statement)
                        statement = ""
                if statement.strip():
                    raise ContractError("incomplete registry schema")
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @contextmanager
    def connect(self):
        self.runtime.checked(self.runtime.db)
        db = sqlite3.connect(str(self.runtime.db), timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        try:
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    yield db
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
        except sqlite3.OperationalError as exc:
            self.runtime.record_fault("registry_transaction", exc)
            raise

    def rows(self, sql, args=()):
        with self.connect() as db:
            return [dict(r) for r in db.execute(sql, args)]

    def receipt(self, entry_id):
        validate_id(entry_id, "entry")
        rows = self.rows("SELECT * FROM intake_custody WHERE entry_id=?", (entry_id,))
        if not rows:
            raise ContractError("unknown entry")
        return rows[0]

    def entry(self, entry_id):
        validate_id(entry_id, "entry")
        rows = self.rows("""SELECT e.*,a.safe_filename,a.size_bytes,a.sha256,a.md5,
            a.transport_length_source,r.acceptance_id,r.status,r.attempt_id,r.failure_episode_id,
            f.relative_path,f.custody_health FROM intake_entries e
            JOIN assets a ON a.asset_id=e.asset_id JOIN staging_receipts r ON r.entry_id=e.entry_id
            JOIN staging_files f ON f.asset_id=e.asset_id WHERE e.entry_id=?""", (entry_id,))
        if not rows:
            raise ContractError("unknown entry")
        return rows[0]

    def create_group(self, source_group_id=None, revision=1):
        gid = source_group_id or new_id("group")
        validate_id(gid, "group")
        if type(revision) is not int or revision < 1:
            raise ContractError("invalid revision")
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO source_groups(source_group_id,membership_revision) VALUES (?,?)", (gid, revision))
        return {"source_group_id": gid, "membership_revision": revision}

    def register(self, data):
        validate_entry(data)
        digest = stable_key(data)
        with self.transaction() as db:
            chat = data.get("lineage", {}).get("chat_id")
            db.execute("INSERT OR IGNORE INTO intakes VALUES (?,?,?)", (new_id("intake"), data["intake_key"], chat))
            intake = db.execute("SELECT * FROM intakes WHERE replay_key=?", (data["intake_key"],)).fetchone()
            iid = intake["intake_id"]
            if intake["notification_chat_id"] != chat:
                raise ContractError("intake notification target conflicts with frozen lineage")
            existing = db.execute("SELECT * FROM intake_entries WHERE intake_id=? AND replay_key=?", (iid, data["entry_key"])).fetchone()
            if existing:
                if existing["contract_digest"] != digest:
                    raise ContractError("entry replay conflicts with its original contract")
                return dict(existing)
            aid, eid, rid = new_id("asset"), new_id("entry"), new_id("acceptance")
            filename = safe_filename(data["received_filename"])
            lineage = {key: data.get("lineage", {}).get(key) for key in LINEAGE_FIELDS}
            lineage.update(intake_id=iid, received_filename=data["received_filename"])
            db.execute("INSERT INTO assets(asset_id,kind,filename,safe_filename,created_at) VALUES (?,'raw_upload',?,?,?)",
                       (aid, data["received_filename"], filename, now()))
            db.execute("INSERT INTO intake_entries VALUES (?,?,?,?,?,?,?,?)",
                       (eid, iid, data["entry_key"], digest, aid, data["received_filename"], encoded(lineage), encoded(data.get("expected", {}))))
            db.execute("INSERT INTO staging_files(asset_id,relative_path) VALUES (?,?)",
                       (aid, f"source-staging/{aid}/{filename}"))
            db.execute("INSERT INTO staging_receipts(acceptance_id,intake_id,entry_id,asset_id,status) VALUES (?,?,?,?,'pending')", (rid, iid, eid, aid))
            if data.get("group"):
                g = data["group"]
                db.execute("INSERT INTO source_group_memberships VALUES (?,?,?,?)",
                           (g["source_group_id"], g["membership_revision"], eid, int(g["required"])))
        return self.entry(eid)

    @staticmethod
    def event(db, event_type, aggregate, dedupe, payload):
        db.execute("INSERT OR IGNORE INTO custody_events VALUES (?,?,?,?,?,?,?)",
                   (new_id("event"), dedupe, event_type, aggregate, now(), EVENT_VERSION, encoded(payload)))

    @staticmethod
    def notice(db, scope_type, scope, kind, intake_id, episode=None):
        # Template revisions do not enter this key: a new template is not a new notification.
        key = stable_key(intake_id, scope_type, scope, kind, episode)
        message = FAILURE if kind == "failure" else GROUP_SUCCESS if scope_type == "source_group" else SUCCESS
        target = db.execute("SELECT notification_chat_id FROM intakes WHERE intake_id=?", (intake_id,)).fetchone()[0]
        db.execute("""INSERT OR IGNORE INTO notification_outbox
          (notification_id,dedupe_key,scope_type,scope_id,kind,template_version,payload,provider_request_key,created_at,intake_id,target_chat_id)
          VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (new_id("notification"), key, scope_type, scope, kind,
                                      TEMPLATE_VERSION, message, "custody-" + key[:40], now(), intake_id, target))

    @staticmethod
    def cancel_failed(db, scope):
        db.execute("""UPDATE notification_outbox SET delivery_state='cancelled'
            WHERE scope_id=? AND kind='failure' AND delivery_state IN ('pending','failed_retryable')""", (scope,))

    @staticmethod
    def group_scope(group):
        return f"{group['intake_id']}:{group['source_group_id']}:{group['membership_revision']}"

    def group_conditions(self, db, gid, revision):
        group = db.execute("SELECT * FROM source_groups WHERE source_group_id=? AND membership_revision=?", (gid, revision)).fetchone()
        required = db.execute("""SELECT r.entry_id,r.status,f.custody_health FROM source_group_memberships m
            JOIN staging_receipts r ON r.entry_id=m.entry_id JOIN staging_files f ON f.asset_id=r.asset_id
            WHERE m.source_group_id=? AND m.membership_revision=? AND m.required=1""", (gid, revision)).fetchall()
        safe = bool(group and group["intake_id"] and group["membership_sealed"] and required and
                     all(r["status"] == "accepted" and r["custody_health"] == "healthy" for r in required))
        history = bool(group and group["intake_id"] and db.execute(
            "SELECT 1 FROM custody_events WHERE event_type='source_group_staging_accepted' AND dedupe_key=?",
            (self.group_scope(group) + ":accepted",)).fetchone())
        return group, required, safe, history

    def group_ready(self, db, gid, revision):
        """Create the FIRST promise only when safe; never undo a historical one."""
        group, required, safe, history = self.group_conditions(db, gid, revision)
        if history:
            return True
        if safe:
            scope = self.group_scope(group)
            self.event(db, "source_group_staging_accepted", gid, scope + ":accepted",
                       {"intake_id": group["intake_id"], "source_group_id": gid,
                        "membership_revision": revision, "required_entries": [r["entry_id"] for r in required]})
            self.cancel_failed(db, scope)
            self.notice(db, "source_group", scope, "success", group["intake_id"])
        return safe

    def seal_group(self, gid, revision):
        with self.transaction() as db:
            count = db.execute("SELECT count(*) FROM source_group_memberships WHERE source_group_id=? AND membership_revision=? AND required=1", (gid, revision)).fetchone()[0]
            if not count:
                raise ContractError("a sealed group requires at least one required member")
            db.execute("UPDATE source_groups SET membership_sealed=1 WHERE source_group_id=? AND membership_revision=?", (gid, revision))
            return self.group_ready(db, gid, revision)

    def group_retention(self, gid, revision):
        return self.group_state(gid, revision)["feishu_attachment_retention"]

    def group_state(self, gid, revision):
        # Both predicates use the same query; neither mutates group/events/outbox.
        with self.connect() as db:
            _, _, safe, history = self.group_conditions(db, gid, revision)
        return {"currently_safe_for_first_promise": safe, "historical_cleanup_promise": history,
                "feishu_attachment_retention": "cleanup_eligible" if history else "retain_required"}

    def begin_attempt(self, eid):
        """Called with the exclusive asset lock. Incomplete prior attempts have ended."""
        attempt = new_id("attempt")
        with self.transaction() as db:
            r = db.execute("SELECT * FROM staging_receipts WHERE entry_id=?", (eid,)).fetchone()
            db.execute("UPDATE staging_attempts SET finished_at=?,outcome='interrupted',error_code='WorkerInterrupted',error_summary='Recovered after exclusive asset lock acquisition' WHERE acceptance_id=? AND finished_at IS NULL", (now(), r["acceptance_id"]))
            db.execute("INSERT INTO staging_attempts(attempt_id,acceptance_id,intake_id,entry_id,asset_id,started_at,phase) VALUES (?,?,?,?,?,?,'copying')",
                       (attempt,r["acceptance_id"],r["intake_id"],eid,r["asset_id"],now()))
            db.execute("UPDATE staging_receipts SET status='copying',attempt_id=? WHERE entry_id=?", (attempt,eid))
        return attempt

    def attempt_phase(self, eid, phase):
        with self.transaction() as db:
            db.execute("UPDATE staging_receipts SET status=? WHERE entry_id=?", (phase,eid))
            db.execute("UPDATE staging_attempts SET phase=? WHERE attempt_id=(SELECT attempt_id FROM staging_receipts WHERE entry_id=?) AND finished_at IS NULL", (phase,eid))

    @staticmethod
    def finish_attempt(db, attempt, outcome, phase, error=None):
        db.execute("UPDATE staging_attempts SET finished_at=?,outcome=?,phase=?,error_code=?,error_summary=? WHERE attempt_id=? AND finished_at IS NULL",
                   (now(), outcome, phase, type(error).__name__ if error else None, str(error)[:500] if error else None, attempt))

    def failed(self, eid, error):
        with self.transaction() as db:
            r = db.execute("SELECT * FROM staging_receipts WHERE entry_id=?", (eid,)).fetchone()
            if r["status"] == "accepted":
                return  # Integrity failures have a separate history, never erase acceptance.
            episode = r["failure_episode_id"] or new_id("failure")
            reason = {"code": type(error).__name__, "message": str(error), "at": now(), "attempt_id": r["attempt_id"]}
            db.execute("UPDATE staging_receipts SET status='failed',failure_episode_id=?,last_error_json=? WHERE entry_id=?", (episode, encoded(reason), eid))
            phase = db.execute("SELECT phase FROM staging_attempts WHERE attempt_id=?", (r["attempt_id"],)).fetchone()
            self.finish_attempt(db, r["attempt_id"], "failed", phase[0] if phase else "copying", error)
            self.event(db, "source_staging_failed", r["acceptance_id"], f"{r['acceptance_id']}:{episode}:failed", reason)
            group = db.execute("SELECT m.*,g.intake_id FROM source_group_memberships m JOIN source_groups g USING(source_group_id,membership_revision) WHERE entry_id=?", (eid,)).fetchone()
            if group and group["required"]:
                self.notice(db, "source_group", self.group_scope(group), "failure", r["intake_id"], "group-failure")
            else:
                self.notice(db, "single_source", r["acceptance_id"], "failure", r["intake_id"], episode)

    def accept(self, eid, evidence, hook=lambda point: None):
        with self.transaction() as db:
            r = db.execute("SELECT * FROM staging_receipts WHERE entry_id=?", (eid,)).fetchone()
            if r["status"] == "accepted":
                return
            asset = db.execute("SELECT size_bytes,sha256,md5 FROM assets WHERE asset_id=?", (r["asset_id"],)).fetchone()
            if (r["status"] != "verifying" or not asset["sha256"] or evidence.get("durable") is not True
                    or evidence.get("expected") != dict(asset) or evidence.get("observed") != dict(asset)
                    or not evidence.get("reopened_at")):
                raise ContractError("acceptance requires durable re-read evidence matching the persisted baseline")
            db.execute("UPDATE staging_receipts SET status='accepted',accepted_at=?,validation_json=?,last_error_json=NULL WHERE entry_id=?",
                       (now(), encoded(evidence), eid))
            db.execute("UPDATE staging_files SET custody_health='healthy' WHERE asset_id=?", (r["asset_id"],))
            self.finish_attempt(db, r["attempt_id"], "succeeded", "accept_commit")
            self.event(db, "source_staging_accepted", r["acceptance_id"], r["acceptance_id"] + ":accepted",
                       {"entry_id": eid, "asset_id": r["asset_id"], "attempt_id": r["attempt_id"], "evidence": evidence})
            group = db.execute("SELECT * FROM source_group_memberships WHERE entry_id=?", (eid,)).fetchone()
            if group and group["required"]:
                self.group_ready(db, group["source_group_id"], group["membership_revision"])
            else:
                self.cancel_failed(db, r["acceptance_id"])
                self.notice(db, "single_source", r["acceptance_id"], "success", r["intake_id"])
            hook("before_accept_commit")

    def health(self, eid, health, reason):
        with self.transaction() as db:
            row = db.execute("SELECT f.* FROM staging_files f JOIN intake_entries e ON e.asset_id=f.asset_id WHERE e.entry_id=?", (eid,)).fetchone()
            if row["custody_health"] != health:
                rev = row["health_revision"] + 1
                db.execute("UPDATE staging_files SET custody_health=?,health_revision=? WHERE asset_id=?", (health, rev, row["asset_id"]))
                self.event(db, "source_staging_integrity_alert", row["asset_id"], f"{row['asset_id']}:health:{rev}", {"custody_health": health, "reason": reason})
                if health == "healthy":
                    # Recovery may make a sealed group safe for its FIRST promise.
                    # Use the same transaction and predicates as normal acceptance;
                    # an existing historical promise never creates another notice.
                    group = db.execute("""SELECT m.* FROM source_group_memberships m
                        JOIN staging_receipts r ON r.entry_id=m.entry_id
                        WHERE m.entry_id=? AND m.required=1 AND r.status='accepted'""", (eid,)).fetchone()
                    if group:
                        self.group_ready(db, group["source_group_id"], group["membership_revision"])
