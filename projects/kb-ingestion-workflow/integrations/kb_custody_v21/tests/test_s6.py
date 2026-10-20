import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
import uuid
from concurrent.futures import ThreadPoolExecutor

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.notifications import DeliveryUnknown, OutboxWorker
from kb_custody_v21.registry import Registry
from kb_custody_v21.runtime import Runtime, DEFAULTS
from kb_custody_v21.s6 import Client, Sender, PREFIX, Wire, receive, storage
from kb_custody_v21.staging import Staging, IntegrityError

PACKAGE = Path(__file__).resolve().parents[1]
WORKTREE = Path(subprocess.check_output(["git", "-C", str(PACKAGE), "rev-parse", "--show-toplevel"], text=True).strip())
CONTENT = b"KB V2.1 S6 synthetic offline fixture\n"


class FakeWire:
    def __init__(self, config):
        self.config = config
        self.calls = []
        self.fail_send = False
        self.mismatch = False
        self.content = CONTENT

    def request(self, method, path, body, token):
        self.calls.append((method, path, json.loads(body) if body else None))
        if "/auth/" in path:
            result = {"code": 0, "tenant_access_token": "TEST-SECRET-TOKEN"}
        elif "/resources/" in path:
            return 200, self.content
        elif method == "POST":
            if self.fail_send:
                raise OSError("Authorization: SECRET-MUST-NOT-BE-LOGGED")
            result = {"code": 0, "data": {"message_id": "om_synthetic_receipt"}}
        else:
            source = self.config["sources"][0]
            result = {"code": 0, "data": {"items": [{
                "message_id": source["message_id"], "chat_id": "oc_wrong" if self.mismatch else self.config["chat_id"],
                "sender": {"id": self.config["account_id"], "id_type": "open_id", "sender_type": "user"},
                "msg_type": "file", "body": {"content": json.dumps({"file_key": source["file_key"], "file_name": source["filename"]})}
            }]}}
        return 200, json.dumps(result).encode()


class S6Tests(unittest.TestCase):
    def setUp(self):
        run = "s6-offline-" + uuid.uuid4().hex
        self.runtime = storage(WORKTREE, run)
        self.registry = Registry(self.runtime)
        secret = self.runtime.root / "fixtures/fake-secret"
        secret.write_text("TEST-SECRET")
        secret.chmod(0o600)
        current = datetime.now(timezone.utc)
        self.config = {
            "schema": "s6-authorization/v1", "enabled": True, "user_confirmed": True,
            "authorization_reference": "SYNTHETIC TEST ONLY - not human platform authorization",
            "run_id": run, "app_id": "cli_test", "chat_id": "oc_test", "account_id": "ou_test",
            "confirmed_at": (current-timedelta(minutes=1)).isoformat(),
            "expires_at": (current+timedelta(hours=1)).isoformat(), "secret_path": str(secret),
            "isolation": {"strategy": "dedicated_app_no_production_subscription", "confirmed": True, "evidence": "fake test"},
            "sources": [{"message_id": "om_fixture", "file_key": "file_fixture",
                "filename": "kb-v21-s6-normal-test.txt", "synthetic": True, "size_bytes":len(CONTENT),
                "sha256":hashlib.sha256(CONTENT).hexdigest(), "md5":hashlib.md5(CONTENT).hexdigest()}],
            "limits": {"send":4,"download":2,"read":8,"auth":2}, "total_requests":16,"logical_notifications":2}
        self.wire = FakeWire(self.config)
        self.client = Client(self.registry, self.config, self.wire)

    def tearDown(self):
        snapshot = {t:self.registry.rows("SELECT * FROM " + t) for t in (
            "intake_custody","custody_events","notification_outbox","notification_attempts")}
        # Never export fake credential body either: only paths and call count.
        snapshot["offline_only"] = True
        snapshot["wire_calls"] = [{"method":m,"path":p} for m,p,b in self.wire.calls]
        (self.runtime.root/"test-output/s6-observed.json").write_text(json.dumps(snapshot,ensure_ascii=False,indent=2))
        self.assertNotIn("TEST-SECRET", json.dumps(snapshot))
        self.assertNotIn("SECRET-MUST-NOT-BE-LOGGED", json.dumps(snapshot))

    def item(self):
        receive(self.client,0)
        return self.registry.rows("SELECT * FROM notification_outbox")[0]

    def test_disabled_config_never_reads_secret_or_network(self):
        self.client.config["enabled"] = False
        with patch.object(Path,"read_text",side_effect=AssertionError("secret read")):
            with self.assertRaises(ContractError):
                self.client.authenticate()
        self.assertFalse(self.wire.calls)

    def test_each_authorization_prerequisite_fails_closed(self):
        for field,value in [("chat_id",None),("account_id",None),("app_id",None),
                            ("user_confirmed",False),("authorization_reference",None),
                            ("limits",{}),("sources",[]),("secret_path",None),("isolation",{})]:
            with self.subTest(field=field):
                c=copy.deepcopy(self.config);c[field]=value
                with self.assertRaises(ContractError):
                    Client(self.registry,c,self.wire).authenticate()
        self.assertFalse(self.wire.calls)

    def test_expired_authorization_never_calls(self):
        self.client.config["expires_at"]=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
        with self.assertRaises(ContractError):self.client.authenticate()
        self.assertFalse(self.wire.calls)

    def test_changed_config_rejected_after_restart(self):
        self.client.gate()
        c=copy.deepcopy(self.config);c["chat_id"]="oc_other"
        with self.assertRaises(ContractError):Client(Registry(self.runtime),c,self.wire).gate()

    def test_group_name_without_isolation_proof_rejected(self):
        self.client.config["isolation"].update(strategy="reviewed_group_guard",no_mentions=True)
        with self.assertRaises(ContractError):self.client.gate()
        self.assertFalse(self.wire.calls)

    def test_dev_cannot_enable_real_sender(self):
        for field,value in [("sender","real_feishu"),("real_feishu_sending",True)]:
            with self.assertRaises(ContractError):Runtime(WORKTREE,dict(DEFAULTS,**{field:value}))
        self.assertEqual(self.runtime.make_sender().__class__.__name__,"PersistentMockSender")

    def test_real_client_disallows_injected_wire(self):
        with self.assertRaises(ContractError):Client(self.registry,self.config,self.wire,real=True)

    def test_other_endpoints_and_delete_rejected(self):
        for method,path in [("DELETE","/open-apis/im/v1/messages/om_fixture"),
                            ("GET","https://example.com"),("GET","/open-apis/im/v1/messages/om_other?user_id_type=open_id")]:
            with self.assertRaises(ContractError):self.client.request("read",method,path)
        self.assertFalse(self.wire.calls)

    def test_download_requires_platform_identity_match(self):
        self.wire.mismatch=True
        with self.assertRaises(ContractError):receive(self.client,0)
        self.assertFalse(any("/resources/" in c[1] for c in self.wire.calls))

    def test_known_hash_mismatch_does_not_accept(self):
        self.wire.content=b"unexpected bytes"
        with self.assertRaises(IntegrityError):receive(self.client,0)
        self.assertNotEqual(self.registry.rows("SELECT * FROM intake_custody")[0]["source_staging_status"],"accepted")

    def test_real_shaped_receive_replay_is_download_independent(self):
        result=receive(self.client,0)
        before=self.registry.rows("SELECT * FROM intake_custody")
        count=len(self.wire.calls)
        with patch.object(self.wire,"request",side_effect=AssertionError("must not use platform")):
            client=Client(Registry(self.runtime),self.config,self.wire)
            receive(client,0)
            with Staging(client.registry).input_binding(result["entry_id"]):
                pass
        self.assertEqual(before,self.registry.rows("SELECT * FROM intake_custody"))
        self.assertEqual(count,len(self.wire.calls))

    def test_key_passes_unchanged_and_payload_is_frozen(self):
        item=self.item()
        worker=OutboxWorker(self.registry,Sender(self.client))
        worker.drain()
        send=[c for c in self.wire.calls if c[0]=="POST" and "/im/" in c[1]][0]
        self.assertEqual(send[2]["uuid"],item["provider_request_key"])
        self.assertEqual(send[2]["receive_id"],self.config["chat_id"])
        self.assertTrue(json.loads(send[2]["content"])["text"].startswith(PREFIX))
        self.assertEqual(self.registry.rows("SELECT * FROM notification_outbox")[0]["delivery_state"],"delivered")
        with self.assertRaises(ContractError):
            Sender(self.client).send(dict(item,payload="changed"),"attempt_fake")

    def test_direct_send_cannot_bypass_frozen_target(self):
        with self.assertRaises(ContractError):
            self.client.request("send","POST","/open-apis/im/v1/messages?receive_id_type=chat_id",
                                {"receive_id":"oc_other","msg_type":"text","uuid":"x"})
        self.assertFalse(self.wire.calls)

    def test_unknown_does_not_retry_or_log_credential(self):
        self.item();self.wire.fail_send=True
        worker=OutboxWorker(self.registry,Sender(self.client))
        worker.drain();count=len(self.wire.calls)
        OutboxWorker(Registry(self.runtime),Sender(Client(self.registry,self.config,self.wire))).drain()
        self.assertEqual(count,len(self.wire.calls))
        self.assertEqual(self.registry.rows("SELECT * FROM notification_outbox")[0]["delivery_state"],"delivery_unknown")
        self.assertEqual(self.registry.rows("SELECT * FROM intake_custody")[0]["source_staging_status"],"accepted")

    def test_same_key_direct_repeat_remains_blocked(self):
        item=self.item();sender=Sender(self.client)
        sender.send(item,"attempt_one")
        count=len(self.wire.calls)
        with self.assertRaises(DeliveryUnknown):sender.send(item,"attempt_two")
        self.assertEqual(len(self.wire.calls),count)

    def test_j_fault_after_provider_before_commit_keeps_unknown(self):
        self.item()
        def hook(point):
            if point=="after_provider_call":raise SystemExit(73)
        with self.assertRaises(SystemExit):
            OutboxWorker(self.registry,Sender(self.client),hook=hook).drain()
        with self.registry.transaction() as db:
            db.execute("UPDATE notification_outbox SET lease_until=0")
        client=Client(Registry(self.runtime),self.config,self.wire)
        count=len(self.wire.calls)
        OutboxWorker(client.registry,Sender(client)).drain()
        self.assertEqual(len(self.wire.calls),count)
        self.assertEqual(self.registry.rows("SELECT * FROM notification_outbox")[0]["delivery_state"],"delivery_unknown")

    def test_persistent_budget_survives_restart_and_concurrency(self):
        self.client.gate()
        def reserve(i):
            try:
                Client(Registry(self.runtime),self.config,self.wire).reserve("download","fixture",{"n":i})
                return True
            except ContractError:return False
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(sum(pool.map(reserve,range(6))),2)
        with self.assertRaises(ContractError):self.client.reserve("download","fixture",{})

    def test_status_has_no_sender_or_transport_side_effect(self):
        self.item()
        before=len(self.wire.calls)
        self.registry.rows("SELECT * FROM intake_custody")
        self.assertEqual(len(self.wire.calls),before)

    def test_cli_disabled_preflight_is_network_free(self):
        cfg=self.runtime.root/"fixtures/disabled.json"
        cfg.write_text((PACKAGE/"s6.example.json").read_text())
        result=subprocess.run([sys.executable,str(PACKAGE/"s6_harness.py"),"preflight","--config",str(cfg)],
                              capture_output=True,text=True)
        self.assertEqual(result.returncode,2)
        self.assertIn("BLOCKED",result.stderr)

    def test_wire_never_retries_or_follows_redirects(self):
        with patch("kb_custody_v21.s6.http.client.HTTPSConnection") as constructor:
            connection=constructor.return_value
            response=connection.getresponse.return_value
            response.status=302;response.read.return_value=b"redirect"
            self.assertEqual(Wire().request("GET","/allowed",None,None),(302,b"redirect"))
            self.assertEqual(connection.request.call_count,1)
            connection.request.side_effect=OSError("injected connection loss")
            with self.assertRaises(OSError):Wire().request("GET","/allowed",None,None)
            self.assertEqual(connection.request.call_count,2)
            self.assertEqual(connection.close.call_count,2)

    def test_non_success_api_response_stays_unknown_without_retry(self):
        self.item()
        with patch.object(self.wire,"request",return_value=(429,b'{"code":99991400}')) as call:
            worker=OutboxWorker(self.registry,Sender(self.client))
            worker.drain();worker.drain()
            self.assertEqual(call.call_count,1)
        self.assertEqual(self.registry.rows("SELECT * FROM notification_outbox")[0]["delivery_state"],"delivery_unknown")

    def test_total_budget_includes_all_request_categories(self):
        self.client.gate()
        for kind,count in (("auth",2),("read",8),("download",2),("send",4)):
            for i in range(count):
                self.client.reserve(kind,"offline-only",{"provider_request_key":str(i)})
        with self.assertRaises(ContractError):self.client.reserve("read","offline-only",{})
        self.assertEqual(len(self.registry.rows("SELECT * FROM custody_events WHERE event_type='s6_http_started'")),16)

    def test_new_provider_key_fits_cli_limit_and_old_key_never_rekeyed(self):
        item = self.item()
        self.assertLessEqual(len(item['provider_request_key']),50)
        original = item['provider_request_key']
        item['provider_request_key'] = 'custody-' + 'a'*64
        count = len(self.wire.calls)
        with self.assertRaisesRegex(ContractError,'never rewrite/re-key'):
            Sender(self.client).send(item,'synthetic-attempt')
        self.assertEqual(len(self.wire.calls),count)
        self.assertEqual(self.registry.rows('SELECT provider_request_key FROM notification_outbox')[0]['provider_request_key'],original)
