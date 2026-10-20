"""Synthetic Feishu admission, durable replies and fixed callbacks."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from kb_custody_v21.contracts import stable_key, ContractError
from kb_custody_v21.daily_cards import CardStore, render_card, REFERENCE
from kb_custody_v21.qa_flow import QAFlow
from kb_custody_v21.registry import encoded
import test_reading_flow as reading_fixture
import test_daily_collector as collector_fixture
from kb_custody_v21.qa_store import QAStore


class QAFeishuTests(unittest.TestCase):
    def setUp(self):
        self.f=reading_fixture.ReadingFlowTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.b,self.a,self.daily=self.f.b,self.f.a,self.f.daily
        self.a.qa=SimpleNamespace(since=0,script=self.f.a.reading.script,generation=None)
        self.flow=QAFlow(self.b,wire=self.f.wire);self.b.qa=self.flow

    def message(self, text, *, mid='om_qa', created=None):
        return dict(message_id=mid,chat_id=self.a.policy.chat_id,msg_type='text',deleted=False,updated=False,
            create_time=str(int((created or self.daily.now())*1000)),body=dict(content=encoded(dict(text=text))),
            sender=dict(id=self.a.policy.account_id,id_type='open_id',sender_type='user'))

    def observe(self,item):
        scan=self.daily.scan('qa-synthetic',initial_start=self.daily.now()-60)
        self.daily.save_page(scan,[item],has_more=False)
        self.b.collector.message=lambda mid:(item,dict(evidence={}),dict(evidence={}))

    def test_plain_archive_confirmation_never_enters_qa(self):
        self.observe(self.message('确认入库'));self.flow.collect()
        self.assertFalse(self.flow.pending())

    def test_history_before_enable_time_not_replayed(self):
        self.a.qa.since=self.daily.now()+10
        self.observe(self.message('问库：本周周报'));self.flow.collect()
        self.assertFalse(self.flow.pending())

    def test_question_received_once_and_reply_survives_restart(self):
        self.observe(self.message('问库：帮助'));self.flow.collect();self.flow.collect()
        self.assertEqual(len(self.daily.rows('SELECT * FROM qa_jobs')),1)
        self.flow.step();self.flow.report()
        count=len(self.f.f.transport.calls)
        self.flow.report()
        self.assertEqual(len(self.f.f.transport.calls),count)
        job=self.flow.store.get(stable_key('qa-message','om_qa'))
        self.assertEqual(job['state'],'sent');self.assertIn('查文献',job['result']['text'])

    def test_evidence_callback_resumes_without_recreating_saved_result(self):
        self.observe(self.message('问库：帮助'));self.flow.collect()
        self.flow.step();self.flow.report()
        job=self.flow.store.get(stable_key('qa-message','om_qa'))
        card=dict(card_id='c'*64,state='sent',binding=dict(job_id=job['id']))
        cards=Mock()
        cards.prepare.return_value=card
        cards.choice.return_value=dict(command='查看原文依据',result=None,evidence=dict(receipts={}))
        cards.get.return_value=card
        self.b.card_listener=SimpleNamespace(ready=True)
        self.b.card_flow=SimpleNamespace(cards=cards,delivery=Mock())
        with patch.object(self.flow.store,'enqueue',return_value=dict(state='ready')), \
                patch.object(self.flow.store,'complete') as complete, \
                patch.object(self.flow.model,'saved') as saved:
            self.flow.report()
        complete.assert_not_called();saved.assert_not_called()
        cards.finish.assert_called_once()
        self.b.card_flow.delivery.update.assert_called_once()

    def test_qa_card_accepts_only_fixed_owner_command(self):
        cards=CardStore(self.daily)
        binding=dict(purpose='qa',document_id='om_question',source_sha256='a'*64,target_message_id='om_answer',job_id='b'*64,details='合成问答选项')
        card=cards.prepare(binding,'继续查询')
        cards.bind_delivery(card['card_id'],'om_card',dict(synthetic=True),checked_access=lambda:None)
        def event(command,owner=None):
            return encoded(dict(type='card.action.trigger',event_id='synthetic-event',operator_id=owner or self.a.policy.account_id,
                chat_id=self.a.policy.chat_id,host='im_message',message_id='om_card',action_tag='button',timestamp=str(int(self.daily.now()*1000)),
                action_value=encoded(dict(reference=REFERENCE,card_id=card['card_id'],command=command)),card_content=encoded(render_card(card))))
        with self.assertRaises(ContractError):cards.accept_event(event('执行自定义命令'),{},app_id=self.a.policy.app_id,current_binding=binding,checked_access=lambda:None)
        with self.assertRaises(ContractError):cards.accept_event(event('本周周报','ou_other'),{},app_id=self.a.policy.app_id,current_binding=binding,checked_access=lambda:None)
        outcome=cards.accept_event(event('本周周报'),dict(synthetic_verified=True),app_id=self.a.policy.app_id,current_binding=binding,checked_access=lambda:None)
        self.assertEqual(outcome,'accepted')


class QAThreadTests(unittest.TestCase):
    def test_completed_qa_root_still_collects_later_thread_reply(self):
        f=collector_fixture.CollectorTests();f.setUp();self.addCleanup(f.doCleanups)
        f.a.config['qa']={'reference':'P43-qa-v1','enabled':True}
        store=QAStore(f.store)
        job=store.enqueue('a'*64,'om_question','查文献 合成材料',{'receipts':{}})
        store.complete(job['id'],dict(status='retrieval',text='合成依据',hits=[]))
        notice=dict(message_id='om_pdf',receipt=f.proof('answer',dict(code=0,data=dict(message_id='om_pdf'))))
        store.delivered(job['id'],notice)
        f.collector.collect()
        item,proof=f.collector.observed('om_reply')
        self.assertEqual(item['thread_id'],'omt_known');f._checked_result(proof)
