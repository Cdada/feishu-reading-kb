from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import copy
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from kb_custody_v21.contracts import (ContractError, ENTRY_SCHEMA, SUCCESS, GROUP_SUCCESS,
    FAILURE, new_id, read_version, safe_filename, validate_entry, validate_id)
from kb_custody_v21.runtime import Runtime, DEFAULTS
from kb_custody_v21.registry import Registry
from kb_custody_v21.staging import Staging, IntegrityError, write_all
from kb_custody_v21.intake import FeishuMockIntake, MockAttachment, normalized_message_contract
from kb_custody_v21.notifications import PersistentMockSender, OutboxWorker

PACKAGE = Path(__file__).resolve().parents[1]
WORKTREE = Path(subprocess.check_output(["git", "-C", str(PACKAGE), "rev-parse", "--show-toplevel"], text=True).strip())
BASE = WORKTREE / ".dev-runtime/kb-v21"
CONTENT = b"%PDF-1.4\n% synthetic evidence only\n%%EOF\n"


class CustodyTests(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp(prefix=self._testMethodName + "-", dir=BASE / "test-output"))
        self.runtime = Runtime(WORKTREE, DEFAULTS, root)
        self.registry = Registry(self.runtime)
        self.staging = Staging(self.registry)
        self.sender = PersistentMockSender(self.runtime)
        self.worker = OutboxWorker(self.registry, self.sender)
        self.adapter = FeishuMockIntake(self.registry, self.staging, self.worker)

    def tearDown(self):
        # Keep synthetic DB + on-disk originals and structured observations for audit.
        tables = ("assets", "staging_files", "staging_receipts", "intake_entries", "source_groups",
                  "source_group_memberships", "custody_events", "notification_outbox", "notification_attempts", "asset_leases", "staging_attempts", "intakes", "intake_custody")
        snapshot = {t: self.registry.rows(f"SELECT * FROM {t}") for t in tables}
        snapshot["mock_deliveries"] = self.sender.records("deliveries")
        snapshot["mock_calls"] = self.sender.records("calls")
        (self.runtime.root / "test-output/observed-state.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2))

    def contract(self, key="1", **kwargs):
        value = {"schema_version": ENTRY_SCHEMA, "intake_key": "mock-intake", "entry_key": key,
                 "received_filename": "测试.pdf", "lineage": {"chat_id": "mock-chat"}}
        value.update(kwargs)
        return value

    def receive(self, content=CONTENT, key="1", **kwargs):
        return self.adapter.receive(self.contract(key, **kwargs), MockAttachment(content))

    def rows(self, table):
        return self.registry.rows(f"SELECT * FROM {table}")

    def events(self, kind):
        return self.registry.rows("SELECT * FROM custody_events WHERE event_type=?", (kind,))

    def original(self, eid):
        return self.runtime.root / self.registry.entry(eid)["relative_path"]

    def assertAccepted(self, result, content=CONTENT, sent=1):
        eid = result["entry_id"]
        row = self.registry.entry(eid)
        self.assertEqual(self.registry.receipt(eid)["source_staging_status"], "accepted")
        self.assertEqual(self.registry.receipt(eid)["feishu_attachment_retention"], "cleanup_eligible")
        self.assertEqual(self.original(eid).read_bytes(), content)
        self.assertEqual(row["size_bytes"], len(content))
        self.assertEqual(row["sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual(row["md5"], hashlib.md5(content).hexdigest())
        events = [e for e in self.events("source_staging_accepted") if e["aggregate_id"] == row["acceptance_id"]]
        self.assertEqual(len(events), 1)
        evidence = json.loads(self.rows("staging_receipts")[0]["validation_json"])
        self.assertEqual(evidence["expected"], evidence["observed"])
        self.assertTrue(evidence["durable"])
        self.assertEqual(len(self.sender.records("deliveries")), sent)
        with self.staging.input_binding(eid) as binding:
            self.assertEqual(binding["staging_asset_id"], row["asset_id"])
            self.assertEqual(binding["acceptance_id"], row["acceptance_id"])
            self.assertIn("source-staging", binding["path"])
            self.assertEqual(binding["stream"].read(), content)

    def assertNotAccepted(self):
        self.assertEqual(len(self.events("source_staging_accepted")), 0)
        self.assertTrue(all(r["source_staging_status"] != "accepted" for r in self.rows("intake_custody")))
        self.assertTrue(all(r["feishu_attachment_retention"] == "retain_required" for r in self.rows("intake_custody")))
        self.assertFalse(any(n["kind"] == "success" for n in self.rows("notification_outbox")))

    def test_A_normal_automatic_notification(self):
        """A：原件/三项哈希/receipt/event/outbox/attempt/文案/输入绑定同时成立。"""
        result = self.receive()
        self.assertAccepted(result)
        self.assertEqual(self.rows("notification_outbox")[0]["delivery_state"], "delivered")
        self.assertEqual(len(self.rows("notification_attempts")), 1)
        self.assertEqual(self.sender.records("deliveries")[0]["payload"], SUCCESS)
        self.assertEqual(self.sender.records("calls")[0]["provider_request_key"], self.rows("notification_outbox")[0]["provider_request_key"])

    def test_B_real_process_restart_no_duplicate(self):
        """B：新进程恢复并重复 dispatch，两次均无第二条消息或通知。"""
        result = self.receive()
        for _ in range(2):
            run = subprocess.run([sys.executable, str(PACKAGE / "dev.py"), "dispatch", "--runtime", str(self.runtime.root)], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(json.loads(run.stdout)["processed"], 0)
        self.assertAccepted(result)
        self.assertEqual(len(self.rows("notification_outbox")), 1)
        self.assertEqual(len(self.rows("notification_attempts")), 1)

    def test_C_copy_failure(self):
        """C：复制异常只产生 failed 和保留附件提醒，不产生 accepted。"""
        def fail(fd, data):
            raise OSError("injected copy failure")
        adapter = FeishuMockIntake(self.registry, Staging(self.registry, writer=fail), self.worker)
        with self.assertRaises(OSError):
            adapter.receive(self.contract(), MockAttachment(CONTENT))
        self.assertNotAccepted()
        self.assertEqual(self.rows("staging_receipts")[0]["status"], "failed")
        self.assertEqual(len(self.events("source_staging_failed")), 1)
        self.assertEqual(self.sender.records("deliveries")[0]["payload"], FAILURE)

    def test_D_group_partial_failure_then_repair(self):
        """D/L：必需附件部分失败不发整组成功，修复只发一条汇总。"""
        group = self.registry.create_group()
        one = self.contract("1", group={**group, "required": True})
        two = self.contract("2", group={**group, "required": True})
        self.registry.register(one)
        self.registry.register(two)
        self.registry.seal_group(group["source_group_id"], 1)
        self.adapter.receive(one, MockAttachment(CONTENT))
        self.assertEqual(len(self.sender.records("deliveries")), 0)
        with self.assertRaises(OSError):
            self.adapter.receive(two, MockAttachment(CONTENT, False))
        self.assertFalse(self.events("source_group_staging_accepted"))
        self.assertEqual(self.registry.group_retention(group["source_group_id"], 1), "retain_required")
        self.adapter.receive(two, MockAttachment(CONTENT))
        self.assertEqual([x["payload"] for x in self.sender.records("deliveries")], [FAILURE, GROUP_SUCCESS])
        self.assertEqual(len(self.events("source_group_staging_accepted")), 1)
        self.assertEqual(len(self.events("source_staging_accepted")), 2)
        self.assertEqual(self.registry.group_retention(group["source_group_id"], 1), "cleanup_eligible")

    def test_E_F_no_inbound_or_feishu_needed(self):
        """E/F 模拟：删除本次合成 inbound、禁用下载后，重放/原文读取/lease 正常。"""
        attachment = MockAttachment(CONTENT)
        result = self.adapter.receive(self.contract(), attachment)
        attachment.available = False
        for file in (self.runtime.root / "inbound").iterdir():
            file.unlink()
        recovered = FeishuMockIntake(Registry(self.runtime), Staging(self.registry), self.worker).receive(self.contract(), attachment)
        self.assertEqual(recovered["entry_id"], result["entry_id"])
        self.assertEqual(attachment.download_calls, 1)
        self.assertAccepted(recovered)
        self.assertTrue(all(l["released_at"] for l in self.rows("asset_leases")))

    def test_G_expected_size_sha_md5_mismatch(self):
        """G：三个接收基准分别不符均拒绝 accepted，保留失败证据。"""
        for index, expected in enumerate(({"size_bytes": 1}, {"sha256": "0" * 64}, {"md5": "0" * 32})):
            with self.subTest(expected=expected), self.assertRaises(IntegrityError):
                self.receive(key=str(index), expected=expected)
        self.assertNotAccepted()
        self.assertEqual(len(self.events("source_staging_failed")), 3)
        self.assertTrue(all(r["sha256"] is None for r in self.rows("assets")))

    def crash(self, point):
        run = subprocess.run([sys.executable, str(PACKAGE / "tests/crash_worker.py"), str(WORKTREE), str(self.runtime.root), point], capture_output=True, text=True)
        self.assertEqual(run.returncode, 73, run.stderr)

    def test_H_durable_then_hard_process_exit(self):
        """H：真实子进程 os._exit；文件已持久保存，重启按旧基准复核且只保留一份原件。"""
        self.crash("after_durable")
        row = self.rows("intake_entries")[0]
        path = self.original(row["entry_id"])
        inode = path.stat().st_ino
        self.assertNotAccepted()
        result = Staging(Registry(self.runtime)).ingest(row["entry_id"])
        self.worker.drain()
        self.assertAccepted(result, b"%PDF synthetic crash fixture")
        self.assertEqual(path.stat().st_ino, inode)
        self.assertEqual(len(self.rows("assets")), 1)

    def test_H_sqlite_transaction_rollback(self):
        """H：accepted 事务内注入 SQLite failure，receipt/event/outbox 全部回滚。"""
        def hook(point):
            if point == "before_accept_commit":
                raise sqlite3.OperationalError("injected commit failure")
        adapter = FeishuMockIntake(self.registry, Staging(self.registry, hook=hook))
        with self.assertRaises(sqlite3.OperationalError):
            adapter.receive(self.contract(), MockAttachment(CONTENT))
        self.assertNotAccepted()
        eid = self.rows("intake_entries")[0]["entry_id"]
        self.assertEqual(self.original(eid).read_bytes(), CONTENT)
        self.staging.ingest(eid)
        self.worker.drain()
        self.assertAccepted(self.registry.receipt(eid))

    def test_H_hard_exit_inside_transaction(self):
        """H：事务中硬退出由 SQLite 恢复，accepted 事件不会单独残留。"""
        self.crash("before_accept_commit")
        self.assertNotAccepted()
        eid = self.rows("intake_entries")[0]["entry_id"]
        self.staging.ingest(eid)
        self.worker.drain()
        self.assertAccepted(self.registry.receipt(eid), b"%PDF synthetic crash fixture")

    def test_baseline_committed_partial_recovery(self):
        """接收基准已提交但未提升最终文件：重启复核 partial 后恢复，无重新下载。"""
        self.crash("after_baseline_commit")
        eid = self.rows("intake_entries")[0]["entry_id"]
        self.assertFalse(self.original(eid).exists())
        self.staging.ingest(eid)
        self.worker.drain()
        self.assertAccepted(self.registry.receipt(eid), b"%PDF synthetic crash fixture")

    def test_I_committed_before_sender_process_crash(self):
        """I：accepted/outbox 已提交后硬退出，新进程发送一次。"""
        self.crash("after_accept_commit")
        self.assertEqual(len(self.events("source_staging_accepted")), 1)
        self.assertEqual(self.rows("notification_outbox")[0]["delivery_state"], "pending")
        self.assertEqual(len(self.sender.records("deliveries")), 0)
        run = subprocess.run([sys.executable, str(PACKAGE / "dev.py"), "dispatch", "--runtime", str(self.runtime.root)], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(len(self.sender.records("deliveries")), 1)
        self.assertEqual(len(self.rows("notification_attempts")), 1)

    def test_J_server_delivered_response_lost(self):
        """J：模拟服务端送达但客户端超时；同 request key 查回，只发生一次 send。"""
        self.sender.mode = "success_lost_response"
        result = self.receive()
        key = self.rows("notification_outbox")[0]["provider_request_key"]
        self.assertEqual(self.rows("notification_outbox")[0]["delivery_state"], "delivery_unknown")
        self.worker.drain()
        self.assertAccepted(result)
        self.assertEqual([r["operation"] for r in self.sender.records("calls")], ["send", "lookup"])
        self.assertEqual({r["provider_request_key"] for r in self.rows("notification_attempts")}, {key})

    def test_J_hard_exit_after_provider_before_receipt(self):
        """J：发送后本地记录前进程退出；lease 到期先查回，不重复发送。"""
        FeishuMockIntake(self.registry, self.staging).receive(self.contract(), MockAttachment(CONTENT))
        self.crash("after_provider_call")
        self.assertEqual(self.rows("notification_outbox")[0]["delivery_state"], "sending")
        self.assertEqual(len(self.sender.records("deliveries")), 1)
        time.sleep(0.07)
        OutboxWorker(Registry(self.runtime), PersistentMockSender(self.runtime)).drain()
        self.assertEqual(self.rows("notification_outbox")[0]["delivery_state"], "delivered")
        self.assertEqual([r["operation"] for r in self.sender.records("calls")], ["send", "lookup"])

    def test_unknown_not_blindly_resent(self):
        """结果不明且查回仍不明：维持 unknown，不以换 key 重发。"""
        self.sender.mode = "timeout"
        self.sender.lookup_authoritative = False
        self.receive()
        for _ in range(3):
            self.worker.drain()
        self.assertEqual(self.rows("notification_outbox")[0]["delivery_state"], "delivery_unknown")
        self.assertEqual(len([r for r in self.sender.records("calls") if r["operation"] == "send"]), 1)
        self.assertEqual(len(self.sender.records("deliveries")), 0)

    def test_timeout_confirmed_unsent_can_retry_same_key(self):
        """超时后明确查证未发送才重试，重试沿用同一持久请求键。"""
        self.sender.mode = "timeout"
        self.receive()
        self.worker.drain()
        self.assertEqual(self.rows("notification_outbox")[0]["delivery_state"], "failed_retryable")
        self.sender.mode = "success"
        self.worker.drain()
        self.assertEqual(len(self.sender.records("deliveries")), 1)
        self.assertEqual(len({r["provider_request_key"] for r in self.rows("notification_attempts")}), 1)

    def test_K_concurrent_entries(self):
        """K：8 个并发接管仅分配一个 asset/receipt、下载一次、发一条成功通知。"""
        attachment = MockAttachment(CONTENT)
        def receive(_):
            registry = Registry(self.runtime)
            return FeishuMockIntake(registry, Staging(registry)).receive(self.contract(), attachment)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(receive, range(8)))
        self.worker.drain()
        self.assertEqual(len({r["entry_id"] for r in results}), 1)
        self.assertEqual(len(self.rows("assets")), 1)
        self.assertEqual(len(self.rows("staging_receipts")), 1)
        self.assertEqual(attachment.download_calls, 1)
        self.assertAccepted(results[0])

    def test_K_concurrent_senders(self):
        """K：并发 sender 在 SQLite 中抢占 lease，每个逻辑通知仅发送一次。"""
        FeishuMockIntake(self.registry, self.staging).receive(self.contract(), MockAttachment(CONTENT))
        def send(_):
            return OutboxWorker(Registry(self.runtime), PersistentMockSender(self.runtime, mode="delay", delay=0.03)).drain()
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(send, range(8)))
        self.assertEqual(len(self.rows("notification_attempts")), 1)
        self.assertEqual(len(self.sender.records("deliveries")), 1)
        self.assertEqual(len(self.sender.records("calls")), 1)

    def test_K_expired_lease_fences_late_worker(self):
        """K：旧 worker 返回晚于新 lease，不得覆盖新状态；provider key 仍去重。"""
        FeishuMockIntake(self.registry, self.staging).receive(self.contract(), MockAttachment(CONTENT))
        slow = OutboxWorker(self.registry, PersistentMockSender(self.runtime, delay=0.15), lease_seconds=0.02)
        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(slow.drain)
            time.sleep(0.05)
            self.worker.drain()  # authoritative mock lookup says not yet sent
            self.worker.drain()  # same key; provider can deduplicate late send
            future.result()
        self.assertEqual(len(self.sender.records("deliveries")), 1)
        self.assertEqual(self.rows("notification_outbox")[0]["delivery_state"], "delivered")
        self.assertEqual(len({r["provider_request_key"] for r in self.sender.records("calls")}), 1)

    def test_L_cancel_unsent_failed_notice(self):
        """L：未发送的失败提醒在成功接管时取消，成功仅发一次。"""
        adapter = FeishuMockIntake(self.registry, self.staging)
        with self.assertRaises(OSError):
            adapter.receive(self.contract(), MockAttachment(CONTENT, False))
        adapter.receive(self.contract(), MockAttachment(CONTENT))
        self.worker.drain()
        states = {r["kind"]: r["delivery_state"] for r in self.rows("notification_outbox")}
        self.assertEqual(states, {"failure": "cancelled", "success": "delivered"})
        self.assertEqual([r["payload"] for r in self.sender.records("deliveries")], [SUCCESS])

    def test_L_keep_delivered_failed_history(self):
        """L：失败已通知时保留历史，修复后另发唯一成功通知。"""
        with self.assertRaises(OSError):
            self.adapter.receive(self.contract(), MockAttachment(CONTENT, False))
        self.receive()
        self.receive()
        self.assertEqual([r["payload"] for r in self.sender.records("deliveries")], [FAILURE, SUCCESS])
        self.assertEqual(len(self.events("source_staging_failed")), 1)
        self.assertEqual(len(self.events("source_staging_accepted")), 1)

    def test_failure_episode_coalesces(self):
        """同一未解决失败反复恢复不刷屏，attempt 更新而 failure episode 不变。"""
        for _ in range(3):
            with self.assertRaises(OSError):
                self.adapter.receive(self.contract(), MockAttachment(CONTENT, False))
        self.assertEqual(len(self.events("source_staging_failed")), 1)
        self.assertEqual(len(self.sender.records("deliveries")), 1)

    def test_M_unsealed_and_optional_members(self):
        """M：未 sealed 不宣布整组完成；sealed 后可选附件不阻塞必需组。"""
        group = self.registry.create_group()
        required = self.contract("1", group={**group, "required": True})
        optional = self.contract("2", group={**group, "required": False})
        optional_entry = self.registry.register(optional)
        self.adapter.receive(required, MockAttachment(CONTENT))
        self.assertFalse(self.events("source_group_staging_accepted"))
        self.assertEqual(len(self.sender.records("deliveries")), 0)
        self.registry.seal_group(group["source_group_id"], 1)
        self.worker.drain()
        self.assertEqual([r["payload"] for r in self.sender.records("deliveries")], [GROUP_SUCCESS])
        optional_receipt = self.registry.receipt(optional_entry["entry_id"])
        self.assertEqual(optional_receipt["source_staging_status"], "pending")
        self.assertEqual(optional_receipt["feishu_attachment_retention"], "retain_required")
        with self.assertRaises(sqlite3.IntegrityError):
            self.registry.register(self.contract("3", group={**group, "required": True}))
        self.assertEqual(len(self.rows("assets")), 2)
        new_group = self.registry.create_group(group["source_group_id"], 2)
        self.registry.register(self.contract("new-revision", group={**new_group, "required": True}))
        self.assertEqual(self.registry.group_retention(group["source_group_id"], 2), "retain_required")

    def required_complete_optional_pending(self):
        group = self.registry.create_group()
        optional = self.contract("optional", group={**group, "required": False})
        entry = self.registry.register(optional)
        required = self.receive(key="required", group={**group, "required": True})
        self.registry.seal_group(group["source_group_id"], 1)
        self.worker.drain()
        return group, required, optional, entry

    def test_optional_group_member_does_not_inherit_group_cleanup_promise(self):
        """P0：required 承诺只覆盖必需成员，pending optional 保留附件。"""
        group, required, _, entry = self.required_complete_optional_pending()
        self.assertEqual(self.registry.receipt(required["entry_id"])["feishu_attachment_retention"], "cleanup_eligible")
        receipt = self.registry.receipt(entry["entry_id"])
        self.assertEqual(receipt["source_staging_status"], "pending")
        self.assertEqual(receipt["custody_health"], "missing")
        self.assertEqual(receipt["feishu_attachment_retention"], "retain_required")
        self.assertEqual(len(self.events("source_group_staging_accepted")), 1)
        self.assertEqual([r["payload"] for r in self.sender.records("deliveries")], [GROUP_SUCCESS])

    def test_optional_group_member_gets_entry_cleanup_only_after_acceptance(self):
        """P0：optional 独立成功；响应丢失、重启查回仍只发一次且请求键不变。"""
        _, _, contract, entry = self.required_complete_optional_pending()
        group_events = self.events("source_group_staging_accepted")
        self.sender.mode = "success_lost_response"
        self.adapter.receive(contract, MockAttachment(CONTENT))
        receipt = self.registry.receipt(entry["entry_id"])
        self.assertEqual(receipt["feishu_attachment_retention"], "cleanup_eligible")
        notices = [n for n in self.rows("notification_outbox") if n["scope_type"] == "single_source"]
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["scope_id"], receipt["acceptance_id"])
        self.assertEqual(notices[0]["kind"], "success")
        self.assertEqual(notices[0]["delivery_state"], "delivery_unknown")
        registry = Registry(self.runtime)
        sender = PersistentMockSender(self.runtime)
        worker = OutboxWorker(registry, sender)
        worker.drain()
        FeishuMockIntake(registry, Staging(registry), worker).receive(contract, MockAttachment(CONTENT))
        worker.drain()
        self.assertEqual(self.events("source_group_staging_accepted"), group_events)
        calls = [c for c in sender.records("calls") if c["provider_request_key"] == notices[0]["provider_request_key"]]
        self.assertEqual([c["operation"] for c in calls], ["send", "lookup"])
        self.assertEqual([d["payload"] for d in sender.records("deliveries")], [GROUP_SUCCESS, SUCCESS])

    def test_optional_failure_repair_keeps_required_group_history(self):
        """P0：optional 连续失败按自身 episode 聚合，修复不改写组承诺。"""
        group, required, contract, entry = self.required_complete_optional_pending()
        original_events = self.events("source_group_staging_accepted")
        for _ in range(3):
            with self.assertRaises(OSError):
                self.adapter.receive(contract, MockAttachment(CONTENT, False))
        receipt = self.registry.receipt(entry["entry_id"])
        self.assertEqual(receipt["source_staging_status"], "failed")
        self.assertEqual(receipt["feishu_attachment_retention"], "retain_required")
        self.assertEqual(self.registry.receipt(required["entry_id"])["feishu_attachment_retention"], "cleanup_eligible")
        self.assertTrue(self.registry.group_state(group["source_group_id"], 1)["historical_cleanup_promise"])
        failures = [n for n in self.rows("notification_outbox") if n["kind"] == "failure"]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["scope_type"], "single_source")
        self.assertEqual(failures[0]["scope_id"], receipt["acceptance_id"])
        self.assertEqual(len(self.events("source_staging_failed")), 1)
        self.adapter.receive(contract, MockAttachment(CONTENT))
        self.adapter.receive(contract, MockAttachment(CONTENT))
        self.assertEqual(self.events("source_group_staging_accepted"), original_events)
        self.assertEqual([d["payload"] for d in self.sender.records("deliveries")], [GROUP_SUCCESS, FAILURE, SUCCESS])
        self.assertEqual(self.registry.receipt(entry["entry_id"])["feishu_attachment_retention"], "cleanup_eligible")
        attempts = [a for a in self.rows("staging_attempts") if a["entry_id"] == entry["entry_id"]]
        self.assertEqual([a["outcome"] for a in attempts], ["failed", "failed", "failed", "succeeded"])
        self.assertEqual([n for n in self.rows("notification_outbox") if n["kind"] == "failure"], failures)

    def test_optional_repair_cancels_stale_entry_failure(self):
        """P0：尚未投递的 optional 失败提醒由 entry 成功取消。"""
        _, _, contract, entry = self.required_complete_optional_pending()
        adapter = FeishuMockIntake(self.registry, self.staging)
        with self.assertRaises(OSError):
            adapter.receive(contract, MockAttachment(CONTENT, False))
        self.assertEqual(len([n for n in self.rows("notification_outbox") if n["kind"] == "failure"]), 1)
        adapter.receive(contract, MockAttachment(CONTENT))
        self.worker.drain()
        notices = [n for n in self.rows("notification_outbox") if n["scope_id"] == entry["acceptance_id"]]
        self.assertEqual({n["kind"]: n["delivery_state"] for n in notices}, {"failure": "cancelled", "success": "delivered"})
        self.assertEqual([d["payload"] for d in self.sender.records("deliveries")], [GROUP_SUCCESS, SUCCESS])

    def test_existing_dev_view_refresh_preserves_custody_facts(self):
        """已有 revision-2 DEV 库重开时更新派生 view，全部持久事实原样保留。"""
        _, _, _, entry = self.required_complete_optional_pending()
        with self.registry.transaction() as db:
            sql = db.execute("SELECT sql FROM sqlite_master WHERE type='view' AND name='intake_custody'").fetchone()[0]
            db.execute("DROP VIEW intake_custody")
            db.execute(sql.replace("m.entry_id IS NOT NULL AND m.required=1", "m.entry_id IS NOT NULL"))
        self.assertEqual(self.registry.receipt(entry["entry_id"])["feishu_attachment_retention"], "cleanup_eligible")
        tables = [r["name"] for r in self.registry.rows("SELECT name FROM sqlite_master WHERE type='table'")]
        before = {t: self.rows(t) for t in tables}
        reopened = Registry(self.runtime)
        self.assertEqual(reopened.receipt(entry["entry_id"])["feishu_attachment_retention"], "retain_required")
        self.assertEqual({t: self.rows(t) for t in tables}, before)

    def test_empty_group_cannot_seal(self):
        """空的必需附件集合禁止 sealed，避免空集合误报全部成功。"""
        group = self.registry.create_group()
        with self.assertRaises(ContractError):
            self.registry.seal_group(group["source_group_id"], 1)
        self.assertFalse(self.rows("notification_outbox"))

    def test_N_retryable_channel_failure(self):
        """N：通知失败不撤销 accepted；修复通道后同 key 重试。"""
        self.sender.mode = "retryable_failure"
        result = self.receive()
        self.assertAccepted(result, sent=0)
        self.assertEqual(self.rows("notification_outbox")[0]["delivery_state"], "failed_retryable")
        self.sender.mode = "success"
        self.worker.drain()
        self.assertAccepted(result)
        self.assertEqual(len({r["provider_request_key"] for r in self.rows("notification_attempts")}), 1)

    def test_N_definite_channel_failure_recorded(self):
        """N：明确不可重试失败保留原因/attempt，取消通知但不撤销接管。"""
        self.sender.mode = "definite_failure"
        result = self.receive()
        self.assertAccepted(result, sent=0)
        outbox = self.rows("notification_outbox")[0]
        self.assertEqual(outbox["delivery_state"], "cancelled")
        self.assertTrue(outbox["last_error"])
        self.assertEqual(len(self.events("notification_delivery")), 1)

    def test_V_cache_cleanup_cannot_touch_staging(self):
        """V：模拟清理 DEV Cache；Staging 原件、哈希和 accepted 不受影响。"""
        result = self.receive()
        cache = self.runtime.root / "cache/cached-image.bin"
        cache.write_bytes(b"re-downloadable")
        for file in (self.runtime.root / "cache").iterdir():
            file.unlink()
        self.assertAccepted(result)
        self.assertEqual(self.rows("staging_files")[0]["cleanup_hold"], 1)

    def test_X_accepted_corruption_preserves_history(self):
        """X：已接管文件篡改变为 suspect；历史 accepted/event 不变，下游读取被阻止。"""
        result = self.receive()
        self.original(result["entry_id"]).write_bytes(b"corrupted")
        for _ in range(2):
            with self.assertRaises(IntegrityError):
                with self.staging.input_binding(result["entry_id"]):
                    self.fail("corrupt bytes must not reach downstream")
        self.assertEqual(self.rows("staging_files")[0]["custody_health"], "suspect")
        self.assertEqual(self.rows("staging_receipts")[0]["status"], "accepted")
        self.assertEqual(len(self.events("source_staging_accepted")), 1)
        self.assertEqual(len(self.events("source_staging_integrity_alert")), 1)
        self.assertEqual(len(self.sender.records("deliveries")), 1)

    def test_X_missing_original(self):
        """X：接管后原件缺失记录 missing，不能靠 inbound 悄悄补写或改变 accepted 历史。"""
        result = self.receive()
        self.original(result["entry_id"]).unlink()
        with self.assertRaises(IntegrityError):
            self.staging.ingest(result["entry_id"])
        self.assertEqual(self.rows("staging_files")[0]["custody_health"], "missing")
        self.assertEqual(self.rows("staging_receipts")[0]["status"], "accepted")
        self.assertEqual(len(self.events("source_staging_integrity_alert")), 1)

    def test_empty_file(self):
        """空字节文件可完成证据接管；是否可转换留给 S7，不混同完整性。"""
        self.assertAccepted(self.receive(b""), b"")

    def test_size_boundary(self):
        """配置边界恰好允许，超一字节拒绝；拒绝项无 accepted。"""
        self.runtime.config["max_asset_bytes"] = len(CONTENT)
        self.assertAccepted(self.receive())
        with self.assertRaises(IntegrityError):
            self.receive(CONTENT + b"x", key="too-large")
        self.assertEqual(len(self.events("source_staging_accepted")), 1)

    def test_short_writes_are_completed(self):
        """短写循环写完全部字节，不把一次 write 返回当成完整写入。"""
        short = lambda fd, data: os.write(fd, data[:3])
        result = FeishuMockIntake(self.registry, Staging(self.registry, writer=short), self.worker).receive(self.contract(), MockAttachment(CONTENT))
        self.assertAccepted(result)

    def test_zero_write_rejected(self):
        """write 返回零时失败，不死循环，也不产生正式原件。"""
        with self.assertRaises(OSError):
            FeishuMockIntake(self.registry, Staging(self.registry, writer=lambda fd, data: 0), self.worker).receive(self.contract(), MockAttachment(CONTENT))
        self.assertNotAccepted()

    def test_permission_and_disk_full(self):
        """分别注入 EACCES/ENOSPC；账本失败，原始接管基准保持未完成。"""
        for code in (errno.EACCES, errno.ENOSPC):
            def fail(fd, data):
                raise OSError(code, os.strerror(code))
            with self.subTest(code=code), self.assertRaises(OSError):
                FeishuMockIntake(self.registry, Staging(self.registry, writer=fail), self.worker).receive(self.contract(str(code)), MockAttachment(CONTENT))
        self.assertNotAccepted()
        self.assertEqual(len(self.events("source_staging_failed")), 2)

    def test_midstream_error_then_restart(self):
        """读入中途异常留下 partial，重试从受控入口重写 partial 后成功。"""
        row = self.registry.register(self.contract())
        class Interrupted(io.BytesIO):
            def read(self, n):
                if self.tell():
                    raise OSError("transport interrupted")
                return super().read(4)
        with self.assertRaises(OSError):
            self.staging.ingest(row["entry_id"], lambda: Interrupted(CONTENT))
        self.assertNotAccepted()
        self.assertFalse(self.original(row["entry_id"]).exists())
        result = self.receive()
        self.assertAccepted(result)

    def test_same_filename_different_bytes_separate_assets(self):
        """同名异内容各有 asset/path/hash，不覆盖另一原件。"""
        a, b = self.receive(), self.receive(b"another", key="2")
        self.assertNotEqual(a["source_staging_asset_id"], b["source_staging_asset_id"])
        self.assertEqual(self.original(a["entry_id"]).read_bytes(), CONTENT)
        self.assertEqual(self.original(b["entry_id"]).read_bytes(), b"another")
        self.assertEqual(len(self.rows("assets")), 2)

    def test_replay_contract_conflict(self):
        """相同入口键改变接收基准/文件名时拒绝重放，不偷换原资产。"""
        self.receive(expected={"size_bytes": len(CONTENT)})
        with self.assertRaises(ContractError):
            self.receive(expected={"size_bytes": 1})
        self.assertEqual(len(self.rows("assets")), 1)

    def test_symlink_final_not_followed(self):
        """最终 staging 文件为 symlink 时拒绝；链接目标不修改。"""
        row = self.registry.register(self.contract())
        target = self.runtime.root / "fixtures/target.txt"
        target.write_bytes(b"protected")
        path = self.original(row["entry_id"])
        path.parent.mkdir()
        path.symlink_to(target)
        with self.assertRaises(IntegrityError):
            self.staging.ingest(row["entry_id"], lambda: io.BytesIO(CONTENT))
        self.assertEqual(target.read_bytes(), b"protected")
        self.assertNotAccepted()

    def test_symlink_asset_directory_not_followed(self):
        """asset 目录为 symlink 时 O_NOFOLLOW 拒绝，不在链接目标写文件。"""
        row = self.registry.register(self.contract())
        target = self.runtime.root / "fixtures/elsewhere"
        target.mkdir()
        self.original(row["entry_id"]).parent.symlink_to(target)
        with self.assertRaises(OSError):
            self.staging.ingest(row["entry_id"], lambda: io.BytesIO(CONTENT))
        self.assertEqual(list(target.iterdir()), [])
        self.assertNotAccepted()

    def test_partial_symlink_rejected(self):
        """partial 为 symlink 不跟随、不删除目标、不接受。"""
        row = self.registry.register(self.contract())
        path = self.original(row["entry_id"])
        path.parent.mkdir()
        target = self.runtime.root / "fixtures/target"
        target.write_bytes(b"untouched")
        (path.parent / ".original.partial").symlink_to(target)
        with self.assertRaises(IntegrityError):
            self.staging.ingest(row["entry_id"], lambda: io.BytesIO(CONTENT))
        self.assertEqual(target.read_bytes(), b"untouched")
        self.assertNotAccepted()

    def test_path_traversal_and_unsafe_filename(self):
        """../ 输入路径被拒绝；原始文件名保留但磁盘名安全化，不作为路径解释。"""
        with self.assertRaises(ContractError):
            self.runtime.checked(self.runtime.root / "fixtures/../../outside")
        name = "../../危险\\名称:\x00" + "图" * 200 + ".pdf"
        result = self.receive(received_filename=name)
        self.assertAccepted(result)
        row = self.registry.entry(result["entry_id"])
        self.assertEqual(row["received_filename"], name)
        self.assertNotIn("/", row["safe_filename"])
        self.assertNotIn("..", row["safe_filename"])
        self.assertLessEqual(len(row["safe_filename"].encode()), 160)

    def test_existing_unaccounted_final_not_overwritten(self):
        """没有接收基准的已有最终文件不得被重新基准化或覆盖。"""
        row = self.registry.register(self.contract())
        path = self.original(row["entry_id"])
        path.parent.mkdir()
        path.write_bytes(b"unaccounted")
        with self.assertRaises(IntegrityError):
            self.staging.ingest(row["entry_id"], lambda: io.BytesIO(CONTENT))
        self.assertEqual(path.read_bytes(), b"unaccounted")
        self.assertNotAccepted()

    def test_final_reread_detects_tamper(self):
        """基准已提交后篡改 partial，重新读取时发现不符，不提交 accepted。"""
        row = self.registry.register(self.contract())
        def hook(point):
            if point == "after_baseline_commit":
                (self.original(row["entry_id"]).parent / ".original.partial").write_bytes(b"tampered")
        with self.assertRaises(IntegrityError):
            Staging(self.registry, hook=hook).ingest(row["entry_id"], lambda: io.BytesIO(CONTENT))
        self.assertNotAccepted()
        self.assertEqual(self.rows("assets")[0]["sha256"], hashlib.sha256(CONTENT).hexdigest())

    def test_status_read_has_no_notification_side_effect(self):
        """重复状态查询只读，不生成新事件、attempt 或发送调用。"""
        result = FeishuMockIntake(self.registry, self.staging).receive(self.contract(), MockAttachment(CONTENT))
        for _ in range(10):
            self.registry.receipt(result["entry_id"])
        self.assertEqual(len(self.rows("notification_outbox")), 1)
        self.assertFalse(self.rows("notification_attempts"))
        self.assertFalse(self.sender.records("calls"))

    def test_persistent_provider_deduplicates_direct_calls(self):
        """mock 服务端直接重复调用也按持久 request key 去重，调用记录仍完整。"""
        self.receive()
        item = self.rows("notification_outbox")[0]
        message = self.sender.send(item, new_id("attempt"))
        self.assertEqual(message, item["provider_message_id"])
        self.assertEqual(len(self.sender.records("deliveries")), 1)
        self.assertEqual(len(self.sender.records("calls")), 2)

    def test_lineage_null_and_version_boundaries(self):
        """ID 与 lineage 分开；未知 null；旧 V1 只读分流，绝不补 accepted。"""
        result = self.receive()
        row = self.registry.entry(result["entry_id"])
        lineage = json.loads(row["lineage_json"])
        self.assertIsNone(lineage["file_key"])
        self.assertIsNone(lineage["message_id"])
        self.assertEqual(lineage["intake_id"], row["intake_id"])
        for field, kind in (("asset_id", "asset"), ("entry_id", "entry"), ("intake_id", "intake"), ("acceptance_id", "acceptance"), ("attempt_id", "attempt")):
            validate_id(row[field], kind)
        legacy = {"title": "V1 document", "source": "legacy"}
        before = copy.deepcopy(legacy)
        self.assertEqual(read_version(legacy), "v1_read_only")
        self.assertEqual(legacy, before)
        with self.assertRaises(ContractError):
            read_version({"schema_version": "registry/v999"})
        with self.assertRaises(ContractError):
            self.registry.register({**self.contract("legacy"), "schema_version": "registry/v1"})
        with self.assertRaises(ContractError):
            validate_entry({**self.contract(), "accepted": True})

    def test_events_and_accepted_identity_immutable(self):
        """SQL 约束禁止改历史事件、accepted 状态或已建立的字节身份。"""
        self.receive()
        for statement in ("DELETE FROM custody_events", "UPDATE custody_events SET event_type='fake'",
                          "UPDATE staging_receipts SET status='failed'", "UPDATE assets SET sha256='bad'"):
            with self.subTest(statement=statement), self.assertRaises(sqlite3.IntegrityError):
                with self.registry.transaction() as db:
                    db.execute(statement)
        self.assertEqual(len(self.events("source_staging_accepted")), 1)

    def test_dev_gate_rejects_production_and_real_capabilities(self):
        """S0：真实发送/百度/清理/生产路径/错误 branch/不完整配置均拒绝。"""
        for field, value in (("sender", "feishu"), ("real_feishu_sending", True), ("baidu_enabled", True),
                             ("destructive_cleanup", True), ("runtime_relative", "/production"),
                             ("runtime_relative", ".dev-runtime/../../v1"), ("branch", "main"), ("listen", True)):
            with self.subTest(field=field), self.assertRaises(ContractError):
                Runtime(WORKTREE, {**DEFAULTS, field: value})
        with self.assertRaises(ContractError):
            Runtime(WORKTREE.parent / "feishu-bridge-v1", DEFAULTS)
        with self.assertRaises(ContractError):
            Runtime(WORKTREE, {})
        with self.assertRaises(ContractError):
            Runtime(WORKTREE, DEFAULTS, WORKTREE.parent / "feishu-bridge-v1")

    def test_dev_gate_rejects_symlink_registry(self):
        """S0：registry 路径被链接替换时，安全门拒绝打开该 DB。"""
        root = Path(tempfile.mkdtemp(prefix="symlink-gate-", dir=BASE / "test-output"))
        runtime = Runtime(WORKTREE, DEFAULTS, root)
        runtime.initialize()
        runtime.db.symlink_to(self.runtime.db)
        with self.assertRaises(ContractError):
            Registry(runtime)

    def test_registry_refuses_v1_database_without_migration(self):
        """S1：DEV 内放入仿 V1 数据库也拒绝接管，不创建 V2 表。"""
        root = Path(tempfile.mkdtemp(prefix="legacy-gate-", dir=BASE / "test-output"))
        runtime = Runtime(WORKTREE, DEFAULTS, root)
        runtime.initialize()
        with sqlite3.connect(runtime.db) as db:
            db.execute("CREATE TABLE legacy_tasks(id TEXT)")
        with self.assertRaises(ContractError):
            Registry(runtime)
        with sqlite3.connect(runtime.db) as db:
            tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        self.assertEqual(tables, ["legacy_tasks"])

    def test_no_pending_input_binding(self):
        """S4：未 accepted 的文件不能产生下游 staging 输入绑定。"""
        row = self.registry.register(self.contract())
        with self.assertRaises(IntegrityError):
            with self.staging.input_binding(row["entry_id"]):
                self.fail("unaccepted input must not be read")

    def test_reject_unverified_acceptance_transaction(self):
        """S3：缺少持久接收基准/重新读取凭据，调用 accepted 事务也会被拒绝。"""
        row = self.registry.register(self.contract())
        with self.assertRaises(ContractError):
            self.registry.accept(row["entry_id"], {"durable": True})
        self.assertNotAccepted()

    def test_unknown_failed_notice_resolved_before_retry(self):
        """失败通知超时后材料已修复：查证未发送时取消旧提醒，只保留成功通知。"""
        self.sender.mode = "timeout"
        with self.assertRaises(OSError):
            self.adapter.receive(self.contract(), MockAttachment(CONTENT, False))
        self.sender.mode = "success"
        self.receive()
        self.worker.drain()
        self.assertEqual([r["payload"] for r in self.sender.records("deliveries")], [SUCCESS])
        self.assertEqual({r["kind"]: r["delivery_state"] for r in self.rows("notification_outbox")},
                         {"failure": "cancelled", "success": "delivered"})

    def test_concurrent_independent_processes(self):
        """K：4 个真实进程同时处理同一入口，共用 SQLite/flock 仍只有一个资产和通知。"""
        command = [sys.executable, str(PACKAGE / "dev.py"), "demo", "--runtime", str(self.runtime.root)]
        children = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(4)]
        replies = []
        outputs = [child.communicate(timeout=20) for child in children]
        for child, (out, err) in zip(children, outputs):
            self.assertEqual(child.returncode, 0, err)
            replies.append(json.loads(out))
        self.assertEqual(len({r["entry_id"] for r in replies}), 1)
        self.assertEqual(len(self.rows("assets")), 1)
        self.assertEqual(len(self.events("source_staging_accepted")), 1)
        self.assertEqual(len(self.rows("notification_outbox")), 1)
        self.assertEqual(len(self.sender.records("deliveries")), 1)

    def test_S4_existing_gateway_envelope_lineage_and_replay(self):
        """S4：沿用现有网关字段；引用消息和资源消息分存，重放不按随机路径识别。"""
        message = {"messageId": "mock-reply-id", "resourceMessageId": "mock-original-message",
                   "chatId": "mock-chat", "messageAt": "2026-09-30T00:00:00Z",
                   "resources": [{"type": "file", "fileKey": "mock-file-key", "fileName": "实际接收名.pdf", "sizeBytes": len(CONTENT)}]}
        attachment = MockAttachment(CONTENT)
        one = self.adapter.receive_message(message, 0, attachment)
        two = self.adapter.receive_message(message, 0, attachment)
        self.assertEqual(one, two)
        self.assertEqual(attachment.download_calls, 1)
        lineage = json.loads(self.registry.entry(one["entry_id"])["lineage_json"])
        self.assertEqual(lineage["message_id"], "mock-reply-id")
        self.assertEqual(lineage["resource_message_id"], "mock-original-message")
        self.assertEqual(lineage["file_key"], "mock-file-key")
        self.assertIsNone(lineage["attachment_id"])
        self.assertAccepted(one)
        self.assertNotEqual(one["source_staging_asset_id"], "mock-file-key")

    def test_S4_ambiguous_envelope_fails_early(self):
        """S4：缺稳定消息身份或接收文件名时拒绝，不猜来源或创建半份资产。"""
        for message in ({}, {"messageId": "x", "chatId": "y", "resources": [{"type": "file"}]}):
            with self.subTest(message=message), self.assertRaises(ContractError):
                normalized_message_contract(message, 0)
        self.assertFalse(self.rows("assets"))

    def test_group_rejects_cross_intake_membership(self):
        """审查 P0：第一成员绑定 intake；跨 intake 加入和更改归属被 SQL 拒绝且整体回滚。"""
        group = self.registry.create_group()
        first = self.registry.register(self.contract("1", group={**group, "required": True}))
        with self.assertRaises(sqlite3.IntegrityError):
            self.registry.register(self.contract("2", intake_key="different-intake", group={**group, "required": True}))
        self.assertEqual(len(self.rows("intakes")), 1)
        self.assertEqual(len(self.rows("intake_entries")), 1)
        self.assertEqual(self.rows("source_groups")[0]["intake_id"], first["intake_id"])
        self.registry.seal_group(group["source_group_id"], 1)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.registry.transaction() as db:
                db.execute("UPDATE source_groups SET intake_id=NULL")

    def unsealed_accepted_group(self):
        group = self.registry.create_group()
        contracts = [self.contract(str(i), group={**group, "required": True}) for i in range(2)]
        receipts = [self.adapter.receive(contract, MockAttachment(CONTENT)) for contract in contracts]
        return group, receipts

    def test_group_damage_before_first_promise(self):
        """审查 A：全部 receipt accepted 但未承诺时损坏，seal 不发 group success，保留风险事件。"""
        group, receipts = self.unsealed_accepted_group()
        eid = receipts[0]["entry_id"]
        self.original(eid).write_bytes(b"damaged before promise")
        with self.assertRaises(IntegrityError):
            with self.staging.input_binding(eid):
                self.fail("unsafe input")
        self.assertFalse(self.registry.seal_group(group["source_group_id"], 1))
        self.worker.drain()
        state = self.registry.group_state(group["source_group_id"], 1)
        self.assertFalse(state["currently_safe_for_first_promise"])
        self.assertFalse(state["historical_cleanup_promise"])
        self.assertEqual(state["feishu_attachment_retention"], "retain_required")
        self.assertEqual(self.registry.receipt(eid)["feishu_attachment_retention"], "retain_required")
        self.assertEqual(len(self.events("source_staging_accepted")), 2)
        self.assertEqual(len(self.events("source_staging_integrity_alert")), 1)
        self.assertFalse(self.events("source_group_staging_accepted"))
        self.assertFalse(self.sender.records("deliveries"))

    def test_group_missing_before_first_promise(self):
        """审查 A：首次承诺之前原件 missing，同样不取得组 cleanup 资格。"""
        group, receipts = self.unsealed_accepted_group()
        eid = receipts[0]["entry_id"]
        self.original(eid).unlink()
        with self.assertRaises(IntegrityError):
            self.staging.ingest(eid)
        self.registry.seal_group(group["source_group_id"], 1)
        self.worker.drain()
        self.assertEqual(self.registry.group_retention(group["source_group_id"], 1), "retain_required")
        self.assertEqual(len(self.events("source_staging_accepted")), 2)
        self.assertFalse(self.sender.records("deliveries"))

    def test_group_recovery_after_pre_promise_damage_auto_emits_success(self):
        """P1：真实损坏后恢复原字节，reverify 自动形成首次组承诺，无第二次 seal。"""
        group, receipts = self.unsealed_accepted_group()
        eid = receipts[0]["entry_id"]
        original = self.original(eid).read_bytes()
        receipt_rows = self.rows("staging_receipts")
        accepted_events = self.events("source_staging_accepted")
        self.original(eid).write_bytes(b"damaged before promise")
        with self.assertRaises(IntegrityError):
            self.staging.ingest(eid)
        self.assertEqual(self.registry.receipt(eid)["custody_health"], "suspect")
        self.assertFalse(self.registry.seal_group(group["source_group_id"], 1))
        self.assertFalse(self.rows("notification_outbox"))
        self.original(eid).write_bytes(original)
        self.staging.ingest(eid)
        self.assertEqual(self.registry.group_state(group["source_group_id"], 1), {
            "currently_safe_for_first_promise": True, "historical_cleanup_promise": True,
            "feishu_attachment_retention": "cleanup_eligible"})
        self.assertEqual(len(self.events("source_group_staging_accepted")), 1)
        self.assertEqual(len(self.rows("notification_outbox")), 1)
        self.worker.drain()
        registry = Registry(self.runtime)
        staging = Staging(registry)
        for _ in range(3):
            staging.ingest(eid)
            OutboxWorker(registry, PersistentMockSender(self.runtime)).drain()
        self.assertEqual(self.rows("staging_receipts"), receipt_rows)
        self.assertEqual(self.events("source_staging_accepted"), accepted_events)
        self.assertEqual(len(self.events("source_group_staging_accepted")), 1)
        self.assertEqual([d["payload"] for d in self.sender.records("deliveries")], [GROUP_SUCCESS])
        # A later missing -> healthy cycle retains the existing promise and notice.
        self.original(eid).unlink()
        with self.assertRaises(IntegrityError):
            staging.ingest(eid)
        self.original(eid).write_bytes(original)
        staging.ingest(eid)
        self.worker.drain()
        self.assertEqual(len(self.events("source_group_staging_accepted")), 1)
        self.assertEqual(len(self.sender.records("deliveries")), 1)
        self.assertEqual(self.rows("staging_receipts"), receipt_rows)

    def test_group_missing_recovery_atomically_creates_first_promise(self):
        """P1：missing 恢复与首次承诺同事务；通知插入失败时健康和事件一起回滚。"""
        group, receipts = self.unsealed_accepted_group()
        eid = receipts[0]["entry_id"]
        self.original(eid).unlink()
        with self.assertRaises(IntegrityError):
            self.staging.ingest(eid)
        self.assertFalse(self.registry.seal_group(group["source_group_id"], 1))
        self.original(eid).write_bytes(CONTENT)
        with patch.object(self.registry, "notice", side_effect=sqlite3.OperationalError("injected group notice failure")):
            with self.assertRaisesRegex(sqlite3.OperationalError, "group notice failure"):
                self.staging.ingest(eid)
        self.assertEqual(self.registry.receipt(eid)["custody_health"], "missing")
        self.assertFalse(self.events("source_group_staging_accepted"))
        self.assertFalse(self.rows("notification_outbox"))
        self.staging.ingest(eid)
        self.worker.drain()
        self.assertTrue(self.registry.group_state(group["source_group_id"], 1)["historical_cleanup_promise"])
        self.assertEqual(len(self.events("source_group_staging_accepted")), 1)
        self.assertEqual(len(self.sender.records("deliveries")), 1)

    def test_group_damage_after_historical_promise(self):
        """审查 B：合法组事件形成后损坏，独立报警，不删除资格/accepted_at，不重复通知。"""
        group, receipts = self.unsealed_accepted_group()
        self.registry.seal_group(group["source_group_id"], 1)
        self.worker.drain()
        original_events = self.events("source_group_staging_accepted")
        original_receipts = self.rows("staging_receipts")
        eid = receipts[0]["entry_id"]
        self.original(eid).write_bytes(b"damaged later")
        with self.assertRaises(IntegrityError):
            self.staging.ingest(eid)
        self.registry.seal_group(group["source_group_id"], 1)
        self.worker.drain()
        self.assertEqual(self.registry.group_retention(group["source_group_id"], 1), "cleanup_eligible")
        self.assertFalse(self.registry.group_state(group["source_group_id"], 1)["currently_safe_for_first_promise"])
        self.assertEqual(self.events("source_group_staging_accepted"), original_events)
        self.assertEqual(self.rows("staging_receipts"), original_receipts)
        self.assertEqual(len(self.events("source_staging_integrity_alert")), 1)
        self.assertEqual(len(self.sender.records("deliveries")), 1)

    def test_group_legal_cleaned_fixture_preserves_cleanup_eligibility(self):
        """审查 C 状态夹具：云端验证/7 天/人工批准后合法 cleaned，不撤回历史飞书资格。"""
        group, receipts = self.unsealed_accepted_group()
        self.registry.seal_group(group["source_group_id"], 1)
        self.worker.drain()
        events, receipt_rows = self.events("source_group_staging_accepted"), self.rows("staging_receipts")
        fixture = {"fixture_only": True, "archive_status": "cloud_archived_verified",
                   "verification_level": "content_md5_verified", "elapsed_days": 7,
                   "user_confirmed": True, "staging_presence": "cleaned"}
        (self.runtime.root / "fixtures/legal-cleaned.json").write_text(json.dumps(fixture))
        # Physical deletion is limited to THIS synthetic fixture. No S13 API exists.
        for receipt in receipts:
            self.original(receipt["entry_id"]).unlink()
        # Minimal test-only representation of physical absence. The separate
        # fixture explains that it was authorized cleanup, not an integrity loss.
        # No production cloud/cleanup transition is introduced by this test.
        with self.registry.transaction() as db:
            db.execute("UPDATE staging_files SET custody_health='missing'")
        self.assertFalse(self.registry.group_state(group["source_group_id"], 1)["currently_safe_for_first_promise"])
        for _ in range(2):
            self.assertEqual(self.registry.group_retention(group["source_group_id"], 1), "cleanup_eligible")
            for receipt in receipts:
                self.assertEqual(self.registry.receipt(receipt["entry_id"])["feishu_attachment_retention"], "cleanup_eligible")
            self.worker.drain()
        self.assertEqual(self.events("source_group_staging_accepted"), events)
        self.assertEqual(self.rows("staging_receipts"), receipt_rows)
        self.assertEqual(len(self.sender.records("deliveries")), 1)
        self.assertFalse(self.events("source_staging_integrity_alert"))

    def test_staging_attempt_history_survives_multiple_retries(self):
        """审查 P1：失败三次再成功=4 个独立 attempt、1 receipt、1 accepted、1 聚合失败提醒。"""
        for _ in range(3):
            with self.assertRaises(OSError):
                self.adapter.receive(self.contract(), MockAttachment(CONTENT, False))
        finished = self.rows("staging_attempts")
        self.assertEqual(len(finished), 3)
        self.receive()
        attempts = self.registry.rows("SELECT * FROM staging_attempts ORDER BY started_at")
        self.assertEqual([r["outcome"] for r in attempts], ["failed"] * 3 + ["succeeded"])
        self.assertEqual(len({r["attempt_id"] for r in attempts}), 4)
        self.assertEqual(len({r["acceptance_id"] for r in attempts}), 1)
        self.assertTrue(all(r["started_at"] and r["finished_at"] and r["phase"] for r in attempts))
        self.assertTrue(all(r["error_code"] and r["error_summary"] for r in attempts[:3]))
        self.assertEqual(attempts[:3], finished)
        self.assertEqual(len(self.rows("staging_receipts")), 1)
        self.assertEqual(len(self.events("source_staging_accepted")), 1)
        self.assertEqual(len(self.events("source_staging_failed")), 1)
        self.assertEqual([r["payload"] for r in self.sender.records("deliveries")], [FAILURE, SUCCESS])
        for sql in ("UPDATE staging_attempts SET outcome='failed'", "DELETE FROM staging_attempts"):
            with self.assertRaises(sqlite3.IntegrityError):
                with self.registry.transaction() as db:
                    db.execute(sql)

    def test_interrupted_attempt_retained_on_recovery(self):
        """进程硬退出后原 attempt 保留为 interrupted；恢复另建 attempt 并成功。"""
        self.crash("after_durable")
        eid = self.rows("intake_entries")[0]["entry_id"]
        self.staging.ingest(eid)
        attempts = self.registry.rows("SELECT * FROM staging_attempts ORDER BY started_at")
        self.assertEqual([r["outcome"] for r in attempts], ["interrupted", "succeeded"])
        self.assertNotEqual(attempts[0]["attempt_id"], attempts[1]["attempt_id"])

    def test_notification_target_is_frozen_from_intake(self):
        """通知目标来自 intake chat；原消息消失/重启/重放后目标不变，禁止 sender 猜目标。"""
        c = self.contract(lineage={"chat_id": "frozen-chat", "message_id": "deleted-message"})
        FeishuMockIntake(self.registry, self.staging).receive(c, MockAttachment(CONTENT))
        item = self.rows("notification_outbox")[0]
        self.assertEqual(item["target_chat_id"], "frozen-chat")
        OutboxWorker(Registry(self.runtime), self.runtime.make_sender()).drain()
        self.adapter.receive(c, MockAttachment(CONTENT, False))
        self.assertEqual(self.sender.records("deliveries")[0]["target_chat_id"], "frozen-chat")
        self.assertEqual(self.rows("notification_outbox")[0]["provider_request_key"], item["provider_request_key"])
        with self.assertRaises(ContractError):
            self.registry.register(self.contract("another", lineage={"chat_id": "wrong-chat"}))
        for sql in ("UPDATE intakes SET notification_chat_id='wrong'", "UPDATE notification_outbox SET target_chat_id='wrong'"):
            with self.assertRaises(sqlite3.IntegrityError):
                with self.registry.transaction() as db:
                    db.execute(sql)

    def test_group_notification_has_single_intake_target(self):
        """同 intake 双 required 正常 sealed/accepted；group scope 包含 intake/group/revision，目标唯一。"""
        group, receipts = self.unsealed_accepted_group()
        self.registry.seal_group(group["source_group_id"], 1)
        self.worker.drain()
        iid = receipts[0]["intake_id"]
        item = self.rows("notification_outbox")[0]
        payload = json.loads(self.events("source_group_staging_accepted")[0]["payload_json"])
        self.assertEqual(item["scope_id"], f"{iid}:{group['source_group_id']}:1")
        self.assertEqual(item["intake_id"], iid)
        self.assertEqual(item["target_chat_id"], "mock-chat")
        self.assertEqual(payload["intake_id"], iid)
        self.assertEqual(payload["source_group_id"], group["source_group_id"])
        self.assertEqual(payload["membership_revision"], 1)
        self.assertEqual(len(payload["required_entries"]), 2)
        self.assertEqual(len(self.sender.records("deliveries")), 1)

    def test_missing_notification_target_is_not_invented(self):
        """未知 chat 保持 null，接管可完成但 worker 明确记录缺目标，不随意发送。"""
        result = self.receive(lineage={})
        self.assertEqual(result["source_staging_status"], "accepted")
        item = self.rows("notification_outbox")[0]
        self.assertIsNone(item["target_chat_id"])
        self.assertIn("missing frozen", item["last_error"])
        self.assertFalse(self.sender.records("deliveries"))

    def test_sender_protocol_seam_and_dev_factory(self):
        """核心只依赖 send/lookup；DEV factory 仍禁止真实 sender 配置。"""
        class Adapter:
            def send(inner, item, attempt):
                return self.sender.send(item, attempt)
            def lookup(inner, item, attempt):
                return self.sender.lookup(item, attempt)
        FeishuMockIntake(self.registry, self.staging).receive(self.contract(), MockAttachment(CONTENT))
        OutboxWorker(self.registry, Adapter()).drain()
        self.assertEqual(len(self.sender.records("deliveries")), 1)
        self.runtime.config["sender"] = "real_feishu"
        with self.assertRaises(ContractError):
            self.runtime.make_sender()

    def test_sqlite_unwritable_preserves_file_and_records_local_fault(self):
        """SQLite 完全 query_only：不 accepted/不成功通知，保留落地原件，仅写诊断日志。"""
        self.crash("after_durable")
        eid = self.rows("intake_entries")[0]["entry_id"]
        original_connect = self.registry.connect
        @contextmanager
        def readonly():
            with original_connect() as db:
                db.execute("PRAGMA query_only=ON")
                yield db
        with patch.object(self.registry, "connect", readonly):
            with self.assertRaises(sqlite3.OperationalError):
                self.staging.ingest(eid)
            with self.assertRaises(sqlite3.OperationalError):
                self.worker.drain()
        self.assertNotAccepted()
        self.assertTrue(self.original(eid).exists())
        self.assertFalse(self.sender.records("deliveries"))
        records = [json.loads(line) for line in (self.runtime.root / "logs/custody-faults.jsonl").read_text().splitlines()]
        self.assertTrue(records)
        self.assertTrue(all(r["diagnostic_only"] for r in records))
        self.assertIn("保留飞书附件", records[0]["human_message"])

    def test_registry_constraints_and_indexes(self):
        """实际 PRAGMA 验证唯一索引/外键；attempt 无独立 accepted 状态列。"""
        self.receive()
        with self.registry.connect() as db:
            self.assertEqual(list(db.execute("PRAGMA foreign_key_check")), [])
            for table in ("intakes", "source_groups", "intake_entries", "staging_receipts", "custody_events", "notification_outbox"):
                self.assertTrue(any(row["unique"] for row in db.execute(f"PRAGMA index_list({table})")), table)
            names = {r["name"] for r in db.execute("PRAGMA index_list(staging_attempts)")}
            self.assertIn("staging_attempt_receipt", names)
            columns = {r["name"] for r in db.execute("PRAGMA table_info(staging_attempts)")}
            self.assertNotIn("status", columns)
            self.assertIn("outcome", columns)

    def test_previous_dev_schema_is_not_silently_upgraded(self):
        """上一轮 DEV schema 明确拒绝，不自动改表；提示使用新的隔离测试 runtime。"""
        root = Path(tempfile.mkdtemp(prefix="previous-dev-schema-", dir=BASE / "test-output"))
        runtime = Runtime(WORKTREE, DEFAULTS, root)
        runtime.initialize()
        with sqlite3.connect(runtime.db) as db:
            db.execute("CREATE TABLE registry_meta(schema_version TEXT PRIMARY KEY)")
            db.execute("INSERT INTO registry_meta VALUES ('registry/v2.1')")
        with self.assertRaisesRegex(ContractError, "prior DEV schema"):
            Registry(runtime)
        with sqlite3.connect(runtime.db) as db:
            self.assertEqual([r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")], ["registry_meta"])

    def test_sender_missing_receipt_remains_unknown(self):
        """解耦后的 sender 未返回有效 message ID 时不伪标 delivered；请求键和目标只读。"""
        outer = self
        class IncompleteSender:
            def send(self, item, attempt):
                with outer.assertRaises(TypeError):
                    item["provider_request_key"] = "replacement"
                return None
            def lookup(self, item, attempt):
                return "unknown", None
        FeishuMockIntake(self.registry, self.staging).receive(self.contract(), MockAttachment(CONTENT))
        worker = OutboxWorker(self.registry, IncompleteSender())
        worker.drain()
        self.assertEqual(self.rows("notification_outbox")[0]["delivery_state"], "delivery_unknown")
        worker.drain()
        self.assertEqual(self.rows("staging_receipts")[0]["status"], "accepted")
        self.assertEqual(len({r["provider_request_key"] for r in self.rows("notification_attempts")}), 1)


if __name__ == "__main__":
    unittest.main()
