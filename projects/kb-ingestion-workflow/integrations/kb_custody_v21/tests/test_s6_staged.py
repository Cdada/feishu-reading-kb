import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
import uuid
from concurrent.futures import ThreadPoolExecutor

from kb_custody_v21.contracts import ContractError, stable_key
from kb_custody_v21.registry import Registry
from kb_custody_v21.s6 import receive, Sender
from kb_custody_v21.s6_staged import StagedClient, FIXTURE_NAMES, OPERATIONS
from kb_custody_v21.notifications import DeliveryUnknown
from kb_custody_v21.s6 import storage
from s6_fake_cli import FakeWire

PACKAGE = Path(__file__).resolve().parents[1]
WORKTREE = Path(subprocess.check_output(["git", "-C", str(PACKAGE), "rev-parse", "--show-toplevel"], text=True).strip())


class StagedTests(unittest.TestCase):
    def setUp(self):
        self.run_id = "s6-chain-" + uuid.uuid4().hex
        self.runtime = storage(WORKTREE, self.run_id)
        self.registry = Registry(self.runtime)
        self.directory = self.runtime.root / "fixtures"
        self.secret = self.directory / "fake-secret"
        self.secret.write_text("OFFLINE-SECRET")
        self.secret.chmod(0o600)
        current = datetime.now(timezone.utc)
        self.config = {
            "schema": "s6-authorization/v2", "enabled": True, "user_confirmed": True,
            "authorization_reference": "OFFLINE-ONLY-" + self.run_id,
            "run_id": self.run_id, "app_id": "cli_offline", "chat_id": "oc_offline", "account_id": "ou_offline",
            "confirmed_at": (current-timedelta(minutes=1)).isoformat(),
            "expires_at": (current+timedelta(hours=1)).isoformat(), "secret_path": str(self.secret),
            "isolation": {"strategy": "dedicated_app_no_production_subscription", "confirmed": True, "evidence": "OFFLINE ONLY"},
            "fixtures": [], "sources": [], "permitted_operations": list(OPERATIONS),
            "discovery_max_seconds":600, "discovery_page_size":20,
            "limits": {"send":4, "download":2, "read":8, "auth":4}, "total_requests":18, "logical_notifications":2}
        self.platform = {"items": []}
        for i, name in enumerate(FIXTURE_NAMES):
            content = ("Offline chain synthetic fixture " + str(i) + "\n").encode()
            (self.directory / name).write_bytes(content)
            self.config["fixtures"].append({"filename":name,"synthetic":True,"size_bytes":len(content),
                "sha256":hashlib.sha256(content).hexdigest(),"md5":hashlib.md5(content).hexdigest()})
            self.platform["items"].append({"message_id":"om_offline_"+str(i),"chat_id":"oc_offline",
                "sender":{"id":"ou_offline","id_type":"open_id","sender_type":"user"},"msg_type":"file",
                "create_time":str(int(time.time())*1000),
                "body":{"content":json.dumps({"file_key":"file_offline_"+str(i),"file_name":name})}})
        self.config_path = self.directory / "authorization.json"
        self.config_path.write_text(json.dumps(self.config))
        self.save_platform()
        self.commands = []
        self.code_hashes = {name:hashlib.sha256((PACKAGE/name).read_bytes()).hexdigest() for name in (
            "s6_harness.py", "src/kb_custody_v21/s6.py", "src/kb_custody_v21/s6_staged.py",
            "tests/s6_fake_cli.py", "tests/test_s6_staged.py")}

    def save_platform(self):
        (self.directory / "platform.json").write_text(json.dumps(self.platform))

    def client(self, operation="preflight", config=None):
        cfg = config or self.config
        return StagedClient(self.registry, cfg, FakeWire(cfg), operation=operation)

    def budget(self):
        return self.client().budget()

    def command(self, operation, *extra, expected=0):
        command = [sys.executable, str(PACKAGE / "tests/s6_fake_cli.py"), operation, "--config", str(self.config_path), *extra]
        before = hashlib.sha256(self.config_path.read_bytes()).hexdigest()
        start = datetime.now(timezone.utc).isoformat()
        platform_hash = hashlib.sha256((self.directory/"platform.json").read_bytes()).hexdigest()
        result = subprocess.run(command, cwd=WORKTREE, capture_output=True, text=True)
        self.commands.append({"command": command,"cwd":str(WORKTREE),"started_at":start,
            "ended_at":datetime.now(timezone.utc).isoformat(),"exit_code":result.returncode,
            "stdout":result.stdout,"stderr":result.stderr,"budget_after":self.budget(),
            "fake_platform_sha256":platform_hash,
            "authorization_sha256_before":before,"authorization_sha256_after":hashlib.sha256(self.config_path.read_bytes()).hexdigest()})
        self.assertEqual(result.returncode, expected, result.stderr)
        self.assertEqual(self.commands[-1]["authorization_sha256_after"], before)
        return result

    def upload_window(self):
        # Test fixture time only: real CLI requires a completed upload window.
        start = int(time.time())
        for message in self.platform["items"]:
            message["create_time"] = str(start*1000)
        self.save_platform()
        while int(time.time()) <= start:
            time.sleep(0.05)
        return start, start+1

    def prepare(self, freeze=True):
        self.command("verify-target")
        start, end = self.upload_window()
        result = self.command("bind-attachments", "--window-start", str(start), "--window-end", str(end))
        digest = json.loads(result.stdout)["binding_sha256"]
        if freeze:
            self.command("freeze-inputs", "--binding-sha256", digest)
        return digest

    def tearDown(self):
        # Export only non-secret evidence; fake transport also omits auth bodies.
        evidence = {"offline_only":True,"experiment_run_id":self.run_id,"commands":self.commands,
            "tested_code_sha256":self.code_hashes,"output_directory":str(self.runtime.root/"test-output"),
            "budget":self.budget(),"config":{k:v for k,v in self.config.items() if k!="secret_path"},
            "events":self.registry.rows("SELECT * FROM custody_events"),
            "intakes":self.registry.rows("SELECT * FROM intake_custody"),
            "outbox":self.registry.rows("SELECT * FROM notification_outbox")}
        wire = self.directory / "wire.jsonl"
        evidence["wire_calls"] = [json.loads(line) for line in wire.read_text().splitlines()] if wire.exists() else []
        text = json.dumps(evidence,ensure_ascii=False,indent=2)
        self.assertNotIn("OFFLINE-SECRET",text)
        (self.runtime.root / "test-output/staged-chain.json").write_text(text)

    def test_full_command_chain_j_restart_preserves_unknown_and_budget(self):
        self.assertEqual(self.config["sources"], [])
        self.command("preflight")
        self.prepare()
        self.assertFalse(self.registry.rows("SELECT * FROM intake_custody"))
        self.assertFalse(self.registry.rows("SELECT * FROM notification_outbox"))
        self.assertEqual(self.budget(), {"auth":2,"read":5,"download":0,"send":0})
        self.command("ingest", "--source-index", "0")
        self.assertEqual(self.budget(), {"auth":3,"read":6,"download":1,"send":1})
        self.command("ingest", "--source-index", "1", "--crash-after-send", expected=73)
        self.assertEqual(self.budget(), {"auth":4,"read":7,"download":2,"send":2})
        self.command("dispatch", "--expire-offline-lease", expected=2)
        self.command("dispatch", expected=2)
        self.command("status")
        self.assertEqual(self.budget(), {"auth":4,"read":7,"download":2,"send":2})
        rows = self.registry.rows("SELECT * FROM notification_outbox ORDER BY created_at")
        self.assertEqual([r["delivery_state"] for r in rows], ["delivered", "delivery_unknown"])
        self.assertTrue(all(len(r["provider_request_key"])==48 for r in rows))
        self.assertEqual([r["source_staging_status"] for r in self.registry.rows("SELECT * FROM intake_custody")], ["accepted","accepted"])

    def test_real_rejects_obsolete_group_guard_before_credentials_or_network(self):
        """R02: matching code hashes and no-mention policy cannot prove isolation."""
        config = copy.deepcopy(self.config)
        proofs = []
        for name in ("gateway.mjs", "document-reference.mjs", "intake-flow.mjs", "index.js"):
            path = self.directory / name
            path.write_bytes(b"// synthetic historical isolation proof\n")
            proofs.append({"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        config["isolation"].update(strategy="reviewed_group_guard", no_mentions=True, file_hashes=proofs)
        client = StagedClient(self.registry, config, real=True, operation="verify-target")
        with patch.object(Path, "read_text", side_effect=AssertionError("credential read forbidden")) as read_secret, \
                patch("kb_custody_v21.s6.Wire.request", side_effect=AssertionError("HTTP forbidden")) as wire:
            with self.assertRaisesRegex(ContractError, "historical group guard cannot authorize real S6"):
                client.authenticate()
            read_secret.assert_not_called()
            wire.assert_not_called()
        self.assertFalse(self.registry.rows("SELECT * FROM custody_events"))

    def test_original_two_auth_budget_stops_before_first_intake(self):
        self.config["limits"]["auth"] = 2
        self.config["total_requests"] = 16
        self.config_path.write_text(json.dumps(self.config))
        self.prepare()
        self.command("ingest", expected=2)
        self.assertEqual(self.budget(), {"auth":2,"read":5,"download":0,"send":0})
        self.assertFalse(self.registry.rows("SELECT * FROM intake_custody"))

    def test_readonly_mode_rejects_custody_send_download_and_discovery(self):
        client = self.client("verify-target")
        client.verify_target()
        for operation in (lambda: receive(client,0),
                          lambda: client.request("download","GET","/open-apis/im/v1/messages/om_offline_0/resources/file_offline_0?type=file"),
                          lambda: client.request("send","POST","/open-apis/im/v1/messages?receive_id_type=chat_id"),
                          lambda: client.bind_attachments(1,2)):
            with self.assertRaises(ContractError): operation()
        self.assertEqual(self.budget(), {"auth":1,"read":4,"download":0,"send":0})
        self.assertFalse(self.registry.rows("SELECT * FROM intake_custody"))

    def test_scoped_readonly_authorization_cannot_start_later_phase(self):
        self.config["permitted_operations"] = ["verify-target"]
        self.config["limits"] = {"auth":1,"read":4,"download":0,"send":0}
        self.config["total_requests"] = 5
        self.client("verify-target").verify_target()
        with self.assertRaises(ContractError): self.client("bind-attachments").gate()
        with self.assertRaises(ContractError): self.client("ingest").gate()

    def test_missing_authorization_and_fake_real_flag_stop_without_calls(self):
        for field,value in (("user_confirmed",False),("app_id",None),("chat_id",None),("account_id",None),("limits",{}),("isolation",{})):
            cfg=copy.deepcopy(self.config);cfg[field]=value
            with self.assertRaises(ContractError): self.client("verify-target",cfg).verify_target()
        self.command("verify-target","--real",expected=2)
        self.assertEqual(sum(self.budget().values()),0)

    def test_target_query_failure_blocks_binding_without_download(self):
        self.platform["chat_mode"]="p2p";self.save_platform()
        self.command("verify-target",expected=2)
        self.command("bind-attachments","--window-start","1","--window-end","2",expected=2)
        self.assertEqual(self.budget(),{"auth":1,"read":2,"download":0,"send":0})

    def test_binding_ambiguous_or_wrong_identity_does_not_freeze(self):
        self.command("verify-target")
        start,end=self.upload_window()
        self.platform["items"][1]["sender"]["id"]="ou_other";self.save_platform()
        self.command("bind-attachments","--window-start",str(start),"--window-end",str(end),expected=2)
        self.assertIsNone(self.client().event("s6:attachments_bound"))
        self.command("freeze-inputs","--binding-sha256","bad",expected=2)
        self.assertFalse(self.registry.rows("SELECT * FROM intake_custody"))

    def test_binding_never_paginates_or_widens_window(self):
        self.command("verify-target");start,end=self.upload_window()
        self.platform["has_more"]=True;self.save_platform()
        self.command("bind-attachments","--window-start",str(start),"--window-end",str(end),expected=2)
        before=self.budget()
        self.command("bind-attachments","--window-start",str(start-1),"--window-end",str(end),expected=2)
        self.assertEqual(self.budget(),before)

    def test_freeze_digest_config_attachment_and_payload_immutable(self):
        digest=self.prepare(freeze=False)
        self.command("ingest",expected=2)
        self.command("freeze-inputs","--binding-sha256","bad",expected=2)
        self.command("freeze-inputs","--binding-sha256",digest)
        self.command("bind-attachments","--window-start","1","--window-end","2",expected=2)
        for field,value in (("chat_id","oc_other"),("account_id","ou_other"),("app_id","cli_other"),
                            ("sources",[{"message_id":"om_changed","file_key":"file_changed"}])):
            cfg=copy.deepcopy(self.config);cfg[field]=value
            with self.assertRaises(ContractError): self.client("ingest",cfg).gate()
        self.command("ingest")
        item=self.registry.rows("SELECT * FROM notification_outbox")[0]
        with self.assertRaises(ContractError): Sender(self.client("dispatch")).send(dict(item,payload="changed"),"attempt_test")
        with self.assertRaises(DeliveryUnknown): Sender(self.client("dispatch")).send(item,"attempt_test")
        self.assertEqual(self.budget()["send"],1)

    def test_same_approval_cannot_switch_run_or_rebuild_registry(self):
        self.client().gate()
        cfg=copy.deepcopy(self.config);cfg["run_id"]="s6-other-"+uuid.uuid4().hex
        r=Registry(storage(WORKTREE,cfg["run_id"]))
        with self.assertRaises(ContractError): StagedClient(r,cfg,FakeWire(cfg)).gate()
        # Move only this test's DB; Registry initialization cannot reset approval.
        self.runtime.db.rename(self.runtime.db.with_suffix(".saved"))
        self.registry=Registry(self.runtime)
        with self.assertRaises(ContractError): self.client().gate()

    def test_request_witness_mismatch_blocks_before_auth(self):
        c=self.client();c.gate()
        with c.anchor.open("a") as out: out.write('{"http_attempt":"http_interrupted"}\n')
        with self.assertRaises(ContractError): self.client("verify-target").verify_target()
        self.assertEqual(sum(self.budget().values()),0)

    def test_fake_authorization_cannot_be_reused_for_real_transport(self):
        self.client().gate()
        with self.assertRaises(ContractError): StagedClient(self.registry,self.config,real=True).gate()

    def test_concurrent_reservations_share_authorization_budget(self):
        self.client().gate()
        def reserve(i):
            try:
                self.client().reserve("download","OFFLINE-RESERVATION",{"i":i})
                return True
            except ContractError:
                return False
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(sum(pool.map(reserve,range(6))),2)
        self.client().gate()
        self.assertEqual(self.budget()["download"],2)

    def test_missing_anchor_cannot_reset_existing_authorization(self):
        c=self.client();c.gate()
        c.anchor.rename(c.anchor.with_suffix(".test-saved"))
        with self.assertRaises(ContractError): self.client("verify-target").verify_target()
        self.assertEqual(sum(self.budget().values()),0)

    def test_reordered_json_does_not_change_authorization_identity(self):
        self.client().gate()
        cfg=json.loads(json.dumps(self.config,sort_keys=True))
        self.client(config=cfg).gate()

    def test_duplicate_filename_or_changed_resource_after_freeze_stops(self):
        self.prepare()
        self.platform["items"][0]["body"]["content"]=json.dumps({"file_key":"file_replaced","file_name":FIXTURE_NAMES[0]})
        self.save_platform()
        self.command("ingest",expected=2)
        self.assertEqual(self.budget()["download"],0)
        self.assertFalse(self.registry.rows("SELECT * FROM notification_outbox"))

    def test_duplicate_upload_is_ambiguous_not_arbitrary_first_choice(self):
        self.command("verify-target");start,end=self.upload_window()
        duplicate=copy.deepcopy(self.platform["items"][0]);duplicate["message_id"]="om_duplicate"
        self.platform["items"].append(duplicate);self.save_platform()
        self.command("bind-attachments","--window-start",str(start),"--window-end",str(end),expected=2)
        self.assertIsNone(self.client().event("s6:attachments_bound"))

    def test_extra_bot_in_dedicated_group_blocks_binding(self):
        self.platform['bot_count'] = '2'
        self.save_platform()
        with self.assertRaisesRegex(ContractError,'exactly the approved user'):
            self.client('verify-target').verify_target()
        self.assertFalse(self.registry.rows("SELECT 1 FROM custody_events WHERE dedupe_key='s6:target_verified'"))
