"""Persistent mock provider + leased outbox. Unknown delivery is reconciled first."""
import sqlite3
import time
from typing import Protocol, runtime_checkable
from types import MappingProxyType

from .contracts import ContractError, new_id, validate_id
from .registry import now


class RetryableSendError(OSError):
    pass


class DefiniteSendError(OSError):
    pass


class DeliveryUnknown(OSError):
    pass


@runtime_checkable
class Sender(Protocol):
    """Use item's frozen target/key; send returns message ID; lookup is tri-state.

    Providers must never replace provider_request_key. Unknown results require
    lookup; only authoritative 'not_sent' allows retry. Credentials stay outside
    item/ledger. Runtime selection is the caller's responsibility.
    """
    def send(self, item, attempt): ...
    def lookup(self, item, attempt): ...


class PersistentMockSender:
    """Separate DB simulates the provider's durable state, not a second outbox."""
    MODES = {"success", "definite_failure", "retryable_failure", "timeout",
             "success_lost_response", "delay"}

    def __init__(self, runtime, mode="success", delay=0, lookup_authoritative=True):
        if mode not in self.MODES:
            raise ContractError("unknown mock mode")
        self.path = runtime.checked(runtime.root / "test-output/mock-provider.sqlite3")
        self.mode, self.delay, self.lookup_authoritative = mode, delay, lookup_authoritative
        with runtime.initializing("mock-provider"), self.connect() as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS deliveries (
                provider_request_key TEXT PRIMARY KEY, notification_id TEXT NOT NULL,
                payload TEXT NOT NULL, message_id TEXT NOT NULL UNIQUE, target_chat_id TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS calls (
                call_id INTEGER PRIMARY KEY, notification_id TEXT NOT NULL,
                provider_request_key TEXT NOT NULL, payload TEXT NOT NULL,
                attempt TEXT NOT NULL, operation TEXT NOT NULL, result TEXT NOT NULL, target_chat_id TEXT NOT NULL);
            """)

    def connect(self):
        # Context-managed by each caller with explicit close below.
        from contextlib import contextmanager

        @contextmanager
        def connection():
            db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
            try:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA synchronous=FULL")
                yield db
            finally:
                db.close()
        return connection()

    def send(self, item, attempt):
        if not isinstance(item.get("target_chat_id"), str) or not item["target_chat_id"]:
            raise ContractError("notification target must come from intake lineage")
        if self.delay or self.mode == "delay":
            time.sleep(self.delay or 0.02)
        error = None
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT message_id,payload,notification_id,target_chat_id FROM deliveries WHERE provider_request_key=?", (item["provider_request_key"],)).fetchone()
            if old and (old[1] != item["payload"] or old[2] != item["notification_id"] or old[3] != item["target_chat_id"]):
                db.rollback()
                raise ContractError("request key reused for a different notification")
            if old:
                result, message = "deduplicated", old[0]
            elif self.mode in ("success", "delay", "success_lost_response"):
                message = new_id("mock_message")
                db.execute("INSERT INTO deliveries VALUES (?,?,?,?,?)", (item["provider_request_key"], item["notification_id"], item["payload"], message, item["target_chat_id"]))
                result = "delivered" if self.mode != "success_lost_response" else "delivered_response_lost"
                if self.mode == "success_lost_response":
                    error = DeliveryUnknown("mock provider accepted; response lost")
            else:
                message, result = None, self.mode
                error = {"timeout": DeliveryUnknown, "retryable_failure": RetryableSendError,
                         "definite_failure": DefiniteSendError}[self.mode]("mock " + self.mode)
            db.execute("INSERT INTO calls(notification_id,provider_request_key,payload,attempt,operation,result,target_chat_id) VALUES (?,?,?,?,?,?,?)",
                       (item["notification_id"], item["provider_request_key"], item["payload"], attempt, "send", result, item["target_chat_id"]))
            db.commit()
        if error:
            raise error
        return message

    def lookup(self, item, attempt):
        with self.connect() as db:
            row = db.execute("SELECT message_id FROM deliveries WHERE provider_request_key=?", (item["provider_request_key"],)).fetchone()
            result = "delivered" if row else "not_sent" if self.lookup_authoritative else "unknown"
            db.execute("INSERT INTO calls(notification_id,provider_request_key,payload,attempt,operation,result,target_chat_id) VALUES (?,?,?,?,?,?,?)",
                       (item["notification_id"], item["provider_request_key"], item["payload"], attempt, "lookup", result, item["target_chat_id"]))
        return result, row[0] if row else None

    def records(self, table):
        if table not in ("deliveries", "calls"):
            raise ContractError("invalid mock table")
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute(f"SELECT * FROM {table}")]


class OutboxWorker:
    def __init__(self, registry, sender, lease_seconds=30, hook=lambda point: None):
        if not isinstance(sender, Sender) or not callable(sender.send) or not callable(sender.lookup):
            raise ContractError("sender must implement send and lookup")
        if lease_seconds <= 0:
            raise ContractError("lease_seconds must be positive")
        self.registry, self.sender = registry, sender
        self.lease_seconds, self.hook = lease_seconds, hook

    @staticmethod
    def failure_resolved(db, item):
        if item["kind"] != "failure":
            return False
        if item["scope_type"] == "single_source":
            row = db.execute("SELECT status FROM staging_receipts WHERE acceptance_id=?", (item["scope_id"],)).fetchone()
            return bool(row and row[0] == "accepted")
        return db.execute("SELECT 1 FROM custody_events WHERE dedupe_key=?", (item["scope_id"] + ":accepted",)).fetchone() is not None

    def claim(self, exclude, *, notification_id=None):
        # A bounded caller must not expire or reconcile unrelated historical work.
        if notification_id is not None:
            validate_id(notification_id, "notification")
        selected = "" if notification_id is None else " AND notification_id=?"
        args = () if notification_id is None else (notification_id,)
        with self.registry.transaction() as db:
            # Expired send attempts have unknown outcomes, never assume they failed.
            expired = db.execute("SELECT notification_id FROM notification_outbox WHERE delivery_state='sending' AND lease_until<=?" + selected, (time.time(), *args)).fetchall()
            for r in expired:
                nid = r["notification_id"]
                for attempt in db.execute("SELECT * FROM notification_attempts WHERE notification_id=? AND result='started'", (nid,)).fetchall():
                    db.execute("UPDATE notification_attempts SET finished_at=?,result='delivery_unknown' WHERE attempt_id=?", (now(), attempt["attempt_id"]))
                    self.registry.event(db, "notification_delivery", nid, f"{nid}:{attempt['attempt_id']}:lease_expired", {"attempt_id": attempt["attempt_id"], "result": "delivery_unknown", "reason": "worker lease expired"})
                db.execute("UPDATE notification_outbox SET delivery_state='delivery_unknown',lease_token=NULL,lease_until=NULL WHERE notification_id=?", (nid,))
            candidates = db.execute("SELECT * FROM notification_outbox WHERE delivery_state IN ('pending','failed_retryable','delivery_unknown')" + selected + " ORDER BY created_at", args).fetchall()
            row = None
            for candidate in candidates:
                if candidate["notification_id"] in exclude:
                    continue
                if not candidate["target_chat_id"]:
                    db.execute("UPDATE notification_outbox SET last_error='missing frozen intake notification target' WHERE notification_id=?", (candidate["notification_id"],))
                    continue
                if candidate["delivery_state"] != "delivery_unknown" and self.failure_resolved(db, candidate):
                    db.execute("UPDATE notification_outbox SET delivery_state='cancelled' WHERE notification_id=?", (candidate["notification_id"],))
                    continue
                row = candidate
                break
            if row is None:
                return None
            item = dict(row)
            operation = "lookup" if row["delivery_state"] == "delivery_unknown" else "send"
            attempt = new_id("attempt")
            db.execute("UPDATE notification_outbox SET delivery_state='sending',lease_token=?,lease_until=? WHERE notification_id=?", (attempt, time.time() + self.lease_seconds, item["notification_id"]))
            db.execute("INSERT INTO notification_attempts VALUES (?,?,?,?,?,NULL,'started',NULL)", (attempt, item["notification_id"], item["provider_request_key"], operation, now()))
            return item, attempt, operation

    def finish(self, item, attempt, result, message=None, error=None):
        nid = item["notification_id"]
        with self.registry.transaction() as db:
            if result == "failed_retryable" and self.failure_resolved(db, item):
                result = "cancelled"  # Proven unsent failure must not be retried after acceptance.
            db.execute("UPDATE notification_attempts SET finished_at=?,result=?,provider_message_id=? WHERE attempt_id=?", (now(), result, message, attempt))
            # A late worker may record its observation but cannot steal a newer lease.
            db.execute("""UPDATE notification_outbox SET delivery_state=?,provider_message_id=?,last_error=?,lease_token=NULL,lease_until=NULL
                WHERE notification_id=? AND lease_token=?""", (result, message, error, nid, attempt))
            self.registry.event(db, "notification_delivery", nid, f"{nid}:{attempt}:{result}",
                                {"notification_id": nid, "attempt_id": attempt, "result": result,
                                 "provider_message_id": message, "error": error})

    def drain(self, limit=100, *, notification_id=None):
        seen = set()
        while len(seen) < limit:
            claim = self.claim(seen, notification_id=notification_id)
            if claim is None:
                break
            item, attempt, operation = claim
            seen.add(item["notification_id"])
            try:
                self.hook("before_provider_call")
                if operation == "lookup":
                    status, message = self.sender.lookup(MappingProxyType(item), attempt)
                    if status not in ("delivered", "not_sent", "unknown"):
                        raise DeliveryUnknown("provider lookup returned an unsupported result")
                    result = {"delivered": "delivered", "not_sent": "failed_retryable", "unknown": "delivery_unknown"}[status]
                else:
                    message = self.sender.send(MappingProxyType(item), attempt)
                    result = "delivered"
                if result == "delivered" and (not isinstance(message, str) or not message):
                    raise DeliveryUnknown("provider supplied no reliable message ID")
                self.hook("after_provider_call")
                self.finish(item, attempt, result, message)
            except RetryableSendError as exc:
                self.finish(item, attempt, "failed_retryable", error=str(exc))
            except DefiniteSendError as exc:
                self.finish(item, attempt, "cancelled", error=str(exc))
            except DeliveryUnknown as exc:
                self.finish(item, attempt, "delivery_unknown", error=str(exc))
            # Unexpected errors/crashes propagate, leaving the lease to reconcile.
        return len(seen)
