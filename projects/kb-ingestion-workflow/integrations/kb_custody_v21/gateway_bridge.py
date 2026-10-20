#!/usr/bin/env python3
"""Offline JSON-line bridge used by the actual Node IntakeFlow V2.1 branch.

The Python process owns the asset lock before asking Node to download. A replay
of accepted bytes never emits that request. No network or provider credentials.
"""
import argparse
import base64
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys

PACKAGE = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE / "src"))
from dev import offline_guard
from kb_custody_v21.contracts import ContractError
from kb_custody_v21.intake import normalized_message_contract
from kb_custody_v21.runtime import Runtime
from kb_custody_v21.registry import Registry
from kb_custody_v21.staging import Staging
from kb_custody_v21.notifications import OutboxWorker


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worktree", required=True)
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--pilot-config")
    args = parser.parse_args()
    runtime = Runtime(args.worktree, json.loads((PACKAGE / "dev.example.json").read_text()), args.runtime)
    sys.addaudithook(offline_guard)
    registry = Registry(runtime)
    staging = Staging(registry)
    sender = runtime.make_sender()
    worker = OutboxWorker(registry, sender)
    request = json.loads(sys.stdin.readline())
    op = request.get("operation")
    message = request.get("message", {})
    pilot = None
    if args.pilot_config:
        # Configuration is a local launch argument, never an event field.
        sys.path.insert(0, str(PACKAGE.parent / 'local_conversion_mcp/src'))
        from kb_custody_v21.pilot_intake import PilotIntake
        from kb_custody_v21.pilot_preview import PilotScope
        with os.fdopen(runtime.read_fd(args.pilot_config), 'r') as stream:
            config = json.load(stream)
        if set(config) != {'scope', 'max_attachments', 'max_pending'}:
            raise ContractError('invalid explicit Pilot configuration')
        pilot = PilotIntake(registry, PilotScope(**config['scope']),
                            max_attachments=config['max_attachments'], max_pending=config['max_pending'])

    def contract(index):
        group = message.get("sourceGroup")
        if group is not None:
            if set(group) != {"source_group_id", "membership_revision", "required_indices"}:
                raise ContractError("invalid explicit source group")
            required = group["required_indices"]
            if (not isinstance(required, list) or not required or len(set(required)) != len(required)
                    or any(type(i) is not int or not 0 <= i < len(message["resources"]) for i in required)):
                raise ContractError("invalid required indices")
            group = {"source_group_id": group["source_group_id"],
                     "membership_revision": group["membership_revision"], "required": index in required}
        return normalized_message_contract(message, index, group)

    def binding(row):
        # Only stable references cross this boundary. No inbound path is returned.
        with staging.input_binding(row["entry_id"], "gateway_input_validation"):
            return {"schema_version": "staging-input/v2.1", "entry_id": row["entry_id"],
                    "intake_id": row["intake_id"], "asset_id": row["asset_id"],
                    "staging_asset_id": row["asset_id"], "acceptance_id": row["acceptance_id"],
                    "received_filename": row["received_filename"]}

    if op == "prepare":
        if pilot:
            emit({'result': pilot.prepare(message)})
            return
        contracts = [contract(i) for i in range(len(message.get("resources", [])))]
        if not contracts:
            raise ContractError("no resources")
        if message.get("sourceGroup"):
            group = message["sourceGroup"]
            registry.create_group(group["source_group_id"], group["membership_revision"])
        entries = [registry.register(c) for c in contracts]
        if message.get("sourceGroup"):
            registry.seal_group(group["source_group_id"], group["membership_revision"])
        result = [{"entry_id": r["entry_id"]} for r in entries]
    elif op == "receive":
        row = None if pilot else registry.register(contract(request["resource_index"]))
        @contextmanager
        def transport():
            emit({"action": "download", "inbound_root": str(runtime.root / "inbound")})
            received = json.loads(sys.stdin.readline())
            if received.get("error"):
                raise OSError("mock gateway transport interrupted: " + str(received["error"])[:300])
            path = runtime.checked(received["path"])
            if not path.is_relative_to(runtime.root / "inbound"):
                raise ContractError("gateway transport path must be within DEV inbound")
            with os.fdopen(runtime.read_fd(path), "rb") as stream:
                yield stream
        try:
            if pilot:
                row = pilot.receive(message, request['resource_index'], transport)
                result = binding(registry.entry(row['entry_id']))
                emit({'result': result})
                return
            staging.ingest(row["entry_id"], transport)
            result = binding(registry.entry(row["entry_id"]))
        finally:
            if not pilot:
                worker.drain()
    elif op == 'pilot_work':
        if not pilot:
            raise ContractError('explicit local Pilot configuration required')
        result = pilot.work_once()
    elif op == 'pilot_status':
        if not pilot:
            raise ContractError('explicit local Pilot configuration required')
        result = pilot.status(request['entry_id'])
    elif op == "read":
        supplied = request["binding"]
        if supplied.get("schema_version") != "staging-input/v2.1":
            raise ContractError("versioned staging input required")
        row = registry.entry(supplied["entry_id"])
        for key in ("asset_id", "acceptance_id", "intake_id"):
            if supplied.get(key) != row[key]:
                raise ContractError("input binding identity mismatch")
        if supplied.get("staging_asset_id") != row["asset_id"]:
            raise ContractError("staging asset alias mismatch")
        with staging.input_binding(row["entry_id"], "offline_gateway_read_test") as current:
            data = current["stream"].read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024:
                raise ContractError("offline test reader limit exceeded; S7 streaming reader not implemented")
            result = {"base64": base64.b64encode(data).decode()}
    elif op == "snapshot":
        tables = ("intakes", "assets", "intake_entries", "staging_receipts", "staging_files",
                  "staging_attempts", "source_groups", "source_group_memberships", "custody_events",
                  "notification_outbox", "notification_attempts", "asset_leases", "intake_custody")
        result = {t: registry.rows(f"SELECT * FROM {t}") for t in tables}
        result.update(mock_deliveries=sender.records("deliveries"), mock_calls=sender.records("calls"))
    else:
        raise ContractError("unsupported offline bridge operation")
    emit({"result": result})


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        emit({"error": {"code": type(error).__name__, "message": str(error)}})
        sys.exit(1)
