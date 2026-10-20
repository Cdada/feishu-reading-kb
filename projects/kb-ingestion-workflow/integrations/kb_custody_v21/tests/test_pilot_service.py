"""Continuous finite intake and exact continuation over real local components."""
import copy
from datetime import timedelta
import json
from pathlib import Path
import unittest
from unittest.mock import Mock,patch

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.notifications import DeliveryUnknown
from kb_custody_v21.f01 import encoded,sha
from kb_custody_v21.f05 import file_sha
from kb_custody_v21.pilot_service import PilotServiceRun,service_limits
from kb_custody_v21.pilot_resources import ResourceSample
from pilot_pipeline_fixture import ready
from test_pilot_decision import owner_reply
from test_pilot_download import download_response
import test_pilot_inbox_guide as fixture
import pilot_execute


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.g=fixture.InboxGuideTests();self.g.setUp();self.addCleanup(self.g.doCleanups)
        self.f=self.g.f;self.i=self.g.i;self.intake=self.g.intake;self.registry=self.g.registry
        self.f.approval.update(operation='inbox-service',guide_plan=str(self.g.path))
        self.f.approval['inbox'].update(reference='P23-service-inbox-v1',max_files=2)
        self.f.approval['new_material_guide'].update(reference='P23-service-guide-v1',max_business_turns=2)
        self.plan=dict(reference='P25-service-run-v1',guide_plan_path=str(self.g.path),
            legacy_catalog=self.g.e.catalog,lookback_seconds=3600,max_scans=3,
            poll_interval_seconds=30,previous_previews=[],previous_materials=[])
        self.path=self.f.root/'service.local.json';self.save()
        first=copy.deepcopy(self.f.message)
        second=copy.deepcopy(first);second.update(message_id='om_second',body=dict(
            content=encoded(dict(file_key='file_second',file_name='second.md'))))
        self.messages={first['message_id']:first,second['message_id']:second}
        self.binaries={'file_synthetic':fixture.inbox_fixture.CONTENT,
                       'file_second':'# 合成第二份\n\n第二份独立资料，含有不同内容。\n'.encode()}
        self.f.pages=[dict(items=[first],has_more=False),dict(items=[second],has_more=False),dict(items=[],has_more=False)]
        self.waits=[];self.deliveries=[];self.failure=lambda c:None;self.after_wait=lambda:None
        def hook(c,v):
            if c['kind']=='send':
                payload=json.loads(json.loads(c['body'])['content'])['text']
                v.clear();v.update(code=0,data=dict(message_id='om_status_'+c['key'],chat_id='oc_synthetic',
                    msg_type='text',deleted=False,updated=False,sender=dict(
                        id='ou_synthetic_bot',id_type='open_id',sender_type='app'),body=dict(content=encoded(dict(text=payload)))))
        self.f.hook=hook
        def transport(c):
            self.failure(c)
            if c.get('preview'):return self.preview(c)
            if c['kind']=='message':self.f.message=self.messages[c['path'].rsplit('/',1)[1]]
            if c['kind']=='download':return download_response(c,self.binaries[c['download']['file_key']])
            result=self.f.respond(c);result['mode']=c['mode'];return result
        self.f.runner.side_effect=transport

    def save(self):
        self.path.write_text(encoded(self.plan));self.f.frozen[str(self.path)]=file_sha(self.path)
        self.f.approval['run_plan']=str(self.path)
        self.f.approval['limits']=service_limits(self.f.approval['inbox']['max_files'],self.plan['max_scans'],len(self.plan['previous_previews']))
        self.i.save_approval()

    def preview(self,c):
        p=c['preview'];kind=c['kind'];key=c['key'];mid='om_'+p['publication_id']
        if kind=='upload':value=dict(code=0,data=dict(file_key='file_upload'))
        elif kind=='send':
            value=dict(code=0,data=dict(message_id=mid,chat_id='oc_synthetic',msg_type='file',deleted=False,
                updated=False,sender=dict(id='cli_synthetic',id_type='app_id',sender_type='app'),
                body=dict(content=encoded(dict(file_key='file_message',file_name=p['filename'])))))
        else:
            value=None;self.deliveries.append(dict(publication_id=p['publication_id'],message_id=mid))
        raw=encoded(value).encode() if value else Path(p['pdf_path']).read_bytes()
        (self.f.root/(key+'.response.local')).write_bytes(raw)
        with (self.f.root/'http.local.jsonl').open('a') as stream:
            for category in ('auth',kind):
                stream.write(encoded(dict(type='intent',key=key+':'+category,kind=category))+'\n')
                stream.write(encoded(dict(type='complete',key=key+':'+category,kind=category,http_status=200,
                    **({'response_sha256':sha(raw)} if category!='auth' else {})))+'\n')
        return dict(ok=True,mode=c['mode'])

    def wait(self,seconds):
        self.waits.append(seconds);self.f.now+=timedelta(seconds=seconds);self.after_wait()

    def make(self):
        self.session=self.i.make()
        self.run=PilotServiceRun(self.session,self.intake,self.path,wire=self.g.wire,
            checked_ready=lambda:True,resources=ready(self.registry),wait=self.wait)
        self.addCleanup(self.run.close);return self.run

    def counts(self):
        return {k:sum(e.get('type')=='intent' and e.get('kind')==k for e in self.session.http_events())
                for k in self.session.limits}

    def replies(self):
        values=[]
        for i,d in enumerate(self.deliveries):
            reply=dict(owner_reply(),message_id='om_reply_'+str(i),parent_id=d['message_id'])
            self.messages[reply['message_id']]=reply;values.append(reply)
        return values

    def test_second_material_runs_while_first_preview_waits_without_repeating_model(self):
        result=self.make().run()
        self.assertEqual([m['state'] for m in result['materials']],['awaiting_owner_decision']*2)
        self.assertEqual(self.g.wire.calls,2);self.assertEqual(len(self.deliveries),2)
        self.assertEqual(self.counts()['download'],4);self.assertEqual(self.counts()['upload'],2)
        self.assertFalse(self.registry.rows('SELECT * FROM publication_confirmations'))
        self.assertEqual(len(self.g.children()),2)
        self.assertFalse(result['service_admission_open']);self.assertEqual(self.waits,[30,30])

    def test_two_exact_replies_publish_each_correct_preview_and_send_notices(self):
        def reply_after_second():
            if len(self.deliveries)==2:self.f.pages=[dict(items=self.replies(),has_more=False)]
        self.after_wait=reply_after_second
        result=self.make().run()
        self.assertTrue(all(m['published'] for m in result['materials']))
        self.assertTrue(all(m['notification']['delivery_state']=='delivered' for m in result['materials']))
        self.assertEqual(len(self.registry.rows('SELECT * FROM publication_confirmations')),2)
        self.assertEqual(self.g.wire.calls,2)
        for m in result['materials']:
            pdf=Path(m['decision']['path'])/'发布记录/已确认入库预览.pdf'
            self.assertEqual(file_sha(pdf),m['delivery']['pdf_sha256'])
        for k,v in self.counts().items():self.assertLessEqual(v,self.session.limits[k])

    def test_empty_scan_does_not_end_service_before_later_material(self):
        self.f.pages.insert(0,dict(items=[],has_more=False));self.f.pages.pop()
        result=self.make().run();self.assertEqual(len(result['materials']),2)
        self.assertEqual(self.g.wire.calls,2)

    def test_overlap_deduplicates_original_and_preview_without_second_calls(self):
        self.f.pages=[dict(items=[self.messages['om_synthetic']],has_more=False)]*3
        result=self.make().run();self.assertEqual(len(result['materials']),1)
        self.assertEqual(self.g.wire.calls,1);self.assertEqual(self.counts()['download'],2)

    def test_old_catalog_hold_does_not_block_next_independent_material(self):
        self.g.e.add_plan('synthetic.md',sha(self.binaries['file_synthetic']))
        result=self.make().run()
        self.assertEqual(result['materials'][0]['state'],'needs_review')
        self.assertEqual(result['materials'][0]['next_action'],'review_existing')
        self.assertEqual(result['materials'][1]['state'],'awaiting_owner_decision')
        self.assertEqual(self.g.wire.calls,1)

    def test_failed_conversion_preserved_then_next_material_processed(self):
        self.messages['om_synthetic']['body']['content']=encoded(dict(file_key='file_synthetic',file_name='broken.pdf'))
        self.f.pages[0]['items']=[self.messages['om_synthetic']]
        result=self.make().run()
        self.assertEqual(result['materials'][0]['state'],'needs_review')
        self.assertEqual(result['materials'][0]['next_action'],'technical_review_existing_conversion')
        self.assertEqual(result['materials'][1]['state'],'awaiting_owner_decision')
        self.assertEqual(self.g.wire.calls,1)

    def test_temporary_resource_hold_keeps_saved_conversion_and_resumes(self):
        self.f.pages=[self.f.pages[0],dict(items=[],has_more=False),dict(items=[],has_more=False)]
        run=self.make();calls=0;original=run.guide;original_call=type(original).__call__
        def delayed(*args,**kwargs):
            nonlocal calls
            calls+=1
            return dict(state='paused_resources') if calls==1 else original_call(original,*args,**kwargs)
        with patch.object(type(original),'__call__',lambda _self,*a,**k:delayed(*a,**k)):
            result=run.run()
        self.assertEqual(result['materials'][0]['state'],'awaiting_owner_decision')
        self.assertEqual(len(self.registry.rows('SELECT * FROM conversion_bindings')),1)
        self.assertEqual(self.g.wire.calls,1);self.assertEqual(calls,2)

    def test_source_change_during_wait_stops_before_next_external_request(self):
        self.after_wait=lambda:self.f.source.write_text('changed')
        run=self.make()
        with self.assertRaises(ContractError):run.run()
        self.assertEqual(self.g.wire.calls,1);self.assertEqual(self.counts()['list'],1)

    def test_unknown_model_result_stops_without_processing_next_material(self):
        self.g.wire.mutate=lambda _:(_ for _ in ()).throw(OSError('synthetic unknown'))
        run=self.make()
        with self.assertRaises(ContractError):run.run()
        n=self.f.runner.call_count
        with self.assertRaises(ContractError):run.run()
        self.assertEqual(n,self.f.runner.call_count);self.assertEqual(self.g.wire.calls,1)

    def test_incomplete_scan_keeps_visible_materials_undownloaded(self):
        self.f.pages[0].update(has_more=True,page_token='next')
        result=self.make().run();self.assertEqual(result['state'],'incomplete_scan')
        self.assertEqual(self.counts()['download'],0);self.assertEqual(self.g.wire.calls,0)

    def test_over_limit_batch_never_picks_an_arbitrary_subset(self):
        self.f.pages[0]['items']+= [self.messages['om_second'],dict(self.messages['om_second'],message_id='om_third')]
        result=self.make().run();self.assertEqual(result['state'],'material_limit_reached')
        self.assertEqual(self.counts()['download'],0)

    def test_foreign_owner_and_wrong_preview_never_publish(self):
        def changed():
            replies=self.replies()
            for r in replies:r['sender']['id']='ou_other'
            self.f.pages=[dict(items=replies,has_more=False)]*2
        self.after_wait=changed
        result=self.make().run();self.assertFalse(any(m['published'] for m in result['materials']))
        self.assertFalse(self.registry.rows('SELECT * FROM publication_confirmations'))

    def test_conflicting_replies_stop_before_publication(self):
        def changed():
            replies=self.replies();replies.append(dict(replies[0],message_id='om_conflict'))
            self.f.pages=[dict(items=replies,has_more=False)]
        self.after_wait=changed
        with self.assertRaisesRegex(ContractError,'conflicting'):self.make().run()
        self.assertFalse(self.registry.rows('SELECT * FROM publication_confirmations'))

    def test_failure_after_publication_preserves_published_fact(self):
        def changed():self.f.pages=[dict(items=self.replies(),has_more=False)]*2
        self.after_wait=changed
        def fail(c):
            if c.get('notice') and json.loads(Path(c['notice']['plan_path']).read_bytes())['purpose']=='publication':
                raise OSError('synthetic notification unknown')
        self.failure=fail;run=self.make()
        with self.assertRaises(ContractError):run.run()
        self.assertTrue(run.saved_observation()['materials'][0]['published'])
        self.assertEqual(len(self.registry.rows("SELECT * FROM publications_v21 WHERE state='published'")),1)

    def test_budget_or_delegation_mismatch_rejected_before_transport(self):
        self.f.approval['new_material_guide']['max_business_turns']=3;self.i.save_approval()
        with self.assertRaises(ContractError):self.make()
        self.f.runner.assert_not_called()

    def test_service_cannot_borrow_daily_permission(self):
        self.f.approval['operation']='inbox-daily';self.i.save_approval()
        with self.assertRaises(ContractError):self.make()
        self.f.runner.assert_not_called()

    def malformed_reply(self):
        reply=dict(owner_reply('@_user_1 @确认入库'),message_id='om_bad_reply',
            parent_id=self.deliveries[0]['message_id'],
            mentions=[dict(key='@_user_1',id='ou_synthetic_bot',id_type='open_id')])
        self.messages[reply['message_id']]=reply
        return reply

    def correction_notices(self):
        return [e for e in self.session.events if ':notice:correction:' in e.get('binding','')]

    def test_malformed_reply_allows_second_material_and_later_exact_confirmations(self):
        bad=[]
        def changed():
            if not bad:
                bad.append(self.malformed_reply())
                self.f.pages=[dict(items=bad+[self.messages['om_second']],has_more=False)]
            else:
                self.f.pages=[dict(items=bad+self.replies(),has_more=False)]
        self.after_wait=changed
        result=self.make().run()
        self.assertEqual(result['state'],'scan_limit_reached')
        self.assertTrue(all(m['published'] for m in result['materials']))
        self.assertEqual(self.g.wire.calls,2);self.assertEqual(len(self.deliveries),2)
        self.assertEqual(len(self.correction_notices()),1)
        self.assertNotIn('owner_reply_issue',result['materials'][0])
        self.assertEqual(result['materials'][0]['correction_notice']['delivery_state'],'delivered')
        for m in result['materials']:
            self.assertEqual(file_sha(Path(m['decision']['path'])/'发布记录/已确认入库预览.pdf'),m['delivery']['pdf_sha256'])
        self.assertEqual(len(self.registry.rows('SELECT * FROM publication_confirmations')),2)
        for k,v in self.counts().items():self.assertLessEqual(v,self.session.limits[k])

    def test_repeated_malformed_reply_gets_one_notice_and_stays_unpublished(self):
        def changed():self.f.pages=[dict(items=[self.malformed_reply()],has_more=False)]
        self.after_wait=changed
        result=self.make().run();item=result['materials'][0]
        self.assertEqual(item['state'],'awaiting_owner_decision')
        self.assertEqual(item['owner_reply_issue']['reason'],'unrecognized_owner_reply')
        self.assertEqual(item['owner_reply_issue']['message_ids'],['om_bad_reply'])
        self.assertEqual(len(self.correction_notices()),1)
        self.assertEqual(self.g.wire.calls,1);self.assertEqual(self.counts()['message'],1)
        self.assertFalse(self.registry.rows('SELECT * FROM publication_confirmations'))
        self.assertEqual(result['pause_notice']['delivery_state'],'delivered')

    def test_valid_reply_in_same_page_as_bad_text_needs_no_correction_notice(self):
        self.after_wait=lambda:setattr(self.f,'pages',[dict(items=[self.malformed_reply()]+self.replies(),has_more=False)])
        result=self.make().run()
        self.assertTrue(result['materials'][0]['published']);self.assertFalse(self.correction_notices())
        self.assertEqual(self.g.wire.calls,1)

    def test_unsupported_owner_text_is_not_an_implicit_confirmation(self):
        def changed():
            reply=self.replies()[0];reply['body']=dict(content=encoded(dict(text='好的')))
            self.f.pages=[dict(items=[reply],has_more=False)]
        self.after_wait=changed
        result=self.make().run()
        self.assertFalse(result['materials'][0]['published']);self.assertEqual(len(self.correction_notices()),1)
        self.assertFalse(self.registry.rows('SELECT * FROM pilot_preview_decisions'))

    def test_bad_reply_to_another_preview_does_not_prompt_or_publish(self):
        def changed():
            reply=self.malformed_reply();reply['parent_id']='om_another_preview'
            self.f.pages=[dict(items=[reply],has_more=False)]
        self.after_wait=changed
        result=self.make().run()
        self.assertFalse(result['materials'][0]['published']);self.assertFalse(self.correction_notices())

    def test_changed_detail_after_valid_list_still_stops(self):
        def changed():
            replies=copy.deepcopy(self.replies())
            self.f.pages=[dict(items=replies,has_more=False)]
            self.messages[replies[0]['message_id']]['body']=dict(content=encoded(dict(text='暂不入库')))
        self.after_wait=changed
        with self.assertRaisesRegex(ContractError,'changed between list and detail'):self.make().run()
        self.assertFalse(self.registry.rows('SELECT * FROM publication_confirmations'))
        self.assertFalse(self.correction_notices())

    def test_unknown_correction_notice_stops_without_retrying_or_publishing(self):
        self.after_wait=lambda:setattr(self.f,'pages',[dict(items=[self.malformed_reply()],has_more=False)])
        def fail(c):
            if c.get('notice') and self.correction_notices():
                raise OSError('synthetic correction notice unknown')
        self.failure=fail;run=self.make()
        with self.assertRaises(DeliveryUnknown):run.run()
        n=self.f.runner.call_count
        with self.assertRaises(ContractError):run.run()
        self.assertEqual(self.f.runner.call_count,n)
        self.assertFalse(run.saved_observation()['materials'][0]['published'])
        self.assertFalse(self.registry.rows('SELECT * FROM publication_confirmations'))

    def test_old_service_plan_cannot_inherit_correction_notice_permission(self):
        self.plan['reference']='P23-service-run-v1';self.save()
        with self.assertRaisesRegex(ContractError,'exact frozen'):self.make()
        self.f.runner.assert_not_called()

    def test_explicit_service_budget_never_exceeds_physical_ceiling(self):
        self.assertEqual(sum(service_limits(2,24).values()),100)
        with self.assertRaises(ContractError):service_limits(2,25)
        with self.assertRaises(ContractError):service_limits(3,15)
        with self.assertRaises(ContractError):service_limits(2,2,2)

    def test_wrong_wait_cannot_accelerate_polls(self):
        run=self.make();run.wait=lambda _:None
        with self.assertRaisesRegex(ContractError,'planned interval'):run.run()
        self.assertEqual(self.counts()['list'],1)

    def test_operator_exposes_service_without_reopening_other_operations(self):
        executable=self.f.root/'synthetic-executable';executable.write_bytes(b'fixture');executable.chmod(0o700)
        self.f.frozen[str(executable)]=file_sha(executable)
        manifest_path=self.f.root/'manifest.local.json'
        manifest=dict(worktree=str(self.intake.runtime.worktree),source_commit='synthetic',
            source_snapshot=dict(files={},sha256='synthetic'),approved_files=self.f.frozen)
        manifest_path.write_text(encoded(manifest))
        self.f.approval.update(source_manifest=str(manifest_path),source_manifest_sha256=file_sha(manifest_path),
            run_plan=str(self.path),runtime_config=dict(self.intake.runtime.config,
                runtime_relative=str(self.intake.runtime.root.relative_to(self.intake.runtime.worktree))))
        for key in ('cli_guard','text_node','text_script','codex_binary'):self.f.approval[key]=str(executable)
        self.i.save_approval();process=Mock(side_effect=self.f.runner)
        class FastService(PilotServiceRun):
            def __init__(inner,*a,**k):
                super().__init__(*a,**k);inner.session.clock=lambda:self.f.now;inner.wait=self.wait
        with patch.object(pilot_execute,'checked_source'),patch.object(pilot_execute,'CliProcess',return_value=process),patch.object(
                pilot_execute,'CodexTextWire',return_value=self.g.wire),patch.object(pilot_execute,'PilotServiceRun',FastService),patch(
                'kb_custody_v21.pilot_resources.macos_sample',return_value=ResourceSample(60,'normal',20*1024**3)):
            result=pilot_execute.execute(self.f.root,self.f.digest,'inbox-service')
        self.assertEqual(result['status'],'COMPLETED_BOUNDED_ACTION',result)
        self.assertEqual(len(result['result']['materials']),2);self.assertEqual(self.g.wire.calls,2)


    def change_window(self):
        old=self.f.root
        self.f.root=old.parent/'next-window'/'p11-requests';self.f.root.mkdir(parents=True)
        self.f.session=self.f.root/'approval.local.json';self.f.approval['root']=str(self.f.root)
        self.path=self.f.root/'service.local.json'

    def test_new_window_reuses_accepted_original_and_finished_conversion(self):
        from kb_custody_v21.pilot_service import continuation_material
        session=self.i.ready();received=self.i.receive(session);eid=received['original']['entry_id']
        self.g.pipeline.convert_once(entry_id=eid);session.close()
        binding=continuation_material(self.intake,eid)
        original=self.intake.runtime.root/self.registry.entry(eid)['relative_path']
        self.f.frozen[str(original)]=file_sha(original)
        self.change_window();self.plan['previous_materials']=[binding];self.save()
        self.f.pages=[dict(items=[],has_more=False),dict(items=[self.messages['om_second']],has_more=False),
                      dict(items=[],has_more=False)]
        result=self.make().run()
        self.assertEqual([m['state'] for m in result['materials']],['awaiting_owner_decision']*2)
        self.assertEqual(result['materials'][0]['entry_id'],eid)
        self.assertEqual(self.g.wire.calls,2);self.assertEqual(self.counts()['download'],3)
        self.assertEqual(len(self.registry.rows('SELECT * FROM conversion_bindings')),2)

    def test_model_reservation_prevents_cross_window_regeneration(self):
        from kb_custody_v21.pilot_service import continuation_material
        result=self.make().run();eid=result['materials'][0]['entry_id']
        before=self.g.wire.calls
        with self.assertRaisesRegex(ContractError,'model reservation'):continuation_material(self.intake,eid)
        self.assertEqual(self.g.wire.calls,before)

    def test_new_window_continues_same_delivered_pdf_without_new_model_or_upload(self):
        from kb_custody_v21.pilot_owner_followup import previous_preview
        # Store exact synthetic source proof before the first session is opened.
        files={'synthetic.py':'b'*64}
        manifest=dict(source_commit='a'*40,source_snapshot=dict(files=files,
            sha256=sha(json.dumps(files,sort_keys=True).encode())))
        mp=self.f.root/'manifest.local.json';mp.write_text(encoded(manifest))
        (self.f.root/'executed-source.local.json').write_text(encoded(manifest))
        self.f.approval.update(source_manifest=str(mp),source_manifest_sha256=file_sha(mp));self.save()
        self.f.pages=[self.f.pages[0],dict(items=[],has_more=False),dict(items=[],has_more=False)]
        result=self.make().run();material=result['materials'][0];d=material['delivery']
        original=self.intake.runtime.root/self.registry.entry(material['entry_id'])['relative_path']
        self.f.frozen[str(original)]=file_sha(original)
        binding=dict(root=str(self.f.root),approval_sha256=self.f.digest,
                     **{k:d[k] for k in ('publication_id','message_id','pdf_sha256')})
        self.run.close();self.session.close()
        proof=previous_preview(binding);self.f.frozen.update(proof['files'])
        old_files=dict(proof['files']);self.change_window()
        self.plan['previous_previews']=[binding];self.save()
        replies=self.replies();self.f.pages=[dict(items=replies,has_more=False)]+[dict(items=[],has_more=False)]*2
        before=self.g.wire.calls;result=self.make().run()
        self.assertTrue(result['materials'][0]['published']);self.assertEqual(self.g.wire.calls,before)
        self.assertEqual(self.counts()['upload'],0);self.assertEqual(self.counts()['download'],0)
        self.assertTrue(all(file_sha(Path(p))==h for p,h in old_files.items()))


    def test_changed_selected_previous_original_is_rejected_before_platform(self):
        from kb_custody_v21.pilot_service import continuation_material
        session=self.i.ready();received=self.i.receive(session);eid=received['original']['entry_id'];session.close()
        binding=continuation_material(self.intake,eid)
        original=self.intake.runtime.root/self.registry.entry(eid)['relative_path']
        self.f.frozen[str(original)]=file_sha(original)
        self.change_window();self.plan['previous_materials']=[dict(binding,sha256='f'*64)];self.save()
        self.f.runner.reset_mock()
        with self.assertRaisesRegex(ContractError,'exact unique'):self.make()
        self.f.runner.assert_not_called()

    def test_unselected_resume_marker_cannot_expand_parent_material_scope(self):
        from kb_custody_v21.pilot_service import continuation_material
        from kb_custody_v21.pilot_inbox_guide import PilotInboxGuide
        session=self.i.ready();received=self.i.receive(session);eid=received['original']['entry_id'];session.close()
        binding=continuation_material(self.intake,eid)
        original=self.intake.runtime.root/self.registry.entry(eid)['relative_path']
        self.f.frozen[str(original)]=file_sha(original)
        self.change_window();self.save();session=self.i.make()
        session.bind('service-resume:'+eid,binding)
        guide=PilotInboxGuide(session,self.g.path,wire=self.g.wire)
        with self.assertRaisesRegex(ContractError,'selected by the frozen'):guide._material(self.g.pipeline,eid)
        self.assertEqual(self.g.wire.calls,0)
