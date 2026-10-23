"""Pre-registered judge controls followed by a separately bounded answer pilot."""
from copy import deepcopy

from .contracts import ContractError
from .daily_store import Deferred
from .qa_answer_contract import validate
from .qa_quality import (assess, bind_judgment, calibration, calibrated, digest, evidence_hits,
                        judge_packet, require, sha, text, validate_grade, verify_fixture)
from .qa_quality_report import make_report
from .qa_quality_runner import run_case


def verify_controls(controls, run):
    require(isinstance(controls, list) and 20 <= len(controls) <= 40, '20 to 40 pre-registered calibration controls required')
    ids, fixtures = set(), set()
    for item in controls:
        fields = {'id', 'fixture', 'answer', 'answer_sha256', 'reference_pass', 'reference_critical', 'reference_reason'}
        require(isinstance(item, dict) and set(item) == fields and text(item['id']) and item['id'] not in ids,
                'unique pre-registered control required')
        f = verify_fixture(item['fixture'])
        require(f['id'] not in fixtures and f.get('split') != 'heldout', 'distinct non-heldout control questions required')
        make_report([f], [], run)
        validate(item['answer'], evidence_hits(f))
        require(item['answer_sha256'] == digest(item['answer']) and type(item['reference_pass']) is bool
                and type(item['reference_critical']) is bool and text(item['reference_reason'])
                and not (item['reference_pass'] and item['reference_critical']), 'invalid independent control reference')
        ids.add(item['id']); fixtures.add(f['id'])
    require({c['reference_pass'] for c in controls} == {True, False}
            and any(c['reference_critical'] for c in controls), 'correct, incorrect and critical-error controls required')
    return controls


def judge_decision(fixture, answer, grade, run):
    result = assess(fixture, answer, bind_judgment(fixture, answer, grade,
        judge_fingerprint=run['judge_fingerprint']), judge_fingerprint=run['judge_fingerprint'],
        corpus_id=run['corpus_id'])
    # Compute the candidate decision without pretending calibration has passed.
    return dict(would_pass=result['status'] == 'needs_review' and result['reasons'] == ['judge_not_calibrated'],
        critical=bool(result['critical_errors']), uncertain='uncertain_judgment' in result['reasons']
            or any(row['verdict'] == 'uncertain' for field in ('required', 'forbidden', 'claims') for row in grade[field]),
        assessment=result)


def run_calibration(controls, run, *, calls, grant, checked_access, judge_adapter,
                    save_progress, reference_provenance, reference_kind='independently_reviewed'):
    controls = deepcopy(verify_controls(controls, run))
    require(callable(save_progress) and text(reference_provenance), 'durable local progress and reference provenance required')
    samples, records = [], []
    report = dict(state='running', controls_sha256=digest(controls), requested=len(controls),
        measured=0, new_model_calls=0, records=records, calibration=None, ready_for_answer_pilot=False)
    save_progress(deepcopy(report))
    for index, item in enumerate(controls):
        f, answer = item['fixture'], item['answer']
        try:
            receipt = calls.execute(run_id=run['run_id']+'-calibration', case_id=item['id'], purpose='judge',
                model_fingerprint=run['judge_fingerprint'], request=judge_packet(f, answer),
                grant=grant, checked_access=checked_access, adapter=judge_adapter,
                validator=lambda g: validate_grade(g, f, answer))
        except (Deferred, ContractError) as error:
            report['new_model_calls'] += getattr(error, 'new_model_calls', 0)
            report.update(state='stopped_execution', reason=str(error))
            save_progress(deepcopy(report))
            return report
        report['new_model_calls'] += receipt['new_model_calls']
        d = judge_decision(f, answer, receipt['output'], run)
        sample = dict(id=item['id'], reference_pass=item['reference_pass'], judge_pass=d['would_pass'],
            reference_critical=item['reference_critical'], judge_critical=d['critical'])
        samples.append(sample)
        records.append(dict(id=item['id'], fixture_sha256=f['fixture_sha256'],
            answer_sha256=item['answer_sha256'], grade=receipt['output'], decision=d,
            reference_reason=item['reference_reason'], cached=receipt['cached'],
            provider_model=receipt['provider_model'], usage=receipt['usage']))
        report['measured'] = len(samples)
        report['calibration'] = calibration(samples, judge_fingerprint=run['judge_fingerprint'],
            corpus_id=run['corpus_id'], reference_provenance=reference_provenance, reference_kind=reference_kind)
        # Early failures remain in the report. Do not replace difficult controls.
        mismatch = sample['reference_pass'] != sample['judge_pass']
        if (d['uncertain'] or sample['reference_critical'] != sample['judge_critical']
                or index < 4 and mismatch or sum(s['reference_pass'] != s['judge_pass'] for s in samples) > len(controls)*.1):
            report.update(state='stopped_judge_mismatch', reason='review_judge_before_expanding')
            save_progress(deepcopy(report))
            return report
        save_progress(deepcopy(report))
    report['ready_for_answer_pilot'] = calibrated(report['calibration'], run['judge_fingerprint'], run['corpus_id'])
    report['state'] = 'calibrated' if report['ready_for_answer_pilot'] else 'needs_review'
    save_progress(deepcopy(report))
    return report


def run_answer_pilot(cases, run, *, calibration_report, save_progress, **execution):
    require(isinstance(cases, list) and 1 <= len(cases) <= 4, 'one to four answer pilot cases required')
    require(all(c.get('split') != 'heldout' for c in cases), 'pilot cannot consume heldout cases')
    require(calibrated(calibration_report, run['judge_fingerprint'], run['corpus_id']), 'calibrate judge before generating pilot answers')
    require(callable(save_progress), 'durable local progress required')
    make_report(cases, [], run, calibration_report=calibration_report)
    records = []
    for case in cases:
        record = run_case(case, run, **execution)
        records.append(record)
        report = make_report(cases, records, run, calibration_report=calibration_report)
        save_progress(deepcopy(report))
        # Failed/unknown calls or a critical answer issue require inspection first.
        if record['state'] != 'completed' or any(r.get('critical_errors') for r in report['results'] if r['id'] == case['id']):
            return report
    return report
