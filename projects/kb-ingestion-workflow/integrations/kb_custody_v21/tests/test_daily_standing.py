"""Standing daily authorization, separate finite evaluation grants and quota waits."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.daily_policy import DailyPolicy
from kb_custody_v21.daily_store import DailyStore, Deferred
from kb_custody_v21.daily_subscription import require_subscription
from kb_custody_v21.qa_quality import digest
import test_qa_quality_calls as evaluation_fixture
import test_qa_engine as qa_fixture
import test_reading_flow as reading_fixture


def standing(policy):
    return replace(policy, model_budget_mode='standing_daily', model_turn_limit=None)


class StandingPolicyTests(unittest.TestCase):
    def test_null_budget_requires_explicit_standing_mode(self):
        p=DailyPolicy('cli_synthetic','oc_synthetic','ou_owner','ou_bot',2)
        with self.assertRaises(ContractError): replace(p,model_turn_limit=None)
        with self.assertRaises(ContractError): replace(p,model_budget_mode='standing_daily')
        self.assertFalse(standing(p).model_limit_reached(10001))

    def test_temporary_reserve_expires_without_restart(self):
        p=replace(standing(DailyPolicy('cli_synthetic','oc_synthetic','ou_owner','ou_bot',2)),
            temporary_reserve_percent=1,temporary_reserve_until=1000)
        self.assertEqual(p.remaining_reserve(999),1)
        self.assertEqual(p.remaining_reserve(1000),5)
        for fields in ({'temporary_reserve_until':None},{'temporary_reserve_percent':0},
                {'temporary_reserve_percent':True},{'temporary_reserve_until':float('nan')}):
            with self.assertRaises(ContractError): replace(p,**fields)

    def test_policy_replacement_preserves_old_calls_and_unknown_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp).resolve()/'daily.sqlite3'
            p=DailyPolicy('cli_synthetic','oc_synthetic','ou_owner','ou_bot',1)
            old=DailyStore(path,p);old.reserve_model('a'*64)
            old.model_result('a'*64,1,'unknown',{'reason':'synthetic'})
            with self.assertRaises(Deferred):old.reserve_model('b'*64)
            new=DailyStore(path,standing(p));new.reserve_model('b'*64)
            self.assertEqual(new.summary()['model_turns'],2)
            self.assertEqual(new.rows('SELECT state FROM daily_models WHERE source_sha=?',('a'*64,))[0]['state'],'unknown')
            with self.assertRaises(Deferred):new.reserve_model('a'*64)

    def test_quota_pause_is_checked_without_reserving_and_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();guide=root/'guide.json';guide.write_text(json.dumps({'account_sha256':'a'*64}))
            script=root/'daily-subscription.mjs';script.write_text('// synthetic')
            now=[1000];store=DailyStore(root/'daily.sqlite3',standing(DailyPolicy('cli_synthetic','oc_synthetic','ou_owner','ou_bot',1)),clock=lambda:now[0])
            authority=SimpleNamespace(digest='a'*64,path=root/'rules.json',runtime=SimpleNamespace(worktree=root),
                check=lambda:{str(script):'b'*64},config={'guide_plan':str(guide),'text_script':str(root/'pilot-reading-guide.mjs'),
                'codex_binary':'synthetic-codex','text_node':'synthetic-node'})
            blocked=dict(state='waiting',reason='quota_threshold_reached')
            ready=dict(state='ready')
            with patch.dict('os.environ',{'KB_V21_OFFLINE_TEST':'0'}),patch('kb_custody_v21.daily_subscription.probe',side_effect=[blocked,ready]) as run:
                with self.assertRaisesRegex(Deferred,'subscription_quota_wait'):require_subscription(authority,store)
                with self.assertRaises(Deferred):require_subscription(authority,store)
                self.assertEqual(run.call_count,1)
                self.assertEqual(store.summary()['model_turns'],0)
                now[0]+=300;require_subscription(authority,store)
                self.assertIsNone(store.get('subscription-admission'))
                self.assertEqual(store.summary()['model_turns'],0)

    def test_probe_timeout_stops_only_its_own_process_group(self):
        import subprocess
        import signal
        from kb_custody_v21.daily_subscription import probe
        with patch('kb_custody_v21.daily_subscription.subprocess.Popen') as popen, patch('kb_custody_v21.daily_subscription.os.killpg') as kill:
            process=popen.return_value.__enter__.return_value
            process.pid=12345
            process.communicate.side_effect=[subprocess.TimeoutExpired('synthetic',60),(b'',b'')]
            with self.assertRaises(subprocess.TimeoutExpired):probe(['synthetic'],{})
            self.assertTrue(popen.call_args.kwargs['start_new_session'])
            kill.assert_called_once_with(12345,signal.SIGKILL)


class StandingIntegrationTests(unittest.TestCase):
    def test_evaluation_still_exhausts_its_own_finite_grant(self):
        f=evaluation_fixture.EvaluationCallTests();f.setUp();self.addCleanup(f.doCleanups)
        f.daily.policy=standing(f.daily.policy);f.grant['policy_digest']=f.daily.policy.digest
        f.grant['max_calls']=1
        for i in range(54): f.daily.reserve_model(digest(i))
        f.execute(case_id='one')
        with self.assertRaisesRegex(Deferred,'evaluation_grant_limit'):f.execute(case_id='two')
        self.assertEqual(f.daily.summary()['model_turns'],55)
        self.assertEqual(len(f.invocations),1)

    def test_qa_waits_before_intent_and_can_resume_after_old_window(self):
        f=qa_fixture.QAEngineTests();f.setUp();self.addCleanup(f.doCleanups)
        f.daily.policy=standing(f.daily.policy)
        for i in range(54): f.daily.reserve_model(digest(i))
        job=f.job('综述 城市演变')
        with patch('kb_custody_v21.qa_model.require_subscription',side_effect=Deferred('subscription_quota_wait')):
            with self.assertRaises(Deferred):f.engine.run(job)
        self.assertEqual(f.daily.summary()['model_turns'],54)
        self.assertFalse(f.wire.calls)
        f.clock+=86400
        with patch('kb_custody_v21.qa_model.require_subscription'):
            f.engine.run(f.store.get(job['id']))
        self.assertEqual(f.daily.summary()['model_turns'],55)
        self.assertEqual(len(f.wire.calls),1)

    def test_confirmed_reading_resumes_after_quota_wait_without_new_confirmation(self):
        f=reading_fixture.ReadingFlowTests();f.setUp();self.addCleanup(f.doCleanups)
        f.approve();f.daily.policy=standing(f.daily.policy)
        with patch('kb_custody_v21.reading_model.require_subscription',side_effect=Deferred('subscription_quota_wait')):
            with self.assertRaises(Deferred):
                for _ in range(8):f.step()
        old=f.daily.summary()['model_turns']
        self.assertFalse(f.wire.calls)
        with patch('kb_custody_v21.reading_model.require_subscription'):
            f.finish()
        self.assertEqual(f.daily.summary()['model_turns'],old+2)
