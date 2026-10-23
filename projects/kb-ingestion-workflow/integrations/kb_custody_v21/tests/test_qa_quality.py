"""Synthetic controls for fabricated citations, false refusal and judge uncertainty."""
import copy
import hashlib
import unittest

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.qa_test_fixtures import compile_fixture
from kb_custody_v21.qa_quality import (WEIGHTS, RUBRIC_SHA256, answer_packet, assess, bind_judgment,
    calibration, digest, judge_packet, verify_fixture)
from kb_custody_v21.qa_quality_report import make_report, compare_reports, markdown_report, retrieval_metrics

JUDGE = 'd'*64


def fixture(key='plan', label='supported', body='Day 1: the garden survey is planned. No completion is recorded.'):
    c = dict(id=key, question='Was the garden survey completed?', selectors=[dict(material_id='garden',
        body_sha256=hashlib.sha256(body.encode()).hexdigest(), start=0, end=len(body))],
        constraints=['Use only supplied text.'], conversation=[], expected=dict(answerability=label,
            required_claims=['Only a plan is recorded; completion cannot be confirmed.'],
            forbidden_claims=['The survey is already completed.']))
    result = compile_fixture(c, {'garden': dict(title='Garden record', role='record', text=body)})
    result.update(question_type='timeline', parent_group=key, split='development', core=True)
    return result


def answer():
    return dict(status='grounded', claims=[dict(text='Only a plan is recorded.',
        evidence=[dict(evidence_id='e1', quote='the garden survey is planned.')])], gaps=[])


def grade():
    return dict(scores={k: 4 for k in WEIGHTS}, reasons={k: 'Matches the dated plan and qualification.' for k in WEIGHTS},
        required=[dict(index=0, verdict='met', reason='Clearly limits completion claim.')],
        forbidden=[dict(index=0, verdict='absent', reason='No completion asserted.')],
        claims=[dict(index=0, verdict='supported', reason='Plan is explicit.')], critical_errors=[])


def calibrated_report(kind='independently_reviewed'):
    # This simulates a separately reviewed reference file; it is not real calibration evidence.
    samples = [dict(id=str(i), reference_pass=i % 2 == 0, judge_pass=i % 2 == 0,
        reference_critical=i == 1, judge_critical=i == 1) for i in range(20)]
    return calibration(samples, judge_fingerprint=JUDGE, corpus_id='synthetic-garden',
        reference_provenance='simulated test reference', reference_kind=kind)


def run_config():
    return dict(run_id='synthetic-run', corpus_id='synthetic-garden', corpus_sha256='a'*64,
        code_commit='synthetic-v1', model='synthetic-answer', prompt_sha256='b'*64,
        parameters_sha256='c'*64, judge_fingerprint=JUDGE, evaluation_clock='2030-01-01T00:00:00+00:00')


def record(f, *, g=None, a=None):
    a = answer() if a is None else a
    return dict(id=f['id'], state='completed', fixture_sha256=f['fixture_sha256'], answer=a,
        judgment=bind_judgment(f, a, grade() if g is None else g, judge_fingerprint=JUDGE), model_calls=0, cached=False)


class QualityTests(unittest.TestCase):
    def setUp(self):
        self.f, self.a, self.g = fixture(), answer(), grade()

    def score(self, *, calibration_value=None):
        return assess(self.f, self.a, bind_judgment(self.f, self.a, self.g, judge_fingerprint=JUDGE),
            judge_fingerprint=JUDGE, corpus_id='synthetic-garden', calibration_report=calibration_value)

    def test_answer_packet_never_sees_reference_or_model_identity(self):
        packet = answer_packet(self.f)
        self.assertNotIn('expected', packet)
        self.assertNotIn('reference', packet)
        self.assertNotIn(self.f['expected']['required_claims'][0], str(packet))
        self.assertEqual(packet['evidence'][0]['evidence_id'], 'e1')
        judged = judge_packet(self.f, self.a)
        self.assertNotIn('model', judged)
        self.assertEqual(judged['reference'], self.f['expected'])

    def test_fixture_changes_are_rejected_before_scoring(self):
        self.f['model_input']['documents'][0]['text'] = 'changed'
        with self.assertRaises(ContractError): verify_fixture(self.f)

    def test_quote_existence_is_not_semantic_correctness(self):
        result = self.score()
        self.assertTrue(result['citation_structure_valid'])
        self.assertEqual(result['score'], 100)
        self.assertEqual(result['status'], 'needs_review')
        self.g['claims'][0]['verdict'] = 'unsupported'
        self.assertEqual(self.score()['status'], 'failed')

    def test_fabricated_quote_fails_without_judge(self):
        self.a['claims'][0]['evidence'][0]['quote'] = 'The survey finished yesterday.'
        result = assess(self.f, self.a, judge_fingerprint=JUDGE, corpus_id='synthetic-garden')
        self.assertEqual(result['status'], 'failed')
        self.assertIsNone(result['score'])

    def test_missing_judge_does_not_become_zero_or_pass(self):
        result = assess(self.f, self.a, judge_fingerprint=JUDGE, corpus_id='synthetic-garden')
        self.assertEqual(result['status'], 'needs_review')
        self.assertIsNone(result['score'])

    def test_calibrated_complete_answer_passes(self):
        result = self.score(calibration_value=calibrated_report())
        self.assertEqual(result['status'], 'passed')
        self.assertTrue(result['fully_resolved'])

    def test_synthetic_calibration_cannot_enable_real_pass(self):
        self.assertEqual(self.score(calibration_value=calibrated_report('synthetic'))['status'], 'needs_review')

    def test_uncertain_and_missing_requirements_are_visible(self):
        self.g['required'][0]['verdict'] = 'uncertain'
        self.assertEqual(self.score(calibration_value=calibrated_report())['status'], 'needs_review')
        self.g['required'][0]['verdict'] = 'missing'
        self.assertEqual(self.score(calibration_value=calibrated_report())['status'], 'failed')

    def test_low_scores_keep_their_detailed_reason(self):
        self.g['scores'] = {k: 3 for k in WEIGHTS}
        result = self.score(calibration_value=calibrated_report())
        self.assertIn('score_below_threshold', result['reasons'])
        self.assertEqual(result['dimension_reasons'], self.g['reasons'])
        self.assertEqual(result['point_checks']['required'], self.g['required'])

    def test_false_refusal_fails_even_with_inflated_judge_scores(self):
        self.a = dict(status='not_found', claims=[], gaps=['Cannot determine anything.'])
        self.g['claims'] = []
        self.assertTrue(self.score(calibration_value=calibrated_report())['false_refusal'])
        self.assertEqual(self.score(calibration_value=calibrated_report())['status'], 'failed')

    def test_correct_boundary_is_not_fully_resolved_business_question(self):
        self.f = fixture(label='partial')
        self.a['status'] = 'partial'
        self.a['gaps'] = ['Completion is not evidenced.']
        result = self.score(calibration_value=calibrated_report())
        self.assertEqual(result['status'], 'passed')
        self.assertFalse(result['fully_resolved'])

    def test_judge_must_cover_every_point_with_strict_scores(self):
        for bad in (True, 4.0, 5, -1, float('nan')):
            self.g['scores']['factual'] = bad
            with self.assertRaises(ContractError): self.score()
        self.g = grade(); self.g['required'] = []
        with self.assertRaises(ContractError): self.score()

    def test_judgment_is_bound_to_actual_answer_and_rubric(self):
        bound = bind_judgment(self.f, self.a, self.g, judge_fingerprint=JUDGE)
        for key in ('fixture_sha256', 'answer_sha256', 'rubric_sha256', 'judge_fingerprint'):
            bad = dict(bound, **{key: '0'*64})
            with self.assertRaises(ContractError):
                assess(self.f, self.a, bad, judge_fingerprint=JUDGE, corpus_id='synthetic-garden')

    def test_critical_error_must_locate_answer_text(self):
        self.g['critical_errors'] = [dict(code='fabrication', answer_quote='not in answer', reason='wrong')]
        with self.assertRaises(ContractError): self.score()
        self.g['critical_errors'][0]['answer_quote'] = 'Only a plan'
        self.assertEqual(self.score(calibration_value=calibrated_report())['status'], 'failed')

    def test_calibration_requires_diversity_and_critical_agreement(self):
        original = calibrated_report()
        samples = copy.deepcopy(original['samples'])
        samples[1]['judge_critical'] = False
        result = calibration(samples, judge_fingerprint=JUDGE, corpus_id='synthetic-garden',
            reference_provenance='simulated review', reference_kind='independently_reviewed')
        self.assertFalse(result['eligible'])
        result['eligible'] = True
        with self.assertRaises(ContractError): self.score(calibration_value=result)
        other = calibrated_report(); other['corpus_id'] = 'other-domain'
        self.assertEqual(self.score(calibration_value=other)['status'], 'needs_review')


class ReportsTests(unittest.TestCase):
    def report(self, cases=None, records=None, run=None):
        cases = [fixture()] if cases is None else cases
        return make_report(cases, [record(c) for c in cases] if records is None else records,
            run_config() if run is None else run, calibration_report=calibrated_report())

    def test_missing_results_stay_in_denominator(self):
        cases = [fixture(), fixture('second')]
        report = self.report(cases, [record(cases[0])])
        self.assertEqual(report['summary']['pass_rate'], .5)
        self.assertEqual(report['summary']['states']['not_run'], 1)
        self.assertFalse(report['summary']['quality_passed'])
        self.assertIn('未评分', markdown_report(report))

    def test_error_unknown_and_skip_are_not_answer_failures_or_passes(self):
        for state in ('error', 'unknown', 'skipped'):
            f = fixture()
            r = dict(id=f['id'], fixture_sha256=f['fixture_sha256'], state=state,
                reason='budget or environment', model_calls=1, cached=False)
            result = self.report([f], [r])
            self.assertEqual(result['summary']['measured'], 0)
            self.assertEqual(result['model_calls'], 1)
            self.assertFalse(result['summary']['quality_passed'])

    def test_unknown_duplicate_stale_results_fail_early(self):
        f = fixture(); r = record(f)
        for records in ([r, r], [dict(r, id='unknown')], [dict(r, fixture_sha256='0'*64)]):
            with self.assertRaises(ContractError): self.report([f], records)

    def test_scoring_cannot_relabel_another_librarys_cases(self):
        f = dict(fixture(), corpus_id='another-library')
        with self.assertRaisesRegex(ContractError, 'different library'): self.report([f])

    def test_comparison_rejects_changed_cases_and_corpus(self):
        baseline = self.report()
        for candidate in (self.report([fixture('new')]), self.report(run=dict(run_config(), corpus_sha256='e'*64))):
            self.assertEqual(compare_reports(baseline, candidate)['status'], 'not_comparable')

    def test_prompt_comparison_keeps_model_and_settings_fixed(self):
        baseline = self.report()
        candidate = self.report(run=dict(run_config(), model='other-model'))
        self.assertEqual(compare_reports(baseline, candidate, mode='prompt')['status'], 'not_comparable')
        self.assertTrue(compare_reports(baseline, candidate, mode='system')['release_allowed'])

    def test_observed_provider_revision_change_blocks_prompt_comparison(self):
        f = fixture(); first, second = record(f), record(f)
        first['provider_models'] = dict(answer='revision-one', judge='judge-one')
        second['provider_models'] = dict(answer='revision-two', judge='judge-one')
        self.assertEqual(compare_reports(self.report([f], [first]), self.report([f], [second]), mode='prompt')['status'], 'not_comparable')

    def test_token_usage_reports_unknown_and_does_not_rebill_cached(self):
        f = fixture(); r = record(f)
        r.update(model_calls=1, cached=True, answer_cached=True, judge_cached=False,
            usage=dict(answer=dict(input_tokens=99, output_tokens=99), judge=dict(input_tokens=10, output_tokens=None)))
        usage = self.report([f], [r])['token_usage']
        self.assertEqual(usage, dict(input_tokens_known=10, output_tokens_known=0, calls_with_unknown_usage=1))

    def test_new_critical_failure_is_not_hidden_by_averages(self):
        f = fixture(); bad = grade(); bad['forbidden'][0]['verdict'] = 'present'
        candidate = self.report([f], [record(f, g=bad)])
        diff = compare_reports(self.report(), candidate)
        self.assertEqual(diff['status'], 'failed')
        self.assertTrue(diff['changes'][0]['core_regression'])
        self.assertFalse(diff['release_allowed'])

    def test_unscored_baseline_never_grants_release(self):
        baseline = self.report(records=[])
        diff = compare_reports(baseline, self.report())
        self.assertTrue(diff['baseline_unready'])
        self.assertFalse(diff['release_allowed'])

    def test_report_tampering_is_rejected(self):
        baseline = self.report(); baseline['summary']['passed'] = 99
        with self.assertRaises(ContractError): compare_reports(baseline, self.report())

    def test_equivalent_sources_and_fragment_coverage_are_separate(self):
        groups = [[dict(material_id='a', quote='required words'), dict(material_id='a-copy', quote='required words')],
            [dict(material_id='b', quote='another fact')]]
        hits = [dict(material_id='a-copy', text='required words'), dict(material_id='b', text='unrelated paragraph')]
        result = retrieval_metrics(groups, hits)
        self.assertEqual(result['source_coverage_at_5'], 1)
        self.assertEqual(result['evidence_coverage_at_12'], .5)
        self.assertIsNone(retrieval_metrics([], hits)['hit_at_5'])
        self.assertIsNone(result['semantic_answer_accuracy'])

    def test_retrieval_cutoffs_are_applied(self):
        hits = [dict(material_id=str(i), text='fragment text') for i in range(13)]
        result = retrieval_metrics([[dict(material_id='7')], [dict(material_id='12')]], hits)
        self.assertEqual(result['source_coverage_at_5'], 0)
        self.assertEqual(result['source_coverage_at_8'], .5)
        self.assertEqual(result['evidence_coverage_at_12'], .5)


if __name__ == '__main__':
    unittest.main()
