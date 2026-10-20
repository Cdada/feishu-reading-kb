#!/usr/bin/env python3
"""Offline-only subprocess entry exercising the same harness with fake HTTP.

Never exposed by the production CLI. Socket auditing is installed before imports.
"""
import json
import os
from pathlib import Path
import sys
import hashlib


def deny_network(event, args):
    if event in ("socket.connect", "socket.connect_ex", "socket.bind", "socket.getaddrinfo"):
        raise RuntimeError("offline S6 chain forbids all network")


sys.addaudithook(deny_network)
PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE))
sys.path.insert(0, str(PACKAGE / "src"))
import s6_harness
from kb_custody_v21.contracts import ContractError
from kb_custody_v21.registry import Registry
from kb_custody_v21.s6 import storage


class FakeWire:
    def __init__(self, config):
        if not config["authorization_reference"].startswith("OFFLINE-ONLY-"):
            raise ContractError("fake CLI requires a synthetic authorization")
        self.config = config
        self.directory = Path(config["secret_path"]).parent
        self.calls = []

    def request(self, method, path, body, token):
        decoded = json.loads(body) if body else {}
        record = {"method": method, "path": path}
        if "/im/v1/messages?receive_id_type" in path:
            record.update(provider_request_key=decoded["uuid"], body_sha256=hashlib.sha256(body).hexdigest())
        self.calls.append(record)
        with (self.directory / "wire.jsonl").open("a") as out:
            out.write(json.dumps(record) + "\n")
            out.flush()
            os.fsync(out.fileno())
        platform = json.loads((self.directory / "platform.json").read_text())
        if "/auth/" in path:
            response = {"code": 0, "tenant_access_token": "OFFLINE-SECRET-TOKEN"}
        elif path == "/open-apis/bot/v3/info":
            response = {"code": 0, "bot": {"open_id": "ou_offline_bot", "app_name": "offline"}}
        elif "/members?" in path:
            response = {"code": 0, "data": {"has_more":False, "items": [{"member_id_type": "open_id", "member_id": self.config["account_id"]}]}}
        elif path.endswith("/members/is_in_chat"):
            response = {"code": 0, "data": {"is_in_chat": True}}
        elif "/chats/" in path:
            response = {"code": 0, "data": {"chat_mode": platform.get("chat_mode", "group"),
                "user_count":platform.get('user_count','1'),"bot_count":platform.get('bot_count','1')}}
        elif "/resources/" in path:
            source = next(m for m in platform["items"] if m["message_id"] in path)
            name = json.loads(source["body"]["content"])["file_name"]
            return 200, (self.directory / name).read_bytes()
        elif method == "POST":
            response = {"code": 0, "data": {"message_id": "om_offline_notice"}}
        elif "container_id_type=chat" in path:
            response = {"code": 0, "data": {"items": platform["items"], "has_more": platform.get("has_more", False)}}
        else:
            response = {"code": 0, "data": {"items": [m for m in platform["items"] if m["message_id"] in path]}}
        return 200, json.dumps(response).encode()


if __name__ == "__main__":
    try:
        args = sys.argv[1:]
        if "--real" in args:
            raise ContractError("fake subprocess may never select real")
        if "--expire-offline-lease" in args:
            args.remove("--expire-offline-lease")
            if args[0] != "dispatch":
                raise ContractError("offline lease advance only for restart test")
            config = json.loads(Path(args[args.index("--config")+1]).read_text())
            FakeWire(config)  # synthetic authorization assertion
            import subprocess
            wt = Path(subprocess.check_output(["git", "-C", str(PACKAGE), "rev-parse", "--show-toplevel"], text=True).strip())
            registry = Registry(storage(wt, config["run_id"]))
            with registry.transaction() as db:
                db.execute("UPDATE notification_outbox SET lease_until=0 WHERE delivery_state='sending'")
            print(json.dumps({"offline_only": True, "fixture_mutation": "advance expired sending lease"}), flush=True)
        s6_harness.main(args, fake_wire_factory=FakeWire)
    except (ContractError, OSError) as exc:
        print(json.dumps({"status": "BLOCKED", "reason_type": type(exc).__name__, "reason": str(exc)}), file=sys.stderr)
        sys.exit(2)
