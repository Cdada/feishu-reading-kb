"""Comparable, complete-denominator QA reports and deterministic retrieval diagnostics."""
from collections import Counter, defaultdict
from copy import deepcopy

from .qa_quality import RUBRIC_SHA256, assess, digest, require, sha, text, verify_fixture

RUN_FIELDS = {'run_id', 'corpus_id', 'corpus_sha256', 'code_commit', 'model',
    'prompt_sha256', 'parameters_sha256', 'judge_fingerprint', 'evaluation_clock'}


def summary(rows):
    counts = Counter(r['status'] for r in rows)
    measured = [r for r in rows if r.get('score') is not None]
    supported = [r for r in measured if r['answerability'] == 'supported']
    boundaries = [r for r in measured if r['answerability'] != 'supported']
    calibrated = [r for r in measured if r.get('semantic_calibrated')]
    return dict(planned=len(rows), states=dict(counts), measured=len(measured),
        passed=counts['passed'], pass_rate=counts['passed']/len(rows) if rows else None,
        mean_supported_score=sum(r['score'] for r in supported)/len(supported) if supported else None,
        mean_boundary_score=sum(r['score'] for r in boundaries)/len(boundaries) if boundaries else None,
        calibrated_scored=len(calibrated), critical_cases=sum(bool(r.get('critical_errors')) for r in rows),
        fully_resolved=sum(r.get('fully_resolved', False) for r in rows),
        false_refusals=sum(r.get('false_refusal', False) for r in rows),
        quality_passed=bool(rows) and counts['passed'] == len(rows))


def make_report(cases, records, run, *, calibration_report=None):
    require(isinstance(cases, list) and 1 <= len(cases) <= 10000, 'nonempty bounded cases required')
    require(isinstance(run, dict) and set(run) == RUN_FIELDS and all(map(text, run.values())), 'complete run identity required')
    for key in ('corpus_sha256', 'prompt_sha256', 'parameters_sha256', 'judge_fingerprint'):
        require(sha(run[key]), 'run hash required: '+key)
    require(isinstance(records, list), 'explicit records list required')
    by_case = {}
    for case in cases:
        verify_fixture(case)
        require(case['id'] not in by_case, 'duplicate test id')
        by_case[case['id']] = case
    by_record = {}
    for record in records:
        require(isinstance(record, dict) and text(record.get('id')) and record['id'] in by_case
            and record['id'] not in by_record, 'unknown or duplicate result id')
        require(record.get('fixture_sha256') == by_case[record['id']]['fixture_sha256'], 'result evidence binding differs')
        require(record.get('state') in ('completed', 'error', 'skipped', 'unknown'), 'explicit result state required')
        require(type(record.get('model_calls')) is int and record['model_calls'] >= 0
            and type(record.get('cached')) is bool, 'actual call count and cache status required')
        require(all(type(record[k]) is bool for k in ('answer_cached', 'judge_cached') if k in record), 'cache observations must be boolean')
        if record['state'] != 'completed':
            require(text(record.get('reason')) and not record.get('answer') and not record.get('judgment'), 'unfinished result must explain absence')
        else:
            require('answer' in record, 'completed result lacks an answer')
        if 'provider_models' in record:
            require(isinstance(record['provider_models'], dict) and set(record['provider_models']) == {'answer', 'judge'}
                and all(map(text, record['provider_models'].values())), 'explicit observed model identities required')
        by_record[record['id']] = record
    token_totals = dict(input_tokens_known=0, output_tokens_known=0, calls_with_unknown_usage=0)
    for record in records:
        known_calls = 0
        usage = record.get('usage', {})
        require(isinstance(usage, dict) and set(usage) <= {'answer', 'judge'}, 'invalid token usage roles')
        for role, amounts in usage.items():
            require(isinstance(amounts, dict) and set(amounts) == {'input_tokens', 'output_tokens'}
                and all(v is None or type(v) is int and v >= 0 for v in amounts.values()), 'invalid token usage')
            if record.get(role+'_cached', record['cached']):
                continue
            known_calls += int(all(v is not None for v in amounts.values()))
            for token, count in amounts.items():
                if count is not None:
                    token_totals[token+'_known'] += count
        require(known_calls <= record['model_calls'], 'token receipts exceed actual new calls')
        token_totals['calls_with_unknown_usage'] += record['model_calls']-known_calls
    rows = []
    for case in cases:
        record = by_record.get(case['id'])
        base = dict(id=case['id'], fixture_sha256=case['fixture_sha256'],
            answerability=case['expected']['answerability'], question_type=case.get('question_type', 'unspecified'),
            split=case.get('split', 'unspecified'), parent_group=case.get('parent_group', case['id']),
            core=case.get('core', False))
        require(type(base['core']) is bool and all(text(base[k]) for k in ('question_type', 'split', 'parent_group')), 'invalid report grouping')
        if record is None:
            rows.append(dict(base, status='not_run', score=None, reasons=['answer_missing'], cached=False, model_calls=0))
        elif record['state'] != 'completed':
            rows.append(dict(base, status=record['state'], score=None, reasons=[record['reason']],
                cached=record['cached'], model_calls=record['model_calls']))
        else:
            result = assess(case, record['answer'], record.get('judgment'),
                judge_fingerprint=run['judge_fingerprint'], corpus_id=run['corpus_id'], calibration_report=calibration_report)
            rows.append(dict(base, **{k: v for k, v in result.items() if k not in base},
                cached=record['cached'], model_calls=record['model_calls'],
                provider_models=deepcopy(record.get('provider_models'))))
    slices = {}
    for field in ('question_type', 'answerability', 'split'):
        groups = defaultdict(list)
        for row in rows:
            groups[row[field]].append(row)
        slices[field] = {k: summary(v) for k, v in sorted(groups.items())}
    result = dict(version='qa-quality-report-v1', run=deepcopy(run), rubric_sha256=RUBRIC_SHA256,
        case_set_sha256=digest(sorted((c['id'], c['fixture_sha256']) for c in cases)),
        calibration_sha256=digest(calibration_report) if calibration_report else None,
        summary=summary(rows), slices=slices, results=rows,
        model_calls=sum(r['model_calls'] for r in rows), cached_cases=sum(r['cached'] for r in rows),
        token_usage=token_totals,
        limitation='Scores describe supplied evidence only; uncalibrated scores are provisional. Missing results never pass.')
    result['report_sha256'] = digest(result)
    return result


def verify_report(report):
    require(isinstance(report, dict) and report.get('version') == 'qa-quality-report-v1', 'quality report required')
    require(report.get('report_sha256') == digest({k: v for k, v in report.items() if k != 'report_sha256'}), 'report changed')
    require(report['summary'] == summary(report['results']), 'report summary differs')
    return report


def compare_reports(baseline, candidate, *, mode='system'):
    """Compare only fixed corpus/cases/rubric/judge/calibration; never auto-deploy."""
    verify_report(baseline)
    verify_report(candidate)
    require(mode in ('system', 'prompt'), 'comparison mode must be explicit')
    mismatches = []
    for key in ('case_set_sha256', 'rubric_sha256', 'calibration_sha256'):
        if baseline[key] != candidate[key]:
            mismatches.append(key)
    same = ['corpus_id', 'corpus_sha256', 'judge_fingerprint', 'evaluation_clock']
    if mode == 'prompt':
        same += ['model', 'parameters_sha256', 'code_commit']
    mismatches += [k for k in same if baseline['run'][k] != candidate['run'][k]]
    if baseline['case_set_sha256'] == candidate['case_set_sha256']:
        before_rows = {r['id']: r for r in baseline['results']}
        for row in candidate['results']:
            old_models, new_models = before_rows[row['id']].get('provider_models'), row.get('provider_models')
            if old_models is not None and new_models is not None:
                if old_models['judge'] != new_models['judge'] or (mode == 'prompt' and old_models['answer'] != new_models['answer']):
                    mismatches.append('observed_model_revision:'+row['id'])
            elif (old_models is None) != (new_models is None):
                mismatches.append('observed_model_identity_missing:'+row['id'])
    if mismatches:
        return dict(status='not_comparable', mismatches=mismatches, release_allowed=False, changes=[])
    old = {r['id']: r for r in baseline['results']}
    changes = []
    for row in candidate['results']:
        prior = old[row['id']]
        delta = row['score']-prior['score'] if row['score'] is not None and prior['score'] is not None else None
        changes.append(dict(id=row['id'], before=prior['status'], after=row['status'], score_delta=delta,
            new_failure=prior['status'] == 'passed' and row['status'] != 'passed',
            fixed=prior['status'] != 'passed' and row['status'] == 'passed',
            core_regression=bool(row['core'] and prior['status'] == 'passed' and row['status'] != 'passed')))
    missing = any(r['status'] in ('not_run', 'skipped', 'error', 'unknown', 'needs_review') for r in candidate['results'])
    baseline_unready = not baseline['summary']['quality_passed']
    # Small slices retain counts; a five-point decline triggers inspection regardless of overall average.
    slice_drops = []
    for field, groups in candidate['slices'].items():
        for name, group in groups.items():
            previous = baseline['slices'][field][name]
            for metric in ('mean_supported_score', 'mean_boundary_score'):
                if previous[metric] is not None and group[metric] is not None and group[metric] < previous[metric]-5:
                    slice_drops.append(dict(field=field, value=name, metric=metric, delta=group[metric]-previous[metric]))
    decline = candidate['summary']['pass_rate'] < baseline['summary']['pass_rate']-.03
    mean_drops = []
    for metric in ('mean_supported_score', 'mean_boundary_score'):
        before, after = baseline['summary'][metric], candidate['summary'][metric]
        if before is not None and after is not None and after < before-3:
            mean_drops.append(metric)
    regression = decline or bool(mean_drops or slice_drops) or any(c['core_regression'] for c in changes)
    allowed = candidate['summary']['quality_passed'] and not baseline_unready and not regression
    return dict(status='passed' if allowed else 'needs_review' if missing or baseline_unready else 'failed',
        release_allowed=allowed, changes=changes, pass_rate_drop=decline, mean_score_drops=mean_drops,
        slice_drops=slice_drops, baseline_unready=baseline_unready, incomplete=missing,
        policy='advisory_only_no_deployment_or_rollback')


def retrieval_metrics(groups, hits):
    """Each required group permits alternate sources and an optional exact span.

    Empty groups mean no positive retrieval label, not that related hits are wrong.
    """
    require(isinstance(groups, list) and isinstance(hits, list), 'retrieval inputs must be lists')
    for group in groups:
        require(isinstance(group, list) and group, 'nonempty alternative evidence group required')
        for alt in group:
            require(isinstance(alt, dict) and set(alt) <= {'material_id', 'quote'}
                and text(alt.get('material_id')) and ('quote' not in alt or text(alt['quote'])), 'invalid evidence alternative')
    for hit in hits:
        require(isinstance(hit, dict) and text(hit.get('material_id')) and isinstance(hit.get('text'), str), 'invalid retrieved fragment')
    ids = list(dict.fromkeys(h['material_id'] for h in hits))
    result = dict(required_groups=len(groups), retrieved_sources=len(ids), retrieved_fragments=len(hits),
        semantic_answer_accuracy=None, model_calls=0)
    for k in (5, 8):
        matched = sum(any(a['material_id'] in ids[:k] for a in g) for g in groups)
        result[f'source_coverage_at_{k}'] = matched/len(groups) if groups else None
        result[f'hit_at_{k}'] = bool(matched) if groups else None
    visible = hits[:12]
    matched = sum(any(any(h['material_id'] == a['material_id'] and ('quote' not in a or a['quote'] in h['text'])
        for h in visible) for a in g) for g in groups)
    result['evidence_coverage_at_12'] = matched/len(groups) if groups else None
    result['source_only_groups'] = sum(any('quote' not in a for a in g) for g in groups)
    return result


def markdown_report(report, comparison=None):
    verify_report(report)
    s = report['summary']
    lines = ['# 知识库问答质量报告', '', f"运行：{report['run']['run_id']}", '',
        f"计划 {s['planned']} 题；通过 {s['passed']} 题；已评分 {s['measured']} 题；独立校准覆盖 {s['calibrated_scored']} 题。",
        f"整体状态：{'通过' if s['quality_passed'] else '尚未通过'}。关键错误涉及 {s['critical_cases']} 题。",
        f"实际模型调用：{report['model_calls']}；缓存复用：{report['cached_cases']} 题。", '',
        '评分只针对提供证据。未校准分数为暂定结果；未测、错误和额度中断均不算通过。', '',
        '| 题目 | 状态 | 得分 | 原因 |', '| --- | --- | ---: | --- |']
    def cell(value):
        return str(value).replace('|', '\\|').replace('\n', ' ').replace('<', '&lt;').replace('>', '&gt;')
    for row in report['results']:
        lines.append('| '+ ' | '.join(map(cell, [row['id'], row['status'], row['score'] if row['score'] is not None else '未评分',
            ', '.join(row.get('critical_errors', [])+row.get('reasons', []))]))+' |')
    if comparison is not None:
        lines += ['', '## 与基线比较', '', f"状态：{comparison['status']}；建议放行：{comparison['release_allowed']}。",
            '该报告不自动部署、回退或启动服务。']
    return '\n'.join(lines)+'\n'
