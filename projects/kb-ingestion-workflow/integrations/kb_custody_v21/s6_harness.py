#!/usr/bin/env python3
"""One operation per invocation; real operations require complete explicit authorization."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

PACKAGE = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE / "src"))
from kb_custody_v21.contracts import ContractError
from kb_custody_v21.registry import Registry
from kb_custody_v21.notifications import OutboxWorker
from kb_custody_v21.s6 import Sender, receive, storage
from kb_custody_v21.s6_staged import StagedClient, digest
from kb_custody_v21.staging import Staging


def main(argv=None, *, fake_wire_factory=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("preflight", "verify-target", "bind-attachments", "freeze-inputs", "ingest", "dispatch", "status", "read"))
    parser.add_argument("--config", required=True)
    parser.add_argument("--real", action="store_true", help="necessary, not sufficient; authorization gate still applies")
    parser.add_argument("--source-index", type=int, choices=(0, 1), default=0)
    parser.add_argument("--entry-id")
    parser.add_argument("--window-start", type=int, help="seconds; bounded discovery after approved upload")
    parser.add_argument("--window-end", type=int)
    parser.add_argument("--binding-sha256", help="digest printed by bind-attachments; never edit authorization")
    parser.add_argument("--crash-after-send", action="store_true", help="J: os._exit(73) after provider return before delivered commit")
    args = parser.parse_args(argv)
    if fake_wire_factory is not None and args.real:
        raise ContractError("fake entry cannot enable real transport")
    if args.crash_after_send and args.operation != "ingest":
        raise ContractError("J is restricted to a fresh ingest")
    config = json.loads(Path(args.config).read_text())
    wt = Path(subprocess.check_output(["git", "-C", str(PACKAGE), "rev-parse", "--show-toplevel"], text=True).strip())
    runtime = storage(wt, config["run_id"])
    # Read-only status uses an existing DB; never instantiate sender or credentials.
    if args.operation in ("status", "read") and not runtime.db.exists():
        raise ContractError("no S6 registry exists for this run")
    registry = Registry(runtime)
    if args.operation == "status":
        print(json.dumps(registry.rows("SELECT * FROM intake_custody"), ensure_ascii=False))
        return
    if args.operation == "read":
        if not args.entry_id:
            raise ContractError("read requires entry ID")
        with Staging(registry).input_binding(args.entry_id) as binding:
            # Do not echo the original content or an untrusted path into logs.
            print(json.dumps({"entry_id": args.entry_id, "asset_id": binding["staging_asset_id"], "verified": True}))
        return
    if args.operation not in ("preflight", "freeze-inputs") and not args.real and fake_wire_factory is None:
        raise ContractError("explicit --real is required; no default network operation")
    client = StagedClient(registry, config, operation=args.operation,
                          real=fake_wire_factory is None,
                          wire=fake_wire_factory(config) if fake_wire_factory else None)
    client.gate()  # Purely local even with --real; never reads secret.
    if args.operation == "preflight":
        print(json.dumps({"local_gate": "passed", "platform_verified": False}))
        return
    if args.operation == "verify-target":
        print(json.dumps(client.verify_target(), ensure_ascii=False))
        return
    if args.operation == "bind-attachments":
        bound = client.bind_attachments(args.window_start, args.window_end)
        print(json.dumps({"binding": bound, "binding_sha256": digest(bound)}, ensure_ascii=False))
        return
    if args.operation == "freeze-inputs":
        print(json.dumps(client.freeze_inputs(args.binding_sha256)))
        return
    client.require_frozen()
    if args.operation == "ingest":
        # Stop before accepted/outbox if this process cannot afford its whole phase.
        client.require_capacity(read=1, download=1, send=1)
        result = receive(client, args.source_index)
        print(json.dumps(result, ensure_ascii=False))
    if args.crash_after_send:
        pending = registry.rows("SELECT * FROM notification_outbox WHERE delivery_state!='delivered'")
        if len(pending) != 1 or pending[0]["delivery_state"] != "pending":
            raise ContractError("J injection requires exactly one fresh pending notification")
        if registry.rows("SELECT 1 FROM custody_events WHERE dedupe_key=?",
                         ("s6:send:" + pending[0]["provider_request_key"],)):
            raise ContractError("J injection cannot operate on an earlier send or lookup")
    # Successful ingest automatically uses the existing outbox worker.
    def hook(point):
        if args.crash_after_send and point == "after_provider_call":
            os._exit(73)
    count = OutboxWorker(registry, Sender(client), hook=hook).drain(limit=2)
    unknown = registry.rows("SELECT notification_id FROM notification_outbox WHERE delivery_state='delivery_unknown'")
    print(json.dumps({"outbox_processed": count, "budget_used": client.budget(),
                      "unknown_recovery": "BLOCKED" if unknown else "not_needed"}))
    if unknown:
        raise ContractError("unknown delivery requires verified platform contract; no lookup or resend")


if __name__ == "__main__":
    try:
        main()
    except (ContractError, OSError) as exc:
        # Never print credential-bearing HTTP exceptions or full configuration.
        print(json.dumps({"status": "BLOCKED", "reason_type": type(exc).__name__}), file=sys.stderr)
        sys.exit(2)
