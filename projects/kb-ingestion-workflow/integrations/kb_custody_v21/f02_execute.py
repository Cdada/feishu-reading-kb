#!/usr/bin/env python3
"""Explicit F02 operator entry; never called by CI or a scheduler.

Approval and F01 receipts stay in fixed private DEV storage. The operator enters
the existing test secret once into a hidden TTY prompt. No credential is saved,
exported from a keychain, or used to change a CLI profile.
"""
from datetime import datetime, timezone
import getpass
import json
import os
from pathlib import Path
import re
import subprocess
import sys

PACKAGE = Path(__file__).resolve().parent
sys.path[:0] = [str(PACKAGE/'src'), str(PACKAGE.parent/'local_conversion_mcp/src')]
from kb_custody_v21.contracts import ContractError
from kb_custody_v21.f01 import sha
from kb_custody_v21.f01_flow import completed_steps, fixtures, run
from kb_custody_v21.f02 import ExistingGroupExperiment
from review_evidence import source_snapshot


def checked_source(worktree, approval):
    head = subprocess.check_output(['git', '-C', str(worktree), 'rev-parse', 'HEAD'], text=True).strip()
    dirty = subprocess.check_output(['git', '-C', str(worktree), 'status', '--porcelain'])
    snapshot = source_snapshot(worktree)
    if dirty or approval.get('source_commit') != head or approval.get('source_snapshot') != snapshot:
        raise ContractError('approved clean source version required before credential/network')
    return dict(head=head, **snapshot)


def frozen_files(prior, approval, font):
    data = fixtures((prior/'geometry.png').read_bytes(), font)
    actual = {k: dict(sha256=sha(v), bytes=len(v)) for k, v in data.items()}
    if actual != approval['fixtures'] or any((prior/name).read_bytes() != content for name, content in data.items()):
        raise ContractError('frozen F01 fixture bytes changed; no new material may be sent')
    return data


def write_private(path, value):
    with path.open('x') as stream:
        os.chmod(path, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())


def execute(*, experiment_class=ExistingGroupExperiment, reference='f02', reuse_known_txt=False):
    os.umask(0o077)
    worktree = Path(subprocess.check_output(['git', '-C', str(PACKAGE), 'rev-parse', '--show-toplevel'], text=True).strip())
    prior = worktree/'.dev-runtime/kb-v21/real-validation/f01-v1'
    approval = json.loads((prior/(reference+'-approval.local.json')).read_text())
    source = checked_source(worktree, approval)
    font = os.environ.get('KB_V21_PREVIEW_FONT', '/System/Library/Fonts/STHeiti Light.ttc')
    frozen = frozen_files(prior, approval, font)
    client = experiment_class(prior/'requests', approval)
    outcome = dict(started_at=datetime.now(timezone.utc).isoformat(), real_user_entry='NOT_RUN',
                   real_reading_guide='NOT_RUN', unknown_recovery='NOT_RUN', end_to_end='NOT_RUN')
    secret = None
    try:
        write_private(client.root/'executed-source.local.json', source)
        if not sys.stdin.isatty():
            raise ContractError('hidden operator TTY input required')
        print('Existing dedicated test App Secret only. Input is hidden and is not saved.', flush=True)
        secret = getpass.getpass('TEST SECRET (hidden): ')
        if not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', secret):
            raise ContractError('credential format rejected before network')
        checked_source(worktree, approval)  # Also detect edits while waiting for input.
        client.authenticate(approval['app_id'], secret)
        secret = None
        result = run(client, worktree, worktree/('.dev-runtime/kb-v21/test-output/'+reference+'-live'), frozen, font,
                     reuse_known_txt=reuse_known_txt)
        outcome.update(result, status='PASS_BOUNDED_BOT_TRANSPORT')
    except Exception as error:
        outcome.update(status='STOPPED', error_type=type(error).__name__)
        client.append({'stopped': 'orchestration', 'error_type': type(error).__name__})
    finally:
        secret = None
        outcome['ended_at'] = datetime.now(timezone.utc).isoformat()
        outcome['completed_steps'] = completed_steps(client)
        if reference == 'f03':
            outcome['group_state_reconciliation'] = client.value('group_state_reconciliation') or 'NOT_CONFIRMED'
            outcome['old_f02_intent'] = 'RETAINED_UNRESOLVED'
        if reference == 'f04':
            outcome['known_message_reuse'] = client.value('reused_known_message') or 'NOT_CONFIRMED'
            outcome['old_f02_intent'] = 'RETAINED_UNRESOLVED'
        outcome['physical_requests'] = {k: sum(e.get('kind') == k for e in client.events) for k in client.limits}
        outcome['unfinished_requests'] = len({e['intent'] for e in client.events if 'intent' in e}
            - {e['complete'] for e in client.events if 'complete' in e})
        try:
            write_private(client.root/'result.local.json', outcome)
        finally:
            client.close()
        print(outcome['status']+'; private evidence saved; no automatic retry', flush=True)
    return 0 if outcome['status'] == 'PASS_BOUNDED_BOT_TRANSPORT' else 1


if __name__ == '__main__':
    if sys.argv[1:] != ['--execute']:
        raise SystemExit('explicit --execute and approved fixed private configuration required')
    try:
        sys.exit(execute())
    except Exception as error:
        # Do not expose local identifiers or secret-bearing provider diagnostics.
        print('STOPPED before execution setup: '+type(error).__name__)
        sys.exit(1)
