"""Reuse custody, availability and official-CLI receipt fixtures, without sockets."""
from datetime import timedelta
import copy
import json
import unittest
from unittest.mock import Mock, patch

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.f01 import encoded
from kb_custody_v21.f05 import file_sha
from kb_custody_v21.notifications import DeliveryUnknown
from kb_custody_v21.pilot_feishu import PilotReadSession
from kb_custody_v21.pilot_notices import PilotExecutionSession, PilotNoticeSender, PURPOSES, collection_text
import test_pilot_feishu as platform
import test_pilot_availability as availability


class NoticeTests(unittest.TestCase):
    def setUp(self):
        self.f = platform.PilotFeishuTests(); self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        old=self.f.root
        self.f.root=old.with_name('p11-requests'); old.rename(self.f.root)
        self.f.source=self.f.root/'source.synthetic'
        self.f.frozen={str(self.f.source):file_sha(self.f.source)}
        self.f.session=self.f.root/'approval.local.json'
        self.f.approval.update(reference='P11',root=str(self.f.root),maximum_total=50,notice_purposes=PURPOSES)
        self.save_approval()
        self.item=dict(notification_id='a'*64,provider_request_key='pilot-service-'+'a'*36,
                       target_chat_id='oc_synthetic',payload='合成暂停提醒')
        self.authoritative=Mock(side_effect=lambda:dict(self.item))
        self.changed=lambda c,v:None
        def hook(c,v):
            if c['kind']=='send':
                v.clear();v.update(code=0,data=dict(message_id='om_notice',chat_id='oc_synthetic',
                    msg_type='text',deleted=False,updated=False,
                    sender=dict(id='ou_synthetic_bot',id_type='open_id',sender_type='app'),
                    body=dict(content=encoded(dict(text=self.item['payload'])))))
                self.changed(c,v)
        self.f.hook=hook
        respond=self.f.respond
        def runner(c):
            result=respond(c)
            result['mode']=c['mode']
            return result
        self.f.runner.side_effect=runner

    def save_approval(self):
        self.f.session.write_text(encoded(self.f.approval));self.f.digest=file_sha(self.f.session)

    def make(self, isolate=True):
        s=PilotExecutionSession(self.f.root,self.f.digest,runner=self.f.runner,
            checked_source=self.f.checked_source,clock=lambda:self.f.now)
        self.addCleanup(s.close)
        if isolate:s.verify_isolation()
        return s

    def sender(self,s):
        return PilotNoticeSender(s,'service',self.authoritative)

    def test_new_approval_rejects_wrong_reference_root_budget_and_missing_scope_before_cli(self):
        for change in [dict(reference='N10'),dict(approved=False),dict(maximum_total=51),
            dict(limits=dict(self.f.approval['limits'],auth=True)),dict(limits={'send':1}),
            dict(notice_purposes=[]),dict(automatic_retries=1)]:
            with self.subTest(change=change):
                old=copy.deepcopy(self.f.approval)
                self.f.approval.update(change);self.save_approval()
                with self.assertRaises(ContractError):self.make(False)
                self.f.approval=old
        self.f.runner.assert_not_called()

    def test_old_n10_cannot_inherit_new_session_or_notice_route(self):
        with self.assertRaises(ContractError):
            PilotReadSession(self.f.root,self.f.digest,runner=self.f.runner,
                checked_source=self.f.checked_source,clock=lambda:self.f.now)
        old=platform.PilotFeishuTests();old.setUp();self.addCleanup(old.doCleanups)
        s=old.make()
        with self.assertRaises(ContractError):PilotNoticeSender(s,'service',self.authoritative)
        with self.assertRaises(ContractError):
            s._call('send','/open-apis/im/v1/messages',operation='notice',notice={})
        old.runner.assert_not_called()

    def test_exact_notice_counts_auth_and_send_and_reuses_successful_receipt_without_resend(self):
        s=self.make();sender=self.sender(s)
        self.assertEqual(sender.send(self.item,'attempt1'),'om_notice')
        n=self.f.runner.call_count
        self.assertEqual(sender.send(self.item,'attempt2'),'om_notice')
        self.assertEqual(self.f.runner.call_count,n)
        events=s.http_events()
        self.assertEqual(sum(e['type']=='intent' and e['kind']=='send' for e in events),1)
        self.assertEqual(sum(e['type']=='intent' and e['kind']=='auth' for e in events),4)
        s.close();s=self.make()
        self.assertEqual(self.sender(s).send(self.item,'restart'),'om_notice')
        self.assertEqual(self.f.runner.call_count,n)

    def test_changed_plan_or_source_state_stops_without_sending_or_lookup(self):
        s=self.make();before=self.f.runner.call_count
        self.authoritative.side_effect=lambda:dict(self.item,payload='changed')
        sender=self.sender(s)
        with self.assertRaises(DeliveryUnknown):sender.send(self.item,'attempt')
        self.assertEqual(sender.lookup(self.item,'lookup'),('unknown',None))
        self.assertEqual(self.f.runner.call_count,before)
        s.close()
        with self.assertRaises(ContractError):self.make()

    def test_bad_remote_success_status_identity_body_or_update_flag_never_counts_as_delivery(self):
        for field,value in [('code',9),('chat_id','oc_other'),('deleted',True),('updated',True),
                            ('body',dict(content=encoded(dict(text='changed'))))]:
            with self.subTest(field=field):
                # Independent synthetic session for each irreversible stopped window.
                f=NoticeTests();f.setUp();self.addCleanup(f.doCleanups)
                def changed(c,v):
                    (v if field=='code' else v['data'])[field]=value
                f.changed=changed
                s=f.make()
                with self.assertRaises(DeliveryUnknown):f.sender(s).send(f.item,'attempt')
                self.assertIsNone(s.value('notice:'+f.item['notification_id']+':verified'))
                before=f.f.runner.call_count
                with self.assertRaises(DeliveryUnknown):f.sender(s).send(f.item,'again')
                self.assertEqual(f.f.runner.call_count,before)

    def test_deadline_crossing_during_authoritative_check_sends_no_request(self):
        s=self.make();before=self.f.runner.call_count
        def crossed():
            self.f.now += timedelta(days=1)
            return self.item
        self.authoritative.side_effect=crossed
        with self.assertRaises(DeliveryUnknown):self.sender(s).send(self.item,'attempt')
        self.assertEqual(self.f.runner.call_count,before)

    def test_source_changes_during_send_preserves_unknown_and_blocks_restart(self):
        s=self.make()
        self.changed=lambda c,v:self.f.source.write_text('changed')
        with self.assertRaises(DeliveryUnknown):self.sender(s).send(self.item,'attempt')
        self.assertIsNone(s.value('notice:'+self.item['notification_id']+':verified'))
        self.assertEqual(len(list(self.f.root.glob('*-send.response.local'))),1)
        s.close()
        with self.assertRaises(ContractError):self.make()

    def test_deadline_crosses_during_final_receipt_hash_without_recording_success(self):
        s=self.make()
        def crossed(path):
            result=file_sha(path)
            if str(path).endswith('-send.response.local'):
                self.f.now += timedelta(days=1)
            return result
        with patch('kb_custody_v21.pilot_notices.file_sha',side_effect=crossed):
            with self.assertRaises(DeliveryUnknown):self.sender(s).send(self.item,'attempt')
        self.assertIsNone(s.value('notice:'+self.item['notification_id']+':verified'))
        self.assertEqual(len(list(self.f.root.glob('*-send.response.local'))),1)

    def test_approval_category_budget_is_not_replaced_by_old_extension(self):
        self.f.approval.update(limits=dict(self.f.approval['limits'],send=0),maximum_total=44)
        self.save_approval();s=self.make()
        self.assertEqual(s.limits['send'],0)
        p=self.f.root/'extension.local.json';p.write_text('{}')
        self.f.frozen[str(p)]=file_sha(p)
        with self.assertRaises(ContractError):s.check()

    def test_existing_custody_outbox_identity_and_key_are_not_regenerated(self):
        from kb_custody_v21.intake import MockAttachment
        a=availability.CollectionReportTests();a.setUp();self.addCleanup(a.doCleanups)
        a.intake.receive(a.message,0,MockAttachment(a.body).download)
        items=a.registry.rows('SELECT * FROM notification_outbox')
        self.assertEqual(len(items),1)
        self.item=dict(items[0])
        self.assertTrue(self.item['notification_id'].startswith('notification_'))
        self.authoritative.side_effect=lambda:dict(a.registry.rows('SELECT * FROM notification_outbox')[0])
        s=self.make()
        sender=PilotNoticeSender(s,'custody',self.authoritative)
        self.assertEqual(sender.send(self.item,'attempt'),'om_notice')
        sent=self.f.runner.call_args.args[0]
        self.assertEqual(sent['notice']['notification_id'],items[0]['notification_id'])
        self.assertEqual(json.loads(sent['body'])['uuid'],items[0]['provider_request_key'])
        self.assertEqual(sender.send(self.item,'again'),'om_notice')
        self.assertEqual(sum(e['type']=='intent' and e['kind']=='send' for e in s.http_events()),1)

    def test_service_dispatch_uses_same_sender_and_preserves_unknown_without_second_send(self):
        a=availability.AvailabilityTests();a.setUp();self.addCleanup(a.doCleanups)
        item=a.plan('pausing');self.item=item
        self.authoritative.side_effect=lambda:dict(item)
        s=self.make();sender=self.sender(s)
        self.changed=lambda c,v:v['data'].update(updated=True)
        r=a.notices.dispatch(item['notification_id'],sender,checked_access=s.check,checked_state=a.ready)
        self.assertEqual(r['delivery_state'],'delivery_unknown')
        n=self.f.runner.call_count
        r=a.notices.dispatch(item['notification_id'],sender,checked_access=s.check,checked_state=a.ready)
        self.assertEqual(r['delivery_state'],'delivery_unknown')
        self.assertEqual(self.f.runner.call_count,n)


class ChecklistTests(unittest.TestCase):
    def test_saved_scan_lists_known_files_and_explicit_unchecked_interval(self):
        f=availability.CollectionReportTests();f.setUp();self.addCleanup(f.doCleanups)
        f.fixture.pages[0].update(has_more=True,page_token='synthetic-next')
        report=f.report(f.scan())
        text=collection_text(report)
        self.assertIn('synthetic.md',text);self.assertIn('尚未确认接管',text)
        self.assertIn('无法列出完全未观察到',text);self.assertIn('整个查询区间',text)
        self.assertIn('北京时间',text)
        self.assertNotIn('om_synthetic',text)

    def test_no_file_is_not_no_loss_and_long_report_is_never_silently_truncated(self):
        report=dict(interval=dict(start=1,end=2),materials=[],query_complete=False,notice='清单可能不完整')
        self.assertIn('不证明',collection_text(report))
        report['materials']=[dict(filename='test\n原件已保存'*1000,status='NOT_CONFIRMED',action='请核对')]
        with self.assertRaises(ContractError):collection_text(report)
