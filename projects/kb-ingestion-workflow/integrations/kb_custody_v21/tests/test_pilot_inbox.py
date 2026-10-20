"""Prospective file receipts, local custody and duplicate conversion boundaries."""
from datetime import datetime
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.f01 import encoded, sha
from kb_custody_v21.f05 import file_sha
from kb_custody_v21.pilot_inbox import receive_new_file, receive_inbox
from kb_custody_v21.pilot_notices import PilotExecutionSession, PURPOSES
import test_pilot_download as download_fixture
CONTENT = download_fixture.CONTENT


class PilotInboxTests(unittest.TestCase):
    def setUp(self):
        self.f = download_fixture.PilotDownloadTests('test_other_scope_rejects_before_reading_message')
        self.f.setUp(); self.addCleanup(self.f.doCleanups)
        f = self.f
        # Test intentionally has no local sample or sample digest.
        f.frozen.pop(str(f.sample)); f.sample.unlink()
        previous = f.root; f.root = previous.with_name('p11-requests'); previous.rename(f.root)
        f.source = f.root/'source.synthetic'; f.frozen = {str(f.source):file_sha(f.source)}
        f.session = f.root/'approval.local.json'
        f.approval.update(reference='P11', root=str(f.root), operation='receive-inbox', notice_purposes=PURPOSES,
            limits=dict(auth=12, identity=3, list=3, message=3, download=2, upload=0, send=0),
            inbox=dict(reference='P19-inbox-v1', formats=['.md','.txt','.pdf','.webarchive'], max_asset_bytes=4096, max_files=2))
        self.save_approval()

    def save_approval(self):
        f=self.f; f.approval['maximum_total']=sum(f.approval['limits'].values())
        f.session.write_text(encoded(f.approval)); f.digest=file_sha(f.session)

    def make(self):
        f=self.f
        c=PilotExecutionSession(f.root,f.digest,runner=f.runner,checked_source=f.checked_source,clock=lambda:f.now)
        self.addCleanup(c.close); return c

    def ready(self):
        c=self.make(); c.verify_isolation(); self.f.scan(c); return c

    def receive(self,c,scan='scan_synthetic',mid='om_synthetic'):
        return receive_new_file(c,scan,mid,self.f.flow)

    def scan_plan(self):
        f=self.f; path=f.root/'inbox-plan.local.json'
        path.write_text(encoded(dict(reference='P19-receive-inbox-v1',lookback_seconds=3600)))
        f.frozen[str(path)]=file_sha(path)
        return path

    def next_message(self,c,number,*,name='synthetic.md'):
        f=self.f; mid='om_synthetic'+str(number)
        f.message=dict(f.message,message_id=mid,body=dict(content=encoded(dict(file_key='file_synthetic'+str(number),file_name=name))))
        f.pages=[dict(items=[f.message],has_more=False)]
        scan='scan_next'+str(number)
        c.scan(scan,start=f.now-__import__('datetime').timedelta(hours=1),end=f.now)
        return self.receive(c,scan,mid)

    def test_new_bytes_without_sample_become_bound_original_and_replay_uses_no_download(self):
        c=self.ready(); first=self.receive(c); f=self.f
        self.assertEqual(first['original']['sha256'],sha(CONTENT))
        self.assertEqual(first['status']['original_status'],'accepted')
        self.assertEqual(json.loads(f.registry.entry(first['original']['entry_id'])['expected_json']),{})
        c.close(); c=self.make(); count=f.runner.call_count
        self.assertEqual(self.receive(c)['original'],first['original'])
        self.assertEqual(f.runner.call_count,count)
        self.assertEqual(f.flow.work_once()['processing_state'],'ready_candidate')

    def test_reupload_same_bytes_with_new_name_stops_before_second_conversion(self):
        c=self.ready(); self.receive(c); f=self.f
        f.flow.work_once(); second=self.next_message(c,2,name='renamed.md')
        f.flow.work_once(entry_id=second['original']['entry_id'])
        self.assertEqual(f.flow.status(second['original']['entry_id'])['processing_state'],'duplicate_needs_review')
        self.assertEqual(len(f.registry.rows('SELECT * FROM conversion_bindings')),1)

    def test_missing_parent_policy_or_wrong_operation_blocks_before_detail(self):
        f=self.f; f.approval['operation']='intake'; self.save_approval(); c=self.ready(); count=f.runner.call_count
        with self.assertRaises(ContractError): self.receive(c)
        self.assertEqual(f.runner.call_count,count)

    def test_truncated_scan_does_not_download_visible_subset(self):
        f=self.f; f.pages[0].update(has_more=True,page_token='more'); c=self.ready(); count=f.runner.call_count
        with self.assertRaises(ContractError):self.receive(c)
        self.assertEqual(f.runner.call_count,count)
        self.assertFalse(f.registry.rows('SELECT * FROM intake_entries'))

    def test_other_owner_and_group_are_never_admitted(self):
        f=self.f; f.pages[0]['items']=[dict(f.message,sender=dict(id='ou_other',id_type='open_id',sender_type='user'))]
        c=self.ready(); count=f.runner.call_count
        with self.assertRaises(ContractError): self.receive(c)
        self.assertEqual(f.runner.call_count,count)
        f.flow.scope=__import__('kb_custody_v21.pilot_preview',fromlist=['PilotScope']).PilotScope('cli_synthetic','oc_other','ou_synthetic')
        with self.assertRaises(ContractError): self.receive(c)

    def test_invalid_filename_post_deleted_edited_or_json_never_registers(self):
        for mode in ('path','format','post','deleted','updated','invalid-json'):
            with self.subTest(mode=mode):
                child=PilotInboxTests();child.setUp()
                try:
                    f=child.f
                    if mode in ('path','format'): f.message['body']['content']=encoded(dict(file_key='file_synthetic',file_name='../file.md' if mode=='path' else 'file.exe'))
                    elif mode=='post': f.message['msg_type']='post'
                    elif mode=='invalid-json':f.message['body']['content']='not JSON'
                    else:f.message[mode]=True
                    c=child.ready()
                    with self.assertRaises(ContractError):child.receive(c)
                    self.assertFalse(f.registry.rows('SELECT * FROM intake_entries'))
                    self.assertFalse(any(x.args[0]['kind']=='download' for x in f.runner.call_args_list))
                finally:child.doCleanups()

    def test_material_limit_includes_prior_received_files_and_prevents_extra_registration(self):
        f=self.f;f.approval['inbox']['max_files']=1;f.approval['limits']['download']=1;self.save_approval()
        c=self.ready();self.receive(c)
        with self.assertRaises(ContractError):self.next_message(c,2)
        self.assertEqual(len(f.registry.rows('SELECT * FROM intake_entries')),1)
        self.assertEqual(sum(x.args[0]['kind']=='download' for x in f.runner.call_args_list),1)

    def test_queue_full_blocks_download(self):
        c=self.ready();self.receive(c);f=self.f;f.flow.max_pending=1
        with self.assertRaises(ContractError):self.next_message(c,2)
        self.assertEqual(sum(x.args[0]['kind']=='download' for x in f.runner.call_args_list),1)

    def test_empty_or_oversize_transport_never_accepts_original(self):
        for raw in (b'', b'x'*4097):
            child=PilotInboxTests();child.setUp()
            try:
                child.f.binary=raw;c=child.ready()
                with self.assertRaises(ContractError):child.receive(c)
                self.assertFalse(child.f.registry.rows("SELECT * FROM staging_receipts WHERE status='accepted'"))
            finally:child.doCleanups()

    def test_unknown_download_prevents_restart_and_new_scan(self):
        c=self.ready();f=self.f
        def fail(control):
            if control['kind']=='download':raise OSError('synthetic interrupted transport')
            return f.respond(control)
        f.runner.side_effect=fail
        with self.assertRaises(OSError):self.receive(c)
        c.close();count=f.runner.call_count
        with self.assertRaises(ContractError):self.make()
        self.assertEqual(f.runner.call_count,count)

    def test_source_or_deadline_change_after_download_prevents_acceptance(self):
        for mode in ('source','deadline'):
            child=PilotInboxTests();child.setUp()
            try:
                f=child.f;c=child.ready()
                def change(control):
                    out=f.respond(control)
                    if control['kind']=='download':
                        if mode=='source':f.source.write_text('changed')
                        else:f.now=datetime.fromisoformat(f.approval['expires_at'])
                    return out
                f.runner.side_effect=change
                with self.assertRaises(ContractError):child.receive(c)
                self.assertFalse(f.registry.rows("SELECT * FROM staging_receipts WHERE status='accepted'"))
            finally:child.doCleanups()

    def test_tampered_cached_receipt_is_not_reused(self):
        c=self.ready();self.receive(c);f=self.f
        next(f.root.glob('*-message.response.local')).write_text('{}');count=f.runner.call_count
        with self.assertRaises(ContractError):self.receive(c)
        self.assertEqual(f.runner.call_count,count)

    def test_interrupt_after_successful_download_reuses_receipt_locally(self):
        c=self.ready();original=c._call
        def crash(*args,**kwargs):
            result=original(*args,**kwargs)
            if args[0]=='download':raise KeyboardInterrupt()
            return result
        c._call=crash
        with self.assertRaises(KeyboardInterrupt):self.receive(c)
        c.close();c=self.make();count=self.f.runner.call_count
        self.assertEqual(self.receive(c)['status']['original_status'],'accepted')
        self.assertEqual(self.f.runner.call_count,count)

    def test_bounded_scan_operator_receives_new_file_without_model_and_cannot_restart(self):
        path=self.scan_plan();c=self.make();gate=Mock();gate.check.return_value=dict(state='resources_ready')
        result=receive_inbox(c,self.f.flow,path,resources=gate)
        self.assertEqual(result['state'],'intake_finished')
        self.assertEqual(len(result['accepted']),1)
        self.assertFalse(result['model_started']);self.assertFalse(result['intake_active'])
        count=self.f.runner.call_count
        with self.assertRaises(ContractError):receive_inbox(c,self.f.flow,path,resources=gate)
        self.assertEqual(self.f.runner.call_count,count)

    def test_resource_pressure_pauses_before_platform_calls(self):
        path=self.scan_plan();c=self.make();gate=Mock();gate.check.return_value=dict(state='paused_resources')
        result=receive_inbox(c,self.f.flow,path,resources=gate)
        self.assertEqual(result['state'],'paused_resources');self.f.runner.assert_not_called()

    def test_truncated_or_oversize_batch_is_reported_without_any_download(self):
        for mode in ('truncated','batch'):
            child=PilotInboxTests();child.setUp()
            try:
                f=child.f;path=child.scan_plan();c=child.make();gate=Mock();gate.check.return_value=dict(state='resources_ready')
                if mode=='truncated':f.pages[0].update(has_more=True,page_token='more')
                else:f.pages[0]['items']=[dict(f.message,message_id='om_sample'+str(i)) for i in range(3)]
                result=receive_inbox(c,f.flow,path,resources=gate)
                self.assertEqual(result['state'],'incomplete_scan' if mode=='truncated' else 'batch_limit_exceeded')
                self.assertFalse(result['accepted'])
                self.assertFalse(any(x.args[0]['kind'] in ('message','download') for x in f.runner.call_args_list))
            finally:child.doCleanups()

    def test_explicit_operator_uses_no_text_dependencies(self):
        import pilot_execute
        f=self.f;path=self.scan_plan()
        executable=f.root/'synthetic-executable';executable.write_text('offline only')
        f.approval.update(run_plan=str(path),cli_guard=str(executable),runtime_config=dict(f.runtime.config,
            runtime_relative=str(f.runtime.root.relative_to(f.runtime.worktree))))
        self.save_approval()
        with patch.object(pilot_execute,'execution_inputs',return_value=(f.runtime.worktree,f.approval,{str(executable):file_sha(executable)})), \
                patch.object(pilot_execute,'CliProcess',return_value=f.runner), \
                patch.object(pilot_execute,'CodexTextWire') as text_wire, \
                patch.object(pilot_execute,'receive_inbox',return_value=dict(state='synthetic_operator_composition')) as route:
            result=pilot_execute.execute(f.root,f.digest,'receive-inbox')
            self.assertEqual(result['status'],'COMPLETED_BOUNDED_ACTION')
            route.assert_called_once();text_wire.assert_not_called()

    def test_previous_registered_but_unaccepted_file_cannot_borrow_new_permission(self):
        f=self.f
        f.flow.prepare(dict(appId='cli_synthetic',chatId='oc_synthetic',senderId='ou_synthetic',chatType='group',
            messageId='om_synthetic',resources=[dict(type='file',fileKey='file_synthetic',fileName='synthetic.md')]))
        c=self.ready()
        with self.assertRaisesRegex(ContractError,'incomplete custody'):self.receive(c)
        self.assertFalse(any(x.args[0]['kind']=='download' for x in f.runner.call_args_list))

    def test_already_accepted_preselected_sample_reuses_exact_original(self):
        f=self.f;f.sample=f.root/'synthetic.md';f.sample.write_bytes(CONTENT);f.frozen[str(f.sample)]=sha(CONTENT)
        c=self.ready();before=c.receive_file('scan_synthetic','om_synthetic',f.flow,sample_path=f.sample)
        count=f.runner.call_count;after=self.receive(c)
        self.assertEqual(before['receipt']['entry_id'],after['receipt']['entry_id'])
        self.assertEqual(f.runner.call_count,count)
        self.assertEqual(json.loads(f.registry.entry(after['receipt']['entry_id'])['expected_json'])['sha256'],sha(CONTENT))
