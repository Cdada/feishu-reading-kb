"""Explicit S6 composition. No import-time network, credential read or listener.

Normal DEV Runtime remains mock-only. S6 reuses its guarded storage primitives
in a separate root, and authorizes each physical HTTP request independently.
Unproven lookup/window semantics deliberately provide no automatic resend.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import copy
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import re
import ssl
from urllib.parse import quote

from .contracts import ContractError, stable_key, new_id
from .intake import FeishuMockIntake, normalized_message_contract
from .notifications import DeliveryUnknown, OutboxWorker
from .registry import Registry, encoded, now
from .runtime import DEFAULTS, Runtime, no_symlinks
from .staging import Staging

CAPS = {"send": 4, "download": 2, "read": 8, "auth": 2}
PREFIX = "【V2.1 S6 测试】"
HOST = "open.feishu.cn"


def instant(value):
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            raise ValueError()
        return result
    except (ValueError, TypeError):
        raise ContractError("explicit timezone timestamp required") from None


def storage(worktree, run_id):
    if not isinstance(run_id, str) or not re.fullmatch(r"s6-[a-zA-Z0-9_-]{8,80}", run_id):
        raise ContractError("invalid explicit S6 run_id")
    # Storage only: no override of DEV's sender factory or security gate.
    cfg = dict(DEFAULTS, runtime_relative=".dev-runtime/kb-v21-s6/runs/" + run_id,
               max_asset_bytes=1024 * 1024)
    return Runtime(worktree, cfg)


class Wire:
    """One HTTPS request, zero retries, no redirects/proxy/env credential lookup."""
    def request(self, method, path, body, token, content_type="application/json"):
        connection = http.client.HTTPSConnection(HOST, timeout=20, context=ssl.create_default_context())
        headers = {"Content-Type": content_type}
        if token:
            headers["Authorization"] = "Bearer " + token
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            # S6 fixtures are small. Never buffer an unbounded provider response.
            content = response.read(1024 * 1024 + 1)
            if len(content) > 1024 * 1024:
                raise OSError("S6 response size limit")
            return response.status, content
        finally:
            connection.close()


class Client:
    def __init__(self, registry, config, wire=None, real=False):
        self.registry = registry
        self.config = copy.deepcopy(config)
        self.real = real
        if real and wire is not None:
            raise ContractError("real S6 cannot inject a transport")
        if not real and wire is None:
            raise ContractError("offline S6 requires explicit fake transport")
        self.wire = Wire() if real else wire
        self.token = None

    @property
    def sources(self):
        return self.config.get("sources", [])

    def require_frozen(self):
        self.gate()

    def validate_target(self, schema, caps):
        c = self.config
        if c.get("schema") != schema or c.get("enabled") is not True:
            raise ContractError("S6 disabled: explicit authorization required")
        if c.get("user_confirmed") is not True or not c.get("authorization_reference"):
            raise ContractError("missing user authorization evidence")
        runtime = self.registry.runtime
        if runtime.root != runtime.worktree / ".dev-runtime/kb-v21-s6/runs" / c.get("run_id", ""):
            raise ContractError("S6 requires its own run_id and storage")
        runtime.gate()
        for k, prefix in [("app_id", "cli_"), ("chat_id", "oc_"), ("account_id", "ou_")]:
            if not isinstance(c.get(k), str) or not re.fullmatch(prefix + r"[A-Za-z0-9_-]+", c[k]):
                raise ContractError("missing or invalid authorized identity")
        current = datetime.now(timezone.utc)
        if not instant(c.get("confirmed_at")) <= current < instant(c.get("expires_at")):
            raise ContractError("authorization is not currently valid")
        isolation = c.get("isolation", {})
        if isolation.get("confirmed") is not True or not isolation.get("evidence"):
            raise ContractError("production isolation evidence required")
        if isolation.get("strategy") not in ("dedicated_app_no_production_subscription", "reviewed_group_guard"):
            raise ContractError("unsupported isolation")
        if self.real and isolation["strategy"] == "reviewed_group_guard":
            # R02 found writes before business rejection and unaccounted consumers.
            # Matching those old files cannot establish a safe real experiment.
            raise ContractError("historical group guard cannot authorize real S6; new isolation evidence required")
        # Retain the old checks for offline regression only.
        if isolation["strategy"] == "reviewed_group_guard":
            proofs = isolation.get("file_hashes", [])
            if ({Path(p["path"]).name for p in proofs} != {"gateway.mjs", "document-reference.mjs", "intake-flow.mjs", "index.js"}
                    or isolation.get("no_mentions") is not True):
                raise ContractError("group name alone is not isolation")
            for proof in proofs:
                path = no_symlinks(proof["path"])
                if hashlib.sha256(path.read_bytes()).hexdigest() != proof["sha256"]:
                    raise ContractError("production isolation code changed")
        limits = c.get("limits")
        if not isinstance(limits, dict) or set(limits) != set(caps):
            raise ContractError("explicit category budgets required")
        minimum = 0 if schema == "s6-authorization/v2" else 1
        if any(type(limits[k]) is not int or not minimum <= limits[k] <= caps[k] for k in caps):
            raise ContractError("invalid request budget")
        if c.get("logical_notifications") != 2 or c.get("total_requests") != sum(limits.values()):
            raise ContractError("explicit logical and total budgets required")
        if not isinstance(c.get("secret_path"), str) or not Path(c["secret_path"]).is_absolute():
            raise ContractError("explicit credential file required")

    def gate(self):
        # Legacy composition is retained solely for earlier offline regression.
        if self.real:
            raise ContractError("real operations require the staged authorization entry")
        self.validate_target("s6-authorization/v1", CAPS)
        c = self.config
        sources = c.get("sources")
        if not isinstance(sources, list) or not 1 <= len(sources) <= 2:
            raise ContractError("only explicit synthetic attachment identities allowed")
        seen = set()
        for source in sources:
            for k in ("message_id", "file_key", "filename"):
                if not isinstance(source.get(k), str) or not source[k]:
                    raise ContractError("missing synthetic resource identity")
            if not re.fullmatch(r"om_[A-Za-z0-9_-]+", source["message_id"]) or not re.fullmatch(r"file_[A-Za-z0-9_-]+", source["file_key"]):
                raise ContractError("invalid platform resource ID")
            if source["message_id"] in seen:
                raise ContractError("duplicate experiment source")
            seen.add(source["message_id"])
            if source.get("synthetic") is not True or source["filename"] not in (
                    "kb-v21-s6-normal-test.txt", "kb-v21-s6-recovery-test.txt"):
                raise ContractError("only approved S6 fixtures")
            if type(source.get("size_bytes")) is not int or not 0 < source["size_bytes"] < 4096:
                raise ContractError("small synthetic fixture required")
            for k, length in (("sha256", 64), ("md5", 32)):
                if not re.fullmatch("[a-f0-9]{" + str(length) + "}", str(source.get(k, ""))):
                    raise ContractError("known local hashes required")
        if not isinstance(c.get("secret_path"), str) or not Path(c["secret_path"]).is_absolute():
            raise ContractError("explicit credential file required")
        # Pin the whole authorization to this registry; restart cannot reset budget.
        fingerprint = stable_key(c)
        with self.registry.transaction() as db:
            old = db.execute("SELECT payload_json FROM custody_events WHERE dedupe_key='s6:authorization'").fetchone()
            if old and json.loads(old[0])["sha256"] != fingerprint:
                raise ContractError("authorization changed within an existing S6 run")
            self.registry.event(db, "s6_authorization", c["run_id"], "s6:authorization", {"sha256": fingerprint})

    def reserve(self, kind, path, summary):
        self.gate()
        with self.registry.transaction() as db:
            records = [json.loads(r[0]) for r in db.execute(
                "SELECT payload_json FROM custody_events WHERE event_type='s6_http_started'")]
            if len(records) >= 16 or sum(r["kind"] == kind for r in records) >= self.config["limits"][kind]:
                raise ContractError("S6 physical request budget exhausted")
            if kind == "send" and any(r["kind"] == "send" and
                    r["summary"].get("provider_request_key") == summary.get("provider_request_key") for r in records):
                raise DeliveryUnknown("same-key repeat remains BLOCKED pending window contract")
            attempt = new_id("http")
            self.registry.event(db, "s6_http_started", self.config["run_id"], attempt,
                                {"kind": kind, "path": path, "summary": summary, "started_at": now()})
        return attempt

    def request(self, kind, method, path, body=None, summary=None):
        if not self.endpoint_allowed(kind, method, path):
            raise ContractError("endpoint outside S6 allowlist")
        return self.perform_request(kind, method, path, body, summary)

    def endpoint_allowed(self, kind, method, path):
        # No caller-provided URL, arbitrary endpoint, delete, upload, or redirect.
        valid = (kind == "auth" and method == "POST" and path == "/open-apis/auth/v3/tenant_access_token/internal")
        valid |= kind == "send" and method == "POST" and path == "/open-apis/im/v1/messages?receive_id_type=chat_id"
        valid |= kind == "read" and method == "GET" and path == "/open-apis/im/v1/chats/" + quote(str(self.config.get("chat_id")), safe="")
        for source in self.sources:
            base = "/open-apis/im/v1/messages/" + quote(source["message_id"], safe="")
            valid |= kind == "read" and method == "GET" and path == base + "?user_id_type=open_id"
            valid |= kind == "download" and method == "GET" and path == base + "/resources/" + quote(source["file_key"], safe="") + "?type=file"
        return valid

    def perform_request(self, kind, method, path, body=None, summary=None):
        self.gate()
        if kind == "auth" and (not isinstance(body, dict) or body.get("app_id") != self.config["app_id"]):
            raise ContractError("authentication app outside authorization")
        if kind == "send":
            if not isinstance(body, dict) or body.get("receive_id") != self.config["chat_id"] or body.get("msg_type") != "text":
                raise ContractError("send target/type outside authorization")
            frozen = self.registry.rows("SELECT payload_json FROM custody_events WHERE dedupe_key=?",
                                        ("s6:payload:" + str(body.get("uuid")),))
            if not frozen or json.loads(frozen[0]["payload_json"])["body"] != body:
                raise ContractError("only the persisted frozen request may be sent")
            if not summary or summary.get("provider_request_key") != body["uuid"]:
                raise ContractError("missing request correlation")
        if kind != "auth" and not self.token:
            self.authenticate()
        attempt = self.reserve(kind, path, summary or {})
        try:
            status, raw = self.wire.request(method, path, encoded(body).encode() if body is not None else None,
                                            self.token if kind != "auth" else None)
        except (OSError, http.client.HTTPException):
            self.record_response(attempt, kind, None, None)
            raise DeliveryUnknown("S6 transport outcome unknown; no automatic retry") from None
        # Raw responses and credentials never enter diagnostics or event payloads.
        code = None
        if kind != "download":
            try:
                result = json.loads(raw)
                code = result.get("code")
            except (ValueError, AttributeError):
                result = None
        else:
            result = raw
        self.record_response(attempt, kind, status, code)
        if status != 200 or (kind != "download" and (not isinstance(result, dict) or code != 0)):
            raise DeliveryUnknown("S6 provider response unconfirmed; inspect sanitized status/code")
        return result

    def record_response(self, attempt, kind, status, code):
        with self.registry.transaction() as db:
            self.registry.event(db, "s6_http_finished", self.config["run_id"], attempt + ":finished",
                                {"http_attempt": attempt, "kind": kind, "http_status": status,
                                 "code": code if type(code) is int else None})

    def authenticate(self):
        self.gate()  # Must precede even the credential read.
        path = no_symlinks(self.config["secret_path"])
        if path.stat().st_mode & 0o077:
            raise ContractError("credential file must be private")
        secret = path.read_text().strip()
        if not secret:
            raise ContractError("empty credential")
        result = self.request("auth", "POST", "/open-apis/auth/v3/tenant_access_token/internal",
                              {"app_id": self.config["app_id"], "app_secret": secret})
        token = result.get("tenant_access_token")
        if not isinstance(token, str) or not token:
            raise DeliveryUnknown("missing token; no retry")
        self.token = token  # Memory only. A restart consumes another auth budget.


class Sender:
    def __init__(self, client):
        self.client = client

    def send(self, item, attempt):
        c = self.client
        key = item.get('provider_request_key')
        if not isinstance(key,str) or not 1 <= len(key) <= 50:
            raise ContractError('persisted provider key exceeds platform contract; never rewrite/re-key an old notification')
        c.require_frozen()
        persisted = c.registry.rows("SELECT * FROM notification_outbox WHERE notification_id=?", (item["notification_id"],))
        if not persisted or any(persisted[0][k] != item[k] for k in (
                "provider_request_key", "target_chat_id", "scope_type", "scope_id", "kind", "payload")):
            raise ContractError("sender requires immutable persisted outbox")
        if item["target_chat_id"] != c.config["chat_id"] or item["scope_type"] != "single_source" or item["kind"] != "success":
            raise ContractError("S6 only sends authorized single-source success")
        key = item["provider_request_key"]
        if not isinstance(key, str) or not key:
            raise ContractError("persistent provider key required")
        body = {"receive_id": item["target_chat_id"], "msg_type": "text",
                "content": encoded({"text": PREFIX + item["payload"]}), "uuid": key}
        digest = stable_key(body)
        with c.registry.transaction() as db:
            old = db.execute("SELECT payload_json FROM custody_events WHERE dedupe_key=?", ("s6:payload:" + key,)).fetchone()
            if old and json.loads(old[0])["sha256"] != digest:
                raise ContractError("same provider key changed payload")
            count = db.execute("SELECT count(*) FROM custody_events WHERE event_type='s6_payload_frozen'").fetchone()[0]
            if not old and count >= 2:
                raise ContractError("logical notification budget exhausted")
            c.registry.event(db, "s6_payload_frozen", item["notification_id"], "s6:payload:" + key,
                             {"sha256": digest, "body": body})
            # This version has no verified lookup or window start contract.
            previous = db.execute("SELECT 1 FROM custody_events WHERE dedupe_key=?", ("s6:send:" + key,)).fetchone()
            if previous:
                raise DeliveryUnknown("prior send may have happened; repeat/window recovery BLOCKED")
            c.registry.event(db, "s6_send_reserved", item["notification_id"], "s6:send:" + key,
                             {"provider_request_key": key, "attempt": attempt, "payload_sha256": digest, "first_reserved_at": now()})
        result = c.request("send", "POST", "/open-apis/im/v1/messages?receive_id_type=chat_id", body,
                           {"provider_request_key": key, "attempt": attempt, "payload_sha256": digest})
        mid = result.get("data", {}).get("message_id")
        if not isinstance(mid, str) or not mid:
            raise DeliveryUnknown("no reliable provider message_id")
        return mid

    def lookup(self, item, attempt):
        # A history scan, 404 or lack of matches is NOT authoritative not_sent.
        # Do not use test-only response evidence or manufacture a lookup endpoint.
        return "unknown", None


class Attachment:
    def __init__(self, client, source):
        self.client, self.source = client, source

    @contextmanager
    def download(self):
        path = "/open-apis/im/v1/messages/" + quote(self.source["message_id"], safe="")
        data = self.client.request("download", "GET", path + "/resources/" +
                                   quote(self.source["file_key"], safe="") + "?type=file")
        with io.BytesIO(data) as stream:
            yield stream


def receive(client, source_index):
    client.require_frozen()
    if type(source_index) is not int or not 0 <= source_index < len(client.sources):
        raise ContractError("source index outside approved fixtures")
    source = client.sources[source_index]
    r = client.registry
    message = {"messageId": source["message_id"], "chatId": client.config["chat_id"],
               "resources": [{"type": "file", "fileKey": source["file_key"], "fileName": source["filename"]}]}
    contract = normalized_message_contract(message, 0)
    contract["intake_key"] = stable_key("feishu-s6", message["chatId"], message["messageId"])
    contract["expected"] = {k: source[k] for k in ("size_bytes", "sha256", "md5")}
    row = r.register(contract)
    if r.receipt(row["entry_id"])["source_staging_status"] != "accepted":
        if client.config["schema"] == "s6-authorization/v1" and client.config["isolation"]["strategy"] == "reviewed_group_guard":
            chat = client.request("read", "GET", "/open-apis/im/v1/chats/" + quote(client.config["chat_id"], safe=""))
            if chat.get("data", {}).get("chat_mode") != "group":
                raise ContractError("existing production app may only use a verified group")
        data = client.request("read", "GET", "/open-apis/im/v1/messages/" +
                              quote(source["message_id"], safe="") + "?user_id_type=open_id")
        items = data.get("data", {}).get("items", [])
        if len(items) != 1:
            raise ContractError("exact test message not found")
        m = items[0]
        sender = m.get("sender", {})
        content = json.loads(m.get("body", {}).get("content", "{}"))
        if (m.get("message_id") != source["message_id"] or m.get("chat_id") != client.config["chat_id"]
                or sender.get("id") != client.config["account_id"] or sender.get("id_type") != "open_id"
                or sender.get("sender_type") != "user" or m.get("msg_type") != "file" or m.get("deleted")
                or m.get("mentions") or content.get("file_key") != source["file_key"]
                or content.get("file_name") != source["filename"]):
            raise ContractError("platform message is outside authorized fixture identity")
    # Reuse the existing transport-buffer/staging implementation, not a new receipt state machine.
    return FeishuMockIntake(r, Staging(r)).receive(contract, Attachment(client, source))
