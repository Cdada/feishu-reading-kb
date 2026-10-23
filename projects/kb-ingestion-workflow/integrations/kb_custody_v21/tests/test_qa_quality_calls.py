"""Synthetic-only shared-ledger, restart, concurrency and unknown-outcome tests."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import unittest

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.daily_policy import DailyPolicy
from kb_custody_v21.daily_store import DailyStore, Deferred
from kb_custody_v21.qa_answer_contract import INSTRUCTION
from kb_custody_v21.qa_quality import digest
from kb_custody_v21.qa_quality_calls import EvaluationCalls
from kb_custody_v21.qa_quality_runner import run_case, blind_pair
from test_qa_quality import fixture, answer, grade, run_config, JUDGE


class EvaluationCallTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.now = 1000000
        self.daily = DailyStore(Path(tmp.name).resolve()/'daily.sqlite3',
            DailyPolicy('cli_synthetic', 'oc_synthetic', 'ou_owner', 'ou_bot', 8), clock=lambda: self.now)
        self.calls = EvaluationCalls(self.daily)
        self.run = dict(run_config(), prompt_sha256=digest(INSTRUCTION))
        self.fingerprint = digest(dict(model=self.run['model'], prompt_sha256=self.run['prompt_sha256'],
            parameters_sha256=self.run['parameters_sha256']))
        self.grant = dict(id='synthetic-grant', approved=True, ledger_path=str(self.daily.path),
            scope_sha256=digest(self.daily.policy.scope), policy_digest=self.daily.policy.digest,
            starts_at=self.now-1, expires_at=self.now+60, max_calls=3,
            max_request_bytes=100000, max_response_bytes=100000,
            model_fingerprints=[self.fingerprint, JUDGE], billing='subscription_only')
        self.access = lambda: {'synthetic-approval': 'a'*64}
        self.invocations = []

    def wire(self, request, check):
        check(); self.invocations.append(request)
        output = grade() if request.get('protocol') == 'qa-quality-judge-v1' else answer()
        return dict(output=output, provider_model='synthetic-revision', usage=dict(input_tokens=12, output_tokens=6))

    def execute(self, **kwargs):
        args = dict(run_id='run', case_id='case', purpose='answer', model_fingerprint=self.fingerprint,
            request={'protocol': 'test'}, grant=self.grant, checked_access=self.access,
            adapter=self.wire, validator=lambda v: v)
        args.update(kwargs)
        return self.calls.execute(**args)

    def run_case(self, **kwargs):
        args = dict(calls=self.calls, grant=self.grant, checked_access=self.access,
            answer_adapter=self.wire, judge_adapter=self.wire)
        args.update(kwargs)
        return run_case(fixture(), self.run, **args)

    def test_no_configuration_never_calls_or_resets_ledger(self):
        result = run_case(fixture(), self.run)
        self.assertEqual(result['state'], 'skipped')
        self.assertEqual(self.daily.summary()['model_turns'], 0)

    def test_answer_and_judge_consume_same_shared_counter(self):
        result = self.run_case()
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(result['model_calls'], 2)
        self.assertEqual(self.daily.summary()['model_turns'], 2)
        self.assertNotIn('reference', self.invocations[0])
        self.assertIn('reference', self.invocations[1])

    def test_existing_business_calls_count_toward_evaluation_cap(self):
        with self.daily.transaction() as db:
            for i in range(8):
                db.execute("INSERT INTO daily_models VALUES (?,1,'validated','{}',?)", (str(i), self.now))
        result = self.run_case()
        self.assertEqual(result['reason'], 'model_limit')
        self.assertEqual(result['model_calls'], 0)
        self.assertEqual(self.invocations, [])

    def test_restart_reuses_receipt_but_marks_cached(self):
        self.execute()
        self.calls = EvaluationCalls(self.daily)
        result = self.execute()
        self.assertTrue(result['cached'])
        self.assertEqual(result['new_model_calls'], 0)
        self.assertEqual(len(self.invocations), 1)

    def test_timeout_charged_once_and_never_replayed(self):
        def timeout(request, check):
            raise TimeoutError('synthetic')
        result = self.run_case(answer_adapter=timeout)
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual(result['model_calls'], 1)
        again = self.run_case()
        self.assertEqual(again['state'], 'unknown')
        self.assertEqual(again['model_calls'], 0)
        self.assertEqual(self.invocations, [])
        self.assertEqual(self.daily.summary()['model_turns'], 1)

    def test_invalid_response_is_charged_and_not_retried(self):
        def invalid(request, check):
            return dict(output={}, provider_model='synthetic', usage=dict(input_tokens=None, output_tokens=None))
        first = self.run_case(answer_adapter=invalid)
        self.assertEqual(first['state'], 'error')
        self.assertEqual(first['model_calls'], 1)
        self.assertEqual(self.run_case()['model_calls'], 0)

    def test_grant_limit_spans_multiple_runs(self):
        self.grant['max_calls'] = 1
        self.execute()
        with self.assertRaisesRegex(Deferred, 'grant_limit'): self.execute(run_id='another-run')
        self.assertEqual(self.daily.summary()['model_turns'], 1)

    def test_changed_grant_id_scope_cannot_raise_limit_silently(self):
        self.execute()
        self.grant['max_calls'] = 4
        with self.assertRaisesRegex(ContractError, 'cannot change'): self.execute(case_id='second')

    def test_grant_requires_same_ledger_policy_authority_and_subscription(self):
        for field, bad in [('approved', False), ('ledger_path', 'other'), ('policy_digest', '0'*64),
                           ('billing', 'paid_api'), ('scope_sha256', '0'*64)]:
            g = dict(self.grant, **{field: bad})
            with self.assertRaises(ContractError): self.execute(grant=g)
        with self.assertRaises(ContractError): self.execute(checked_access=lambda: {})
        self.assertEqual(self.daily.summary()['model_turns'], 0)

    def test_expired_or_oversized_input_does_not_consume(self):
        self.now += 61
        with self.assertRaises(Deferred): self.execute()
        self.grant['expires_at'] = self.now+10
        self.grant['max_request_bytes'] = 1
        with self.assertRaisesRegex(Deferred, 'no_truncation'): self.execute()
        self.assertEqual(self.daily.summary()['model_turns'], 0)

    def test_partial_run_caches_answer_then_resumes_only_judge_with_new_grant(self):
        self.grant['max_calls'] = 1
        first = self.run_case()
        self.assertEqual(first['state'], 'skipped')
        self.assertEqual(first['model_calls'], 1)
        self.grant.update(id='explicit-second-grant', max_calls=1)
        second = self.run_case()
        self.assertEqual(second['state'], 'completed')
        self.assertEqual(second['model_calls'], 1)
        self.assertTrue(second['answer_cached'])
        self.assertFalse(second['judge_cached'])
        self.assertEqual(len(self.invocations), 2)

    def test_changed_request_is_not_reused(self):
        self.execute()
        with self.assertRaisesRegex(ContractError, 'request changed'): self.execute(request={'changed': True})

    def test_cached_output_is_revalidated(self):
        self.execute()
        with self.daily.transaction() as db:
            db.execute("UPDATE qa_eval_calls SET result=json_set(result,'$.output_sha256',?)", ('0'*64,))
        with self.assertRaisesRegex(ContractError, 'output changed'): self.execute()
        self.assertEqual(len(self.invocations), 1)

    def test_concurrent_duplicate_reserves_only_once(self):
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def slow(request, check):
            started.set(); release.wait(3)
            return self.wire(request, check)
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(self.execute, adapter=slow)
            self.assertTrue(started.wait(1))
            with self.assertRaises(Deferred): self.execute()
            release.set(); first.result(3)
        self.assertEqual(self.daily.summary()['model_turns'], 1)

    def test_blind_pairs_separate_mapping_and_vary_order(self):
        mappings = set()
        for i in range(20):
            packet = blind_pair(fixture(), answer(), answer(), seed=str(i))
            mappings.add(packet['private_mapping']['A'])
            self.assertNotIn('private_mapping', packet['judge_input'])
            self.assertNotIn('model', packet['judge_input'])
        self.assertEqual(mappings, {'left', 'right'})


if __name__ == '__main__':
    unittest.main()
