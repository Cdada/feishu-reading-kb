"""S6 target authorization, bounded discovery and immutable late binding.

Only this composition can reach the real adapter. Preparation modes have separate
endpoint allowlists, not a bypass of the ingestion gate. No tokens are persisted.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import urlencode

from .contracts import ContractError, new_id, stable_key
from .notifications import DeliveryUnknown
from .registry import encoded, now
from .runtime import no_symlinks
from .s6 import Client, instant

# Capability ceilings, NOT authorization. The shipped config still has auth=2.
CAPABILITIES = {"send": 4, "download": 2, "read": 8, "auth": 4}
FIXTURE_NAMES = ("kb-v21-s6-normal-test.txt", "kb-v21-s6-recovery-test.txt")
OPERATIONS = ("verify-target", "bind-attachments", "ingest", "dispatch")


def digest(value):
    """Canonical JSON keeps hashes stable across process/JSON key ordering."""
    return hashlib.sha256(encoded(value).encode()).hexdigest()


class StagedClient(Client):
    def __init__(self, registry, config, wire=None, real=False, operation="preflight"):
        super().__init__(registry, config, wire, real)
        self.operation = operation
        self.discovery_path = None

    def event(self, key):
        rows = self.registry.rows("SELECT payload_json FROM custody_events WHERE dedupe_key=?", (key,))
        return json.loads(rows[0]["payload_json"]) if rows else None

    def persist(self, key, payload):
        with self.registry.transaction() as db:
            old = db.execute("SELECT payload_json FROM custody_events WHERE dedupe_key=?", (key,)).fetchone()
            if old and json.loads(old[0]) != payload:
                raise ContractError("immutable S6 phase record changed")
            self.registry.event(db, key.replace(":", "_"), self.config["run_id"], key, payload)

    @property
    def sources(self):
        bound = self.event("s6:attachments_bound")
        return bound["sources"] if bound else []

    @property
    def anchor(self):
        # Stable approval reference, independent of process/run_id/runtime DB.
        root = self.registry.runtime.worktree / ".dev-runtime/kb-v21-s6/authorizations"
        no_symlinks(root)
        root.mkdir(parents=True, exist_ok=True)
        return root / (stable_key(self.config["authorization_reference"]) + ".jsonl")

    @contextmanager
    def anchor_lock(self):
        path = self.anchor.with_suffix(".lock")
        no_symlinks(path)
        with path.open("a+") as lock:
            os.chmod(path, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def check_anchor(self, db):
        path = no_symlinks(self.anchor)
        row = db.execute("SELECT payload_json FROM custody_events WHERE dedupe_key='s6:authorization'").fetchone()
        fingerprint = digest({"config": self.config, "transport": "real" if self.real else "fake"})
        if not path.exists():
            if row:
                raise ContractError("authorization anchor missing; never reset the experiment")
            payload = {"sha256": fingerprint, "registry_nonce": new_id("s6db")}
            # A crash between the durable marker and DB commit blocks recovery.
            with path.open("x") as stream:
                os.chmod(path, 0o600)
                stream.write(encoded(payload) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            directory = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            self.registry.event(db, "s6_authorization", self.config["run_id"], "s6:authorization", payload)
            return
        try:
            lines = [json.loads(line) for line in path.read_text().splitlines()]
        except (ValueError, OSError):
            raise ContractError("authorization anchor unreadable; manual audit required") from None
        if not lines or not row or lines[0] != json.loads(row[0]) or lines[0]["sha256"] != fingerprint:
            raise ContractError("approval changed, run switched or registry rebuilt; blocked")
        ids = [r[0] for r in db.execute("SELECT dedupe_key FROM custody_events WHERE event_type='s6_http_started'")]
        recorded = [line.get("http_attempt") for line in lines[1:]]
        if len(recorded) != len(set(recorded)) or set(ids) != set(recorded):
            raise ContractError("request checkpoint mismatch; do not reset or retry")

    def gate(self):
        self.validate_target("s6-authorization/v2", CAPABILITIES)
        c = self.config
        if c.get("sources"):
            raise ContractError("attachment identities must be discovered, never inserted in authorization")
        if (not isinstance(c.get("permitted_operations"), list)
                or not set(c["permitted_operations"]).issubset(OPERATIONS)):
            raise ContractError("explicit operation permissions required")
        if self.operation not in ("preflight", "freeze-inputs", "status", "read") and self.operation not in c["permitted_operations"]:
            raise ContractError("operation not authorized")
        fixtures = c.get("fixtures", [])
        if not isinstance(fixtures, list) or [f.get("filename") for f in fixtures] != list(FIXTURE_NAMES):
            raise ContractError("two ordered approved fixture specifications required")
        for f in fixtures:
            if (set(f) != {"filename", "size_bytes", "sha256", "md5", "synthetic"}
                    or f["synthetic"] is not True or type(f["size_bytes"]) is not int
                    or not 0 < f["size_bytes"] < 4096):
                raise ContractError("invalid synthetic fixture specification")
            for key, length in (("sha256", 64), ("md5", 32)):
                if not re.fullmatch("[a-f0-9]{" + str(length) + "}", str(f[key])):
                    raise ContractError("fixture hashes required")
        if c.get("discovery_max_seconds") != 600 or c.get("discovery_page_size") != 20:
            raise ContractError("bounded discovery scope required")
        with self.anchor_lock(), self.registry.transaction() as db:
            self.check_anchor(db)

    def budget(self):
        records = [json.loads(r["payload_json"]) for r in self.registry.rows(
            "SELECT payload_json FROM custody_events WHERE event_type='s6_http_started'")]
        return {k: sum(r["kind"] == k for r in records) for k in CAPABILITIES}

    def require_capacity(self, **needed):
        self.gate()
        used = self.budget()
        if not self.token and any(needed.values()):
            needed["auth"] = needed.get("auth", 0) + 1
        if any(used[k] + needed.get(k, 0) > self.config["limits"][k] for k in used):
            raise ContractError("insufficient remaining budget for phase; request new approval, no automatic increase")

    def reserve(self, kind, path, summary):
        self.gate()
        with self.anchor_lock(), self.registry.transaction() as db:
            self.check_anchor(db)
            records = [json.loads(r[0]) for r in db.execute(
                "SELECT payload_json FROM custody_events WHERE event_type='s6_http_started'")]
            if (len(records) >= self.config["total_requests"]
                    or sum(r["kind"] == kind for r in records) >= self.config["limits"][kind]):
                raise ContractError("experiment request budget exhausted")
            if kind == "send" and any(r["kind"] == "send" and r["summary"].get("provider_request_key") == summary.get("provider_request_key") for r in records):
                raise DeliveryUnknown("same-key second request remains blocked")
            attempt = new_id("http")
            # This is a rollback-detection witness, not another mutable counter.
            # Write-ahead/fsync: ambiguity costs availability, never grants budget.
            with self.anchor.open("a") as stream:
                stream.write(encoded({"http_attempt": attempt}) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            self.registry.event(db, "s6_http_started", self.config["run_id"], attempt,
                                {"kind": kind, "path": path, "summary": summary, "started_at": now()})
        return attempt

    def endpoint_allowed(self, kind, method, path):
        self.gate()
        if self.operation not in OPERATIONS or self.operation not in self.config["permitted_operations"]:
            return False
        if kind == "auth":
            return method == "POST" and path == "/open-apis/auth/v3/tenant_access_token/internal"
        if self.operation == "verify-target":
            return kind == "read" and method == "GET" and path in self.target_paths()
        if self.operation == "bind-attachments":
            return (kind == "read" and method == "GET" and path == self.discovery_path
                    and self.event("s6:target_verified") is not None and not self.event("s6:inputs_frozen"))
        self.require_frozen()
        # Ingestion reads exactly bound messages; dispatch has no attachment access.
        if self.operation == "dispatch":
            return kind == "send" and super().endpoint_allowed(kind, method, path)
        if kind == "read" and path == self.target_paths()[1]:
            return False
        return super().endpoint_allowed(kind, method, path)

    def require_frozen(self):
        self.gate()
        if self.operation not in ("ingest", "dispatch", "read", "status"):
            raise ContractError("read-only preparation cannot create custody/outbox/send")
        bound, frozen = self.event("s6:attachments_bound"), self.event("s6:inputs_frozen")
        if not bound or not frozen or frozen["binding_sha256"] != digest(bound):
            raise ContractError("experiment inputs not frozen")

    def target_paths(self):
        chat = "/open-apis/im/v1/chats/" + self.config["chat_id"]
        return ("/open-apis/bot/v3/info", chat, chat + "/members?member_id_type=open_id&page_size=100",
                chat + "/members/is_in_chat")

    def verify_target(self):
        if self.operation != "verify-target":
            raise ContractError("wrong phase")
        self.gate()
        old = self.event("s6:target_verified")
        if old:
            return old
        self.require_capacity(read=4)
        bot_path, chat_path, members_path, presence_path = self.target_paths()
        bot = self.request("read", "GET", bot_path).get("bot", {})
        if not isinstance(bot.get("open_id"), str) or not bot["open_id"].startswith("ou_"):
            raise ContractError("bot identity unresolved")
        chat = self.request("read", "GET", chat_path).get("data", {})
        if chat.get("chat_mode") != "group":
            raise ContractError("S6 target must be an ordinary group")
        if str(chat.get('user_count')) != '1' or str(chat.get('bot_count')) != '1':
            raise ContractError('dedicated test group must contain exactly the approved user and one bot')
        members = self.request("read", "GET", members_path).get("data", {})
        if members.get('has_more') is not False or len(members.get('items', [])) != 1:
            raise ContractError('test group user membership must be complete and singular')
        if not any(m.get("member_id_type") == "open_id" and m.get("member_id") == self.config["account_id"] for m in members.get("items", [])):
            raise ContractError("approved account not confirmed in first members page; no pagination")
        if self.request("read", "GET", presence_path).get("data", {}).get("is_in_chat") is not True:
            raise ContractError("authorized bot not confirmed in target group")
        payload = {"app_id": self.config["app_id"], "bot_open_id": bot["open_id"],
                   "chat_id": self.config["chat_id"], "chat_mode": "group", "account_id": self.config["account_id"],
                   "bot_in_chat": True, "verified_at": now(), "offline_only": not self.real}
        self.persist("s6:target_verified", payload)
        return payload

    def bind_attachments(self, start, end):
        if self.operation != "bind-attachments":
            raise ContractError("wrong phase")
        self.gate()
        verified = self.event("s6:target_verified")
        if not verified or self.event("s6:inputs_frozen"):
            raise ContractError("target must be verified and experiment not frozen")
        if (type(start) is not int or type(end) is not int or not 0 < end-start <= 600
                or start < int(instant(verified["verified_at"]).timestamp())
                or end > int(datetime.now(timezone.utc).timestamp())
                or end > int(instant(self.config["expires_at"]).timestamp())):
            raise ContractError("discovery must cover a past <=600-second window after target verification")
        scope = {"chat_id": self.config["chat_id"], "start": start, "end": end, "page_size": 20}
        self.persist("s6:discovery_scope", scope)
        old = self.event("s6:attachments_bound")
        if old:
            return old
        self.require_capacity(read=1)
        self.discovery_path = "/open-apis/im/v1/messages?" + urlencode({
            "container_id_type": "chat", "container_id": self.config["chat_id"],
            "start_time": start, "end_time": end, "sort_type": "ByCreateTimeAsc", "page_size": 20})
        data = self.request("read", "GET", self.discovery_path).get("data", {})
        if data.get("has_more") is not False:
            raise ContractError("incomplete history page; no automatic pagination or widened search")
        found = {}
        for message in data.get("items", []):
            if message.get("msg_type") != "file":
                continue
            try:
                content = json.loads(message.get("body", {}).get("content", "{}"))
            except (ValueError, TypeError):
                raise ContractError("malformed file identity") from None
            filename = content.get("file_name")
            if filename not in FIXTURE_NAMES:
                continue
            sender = message.get("sender", {})
            if (message.get("chat_id") != self.config["chat_id"] or sender.get("id") != self.config["account_id"]
                    or sender.get("id_type") != "open_id" or sender.get("sender_type") != "user"
                    or message.get("mentions") or message.get("deleted") or filename in found
                    or not re.fullmatch(r"om_[A-Za-z0-9_-]+", str(message.get("message_id", "")))
                    or not re.fullmatch(r"file_[A-Za-z0-9_-]+", str(content.get("file_key", "")))):
                raise ContractError("ambiguous or out-of-scope attachment identity")
            try:
                created = int(message["create_time"]) // 1000
            except (KeyError, ValueError, TypeError):
                raise ContractError("message timestamp missing") from None
            if not start <= created < end:
                raise ContractError("message outside authorized discovery window")
            found[filename] = {"message_id": message["message_id"], "file_key": content["file_key"]}
        if set(found) != set(FIXTURE_NAMES) or len({f["message_id"] for f in found.values()}) != 2:
            raise ContractError("exactly two unambiguous fixtures required; do not guess resource IDs")
        payload = {"sources": [dict(f, **found[f["filename"]]) for f in self.config["fixtures"]],
                   "scope": scope, "authorization_sha256": digest(self.config), "bound_at": now()}
        self.persist("s6:attachments_bound", payload)
        return payload

    def freeze_inputs(self, binding_sha256):
        self.gate()
        bound = self.event("s6:attachments_bound")
        if not bound or binding_sha256 != digest(bound):
            raise ContractError("review the actual binding before freezing")
        self.persist("s6:inputs_frozen", {"binding_sha256": binding_sha256})
        return {"inputs_frozen": True, "binding_sha256": binding_sha256}
