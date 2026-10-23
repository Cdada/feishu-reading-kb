"""Local evaluation commands. No model, service or notification is started."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path

from .contracts import ContractError
from .runtime import no_symlinks
from .qa_quality import answer_packet, digest, require, sha, verify_fixture
from .qa_quality_report import make_report, markdown_report, compare_reports, retrieval_metrics
from .qa_regression import plan_regression
from .qa_regression_store import RegressionStore


def read_json(path):
    path = no_symlinks(Path(path).absolute())
    require(path.is_file() and path.stat().st_size <= 64*1024*1024, 'bounded JSON file required')
    def reject_constant(value):
        raise ContractError('non-finite JSON number: '+value)
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'duplicate JSON field')
            result[key] = value
        return result
    return json.loads(path.read_text(encoding='utf-8'), parse_constant=reject_constant, object_pairs_hook=unique_keys)


def write_new(path, value, *, raw=False):
    path = no_symlinks(Path(path).absolute())
    data = value if raw else json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write(data); stream.flush(); os.fsync(stream.fileno())


def new_directory(path):
    path = no_symlinks(Path(path).absolute())
    path.mkdir(mode=0o700, parents=False, exist_ok=False)
    return path


def load_cases(path):
    cases = read_json(path)
    require(isinstance(cases, list) and 1 <= len(cases) <= 10000, 'bounded cases list required')
    ids = set()
    for case in cases:
        verify_fixture(case)
        require(case['id'] not in ids, 'duplicate case id')
        ids.add(case['id'])
    return cases


def prepare(cases, output, *, max_input_bytes=512*1024):
    require(type(max_input_bytes) is int and 1 <= max_input_bytes <= 4*1024*1024, 'bounded request bytes required')
    packets = [(c, answer_packet(c)) for c in cases]
    root = new_directory(output)
    manifest = []
    for case, packet in packets:
        size = len(json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode())
        ready = size <= max_input_bytes
        name = digest(case['id'])+'.request.json'
        if ready:
            write_new(root/name, packet)
        manifest.append(dict(id=case['id'], fixture_sha256=case['fixture_sha256'],
            request_sha256=digest(packet), input_bytes=size, context_characters=case['context_characters'],
            state='prepared' if ready else 'blocked_input_too_large', request_file=name if ready else None))
    result = dict(cases=manifest, model_calls=0, executed=False, references_in_requests=False,
        token_usage=None, limitation='Byte bound only; actual model tokenizer/context window must be checked before execution.')
    write_new(root/'manifest.json', result)
    return dict(prepared=sum(c['state'] == 'prepared' for c in manifest), blocked=sum(c['state'] != 'prepared' for c in manifest), model_calls=0)


def check_plan(cases, plan, state_db, output):
    eligible = {c['id']: c for c in cases if plan['include_heldout'] or c.get('split') != 'heldout'}
    require(plan['case_set_sha256'] == digest(sorted((k, c['fixture_sha256']) for k, c in eligible.items())),
        'plan and test version differ')
    root = new_directory(output)
    store = RegressionStore(state_db)
    key = store.enqueue(plan)
    def evaluate(case_id):
        verify_fixture(eligible[case_id])
        return dict(id=case_id, status='passed', check='frozen_fixture_integrity',
            semantic_quality_tested=False, model_calls=0)
    task = store.run(key, evaluate)
    task['limitation'] = 'This command validates frozen fixtures; it does not re-read original sources or score model answers.'
    write_new(root/'offline-task.json', task)
    write_new(root/'notification.json', store.notification(key))
    return dict(task_state=task['state'], checked=len(task['results']),
        offline_checks_passed=bool(task['results']) and all(r['status'] == 'passed' for r in task['results']),
        semantic_quality_tested=False, model_calls=0)


def retrieve(index, cases, *, now):
    from .qa_query import parse
    require(isinstance(cases, list) and cases, 'retrieval labels required')
    results, ids = [], set()
    for case in cases:
        require(isinstance(case, dict) and {'id', 'question', 'expected_mode', 'evidence_groups'} <= set(case)
            and isinstance(case['id'], str) and case['id'] and case['id'] not in ids, 'unique labelled retrieval case required')
        ids.add(case['id'])
        query = parse(case['question'], case.get('context'), now=now)
        hits = []
        supported = query['mode'] in ('search', 'source', 'answer', 'tools', 'compare', 'followup', 'version', 'clarify')
        if supported and query['mode'] != 'clarify':
            hits = index.search(query.get('query', query['question']), material_ids=query['selected'],
                year_from=query.get('year_from'), year_to=query.get('year_to'), time_scope=query.get('time_scope'))
            if query['mode'] == 'source':
                import re
                quote = re.search(r'[“「"](.+)[”」"]', query['question'])
                if quote:
                    hits = [h for h in hits if quote[1] in h['text']]
        metrics = retrieval_metrics(case['evidence_groups'], hits) if supported else None
        results.append(dict(id=case['id'], mode=query['mode'], route_matches=query['mode'] == case['expected_mode'],
            state='measured' if supported else 'unsupported_route', metrics=metrics,
            retrieved_ids=list(dict.fromkeys(h['material_id'] for h in hits))))
    return dict(cases=len(cases), results=results, model_calls=0, semantic_answer_accuracy=None)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('prepare'); p.add_argument('--cases', required=True); p.add_argument('--out', required=True)
    p.add_argument('--max-input-bytes', type=int, default=512*1024)
    p = sub.add_parser('score')
    for field in ('cases', 'records', 'run', 'out'): p.add_argument('--'+field, required=True)
    p.add_argument('--calibration')
    p = sub.add_parser('compare')
    for field in ('baseline', 'candidate', 'out'): p.add_argument('--'+field, required=True)
    p.add_argument('--mode', choices=('system', 'prompt'), default='system')
    p = sub.add_parser('plan')
    for field in ('cases', 'current', 'system-fingerprint', 'now', 'out'): p.add_argument('--'+field, required=True)
    p.add_argument('--previous'); p.add_argument('--completed'); p.add_argument('--core', action='append', default=[])
    p.add_argument('--enable-offline', action='store_true'); p.add_argument('--include-heldout', action='store_true')
    p = sub.add_parser('check')
    for field in ('cases', 'plan', 'state-db', 'out'): p.add_argument('--'+field, required=True)
    p = sub.add_parser('retrieve')
    for field in ('cases', 'archive', 'selection', 'now', 'out'): p.add_argument('--'+field, required=True)
    args = parser.parse_args(argv)
    exit_code = 0
    if args.command == 'prepare':
        result = prepare(load_cases(args.cases), args.out, max_input_bytes=args.max_input_bytes)
        exit_code = 2 if result['blocked'] else 0
    elif args.command == 'score':
        report = make_report(load_cases(args.cases), read_json(args.records), read_json(args.run),
            calibration_report=read_json(args.calibration) if args.calibration else None)
        root = new_directory(args.out)
        write_new(root/'report.json', report); write_new(root/'report.md', markdown_report(report), raw=True)
        result = report['summary']
        exit_code = 0 if result['quality_passed'] else 2
    elif args.command == 'compare':
        result = compare_reports(read_json(args.baseline), read_json(args.candidate), mode=args.mode)
        write_new(args.out, result)
        exit_code = 0 if result['release_allowed'] else 2
    elif args.command == 'plan':
        result = plan_regression(read_json(args.previous) if args.previous else None, read_json(args.current), load_cases(args.cases),
            now=datetime.fromisoformat(args.now), last_completed=read_json(args.completed) if args.completed else None,
            enabled=args.enable_offline, system_fingerprint=args.system_fingerprint, core_ids=args.core, include_heldout=args.include_heldout)
        write_new(args.out, result)
        result = {k: result[k] for k in ('state', 'reasons', 'max_semantic_calls', 'automatic_model_execution')}
    elif args.command == 'check':
        result = check_plan(load_cases(args.cases), read_json(args.plan), args.state_db, args.out)
        exit_code = 0 if result['offline_checks_passed'] else 2
    else:
        from .qa_benchmark_corpus import BenchmarkCorpus
        from .qa_index import QAIndex
        now = datetime.fromisoformat(args.now)
        require(now.tzinfo is not None, 'timezone-aware retrieval clock required')
        selection = read_json(args.selection)
        require(isinstance(selection, list) and selection and all(sha(s.get('body_sha256')) for s in selection), 'frozen source hashes required')
        labelled = read_json(args.cases)
        root = new_directory(args.out)
        with BenchmarkCorpus(no_symlinks(Path(args.archive).absolute()), selection) as corpus:
            index = QAIndex(root/'retrieval.sqlite3')
            index.sync(corpus)
            result = retrieve(index, labelled, now=now.timestamp())
        write_new(root/'retrieval-report.json', result)
        result = dict(cases=result['cases'], model_calls=0, semantic_answer_accuracy=None)
    print(json.dumps(result, ensure_ascii=False))
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
