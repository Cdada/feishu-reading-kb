"""Synthetic regression of movable candidate bindings and independent notice delivery."""
from pathlib import Path
import unittest
from kb_custody_v21.contracts import ContractError
from kb_custody_v21.f05 import file_sha
import test_pilot_owner_followup as followup_fixture
from kb_custody_v21.pilot_publication_inputs import stable_publication_inputs,reject_moving_inputs
from kb_custody_v21.pilot_publication_notice import deliver_publication_notice as deliver
from kb_custody_v21.pilot_notices import PilotExecutionSession
from kb_custody_v21.pilot_notice_workflow import PilotNoticeWorkflow
import json

class Regression(unittest.TestCase):
    def setUp(self):
        self.f=followup_fixture.OwnerFollowupTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.pub=self.f.f.run.pipeline.publisher;self.runtime=self.f.f.intake.runtime
        self.candidate=self.pub.root/(self.f.pid+'.candidate')
    def actual_checks(self):
        def check():
            for name,digest in self.f.p.frozen.items():
                if file_sha(Path(name))!=digest:raise ContractError('frozen input changed')
            return dict(self.f.p.frozen)
        self.f.p.checked_source.side_effect=check
    def fixed(self):
        copies=stable_publication_inputs(self.f.f.registry,self.f.pid,self.runtime.root/'test-output/stable-owner-evidence')
        self.f.p.frozen.update(copies);reject_moving_inputs(self.runtime,self.f.p.frozen);self.actual_checks()
        return copies
    def test_reproduces_old_failure_after_rename(self):
        self.f.p.frozen.update({str(p):file_sha(p) for p in self.candidate.rglob('*') if p.is_file()})
        self.actual_checks()
        with self.assertRaises(FileNotFoundError):self.f.run_followup()
        self.assertEqual(self.pub._row(self.f.pid)['state'],'publishing')
        self.assertTrue((self.pub.root/self.f.pid).is_dir())
        self.assertFalse(any(e.get('kind')=='send' for e in self.f.session.http_events()))
    def test_stable_copy_allows_publish_and_notice_with_real_file_checks(self):
        copies=self.fixed();before=self.f.f.wire.calls
        result=self.f.run_followup()
        self.assertEqual(result['status'],'published');self.assertEqual(result['notification']['delivery_state'],'delivered')
        self.assertEqual(self.pub._row(self.f.pid)['state'],'published');self.assertFalse(self.candidate.exists())
        self.assertEqual(file_sha(Path(result['path'])/'发布记录/已确认入库预览.pdf'),self.f.binding['pdf_sha256'])
        self.assertTrue(all(file_sha(Path(p))==h for p,h in copies.items()))
        self.assertEqual(self.f.f.wire.calls,before)
        self.assertEqual(len([e for e in self.f.session.http_events() if e['type']=='intent']),12)
        self.assertEqual(len(self.f.f.registry.rows('SELECT * FROM publication_confirmations')),1)
    def test_preflight_rejects_live_candidate_binding(self):
        with self.assertRaisesRegex(ContractError,'live candidate'):reject_moving_inputs(self.runtime,{str(self.candidate/'metadata.json'):'a'*64})
        self.f.p.runner.assert_not_called();self.f.assert_unconfirmed()
    def test_live_candidate_tamper_still_blocks_confirmation(self):
        self.fixed();(self.candidate/'metadata.json').write_text('{}')
        with self.assertRaises(ContractError):self.f.run_followup()
        self.f.assert_unconfirmed()
    def test_evidence_copy_tamper_blocks_before_transport(self):
        copies=self.fixed();Path(next(iter(copies))).write_bytes(b'changed')
        with self.assertRaisesRegex(ContractError,'frozen input'):self.f.run_followup()
        self.f.p.runner.assert_not_called();self.f.assert_unconfirmed()
    def test_notice_only_after_local_recovery_uses_eight_calls_and_cannot_repeat(self):
        moving={str(p):file_sha(p) for p in self.candidate.rglob('*') if p.is_file()}
        self.f.p.frozen.update(moving);self.actual_checks()
        with self.assertRaises(FileNotFoundError):self.f.run_followup()
        self.pub.publish(self.f.pid)
        for name in moving:self.f.p.frozen.pop(name)
        self.fixed();self.f.session.close()
        root=self.f.session.root.parent/'notice-only/p11-requests';root.mkdir(parents=True)
        a=dict(self.f.session.approval,root=str(root),maximum_total=8,limits=dict(auth=4,identity=3,list=0,message=0,download=0,upload=0,send=1))
        ap=root/'approval.local.json';ap.write_text(json.dumps(a));self.f.p.runner.reset_mock();self.f.p.root=root
        session=PilotExecutionSession(root,file_sha(ap),runner=self.f.p.runner,checked_source=self.f.p.checked_source,clock=lambda:self.f.p.now)
        self.addCleanup(session.close)
        expected=PilotNoticeWorkflow(session,self.f.f.intake)._publication_plan(self.f.pid)[1]
        before=self.f.f.wire.calls
        value=deliver(session,self.f.f.intake,self.f.pid,expected)
        self.assertEqual(value['delivery_state'],'delivered')
        kinds=[e['kind'] for e in session.http_events() if e['type']=='intent']
        self.assertEqual(len(kinds),8);self.assertEqual(kinds.count('send'),1)
        self.assertEqual(self.f.f.wire.calls,before)
        count=self.f.p.runner.call_count
        with self.assertRaisesRegex(ContractError,'already attempted'):deliver(session,self.f.f.intake,self.f.pid,expected)
        self.assertEqual(self.f.p.runner.call_count,count)

if __name__=='__main__':unittest.main(verbosity=2)
