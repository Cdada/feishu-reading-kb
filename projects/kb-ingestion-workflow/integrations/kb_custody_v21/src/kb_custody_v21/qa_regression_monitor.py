"""Deployed read-only ZIP observation and offline regression; never a model client.

The scheduler invokes one bounded tick. References remain frozen when source
content changes, and semantic candidates require their own finite authorization.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import zipfile

from .contracts import ContractError
from .daily_store import Deferred
from .daily_transport import durable_file
from .f05 import file_sha
from .locking import open_lock
from .qa_benchmark_corpus import BenchmarkCorpus, _member_name, _metadata
from .qa_quality import digest, require, sha, verify_fixture
from .qa_quality_cli import load_cases, read_json
from .qa_regression import plan_regression, periods
from .qa_regression_store import RegressionStore
from .runtime import no_symlinks


def stamp(path):
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def atomic_json(path, value):
    temporary = path.with_name(path.name+'.tmp')
    no_symlinks(temporary)
    with temporary.open('w', encoding='utf-8') as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)


def observe(archive, selection, corpus_id):
    """Rehash selected source text; inspect all member metadata without extraction."""
    before = stamp(archive)
    with zipfile.ZipFile(archive, metadata_encoding='utf-8') as zipped:
        inventory = sorted((i.filename, i.CRC, i.file_size) for i in zipped.infolist()
            if not i.is_dir() and not _metadata(_member_name(i.filename)))
    names = {i[0] for i in inventory}
    selected = [dict(row) for row in selection if row['path'] in names]
    for row in selected:
        row.pop('body_sha256', None)  # Compare current hashes in the planner.
    sources = []
    if selected:
        with BenchmarkCorpus(archive, selected) as corpus:
            for row in selected:
                source = corpus.get(row['id'])
                sources.append(dict(id=row['id'], canonical_id=row['canonical_id'],
                    body_sha256=source['body_sha256'], metadata_sha256=digest(row), critical=False))
    require(stamp(archive) == before, 'archive changed while observing; retry a stable snapshot')
    return dict(corpus_id=corpus_id, sources=sources), inventory


def monitor(config_path, expected_sha, *, now=None):
    config_path = no_symlinks(Path(config_path).absolute())
    require(sha(expected_sha) and file_sha(config_path) == expected_sha, 'approved monitor configuration changed')
    config = read_json(config_path)
    require(set(config) == {'version','archive','selection','cases','corpus_id','system_files','state_root'}
        and config['version'] == 'qa-offline-monitor-v1', 'explicit offline monitor configuration required')
    for key in ('archive','selection','cases','state_root'):
        require(Path(config[key]).is_absolute(), 'absolute monitor paths required')
    require(isinstance(config['system_files'], list) and config['system_files']
        and all(isinstance(p,str) and Path(p).is_absolute() for p in config['system_files']), 'explicit system inputs required')
    root = no_symlinks(Path(config['state_root']))
    require('.dev-runtime' in root.parts, 'monitor results must remain private')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = open_lock(root/'monitor.lock')
    try:
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error: raise Deferred('regression_monitor_busy') from error
        return _tick(config, root, now or datetime.now(timezone.utc))
    finally:
        os.close(fd)


def _tick(config, root, now):
    state_path = root/'state.local.json'
    state = read_json(state_path) if state_path.exists() else {}
    source = no_symlinks(Path(config['archive']))
    cases = load_cases(config['cases'])
    selection = read_json(config['selection'])
    current, inventory = observe(source, selection, config['corpus_id'])
    system_hash = digest({p:file_sha(no_symlinks(Path(p))) for p in config['system_files']})
    identity = digest(dict(snapshot=current, inventory=inventory, cases=[c['fixture_sha256'] for c in cases],
        system=system_hash, periods=periods(now)))
    if state.get('identity') == identity:
        return dict(state='unchanged', model_calls=0, semantic_quality_tested=False, notify=False,
            last_report=state['report'])
    old_inventory = {r[0]:r[1:] for r in state.get('inventory', inventory)}
    new_inventory = {r[0]:r[1:] for r in inventory}
    changed_members = sorted(k for k in set(old_inventory) | set(new_inventory) if old_inventory.get(k) != new_inventory.get(k))
    # Changes outside reviewed dependencies must not silently retain old coverage.
    reviewed_paths = {r['path'] for r in selection}
    unreviewed_changes = sorted((set(state.get('unreviewed_changes', [])) |
        {p for p in changed_members if p not in reviewed_paths}) - reviewed_paths)
    fingerprint = digest([system_hash, digest(inventory), [c['fixture_sha256'] for c in cases]])
    plan = plan_regression(state.get('snapshot'), current, cases, now=now, enabled=True,
        system_fingerprint=fingerprint, last_completed=state.get('watermarks'), include_heldout=False)
    require(plan['state'] == 'ready', 'changed monitor state must produce a plan')
    store = RegressionStore(root/'regression.sqlite3')
    key = store.enqueue(plan)
    eligible = {c['id']:c for c in cases if c.get('split') != 'heldout'}
    def evaluate(case_id):
        verify_fixture(eligible[case_id])
        return dict(id=case_id, status='passed', check='fixture_integrity_and_current_source_hash',
            model_calls=0, semantic_quality_tested=False)
    task = store.run(key, evaluate)
    result = dict(version='qa-offline-monitor-result-v1', at=now.isoformat(), task_key=key,
        state=task['state'], checked=len(task['results']), selected_sources=len(current['sources']),
        passed=sum(r['status']=='passed' for r in task['results']),
        needs_review=sum(r['status']=='needs_review' for r in task['results']),
        failed=sum(r['status']=='failed' for r in task['results']), changed_members=len(changed_members),
        unreviewed_member_changes=len(unreviewed_changes), heldout_excluded=True,
        model_calls=0, semantic_quality_tested=False, semantic_execution='awaiting_separate_calibrated_finite_grant',
        semantic_candidates=len(plan['semantic_case_ids']), schedules_managed_externally=True,
        limitation='Checks current selected text and frozen cases; does not score generated answers or audit full-corpus answerability.')
    result['offline_checks_passed'] = task['state']=='completed' and bool(task['results']) and result['passed']==len(task['results']) and not unreviewed_changes
    fingerprint = digest({k:result[k] for k in ('state','checked','passed','needs_review','failed','unreviewed_member_changes')})
    result['notify'] = fingerprint != state.get('notification_fingerprint')
    report = root/(key+'.report.local.json')
    durable_file(root/(key+'.plan.local.json'), plan)
    # Report time is observational; rechecking an interrupted task reuses results.
    if not report.exists(): durable_file(report,result)
    if task['state'] in ('completed','partial'):
        atomic_json(state_path,dict(identity=identity,snapshot=current,inventory=inventory,
            watermarks=plan['offline_completion_watermarks'], report=str(report),
            notification_fingerprint=fingerprint, unreviewed_changes=unreviewed_changes))
    return dict(result, report=str(report))


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True);parser.add_argument('--sha256',required=True)
    args=parser.parse_args(argv)
    try:
        result=monitor(args.config,args.sha256)
    except (OSError, ContractError, Deferred, zipfile.BadZipFile) as error:
        # Do not alter watermarks on missing disks, changed sources or corruption.
        result=dict(state='blocked',reason=type(error).__name__,model_calls=0,semantic_quality_tested=False,notify=True)
    print(json.dumps(result,ensure_ascii=False))
    return 2 if result['state'] in ('blocked','failed','deferred') else 0


if __name__=='__main__':
    raise SystemExit(main())
