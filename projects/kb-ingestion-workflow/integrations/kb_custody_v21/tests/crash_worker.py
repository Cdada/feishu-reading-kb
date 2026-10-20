"""Subprocess-only hard crash fixture; exits without Python cleanup."""
import json
import os
from pathlib import Path
import sys

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / "src"))
sys.path.insert(0, str(PACKAGE))
from dev import offline_guard
from kb_custody_v21.runtime import Runtime
from kb_custody_v21.registry import Registry
from kb_custody_v21.staging import Staging
from kb_custody_v21.notifications import OutboxWorker, PersistentMockSender
from kb_custody_v21.intake import FeishuMockIntake, MockAttachment
from kb_custody_v21.contracts import ENTRY_SCHEMA

worktree, root, point = sys.argv[1:]
runtime = Runtime(worktree, json.loads((PACKAGE / "dev.example.json").read_text()), root)
sys.addaudithook(offline_guard)
registry = Registry(runtime)


def crash(name):
    if name == point:
        os._exit(73)


contract = {"schema_version": ENTRY_SCHEMA, "intake_key": "crash-intake", "entry_key": "1", "received_filename": "crash.pdf", "lineage": {"chat_id": "mock-chat"}}
if point in ("after_durable", "before_accept_commit", "after_accept_commit", "after_baseline_commit"):
    FeishuMockIntake(registry, Staging(registry, hook=crash)).receive(contract, MockAttachment(b"%PDF synthetic crash fixture"))
else:
    worker = OutboxWorker(registry, PersistentMockSender(runtime), lease_seconds=0.05, hook=crash)
    worker.drain()
