"""Finite wait reuses exact-preview and physical-transport fixtures, offline."""
import copy
from datetime import timedelta
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.f01 import encoded
from kb_custody_v21.f05 import file_sha
from kb_custody_v21.pilot_owner_wait import owner_wait, owner_wait_limits
import pilot_execute
import test_pilot_owner_followup as followup_fixture


class OwnerWaitTests(unittest.TestCase):
    def setUp(self):
        self.f=followup_fixture.OwnerFollowupTests();self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.p=self.f.p
        self.f.session.close()
        self.f.plan.update(reference='P18-owner-wait-v1',max_scans=3,poll_interval_seconds=30)
        self.f.save_plan()
        limits=owner_wait_limits(3)
        self.p.approval.update(operation='owner-wait',limits=limits,maximum_total=sum(limits.values()))
        self.renew()
        self.waits=[]

    def renew(self):
        self.f.f.f.save_approval()
        (self.p.root/'journal.local.jsonl').write_text(encoded(dict(approval=self.p.approval))+'\n')
        self.session=self.f.f.f.make(False)

    def wait(self,seconds):
        self.waits.append(seconds)
        self.p.now+=timedelta(seconds=seconds)

    def run_wait(self,wait=None):
        return owner_wait(self.session,self.f.f.intake,self.f.path,wait=self.wait if wait is None else wait)

    def kinds(self):
        return [e['kind'] for e in self.session.http_events() if e['type']=='intent']

    def empty(self):return dict(items=[],has_more=False)

    def test_reply_after_empty_scan_publishes_once_without_new_model_or_preview(self):
        self.p.pages.insert(0,self.empty())
        before=self.f.f.wire.calls
        result=self.run_wait()
        self.assertEqual(result['status'],'published')
        self.assertEqual(result['notification']['delivery_state'],'delivered')
        self.assertEqual(self.waits,[30])
        self.assertEqual(len(self.kinds()),14)
        self.assertEqual(self.kinds().count('list'),2)
        self.assertEqual(self.kinds().count('message'),1)
        self.assertEqual(self.kinds().count('send'),1)
        self.assertNotIn('download',self.kinds());self.assertNotIn('upload',self.kinds())
        self.assertEqual(self.f.f.wire.calls,before)
        self.assertEqual(file_sha(Path(result['path'])/'发布记录/已确认入库预览.pdf'),self.f.binding['pdf_sha256'])

    def test_empty_scans_stop_at_exact_bound_and_cannot_restart(self):
        self.p.pages=[self.empty() for _ in range(3)]
        result=self.run_wait()
        self.assertEqual(result['status'],'awaiting_owner_decision')
        self.assertEqual(result['reason'],'scan_limit_reached')
        self.assertEqual(result['scans_completed'],3)
        self.assertFalse(result['waiting_active']);self.f.assert_unconfirmed()
        self.assertEqual(len(self.kinds()),12)
        count=self.p.runner.call_count
        self.session.close();self.session=self.f.f.f.make(False)
        with self.assertRaisesRegex(ContractError,'already started'):self.run_wait()
        self.assertEqual(self.p.runner.call_count,count)

    def test_invalid_bounds_and_fields_are_rejected_before_platform(self):
        for change in (dict(max_scans=True),dict(max_scans=1),dict(max_scans=46),
                       dict(poll_interval_seconds=29),dict(poll_interval_seconds=True),
                       dict(poll_interval_seconds=3601),dict(unapproved=True)):
            original=copy.deepcopy(self.f.plan)
            with self.subTest(change=change):
                self.f.plan.update(change);self.f.save_plan()
                with self.assertRaises(ContractError):self.run_wait()
                self.p.runner.assert_not_called()
            self.f.plan=original;self.f.save_plan()

    def test_exact_wait_budget_cannot_borrow_single_scan_consent(self):
        self.session.close()
        self.p.approval['limits']=owner_wait_limits(2)
        self.p.approval['maximum_total']=sum(self.p.approval['limits'].values())
        self.renew()
        with self.assertRaisesRegex(ContractError,'exact category'):self.run_wait()
        self.p.runner.assert_not_called()

    def test_incomplete_second_scan_stops_without_detail_or_third_scan(self):
        self.p.pages=[self.empty(),dict(items=[],has_more=True,page_token='synthetic-next')]
        with self.assertRaisesRegex(ContractError,'complete bounded'):self.run_wait()
        self.assertEqual(self.kinds().count('list'),2)
        self.assertNotIn('message',self.kinds());self.f.assert_unconfirmed()

    def test_conflicting_replies_stop_without_selecting_latest(self):
        second=copy.deepcopy(self.p.message);second['message_id']='om_other_reply'
        self.p.pages[0]['items'].append(second)
        with self.assertRaisesRegex(ContractError,'unambiguous'):self.run_wait()
        self.assertNotIn('message',self.kinds());self.f.assert_unconfirmed()

    def test_changed_source_during_wait_stops_before_next_query(self):
        self.p.pages.insert(0,self.empty())
        def change(seconds):
            self.wait(seconds);self.p.source.write_text('changed')
        with self.assertRaises(ContractError):self.run_wait(change)
        self.assertEqual(self.kinds().count('list'),1);self.f.assert_unconfirmed()

    def test_superseded_candidate_during_wait_stops_before_next_query(self):
        self.p.pages.insert(0,self.empty())
        def change(seconds):
            self.wait(seconds)
            with self.f.f.registry.transaction() as db:
                db.execute('DELETE FROM pilot_current_candidates WHERE publication_id=?',(self.f.pid,))
        with self.assertRaisesRegex(ContractError,'old preview'):self.run_wait(change)
        self.assertEqual(self.kinds().count('list'),1);self.f.assert_unconfirmed()

    def test_window_jump_during_wait_stops_without_new_query(self):
        self.p.pages.insert(0,self.empty())
        def expire(seconds):self.p.now+=timedelta(days=1)
        with self.assertRaises(ContractError):self.run_wait(expire)
        self.assertEqual(self.kinds().count('list'),1);self.f.assert_unconfirmed()

    def test_short_window_finishes_locally_before_credentials(self):
        self.session.close()
        self.p.approval['expires_at']=(self.p.now+timedelta(seconds=60)).isoformat()
        self.renew()
        result=self.run_wait()
        self.assertEqual(result['reason'],'insufficient_window')
        self.assertEqual(result['scans_completed'],0)
        self.p.runner.assert_not_called();self.f.assert_unconfirmed()

    def test_poll_spacing_is_checked_and_long_wait_is_chunked(self):
        self.f.plan['poll_interval_seconds']=75;self.f.save_plan()
        self.p.pages.insert(0,self.empty())
        self.assertEqual(self.run_wait()['status'],'published')
        self.assertEqual(self.waits,[30,30,15])

    def test_early_wait_return_cannot_turn_into_fast_polling(self):
        self.p.pages.insert(0,self.empty())
        with self.assertRaisesRegex(ContractError,'scheduled time'):self.run_wait(lambda _:None)
        self.assertEqual(self.kinds().count('list'),1);self.f.assert_unconfirmed()

    def test_interrupted_wait_cannot_restart_even_when_prior_scan_completed(self):
        self.p.pages.insert(0,self.empty())
        def interrupt(_):raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):self.run_wait(interrupt)
        count=self.p.runner.call_count
        self.session.close();self.session=self.f.f.f.make(False)
        with self.assertRaisesRegex(ContractError,'already started'):self.run_wait()
        self.assertEqual(self.p.runner.call_count,count);self.f.assert_unconfirmed()

    def test_operator_composition_has_no_model_and_cannot_repeat(self):
        self.session.close()
        approval=dict(self.p.approval,run_plan=str(self.f.path),cli_guard=str(self.p.source),
            runtime_config=dict(self.f.f.intake.runtime.config,runtime_relative=str(
                self.f.f.intake.runtime.root.relative_to(self.f.f.intake.runtime.worktree))))
        process=Mock(side_effect=self.p.runner)
        with patch.object(pilot_execute,'execution_inputs',return_value=(self.f.f.intake.runtime.worktree,approval,self.p.frozen)),patch.object(
                pilot_execute,'CliProcess',return_value=process),patch.object(pilot_execute,'CodexTextWire') as wire:
            result=pilot_execute.execute(self.p.root,self.p.digest,'owner-wait')
            self.assertEqual(result['status'],'COMPLETED_BOUNDED_ACTION',result)
            self.assertEqual(result['result']['status'],'published')
            wire.assert_not_called();count=process.call_count
            with self.assertRaises(FileExistsError):pilot_execute.execute(self.p.root,self.p.digest,'owner-wait')
            self.assertEqual(process.call_count,count)

    def test_operator_rejects_old_action_approval_before_marker(self):
        approval=dict(self.p.approval,operation='owner-followup')
        with patch.object(pilot_execute,'execution_inputs',return_value=(None,approval,{})):
            with self.assertRaisesRegex(ContractError,'own explicit approval'):
                pilot_execute.execute(self.p.root,self.p.digest,'owner-wait')
        self.assertFalse(list(self.p.root.glob('entry-*.local.json')))
