"""New consent reuses the exact old preview, never its exhausted request budget."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.f01 import encoded, sha
from kb_custody_v21.f05 import file_sha
from kb_custody_v21.pilot_guide_preparation import PilotGuidePreparation
from kb_custody_v21.pilot_owner_followup import owner_followup, previous_preview
from kb_custody_v21.pilot_run import PilotBoundedRun
from pilot_pipeline_fixture import ready
import pilot_execute
import test_pilot_guide_preparation as guide_fixture
from test_pilot_decision import owner_reply


class OwnerFollowupTests(unittest.TestCase):
    def setUp(self):
        self.f=guide_fixture.GuidePreparationTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.p=self.f.f.f
        self.f.run.close();self.f.session.close()
        # Complete the synthetic approval before any transport operation.
        manifest=dict(source_commit='a'*40,source_snapshot=dict(files={'synthetic.py':'b'*64},
            sha256=sha(json.dumps({'synthetic.py':'b'*64},sort_keys=True).encode())))
        mp=self.p.root/'source-manifest.local.json';mp.write_text(encoded(manifest))
        (self.p.root/'executed-source.local.json').write_text(encoded(manifest))
        self.p.approval.update(source_manifest=str(mp),source_manifest_sha256=file_sha(mp))
        self.f.f.save_approval()
        journal=self.p.root/'journal.local.jsonl'
        self.assertEqual(len(journal.read_text().splitlines()),1)
        journal.write_text(encoded(dict(approval=self.p.approval))+'\n')
        self.f.session=self.f.f.make(False)
        self.f.guide=PilotGuidePreparation(self.f.session,self.f.path,wire=self.f.wire)
        self.f.run=PilotBoundedRun(self.f.session,self.f.intake,self.f.run_path,
            prepare_candidate=self.f.guide,checked_ready=lambda:True,resources=ready(self.f.registry))
        self.addCleanup(self.f.run.close)
        result=self.f.run.run_to_preview()
        self.pid=result['publication_id'];self.old=self.p.root
        self.binding=dict(root=str(self.old),approval_sha256=self.p.digest,publication_id=self.pid,
            message_id=result['message_id'],pdf_sha256=result['pdf_sha256'])
        self.f.session.append(dict(stopped='operator_entry',error_type='ContractError'))
        frozen=self.f.run._source()
        self.f.run.close();self.f.session.close()
        proof=previous_preview(self.binding)
        self.old_files=dict(proof['files'])
        self.p.frozen=dict(frozen,**proof['files'])
        self.p.root=self.old.parent/'followup'/'p11-requests';self.p.root.mkdir(parents=True)
        self.p.session=self.p.root/'approval.local.json'
        self.p.approval=dict(self.p.approval,root=str(self.p.root),operation='owner-followup',
            limits=dict(auth=6,identity=3,list=1,message=1,download=0,upload=0,send=1),maximum_total=12)
        self.plan=dict(reference='P11-owner-followup-v1',previous_preview=self.binding,lookback_seconds=3600)
        self.path=self.p.root/'owner-plan.local.json';self.save_plan()
        self.f.f.save_approval()
        self.p.message=owner_reply();self.p.pages=[dict(items=[copy.deepcopy(self.p.message)],has_more=False)]
        self.p.runner.reset_mock();self.session=self.f.f.make(False)

    def save_plan(self):
        self.path.write_text(encoded(self.plan));self.p.frozen[str(self.path)]=file_sha(self.path)

    def run_followup(self):return owner_followup(self.session,self.f.intake,self.path)

    def assert_unconfirmed(self):
        self.assertFalse(self.f.registry.rows('SELECT * FROM publication_confirmations'))
        self.assertEqual(self.f.run.pipeline.previews.publisher._row(self.pid)['state'],'prepared')

    def test_correct_new_reply_publishes_same_pdf_with_new_meter_and_notice(self):
        before=self.f.wire.calls
        result=self.run_followup()
        self.assertEqual(result['status'],'published')
        self.assertEqual(result['notification']['delivery_state'],'delivered')
        self.assertEqual(file_sha(Path(result['path'])/'发布记录/已确认入库预览.pdf'),self.binding['pdf_sha256'])
        self.assertEqual(self.f.wire.calls,before)
        self.assertTrue(all(file_sha(Path(p))==h for p,h in self.old_files.items()))
        kinds=[e['kind'] for e in self.session.http_events() if e['type']=='intent']
        self.assertEqual(len(kinds),12);self.assertEqual(kinds.count('message'),1)
        self.assertNotIn('download',kinds);self.assertNotIn('upload',kinds)
        n=self.p.runner.call_count
        with self.assertRaises(ContractError):self.run_followup()
        self.assertEqual(self.p.runner.call_count,n)

    def test_wrong_parent_and_standalone_reply_never_query_detail(self):
        self.p.pages[0]['items'][0].pop('parent_id')
        with self.assertRaisesRegex(ContractError,'unambiguous'):self.run_followup()
        self.assert_unconfirmed()
        self.assertFalse(any(c.args[0]['kind']=='message' for c in self.p.runner.call_args_list))

    def test_detail_must_still_have_direct_parent(self):
        self.p.message.pop('parent_id')
        with self.assertRaises(ContractError):self.run_followup()
        self.assert_unconfirmed()

    def test_multiple_supported_replies_cannot_select_latest(self):
        self.p.pages[0]['items'].append(dict(owner_reply('暂不入库'),message_id='om_second'))
        with self.assertRaisesRegex(ContractError,'unambiguous'):self.run_followup()
        self.assert_unconfirmed()

    def test_incomplete_query_cannot_assume_unambiguous(self):
        self.p.pages[0].update(has_more=True,page_token='synthetic-next')
        with self.assertRaisesRegex(ContractError,'complete bounded'):self.run_followup()
        self.assert_unconfirmed()

    def test_changed_old_receipt_blocks_before_platform_access(self):
        events=[json.loads(line) for line in (self.old/'journal.local.jsonl').read_text().splitlines()]
        key=next(e['intent'] for e in events if e.get('operation')=='preview:'+self.pid+':send')
        (self.old/(key+'.response.local')).write_text('{}')
        with self.assertRaises(ContractError):self.run_followup()
        self.p.runner.assert_not_called();self.assert_unconfirmed()

    def test_unfinished_old_transport_is_not_revived(self):
        path=self.old/'http.local.jsonl'
        with path.open('a') as stream:stream.write(encoded(dict(type='intent',key='unknown:send',kind='send'))+'\n')
        with self.assertRaises(ContractError):previous_preview(self.binding)
        self.p.runner.assert_not_called()

    def test_old_executed_source_must_match_approved_manifest(self):
        (self.old/'executed-source.local.json').write_text('{}')
        with self.assertRaises(ContractError):previous_preview(self.binding)

    def test_previous_evidence_must_be_explicitly_frozen(self):
        self.p.frozen.pop(str(self.old/'journal.local.jsonl'))
        with self.assertRaisesRegex(ContractError,'freeze'):self.run_followup()
        self.p.runner.assert_not_called()

    def test_old_candidate_superseded_stops_before_any_read(self):
        with self.f.registry.transaction() as db:db.execute('DELETE FROM pilot_current_candidates WHERE publication_id=?',(self.pid,))
        with self.assertRaisesRegex(ContractError,'old preview'):self.run_followup()
        self.p.runner.assert_not_called()

    def test_old_evidence_change_during_confirmation_rolls_back(self):
        publisher=self.f.run.pipeline.previews.publisher
        original=type(publisher)._tree;calls=0
        def change(obj,*args):
            nonlocal calls
            value=original(obj,*args);calls+=1
            if calls==2:
                with (self.old/'journal.local.jsonl').open('a') as stream:stream.write('{}\n')
            return value
        with patch.object(type(publisher),'_tree',change),self.assertRaises(ContractError):self.run_followup()
        self.assert_unconfirmed()

    def test_operator_has_no_model_dependency_and_cannot_repeat_entry(self):
        self.session.close()
        approval=dict(self.p.approval,run_plan=str(self.path),cli_guard=str(self.p.source),
            runtime_config=dict(self.f.intake.runtime.config,runtime_relative=str(
                self.f.intake.runtime.root.relative_to(self.f.intake.runtime.worktree))))
        def inputs(*args):return self.f.intake.runtime.worktree,approval,self.p.frozen
        process=Mock(side_effect=self.p.runner)
        with patch.object(pilot_execute,'execution_inputs',side_effect=inputs),patch.object(
                pilot_execute,'CliProcess',return_value=process),patch.object(pilot_execute,'CodexTextWire') as wire:
            result=pilot_execute.execute(self.p.root,self.p.digest,'owner-followup')
            self.assertEqual(result['status'],'COMPLETED_BOUNDED_ACTION',result)
            self.assertEqual(result['result']['status'],'published')
            wire.assert_not_called();n=process.call_count
            with self.assertRaises(FileExistsError):pilot_execute.execute(self.p.root,self.p.digest,'owner-followup')
            self.assertEqual(process.call_count,n)

    def versioned(self):
        self.plan['reference']='P11-owner-followup-v2';self.save_plan()
        self.p.message.update(updated=True,create_time='1791124562087',update_time='1791124562185',
            mentions=[dict(key='@_user_1',id='ou_synthetic_bot',id_type='open_id')],
            body=dict(content=encoded(dict(text='@_user_1 确认入库'))))
        self.p.pages[0]['items']=[copy.deepcopy(self.p.message)]

    def test_matching_current_version_with_bot_mention_preserves_update_uncertainty(self):
        self.versioned();result=self.run_followup()
        self.assertEqual(result['status'],'published')
        proof=json.loads(self.f.registry.rows("SELECT payload_json FROM custody_events WHERE event_type='pilot_decision_observed'")[0]['payload_json'])
        self.assertEqual(proof['reply_observation']['updated'],True)
        self.assertEqual(proof['reply_observation']['update_cause'],'UNDETERMINED')
        self.assertEqual(proof['reply_observation']['edit_history'],'NOT_VERIFIED')
        self.assertEqual(proof['reply_observation']['normalized_command'],'确认入库')

    def test_current_version_changed_timestamp_stops_without_publishing(self):
        self.versioned();self.p.message['update_time']='1791124562285'
        with self.assertRaisesRegex(ContractError,'changed between'):self.run_followup()
        self.assert_unconfirmed()

    def test_current_version_changed_command_stops_without_publishing(self):
        self.versioned();self.p.message['body']=dict(content=encoded(dict(text='@_user_1 暂不入库')))
        with self.assertRaisesRegex(ContractError,'changed between'):self.run_followup()
        self.assert_unconfirmed()
