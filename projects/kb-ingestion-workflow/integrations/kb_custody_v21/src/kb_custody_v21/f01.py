"""One explicit dedicated-app experiment; not a production sender factory.

Reuses S6's single-request HTTPS wire. The caller owns the private approval and
credentials. Each request gets a durable intent before networking; any unknown
intent stops the whole channel. No retries, pagination, listeners or lookup.
"""
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlencode

from .contracts import ContractError
from .runtime import no_symlinks
from .s6 import Wire, instant

LIMITS = dict(auth=2, bot=2, create=1, chat=8, members=8, files=3,
              images=1, send=6, list=2, message=4, download=4)
PREFIX = "/open-apis"


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def identity(value, prefix):
    if not isinstance(value, str) or not re.fullmatch(prefix + r"[A-Za-z0-9_-]+", value):
        raise ContractError("invalid bound identity")
    return value


def validated_message_content(item, *, app_id, bot_id, chat_id, kind, name,
                              message_id=None, text=None):
    """Pure validation shared by new receipts and immutable historical receipts."""
    return _validated_message_content(item, app_id=app_id, bot_id=bot_id,
        chat_id=chat_id, kind=kind, name=name, message_id=message_id, text=text)


def _validated_message_content(item, *, app_id, bot_id, chat_id, kind, name,
                               message_id=None, text=None, observe_updated_text=False):
    """Only the known-notification observer may retain a true update flag."""
    if not isinstance(item, dict) or not isinstance(item.get('sender'), dict):
        raise ContractError('message envelope and sender required')
    mid = identity(item.get('message_id'), 'om_')
    sender = item['sender']
    sender_ok = sender.get('sender_type') == 'app' and (
        sender.get('id_type') == 'app_id' and sender.get('id') == app_id
        or sender.get('id_type') == 'open_id' and sender.get('id') == bot_id)
    if (item.get('chat_id') != chat_id or message_id is not None and mid != message_id
            or item.get('msg_type') != kind or item.get('deleted') is not False
            or not sender_ok):
        raise ContractError('message identity/type/state mismatch')
    updated = item.get('updated', False)
    if (type(updated) is not bool
            or updated and not (observe_updated_text and kind == 'text')):
        raise ContractError('message identity/type/state mismatch')
    content = json.loads(item['body']['content'])
    if not isinstance(content, dict):
        raise ContractError('message content must be an object')
    if kind == 'text':
        if content != {'text': text}:
            raise ContractError('notification text differs')
    else:
        identity(content.get('image_key' if kind == 'image' else 'file_key'),
                 'img_' if kind == 'image' else 'file_')
        if kind == 'file' and content.get('file_name') != name:
            raise ContractError('message filename differs')
    return content


class Experiment:
    reference = 'F01'
    limits = LIMITS

    def __init__(self, root, approval, *, wire=None, clock=None, sleep=time.sleep):
        self.root = no_symlinks(Path(root).absolute())
        if ".dev-runtime" not in self.root.parts:
            raise ContractError("private experiment storage required")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sleep, self.wire = sleep, wire or Wire()
        self.approval = json.loads(encoded(approval))
        self.token = None
        if approval.get("reference") != self.reference or approval.get("limits") != self.limits:
            raise ContractError("exact " + self.reference + " limits required")
        if approval.get("synthetic_only") is not True or approval.get("approved") is not True:
            raise ContractError("explicit synthetic approval required")
        identity(approval.get("app_id"), "cli_")
        identity(approval.get("account_id"), "ou_")
        start, end = instant(approval["starts_at"]), instant(approval["expires_at"])
        if not 0 < (end - start).total_seconds() <= 7200:
            raise ContractError("one bounded window required")
        self.lock = (self.root / "execution.lock").open("a+")
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.path = self.root / "journal.local.jsonl"
        self.events = []
        if self.path.exists():
            self.events = [json.loads(line) for line in self.path.read_text().splitlines()]
            if not self.events or self.events[0] != {"approval": self.approval}:
                self.close()
                raise ContractError("approval changed; never reset experiment")
        else:
            self.append({"approval": self.approval})
        try:
            self.check()
        except Exception:
            self.close()
            raise

    def close(self):
        self.token = None
        self.lock.close()

    def append(self, item):
        with self.path.open("a") as stream:
            os.chmod(self.path, 0o600)
            stream.write(encoded(item) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        self.events.append(item)

    def value(self, name):
        rows = [e["value"] for e in self.events if e.get("binding") == name]
        return rows[0] if rows else None

    def bind(self, name, value):
        old = self.value(name)
        if old is not None and old != value:
            raise ContractError("immutable experiment binding changed")
        if old is None:
            self.append({"binding": name, "value": value})

    def check(self):
        if not instant(self.approval["starts_at"]) <= self.clock() < instant(self.approval["expires_at"]):
            raise ContractError("outside approved " + self.reference + " window")
        started = {e["intent"] for e in self.events if "intent" in e}
        completed = {e["complete"] for e in self.events if "complete" in e}
        if started != completed or any("stopped" in e for e in self.events):
            raise ContractError("unfinished/failed request; no retry or unknown lookup")

    def request(self, kind, method, path, data=None, *, key, binary=False, content_type="application/json"):
        self.check()
        if kind not in self.limits or sum(e.get("kind") == kind for e in self.events) >= self.limits[kind]:
            raise ContractError("endpoint budget exhausted")
        if any(e.get("intent") == key for e in self.events):
            raise ContractError("operation already attempted; no resend")
        if kind != "auth" and not self.token:
            raise ContractError("explicit application authentication required")
        body = data if isinstance(data, bytes) else encoded(data).encode() if data is not None else None
        # Each endpoint is generated by the typed methods below, never accept
        # a user URL or redirect as a destination.
        expected = self.allowed_paths().get(kind, set())
        if (method, path) not in expected:
            raise ContractError("request target outside bound objects")
        if kind == "send" and (not isinstance(data, dict) or data.get("receive_id") != self.value("chat")):
            raise ContractError("message recipient outside bound group")
        if kind == "auth" and (not isinstance(data, dict) or data.get("app_id") != self.approval["app_id"]):
            raise ContractError("authentication app outside approval")
        self.sleep(1)
        self.check()
        self.append({"intent": key, "kind": kind, "method": method, "path": path,
                     "payload_sha256": sha(body or b""), "at": self.clock().isoformat()})
        try:
            status, raw = self.wire.request(method, path, body, self.token, content_type)
            # Preserve failed provider responses privately too, so a contract
            # mismatch can be diagnosed without resending the operation.
            if kind != "auth":
                target = self.root / (sha(key.encode()) + ".response.local")
                target.write_bytes(raw)
                os.chmod(target, 0o600)
                # A received rejection is evidence, not a successful operation.
                # Preserve its HTTP status/hash even for non-JSON responses.
                # Failed intents still block all retries and unknown lookups.
                self.append({"received": key, "http_status": status, "response_sha256": sha(raw)})
            if status != 200:
                raise ContractError("provider HTTP failure")
            result = raw if binary else json.loads(raw)
            if not binary and result.get("code") != 0:
                raise ContractError("provider API failure")
            # Authentication responses (including tokens) never go to disk.
            self.append({"complete": key, "http_status": status, "response_sha256": sha(raw)})
            return result
        except Exception as error:
            # Boundary records the exception type, never credential-bearing data.
            self.append({"stopped": key, "error_type": type(error).__name__})
            raise

    def allowed_paths(self):
        chat = self.value("chat")
        paths = {
            "auth": {("POST", PREFIX + "/auth/v3/tenant_access_token/internal")},
            "bot": {("GET", PREFIX + "/bot/v3/info")},
        }
        if not chat:
            return paths
        c = PREFIX + "/im/v1/chats/" + chat
        paths.update(chat={("GET", c)}, members={("GET", c + "/members?member_id_type=open_id&page_size=100")})
        if not self.value("verified"):
            return paths
        paths.update(files={("POST", PREFIX + "/im/v1/files")}, images={("POST", PREFIX + "/im/v1/images")},
            send={("POST", PREFIX + "/im/v1/messages?receive_id_type=chat_id")})
        paths['list'] = {('GET', self.list_path())}
        paths["message"], paths["download"] = set(), set()
        for e in self.events:
            if e.get("binding", "").startswith("message:"):
                message = e["value"]
                paths["message"].add(("GET", PREFIX + "/im/v1/messages/" + message["message_id"]))
                if "resource_key" in message:
                    paths["download"].add(("GET", PREFIX + "/im/v1/messages/" + message["message_id"] +
                        "/resources/" + message["resource_key"] + "?type=" + message["resource_type"]))
        return paths

    def list_path(self):
        return PREFIX + '/im/v1/messages?' + urlencode(dict(container_id_type='chat',
            container_id=self.value('chat'), start_time=int(instant(self.approval['starts_at']).timestamp()),
            end_time=int(instant(self.approval['expires_at']).timestamp()), page_size=50))

    def list_sent(self):
        """One bounded page of known synthetic messages, never unknown recovery."""
        expected = {e['value']['message_id'] for e in self.events
                    if e.get('binding', '').startswith('message:')}
        if not expected:
            raise ContractError('confirmed send receipts required before list')
        result = self.request('list', 'GET', self.list_path(), key='list:1').get('data', {})
        items = result.get('items', [])
        if (result.get('has_more') is not False or len(items) != len(expected)
                or {item.get('message_id') for item in items} != expected
                or any(item.get('chat_id') != self.value('chat') for item in items)):
            self.append({'stopped': 'message list differs from confirmed synthetic receipts'})
            raise ContractError('message list outside expected complete synthetic set')
        return items

    def authenticate(self, app_id, secret):
        if app_id != self.approval["app_id"] or not secret:
            raise ContractError("credential app differs from approval")
        result = self.request("auth", "POST", PREFIX + "/auth/v3/tenant_access_token/internal",
                              dict(app_id=app_id, app_secret=secret), key="auth:1")
        self.token = result.get("tenant_access_token")
        if not isinstance(self.token, str) or not self.token:
            raise ContractError("token response missing")

    def bot(self):
        result = self.request("bot", "GET", PREFIX + "/bot/v3/info", key="bot:1")
        bot = result.get("bot", {})
        self.bind("bot", identity(bot.get("open_id"), "ou_"))
        return bot

    def create(self):
        # F01's real run proved these isolation controls are response-only on
        # create. They require a separate, unapproved chat-update operation.
        # Keep the successful old create receipt; never create another group
        # through the incomplete F01 permission plan, even in a fresh process.
        raise ContractError('F01 cannot establish required isolation without separately approved chat update')

    def verify(self, batch):
        chat = self.value("chat")
        if not chat:
            raise ContractError("create receipt required")
        path = PREFIX + "/im/v1/chats/" + chat
        info = self.request("chat", "GET", path, key="chat:" + batch).get("data", {})
        if (info.get("chat_mode") != "group" or info.get("chat_type") != "private"
                or info.get("external") is not False or str(info.get("user_count")) != "1"
                or str(info.get("bot_count")) != "1" or info.get("share_card_permission") != "not_allowed"
                or info.get("add_member_permission") != "only_owner"):
            self.append({"stopped": "group identity/membership mismatch"})
            raise ContractError("dedicated group isolation check failed")
        members = self.request("members", "GET", path + "/members?member_id_type=open_id&page_size=100",
                               key="members:" + batch).get("data", {})
        items = members.get("items", [])
        if (members.get("has_more") is not False or members.get("trigger_security_conf_limit") is True
                or len(items) != 1 or items[0].get("member_id") != self.approval["account_id"]
                or items[0].get("member_id_type") != "open_id"):
            self.append({"stopped": "member binding mismatch"})
            raise ContractError("complete singular user membership required")
        self.bind("verified", True)

    def establish(self):
        self.bot()
        self.create()

    def upload(self, name, data, *, image=False):
        spec = self.approval["fixtures"].get(name)
        if spec != {"sha256": sha(data), "bytes": len(data)} or len(data) > 1024 * 1024:
            raise ContractError("only frozen synthetic bytes may be uploaded")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise ContractError("simple fixture name required")
        boundary = "kbv21" + sha(data)[:32]
        fields = {"image_type": "message"} if image else {"file_type": "pdf" if name.endswith('.pdf') else "stream", "file_name": name}
        body = b""
        for k, v in fields.items():
            body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n').encode()
        field = "image" if image else "file"
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{name}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode()
        body += data + f"\r\n--{boundary}--\r\n".encode()
        kind = "images" if image else "files"
        result = self.request(kind, "POST", PREFIX + "/im/v1/" + kind, body, key="upload:" + name,
                              content_type="multipart/form-data; boundary=" + boundary).get("data", {})
        resource = identity(result.get("image_key" if image else "file_key"), "img_" if image else "file_")
        self.bind("upload:" + name, {"resource_key": resource, "resource_type": field, "sha256": sha(data)})
        return resource

    def send(self, name, *, text=None):
        upload = self.value("upload:" + name)
        if text is not None:
            # Notification bytes must be pre-authorized synthetic evidence too.
            if text not in self.approval.get("notifications", []):
                raise ContractError("notification outside approved synthetic content")
            kind, content = "text", {"text": text}
        elif upload:
            kind = upload["resource_type"]
            content = {"image_key" if kind == "image" else "file_key": upload["resource_key"]}
        else:
            raise ContractError("frozen upload receipt required")
        body = dict(receive_id=self.value("chat"), msg_type=kind, content=encoded(content),
                    uuid=sha((self.approval["starts_at"] + name).encode())[:48])
        result = self.request("send", "POST", PREFIX + "/im/v1/messages?receive_id_type=chat_id", body,
                              key="send:" + name).get("data", {})
        content = self.message_content(result, kind=kind, name=name, text=text)
        message = {"message_id": result["message_id"]}
        if upload:
            # Upload keys address uploaded resources for sending. The actual
            # message receipt may return a different key for message download.
            # Preserve both; never infer a mapping from a filename or key shape.
            message.update(upload, upload_resource_key=upload["resource_key"])
            message["resource_key"] = content["image_key" if kind == "image" else "file_key"]
        self.bind("message:" + name, message)
        return message

    def message_content(self, item, *, kind, name, message_id=None, text=None):
        """Validate the envelope before adopting any message resource key."""
        try:
            return validated_message_content(item, app_id=self.approval['app_id'],
                bot_id=self.value('bot'), chat_id=self.value('chat'), kind=kind,
                name=name, message_id=message_id, text=text)
        except (KeyError, TypeError, ValueError, ContractError) as error:
            self.append({"stopped": "message receipt validation failed"})
            raise ContractError("message receipt validation failed") from error

    def download(self, name):
        message = self.value("message:" + name)
        upload = self.value("upload:" + name)
        if (not message or not upload or "upload_resource_key" not in message
                or message["upload_resource_key"] != upload["resource_key"]
                or message.get("resource_type") != upload["resource_type"]
                or message.get("sha256") != upload["sha256"]):
            raise ContractError("validated upload-to-message resource binding required")
        path = PREFIX + "/im/v1/messages/" + message["message_id"]
        items = self.request("message", "GET", path, key="detail:" + name).get("data", {}).get("items", [])
        if len(items) != 1:
            self.append({"stopped": "message detail cardinality mismatch"})
            raise ContractError("one bound message required")
        content = self.message_content(items[0], kind=message["resource_type"], name=name,
                                       message_id=message["message_id"])
        key = "image_key" if message["resource_type"] == "image" else "file_key"
        if content[key] != message["resource_key"]:
            self.append({"stopped": "resource identity mismatch"})
            raise ContractError("message/resource/sender binding failed")
        data = self.request("download", "GET", path + "/resources/" + message["resource_key"] +
            "?type=" + message["resource_type"], key="download:" + name, binary=True)
        if sha(data) != message["sha256"]:
            self.append({"stopped": "download bytes mismatch"})
            raise ContractError("download differs from frozen synthetic bytes")
        return data
