#!/usr/bin/env python3
"""Explicit offline entry point. Does not start the V1 gateway or any service."""
import argparse
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sys

PACKAGE = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE / "src"))
from kb_custody_v21.runtime import Runtime
from kb_custody_v21.registry import Registry
from kb_custody_v21.staging import Staging
from kb_custody_v21.notifications import OutboxWorker, PersistentMockSender
from kb_custody_v21.intake import FeishuMockIntake, MockAttachment
from kb_custody_v21.contracts import ENTRY_SCHEMA


def offline_guard(event, args):
    if event in ("socket.connect", "socket.connect_ex", "socket.bind", "socket.getaddrinfo"):
        raise RuntimeError("network disabled in V2.1 S0-S5 DEV")


def snapshot(worktree):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(worktree), *args], text=True).strip()
    tracked = git("ls-files").splitlines()
    content = hashlib.sha256()
    for name in tracked:
        path = worktree / name
        content.update(name.encode())
        content.update(path.read_bytes() if path.is_file() else b"<absent>")
    return {"path": str(worktree), "branch": git("branch", "--show-current"),
            "head": git("rev-parse", "HEAD"), "status": git("status", "--short", "--untracked-files=normal"),
            "tracked_files": len(tracked), "tracked_content_sha256": content.hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "demo", "dispatch", "status", "recover", "snapshot-v1",
        "backup-create", "backup-status", "backup-upload-reported", "backup-verify", "backup-restore"))
    parser.add_argument("--config", default=str(PACKAGE / "dev.example.json"))
    parser.add_argument("--runtime", help="only allowed inside DEV test-output")
    parser.add_argument("--entry-id")
    parser.add_argument("--publication-id", action="append", help="repeat for each published version in the content batch")
    parser.add_argument("--batch-id")
    parser.add_argument("--downloaded-copy")
    parser.add_argument("--verification-id")
    parser.add_argument("--confirmation-ref", help="explicit local reference for the user's upload report")
    args = parser.parse_args()
    worktree = Path(subprocess.check_output(["git", "-C", str(PACKAGE), "rev-parse", "--show-toplevel"], text=True).strip())
    config = json.loads(Path(args.config).read_text())
    runtime = Runtime(worktree, config, args.runtime)
    sys.addaudithook(offline_guard)
    gate = runtime.initialize()
    if args.command == "snapshot-v1":
        print(json.dumps(snapshot(worktree.parent / "feishu-bridge-v1"), ensure_ascii=False, indent=2))
        return
    registry = Registry(runtime)
    if args.command.startswith("backup-"):
        from kb_custody_v21.manual_backup import ManualBackups
        backup = ManualBackups(registry)
        required = {"backup-create": ("publication_id",), "backup-status": ("batch_id",),
            "backup-upload-reported": ("batch_id", "confirmation_ref"),
            "backup-verify": ("batch_id", "downloaded_copy"), "backup-restore": ("batch_id", "verification_id")}
        for name in required[args.command]:
            if not getattr(args, name): parser.error(args.command + " requires --" + name.replace("_", "-"))
        if args.command == "backup-create": result = backup.create(args.publication_id)
        elif args.command == "backup-status": result = backup.status(args.batch_id)
        elif args.command == "backup-upload-reported":
            result = backup.report_upload(args.batch_id, provider="baidu", confirmation_ref=args.confirmation_ref)
        elif args.command == "backup-verify": result = backup.verify_download(args.batch_id, args.downloaded_copy)
        else: result = backup.restore(args.batch_id, args.verification_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    staging = Staging(registry)
    sender = runtime.make_sender()
    worker = OutboxWorker(registry, sender)
    if args.command == "init":
        result = gate
    elif args.command == "demo":
        contract = {"schema_version": ENTRY_SCHEMA, "intake_key": "synthetic-demo-message",
                    "entry_key": "attachment-1", "received_filename": "合成测试.pdf",
                    "lineage": {"message_id": "mock-message", "chat_id": "mock-chat"}}
        result = FeishuMockIntake(registry, staging, worker).receive(contract, MockAttachment(b"%PDF-1.4\n% synthetic custody-only fixture\n%%EOF\n"))
    elif args.command == "dispatch":
        result = {"processed": worker.drain(), "mock_deliveries": sender.records("deliveries")}
    elif args.command == "recover":
        if not args.entry_id:
            parser.error("recover requires --entry-id")
        result = staging.ingest(args.entry_id)
        worker.drain()
    else:
        # No worker call. The registry's derived view has no send side effect.
        result = registry.rows("SELECT * FROM intake_custody")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
