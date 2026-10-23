#!/usr/bin/env python3
"""Run portable offline checks, preserving unique evidence and exact source hashes."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET

PACKAGE = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE / "src"))
from kb_custody_v21.runtime import Runtime
from dev import offline_guard
from review_evidence import source_snapshot


def main():
    worktree = Path(subprocess.check_output(
        ["git", "-C", str(PACKAGE), "rev-parse", "--show-toplevel"], text=True).strip())
    runtime = Runtime.load(worktree, PACKAGE / "dev.example.json")
    runtime.initialize()
    sys.addaudithook(offline_guard)
    gateway = worktree / "projects/deepseek-harness-feishu-bridge/feishu-gateway"
    run_id = datetime.now(timezone.utc).strftime("review-%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
    output = runtime.root / "test-output/reviews" / run_id
    output.mkdir(parents=True, exist_ok=False)

    def snapshot():
        return source_snapshot(worktree)

    before = snapshot()
    records = []

    def run(name, command, extra_env=None):
        start = datetime.now(timezone.utc).isoformat()
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "KB_V21_PYTHON": sys.executable, **(extra_env or {})}
        result = subprocess.run(command, cwd=worktree, env=env, capture_output=True, text=True)
        (output / (name + ".log")).write_text(result.stdout + result.stderr)
        record = {"name": name, "started_at": start, "ended_at": datetime.now(timezone.utc).isoformat(),
                  "exit_code": result.returncode, "command": command}
        records.append(record)
        print(f"{name}: exit {result.returncode}", flush=True)
        if result.returncode:
            # Surface bounded synthetic diagnostics even when compilation fails
            # before tests can produce individual results.
            print((result.stdout + result.stderr)[-12000:], flush=True)

    # Dependencies are fetched separately before the offline suite. Serial Go
    # compilation bounds concurrent compiler work; GOMEMLIMIT is a soft target.
    run('f05-cli-guard', ['go', 'test', '-C', str(PACKAGE/'f05_cli_guard'),
        '-p=1', '-count=1', '-json', '.'], dict(GOPROXY='off', GOSUMDB='off',
        GOTOOLCHAIN='local', GOMAXPROCS='1', GOMEMLIMIT='256MiB', GOGC='50'))
    go_events = [json.loads(line) for line in (output/'f05-cli-guard.log').read_text().splitlines()
                 if line.startswith('{')]
    go_cases = [e for e in go_events if e.get('Test') and e.get('Action') in ('pass','fail','skip')]
    go_top = [e for e in go_cases if '/' not in e['Test']]
    go_report = dict(tests=len(go_top), subtests=len(go_cases)-len(go_top),
        failures=sum(e['Action']=='fail' for e in go_top), skipped=sum(e['Action']=='skip' for e in go_top))
    go_xml = ET.Element('testsuite', tests=str(len(go_top)), failures=str(go_report['failures']))
    for event in go_top:
        case=ET.SubElement(go_xml,'testcase',name=event['Test'],time=str(event.get('Elapsed',0)))
        if event['Action']=='fail':
            ET.SubElement(case,'failure').text=''.join(e.get('Output','') for e in go_events
                if e.get('Test','').startswith(event['Test']))
    ET.ElementTree(go_xml).write(output/'f05-cli-guard.xml',encoding='utf-8',xml_declaration=True)

    run("python", [sys.executable, str(PACKAGE / "run_tests.py")])
    for name in ("results.json", "junit.xml", "unittest.log"):
        (output / name).write_bytes((runtime.root / "test-output" / name).read_bytes())
    nodes = {}
    for name, tests in (
        ("gateway-mock", ["custody-v21-integration"]),
        ("pilot-text-client", ["daily-subscription", "pilot-reading-guide", "pilot-reading-organize", "pilot-knowledge-qa", "pilot-qa-quality", "codex-app-server-client"]),
        ("v1-pure-logic", ["curation-intent", "content-consent", "message-policy", "user-facing-text",
                           "local-conversion-results", "kb-curation-results", "workflow-v11", "document-reference"]),
    ):
        xml = output / (name + ".xml")
        run(name, ["node", "--import", str(gateway / "test/offline-network-guard.mjs"), "--test", "--test-concurrency=1",
                   "--test-reporter=tap", "--test-reporter-destination=stdout", "--test-reporter=junit",
                   "--test-reporter-destination=" + str(xml),
                   *[str(gateway / "test" / (test + ".test.mjs")) for test in tests]])
        cases = ET.parse(xml).findall(".//testcase") if xml.exists() else []
        nodes[name] = {"tests": len(cases), "failures": sum(c.find("failure") is not None for c in cases)}
    run("diff-check", ["git", "diff", "--check"])
    after = snapshot()
    python = json.loads((output / "results.json").read_text())
    for record in python['records']:
        if record['status'] != 'passed':
            # CI has no access to private local artifacts after the runner ends.
            # Surface one synthetic-test traceback; retain every result on disk.
            print('First Python failure:', record['test'], record['detail'], flush=True)
            break
    report = {"run_id": run_id, "head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
              "source_before": before, "source_after": after, "source_unchanged": before == after,
              "commands": records, "python": python, "node": nodes, "go": go_report,
              "installed_sdk_probe": "NOT_RUN: machine-specific evidence remains local",
              "real_feishu": "NOT_RUN", "real_baidu": "NOT_RUN", "real_model": "NOT_RUN",
              "production_recovery": "NOT_RUN"}
    (output / "review-results.local.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    (runtime.root / "test-output/latest-review.json").write_text(json.dumps({"run_id": run_id, "output": str(output)}))
    print(json.dumps({"python_tests": python["tests_run"], "python_failures": python["failures"],
                      "python_errors": python["errors"], "node": nodes, "go": go_report, "source_unchanged": before == after}))
    print("Evidence:", output)
    return 0 if before == after and go_report['tests'] > 0 and all(r["exit_code"] == 0 for r in records) else 1


if __name__ == "__main__":
    sys.exit(main())
