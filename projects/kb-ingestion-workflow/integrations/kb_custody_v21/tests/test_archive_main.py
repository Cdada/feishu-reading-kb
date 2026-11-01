"""Synthetic receipts through the real guide, decision and publication path."""
import copy
import json
from types import SimpleNamespace
import unittest

from kb_custody_v21.archive_main import MainCards
from kb_custody_v21.daily_cards import render_card
from kb_custody_v21.daily_card_flow import CardFlow
from kb_custody_v21.daily_store import DailyStore
from kb_custody_v21.f05 import file_sha
from kb_custody_v21.registry import encoded
import test_daily_backend as backend_fixture
import test_daily_card_flow as card_fixture
import test_daily_card_io as io_fixture


class MainCardTests(unittest.TestCase):
    def case(self):
        f=backend_fixture.DailyBackendTests();f.setUp();self.addCleanup(f.doCleanups)
        f.store=DailyStore(f.root/'main.sqlite3',f.policy)
        f.store.archive_cards_enabled=True
        f.backend.store=f.store
        f.backend.collector.store=f.store
        f.transport=card_fixture.CardTransport(f.transport);f.backend.transport=f.transport
        f.backend.collector.transport=f.transport
        f.backend.card_flow=CardFlow(f.backend)
        f.backend.card_listener=SimpleNamespace(ready=True)
        f.backend.main_cards=MainCards(f.backend)
        f.authority.config['cards']=dict(reference='P40-cards-v1',enabled=True)
        raw=f.g.f.message
        path=f.root/'synthetic-original.local.json';path.write_text(encoded(raw))
        target=dict(value=dict(code=0,data=dict(items=[raw])),receipt=str(path),evidence={str(path):file_sha(path)})
        f.backend.collector.observed=lambda mid:(raw,target)
        scan=f.store.scan('chat',initial_start=f.store.now()-60)
        f.store.save_page(scan,[raw],has_more=False)
        f.backend.main_cards.report_one(f.mid,force=True)
        f.store.advance(f.mid,'registered','original_saved',entry_id=f.entry)
        f.store.bind_source(f.mid,f.registry.entry(f.entry)['sha256'])
        return f

    def action(self,f):
        f.preview()
        f.backend.advance_light(f.store.document(f.mid))
        state=f.backend.main_cards.state(f.mid)
        return f.backend.card_flow.cards.get(state['binding']['decision']['card_id'])

    def click(self,f,action,command='确认入库',eid='main_event'):
        root=f.backend.card_flow.cards.get(f.backend.main_cards.state(f.mid)['card_id'])
        event=card_fixture.CardFlowTests().event(f,dict(action,message_id=root['message_id']),command,eid)
        ingress=card_fixture.CardFlowTests().ingress(f,event)
        ingress.drain()
        return f.store.rows('SELECT outcome FROM daily_card_inbox WHERE event_id=?',(eid,))[0]['outcome']

    def test_one_card_through_confirmation_preserves_pdf_and_existing_publication(self):
        f=self.case();action=self.action(f)
        self.assertEqual(len(f.transport.messages),1)
        self.assertEqual(self.click(f,action),'accepted')
        self.assertEqual(self.click(f,action,eid='repeat_event'),'duplicate')
        f.backend.advance_light(f.store.document(f.mid))
        self.assertEqual(f.store.document(f.mid)['phase'],'notified')
        self.assertEqual(len(f.registry.rows('SELECT * FROM publication_confirmations')),1)
        self.assertEqual(f.g.wire.calls,1)
        self.assertEqual(len(f.transport.messages),1)
        self.assertFalse(any(op.startswith(('notice:custody:','notice:registered:')) for op,_ in f.transport.calls))
        self.assertTrue(any(op.endswith(':send') and op.startswith('preview:') for op,_ in f.transport.calls))

    def test_callback_read_keeps_decision_receipt_in_each_guarded_request(self):
        f=self.case();action=self.action(f)
        request=f.transport.request
        reads=[]
        def checked(operation,kind,path,**kwargs):
            card=kwargs.get('card') or {}
            if operation.startswith('main-event-read:'):
                target=card['decision_receipt']
                self.assertIn(target,kwargs['evidence'])
                self.assertEqual(kwargs['evidence'][target],file_sha(target))
                reads.append(target)
            return request(operation,kind,path,**kwargs)
        f.transport.request=checked
        self.assertEqual(self.click(f,action),'accepted')
        self.assertEqual(len(reads),1)

    def test_restart_preserves_binding_and_disables_buttons_after_choice(self):
        f=self.case();action=self.action(f)
        original=f.backend.main_cards.state(f.mid)['card_id']
        f.backend.main_cards=MainCards(f.backend)
        self.assertEqual(self.click(f,action),'accepted')
        f.backend.main_cards.report_one(f.mid,force=True)
        self.assertIsNone(f.backend.main_cards.state(f.mid)['binding']['decision'])
        self.assertEqual(self.click(f,action,eid='obsolete_event'),'stale')
        self.assertEqual(original,f.backend.main_cards.state(f.mid)['card_id'])
        self.assertEqual(len(f.transport.messages),1)

    def test_historical_tasks_are_not_adopted_and_new_tasks_are_isolated(self):
        f=self.case(); main=f.backend.main_cards
        raw=copy.deepcopy(f.g.f.message);raw['message_id']='om_second'
        with f.store.transaction() as db:f.store._observe(db,raw)
        self.assertTrue(main.handles('om_second'))
        self.assertIsNone(main.state('om_second'))
        f.store.archive_cards_enabled=False
        raw['message_id']='om_legacy'
        with f.store.transaction() as db:f.store._observe(db,raw)
        self.assertFalse(main.handles('om_legacy'))
        self.assertEqual(len(f.transport.messages),1)

    def test_page_count_is_only_successful_pages_and_not_archive_completion(self):
        f=self.case();doc=f.store.document(f.mid);state=f.backend.main_cards.state(f.mid)
        f.store.put('pdf-progress:'+f.mid,dict(stage='merging',source_sha=doc['source_sha'],completed_pages=20,total_pages=20))
        content=f.backend.main_cards.desired(doc,state)
        self.assertIn('20/20',content['details'])
        self.assertIn('还需生成导读',content['details'])
        self.assertIsNone(content['decision'])

    def test_fallback_is_once_and_does_not_repeat_business_work(self):
        f=self.case();action=self.action(f);state=f.backend.main_cards.state(f.mid)
        state.update(disabled=True,error='synthetic card unavailable')
        f.store.put('archive-main:'+f.mid,state)
        for _ in range(3):f.backend.main_cards.report_one(f.mid,force=True)
        self.assertEqual(sum(op.startswith('notice:archive-main-fallback:') for op,_ in f.transport.calls),1)
        self.assertEqual(f.g.wire.calls,1)
        self.assertEqual(f.store.document(f.mid)['phase'],'awaiting_confirmation')

    def test_buttons_wait_for_listener_and_recover_on_the_same_card(self):
        f=self.case();f.preview();f.backend.card_listener.ready=False
        f.backend.advance_light(f.store.document(f.mid))
        state=f.backend.main_cards.state(f.mid)
        self.assertIsNone(state['binding']['decision'])
        self.assertIn('连接恢复',state['binding']['details'])
        f.backend.card_listener.ready=True
        f.backend.advance_light(f.store.document(f.mid))
        self.assertIsNotNone(f.backend.main_cards.state(f.mid)['binding']['decision'])
        self.assertEqual(len(f.transport.messages),1)


class MainCardIOTests(unittest.TestCase):
    def case(self):
        f=io_fixture.CardIOTests();f.setUp();self.addCleanup(f.doCleanups)
        f.f.a.config['archive_cards']=dict(reference='archive-main-v1',enabled=True)
        from kb_custody_v21.daily_card_io import CardDelivery
        f.card=f.cards.prepare(dict(purpose='archive_main',document_id='om_pdf',target_message_id='om_pdf',
            source_sha256='a'*64,details='正在排队',decision=None),'合成资料')
        f.delivery=CardDelivery(f.f.transport,f.cards)
        main=MainCards(SimpleNamespace(store=f.f.store,transport=f.f.transport,authority=f.f.a,
            card_flow=SimpleNamespace(cards=f.cards,delivery=f.delivery)))
        return f,main

    def test_unknown_main_send_keeps_original_message_and_uuid(self):
        f,main=self.case();f.failure='send-unknown'
        from kb_custody_v21.daily_store import Deferred
        with self.assertRaises(Deferred):f.send()
        f.f.now+=30
        card=f.send()
        self.assertEqual(card['state'],'sent')
        self.assertEqual(sum(c['mode']=='send' for c in f.f.calls),1)

    def test_unknown_main_update_reconciles_before_next_snapshot(self):
        from kb_custody_v21.daily_store import Deferred
        f,main=self.case();card=f.send()
        state=dict(card_id=card['card_id'],receipt=card['receipt'],binding=card['binding'],sequence=0,
            pending=dict(sequence=1,binding=dict(card['binding'],details='成功 1/2 页')))
        f.failure='patch-unknown'
        with self.assertRaises(Deferred):main._write('om_pdf',state)
        main._write('om_pdf',state)
        self.assertEqual(sum(c['mode']=='card_update' for c in f.f.calls),1)
        state['pending']=dict(sequence=2,binding=dict(card['binding'],details='已收到确认'))
        main._write('om_pdf',state)
        self.assertEqual(json.loads(f.remote['body']['content']),render_card(dict(card,binding=state['binding'])))
        self.assertEqual(state['sequence'],2)
