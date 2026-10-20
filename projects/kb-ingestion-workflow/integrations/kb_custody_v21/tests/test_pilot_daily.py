"""Finite full daily route over actual local components and synthetic transports."""
import copy
from datetime import timedelta
import json
import unittest
from unittest.mock import Mock,patch

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.f01 import encoded
from kb_custody_v21.f05 import file_sha
from kb_custody_v21.pilot_daily import PilotDailyRun,daily_limits
from kb_custody_v21.pilot_resources import ResourceSample
from test_pilot_delivery import delivery_response
from test_pilot_decision import owner_reply
from pilot_pipeline_fixture import ready
import test_pilot_inbox_guide as guide_fixture
import pilot_execute


class DailyTests(unittest.TestCase):
    def setUp(self):
        self.g=guide_fixture.InboxGuideTests();self.g.setUp();self.addCleanup(self.g.doCleanups)
        self.f=self.g.f;self.i=self.g.i;self.intake=self.g.intake;self.registry=self.g.registry
        self.f.approval.update(operation='inbox-daily',limits=daily_limits(2),guide_plan=str(self.g.path))
        self.f.approval['inbox']['reference']='P22-daily-inbox-v1'
        self.scan_path=self.f.root/'daily-inbox.local.json'
        self.scan_path.write_text(encoded(dict(reference='P20-receive-inbox-v2',lookback_seconds=3600,
                                              legacy_catalog=self.g.e.catalog)))
        self.path=self.f.root/'daily.local.json'
        self.plan=dict(reference='P22-daily-run-v1',inbox_plan_path=str(self.scan_path),
                       guide_plan_path=str(self.g.path),max_owner_scans=2,poll_interval_seconds=30)
        self.save()
        self.reply=owner_reply();self.reply_pages=None;self.waits=[]
        self.failure=None
        def hook(c,v):
            if c['kind']=='send':
                payload=json.loads(json.loads(c['body'])['content'])['text']
                v.clear();v.update(code=0,data=dict(message_id='om_status_'+c['key'],chat_id='oc_synthetic',
                    msg_type='text',deleted=False,updated=False,
                    sender=dict(id='ou_synthetic_bot',id_type='open_id',sender_type='app'),
                    body=dict(content=encoded(dict(text=payload)))))
        self.f.hook=hook
        def transport(c):
            if self.failure:self.failure(c)
            if c.get('preview'):
                out=delivery_response(c)
                if c['kind']=='download':
                    self.f.message=copy.deepcopy(self.reply)
                    self.f.pages=copy.deepcopy(self.reply_pages if self.reply_pages is not None else
                        [dict(items=[self.reply],has_more=False)])
                return out
            out=self.f.respond(c);out['mode']=c['mode'];return out
        self.f.runner.side_effect=transport

    def save(self):
        self.path.write_text(encoded(self.plan))
        for p in (self.path,self.scan_path):self.f.frozen[str(p)]=file_sha(p)
        self.i.save_approval()

    def wait(self,seconds):
        self.waits.append(seconds);self.f.now+=timedelta(seconds=seconds)

    def make(self):
        self.session=self.i.make()
        self.run=PilotDailyRun(self.session,self.intake,self.path,wire=self.g.wire,
            checked_ready=lambda:True,resources=ready(self.registry),wait=self.wait)
        self.addCleanup(self.run.close);return self.run

    def test_entire_new_file_flow_publishes_exact_pdf_and_delivers_notice_once(self):
        result=self.make().run()
        self.assertEqual(result['state'],'published');self.assertTrue(result['published'])
        self.assertEqual(result['notification']['delivery_state'],'delivered')
        self.assertEqual(result['pause_notice']['delivery_state'],'delivered')
        self.assertEqual(self.g.wire.calls,1);self.assertEqual(self.waits,[])
        self.assertFalse(result['waiting_active']);self.assertFalse(result['service_admission_open'])
        counts={k:sum(e.get('type')=='intent' and e.get('kind')==k for e in self.session.http_events()) for k in daily_limits(2)}
        self.assertEqual(counts,dict(auth=17,identity=3,list=2,message=2,download=2,upload=1,send=7))
        from pathlib import Path
        published=Path(result['path'])/'发布记录/已确认入库预览.pdf'
        received=[p for p in self.f.root.glob('*-download.response.local') if p.read_bytes().startswith(b'%PDF')]
        self.assertEqual(len(received),1);self.assertEqual(published.read_bytes(),received[0].read_bytes())
        count=self.f.runner.call_count
        with self.assertRaises(ContractError):self.run.run()
        self.assertEqual(self.f.runner.call_count,count);self.assertEqual(self.g.wire.calls,1)
        # In-memory and persisted outcome must agree even after adding pause data.
        events=[json.loads(line) for line in self.session.path.read_text().splitlines()]
        saved=[e['value'] for e in events if e.get('binding')==self.run.prefix+'outcome'][0]
        self.assertEqual(saved,self.session.value(self.run.prefix+'outcome'))

    def test_finite_empty_owner_scans_wait_once_and_keep_unpublished_candidate(self):
        self.reply_pages=[dict(items=[],has_more=False),dict(items=[],has_more=False)]
        result=self.make().run()
        self.assertEqual(result['state'],'awaiting_owner_decision')
        self.assertEqual(result['scans_completed'],2);self.assertEqual(self.waits,[30])
        self.assertEqual(result['reason'],'scan_limit_reached')
        self.assertFalse(self.registry.rows('SELECT * FROM publication_confirmations'))
        self.assertEqual(self.g.wire.calls,1)

    def test_owner_can_defer_without_publishing_or_final_success_notice(self):
        self.reply=owner_reply('暂不入库');result=self.make().run()
        self.assertEqual(result['state'],'defer');self.assertFalse(result['published'])
        self.assertNotIn('notification',result)
        self.assertFalse(self.registry.rows('SELECT * FROM publication_confirmations'))

    def test_old_catalog_match_stops_before_conversion_or_model_with_visible_status(self):
        self.g.e.add_plan('synthetic.md',guide_fixture.sha(guide_fixture.inbox_fixture.CONTENT))
        result=self.make().run()
        self.assertEqual(result['state'],'review_existing');self.assertEqual(self.g.wire.calls,0)
        self.assertFalse(self.registry.rows('SELECT * FROM conversion_bindings'))
        self.assertEqual(result['pause_notice']['delivery_state'],'delivered')
        self.assertIn('旧知识库',self.session.value(self.run.prefix+'status-plan')['payload'])

    def test_missing_material_and_multiple_files_never_download(self):
        self.f.pages=[dict(items=[],has_more=False)]
        result=self.make().run();self.assertEqual(result['state'],'no_material')
        self.assertFalse(self.registry.rows('SELECT * FROM intake_entries'));self.assertEqual(self.g.wire.calls,0)

    def test_multiple_native_files_stop_before_selecting_arbitrary_one(self):
        self.f.pages[0]['items'].append(dict(self.f.message,message_id='om_second'))
        result=self.make().run();self.assertEqual(result['state'],'batch_limit_exceeded')
        self.assertFalse(self.registry.rows('SELECT * FROM intake_entries'));self.assertEqual(self.g.wire.calls,0)

    def test_incomplete_intake_scan_reports_limit_without_downloading_subset(self):
        self.f.pages[0].update(has_more=True,page_token='more')
        result=self.make().run();self.assertEqual(result['state'],'incomplete_scan')
        self.assertFalse(self.registry.rows('SELECT * FROM intake_entries'));self.assertEqual(self.g.wire.calls,0)

    def test_wrong_preview_reply_is_not_used_as_confirmation(self):
        self.reply['parent_id']='om_other'
        self.reply_pages=[dict(items=[self.reply],has_more=False),dict(items=[self.reply],has_more=False)]
        result=self.make().run();self.assertEqual(result['state'],'awaiting_owner_decision')
        self.assertFalse(self.registry.rows('SELECT * FROM publication_confirmations'))

    def test_conflicting_owner_replies_stop_without_publication(self):
        self.reply_pages=[dict(items=[self.reply,dict(owner_reply('暂不入库'),message_id='om_other')],has_more=False)]
        with self.assertRaisesRegex(ContractError,'conflicting'):self.make().run()
        self.assertFalse(self.registry.rows('SELECT * FROM publication_confirmations'))
        self.assertEqual(self.run.lifecycle.status()['state'],'interrupted')

    def test_body_over_profile_limit_is_saved_but_not_sent_to_model(self):
        self.g.plan['max_characters']=1;self.g.save();self.save()
        result=self.make().run();self.assertEqual(result['state'],'material_needs_review')
        self.assertEqual(self.g.wire.calls,0);self.assertEqual(len(self.registry.rows('SELECT * FROM intake_entries')),1)

    def test_unknown_model_result_stops_without_retry_or_extra_notice(self):
        self.g.wire.mutate=lambda _: (_ for _ in ()).throw(OSError('synthetic timeout'))
        with self.assertRaises(ContractError):self.make().run()
        count=self.f.runner.call_count
        with self.assertRaises(ContractError):self.run.run()
        self.assertEqual(self.f.runner.call_count,count);self.assertEqual(self.g.wire.calls,1)
        self.assertEqual(self.run.lifecycle.status()['state'],'interrupted')

    def test_missing_daily_permission_or_wrong_budget_blocks_before_transport(self):
        self.f.approval['limits']['send']-=1;self.save()
        with self.assertRaises(ContractError):self.make()
        self.f.runner.assert_not_called()

    def test_daily_cannot_borrow_old_receipt_permission(self):
        self.f.approval['operation']='receive-inbox';self.save()
        with self.assertRaises(ContractError):self.make()
        self.f.runner.assert_not_called()

    def test_resource_hold_before_start_uses_no_platform_or_model(self):
        self.make()
        with patch.object(self.run.pipeline.resources,'check',return_value=dict(state='paused_resources')):
            result=self.run.run()
        self.assertEqual(result['state'],'paused_resources')
        self.f.runner.assert_not_called();self.assertEqual(self.g.wire.calls,0)

    def test_source_change_during_owner_wait_prevents_confirmation(self):
        self.reply_pages=[dict(items=[],has_more=False),dict(items=[self.reply],has_more=False)]
        run=self.make()
        def changed(seconds):
            self.wait(seconds);self.f.source.write_text('changed')
        run.wait=changed
        with self.assertRaises(ContractError):run.run()
        self.assertFalse(self.registry.rows('SELECT * FROM publication_confirmations'))

    def test_operator_routes_daily_with_required_dependencies_and_same_components(self):
        root=self.f.root
        executable=root/'synthetic-executable';executable.write_bytes(b'fixture');executable.chmod(0o700)
        self.f.frozen[str(executable)]=file_sha(executable)
        manifest_path=root/'daily-manifest.local.json'
        manifest=dict(worktree=str(self.intake.runtime.worktree),source_commit='synthetic-commit',
            source_snapshot=dict(files={},sha256='synthetic-snapshot'),approved_files=self.f.frozen)
        manifest_path.write_text(encoded(manifest))
        self.f.approval.update(source_manifest=str(manifest_path),source_manifest_sha256=file_sha(manifest_path),
            run_plan=str(self.path),runtime_config=dict(self.intake.runtime.config,
                runtime_relative=str(self.intake.runtime.root.relative_to(self.intake.runtime.worktree))))
        for key in ('cli_guard','text_node','text_script','codex_binary'):self.f.approval[key]=str(executable)
        self.i.save_approval()
        process=Mock(side_effect=self.f.runner)
        with patch.object(pilot_execute,'checked_source'),patch.object(pilot_execute,'CliProcess',return_value=process),patch.object(
                pilot_execute,'CodexTextWire',return_value=self.g.wire),patch(
                'kb_custody_v21.pilot_resources.macos_sample',return_value=ResourceSample(60,'normal',20*1024**3)):
            result=pilot_execute.execute(root,self.f.digest,'inbox-daily')
        self.assertEqual(result['status'],'COMPLETED_BOUNDED_ACTION',result)
        self.assertEqual(result['result']['state'],'published')
        self.assertEqual(self.g.wire.calls,1)

    def test_publication_notice_failure_retains_confirmed_publication_truth(self):
        def fail(c):
            if c.get('notice'):
                from pathlib import Path
                plan=json.loads(Path(c['notice']['plan_path']).read_bytes())
                if plan['purpose']=='publication':raise OSError('synthetic notice timeout')
        self.failure=fail
        with self.assertRaises(ContractError):self.make().run()
        self.assertEqual(self.run.saved_observation()['published'],True)
        self.assertEqual(self.g.wire.calls,1)
        self.assertEqual(len(self.registry.rows("SELECT * FROM publications_v21 WHERE state='published'")),1)
        count=self.f.runner.call_count
        with self.assertRaises(ContractError):self.run.run()
        self.assertEqual(count,self.f.runner.call_count)

    def test_fast_or_expired_wait_cannot_create_extra_scans(self):
        self.reply_pages=[dict(items=[],has_more=False)]
        run=self.make();run.wait=lambda seconds:None
        with self.assertRaisesRegex(ContractError,'planned interval'):run.run()
        self.assertFalse(self.registry.rows('SELECT * FROM publication_confirmations'))

    def test_owner_detail_version_change_cannot_confirm_saved_preview(self):
        observed=copy.deepcopy(self.reply)
        self.reply_pages=[dict(items=[observed],has_more=False)]
        self.reply=owner_reply('暂不入库')
        with self.assertRaisesRegex(ContractError,'changed between'):self.make().run()
        self.assertFalse(self.registry.rows('SELECT * FROM publication_confirmations'))
