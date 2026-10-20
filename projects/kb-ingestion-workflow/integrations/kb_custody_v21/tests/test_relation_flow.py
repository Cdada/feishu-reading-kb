"""Synthetic Feishu receipts through real card ingress and relation writes."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.daily_card_events import EventIngress
from kb_custody_v21.daily_card_flow import CardFlow
from kb_custody_v21.daily_cards import commands, render_card
from kb_custody_v21.daily_store import Deferred
from kb_custody_v21.daily_status import local_status, user_summary
from kb_custody_v21.f05 import file_sha
from kb_custody_v21.reading_publish import publish_plan
from kb_custody_v21.relation_flow import RelationFlow, TASK
from kb_custody_v21.relation_library import GRAPH, pair_id
from kb_custody_v21.registry import encoded
import test_relations as library_fixture
from test_daily_card_flow import CardTransport
from test_daily_store import message


class RelationFlowTests(unittest.TestCase):
    def setUp(self):
        self.f=library_fixture.RelationTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.a=self.f.material(1);self.other=self.f.material(2,doi_value='10.1234/paper')
        self.store=self.f.daily;self.p=self.store.policy
        root=self.f.profile.runtime.root/'conversion-work/daily-service';root.mkdir(parents=True)
        authority=SimpleNamespace(root=root,policy=self.p,reading=self.f.profile,digest='f'*64)
        authority.check=lambda:None
        old=SimpleNamespace(a=authority,calls=[],results={})
        self.transport=CardTransport(old)
        self.b=SimpleNamespace(store=self.store,authority=authority,transport=self.transport,
                               check_authorization=authority.check,card_listener=SimpleNamespace(ready=True))
        self.notices={}
        def notice(key,text,**kwargs):self.notices[key]=text;return dict(text=text)
        self.b.notice=notice
        self.b.card_flow=CardFlow(self.b)
        self.b.card_flow.material=self.material_binding
        self.b.relations=RelationFlow(self.b);self.flow=self.b.relations
        self.n=0
        self.register(self.a)

    def register(self, item):
        raw=message(1);raw['message_id']=item['document_id']
        scan=self.store.scan('chat',initial_start=self.store.now()-1)
        self.store.save_page(scan,[raw],has_more=False)
        with self.store.transaction() as db:
            db.execute("UPDATE daily_documents SET phase='notified',source_sha=?,data=? WHERE message_id=?",(
                item['source_sha256'],encoded(dict(reading_completed=True,reading_material_id=item['material_id'])),item['document_id']))
        return self.store.document(item['document_id'])

    def material_binding(self, document, purpose):
        parent='om_preview_'+document['message_id'][3:]
        path=self.b.authority.root/(parent+'.local.json')
        value=dict(code=0,data=dict(message_id=parent,chat_id=self.p.chat_id,msg_type='file',deleted=False,
            updated=False,sender=dict(id=self.p.bot_id,id_type='open_id',sender_type='app'),body=dict(content='{}')))
        path.write_text(encoded(value))
        proof=dict(value=value,receipt=str(path),evidence={str(path):file_sha(path)})
        return dict(target_message_id=parent),proof

    def start(self):
        self.flow.start(self.store.document(self.a['document_id']));self.flow.report()
        return self.card('relation_candidates')

    def card(self,purpose):
        rows=self.store.rows("SELECT card_id FROM daily_cards WHERE purpose=? AND state='sent' ORDER BY rowid DESC",(purpose,))
        self.assertTrue(rows,purpose)
        return self.b.card_flow.cards.get(rows[0]['card_id'])

    def click(self,card,command,*,event_id=None):
        self.n+=1
        eid=event_id or 'synthetic_relation_'+str(self.n)
        event=dict(type='card.action.trigger',event_id=eid,timestamp='1000000000',operator_id=self.p.account_id,
            chat_id=self.p.chat_id,host='im_message',message_id=card['message_id'],action_tag='button',
            action_value=encoded(dict(reference='P40-card-v1',card_id=card['card_id'],command=command)))
        session=self.b.authority.root/'listener.local.json';session.write_text('{}')
        queue=EventIngress(self.b);queue.persist(event,app_id=self.p.app_id,session_path=session);queue.drain()
        return event

    def choose_type(self,command='本篇是解读，候选是原文'):
        self.click(self.start(),'候选 1');self.flow.report();self.flow.report()
        card=self.card('relation_type');self.click(card,command);return card

    def test_requires_completed_organization_before_any_search_or_card(self):
        document=self.store.document(self.a['document_id']);document['data']['reading_completed']=False
        with self.assertRaises(ContractError):self.flow.start(document)
        self.assertFalse(self.transport.calls)

    def test_other_completed_material_cannot_supply_this_documents_approval(self):
        document=self.store.document(self.a['document_id'])
        document['data']['reading_material_id']=self.other['material_id']
        with self.assertRaises(ContractError):self.flow.start(document)
        self.assertFalse(self.transport.calls)

    def test_local_status_separates_optional_relationships_from_unfinished_intake(self):
        self.start()
        status=local_status(self.f.profile.runtime.root)
        self.assertEqual(status['summary']['relations']['waiting'],1)
        self.assertEqual(status['summary']['continuing_count'],0)
        self.assertIn('这些资料均已完成入库',user_summary(status))

    def test_end_without_relation_and_no_candidates_finish_without_matching(self):
        self.click(self.start(),'结束关联');self.flow.report()
        self.assertFalse((self.f.profile.root/GRAPH).exists())
        self.assertEqual(self.store.summary()['model_turns'],0)
        with self.store.transaction() as db:
            db.execute("UPDATE reading_materials SET state='reviewing' WHERE material_id=?",(self.other['material_id'],))
        # A new material has no completed peers; old jobs are not backfilled.
        c=self.f.material(3,'完全不同的烹饪资料',keywords=())
        self.flow.start(self.register(c));self.flow.report()
        card=self.card('relation_candidates')
        self.assertEqual(commands(card['binding']),())
        self.assertIn('未找到',encoded(render_card(card)))

    def test_two_explicit_choices_publish_once_and_keep_manage_card(self):
        card=self.choose_type();self.flow.report();self.flow.report()
        self.assertEqual(len(self.f.graph()['history']),1)
        self.assertEqual(self.b.card_flow.cards.choice(card['card_id'])['result']['text'],'关联完成：双方链接和关联目录均已核验。')
        manage=self.card('relation_manage');self.assertEqual(commands(manage['binding']),('更正关联','撤销关联'))
        self.click(card,'本篇是解读，候选是原文');self.flow.report()
        self.assertEqual(len(self.f.graph()['history']),1)
        self.assertEqual(self.store.summary()['model_turns'],0)

    def test_correction_cancel_then_revoke_keeps_originals(self):
        self.choose_type();self.flow.report();self.flow.report()
        self.click(self.card('relation_manage'),'更正关联');self.flow.report();self.flow.report()
        self.click(self.card('relation_type'),'取消本次关联');self.flow.report();self.flow.report()
        self.assertEqual(len(self.f.graph()['history']),1)
        self.click(self.card('relation_manage'),'更正关联');self.flow.report();self.flow.report()
        self.click(self.card('relation_type'),'候选是修订版，本篇是旧版');self.flow.report();self.flow.report()
        self.assertEqual(len(self.f.graph()['history']),2)
        edge=self.f.graph()['edges'][pair_id(self.a['material_id'],self.other['material_id'])]
        self.assertEqual(edge['left'],self.other['material_id'])
        self.click(self.card('relation_manage'),'撤销关联');self.flow.report()
        self.assertEqual(len(self.f.graph()['history']),3)
        self.assertFalse(next(iter(self.f.graph()['edges'].values()))['active'])

    def test_interruption_after_first_write_recovers_same_plan_before_completion(self):
        card=self.choose_type()
        def interrupted(*a,**kw):
            def crash(path):raise OSError('synthetic interrupted publication')
            return publish_plan(*a,**kw,after_write=crash)
        with patch('kb_custody_v21.relation_flow.publish_plan',side_effect=interrupted):self.flow.report()
        self.assertIsNotNone(self.store.get('relation-write-pending'))
        self.assertIsNone(self.b.card_flow.cards.choice(card['card_id'])['result'])
        self.assertFalse((self.f.profile.root/GRAPH).exists())
        self.flow=self.b.relations=RelationFlow(self.b);self.flow.report()
        self.assertIsNone(self.store.get('relation-write-pending'))
        self.assertEqual(len(self.f.graph()['history']),1)
        self.assertIsNotNone(self.b.card_flow.cards.choice(card['card_id'])['result'])

    def test_changed_related_metadata_makes_callback_stale_without_losing_archive(self):
        self.click(self.start(),'候选 1');self.flow.report();self.flow.report()
        card=self.card('relation_type')
        (self.f.profile.root/self.other['group']/'metadata.json').write_text('{}')
        self.click(card,'仅主题相关');self.flow.report()
        self.assertIsNone(self.b.card_flow.cards.choice(card['card_id']))
        self.assertFalse((self.f.profile.root/GRAPH).exists())
        self.assertEqual(self.store.document(self.a['document_id'])['phase'],'notified')

    def test_display_failure_never_repeats_link_write(self):
        card=self.choose_type()
        with patch.object(self.flow.delivery,'update',side_effect=Deferred('offline')):
            self.flow.report();self.flow.report()
        self.flow.report()
        self.assertEqual(len(self.f.graph()['history']),1)
        self.assertEqual(self.store.get('card-display:'+card['card_id'])['status'],'verified')

    def test_disabled_feature_ignores_old_callback_without_network_or_mutation(self):
        card=self.start();before=len(self.transport.calls)
        self.b.relations=None;self.click(card,'候选 1')
        self.assertEqual(len(self.transport.calls),before)
        self.assertEqual(self.store.rows('SELECT outcome FROM daily_card_inbox')[-1]['outcome'],'disabled')
        self.assertIsNone(self.b.card_flow.cards.choice(card['card_id']))

    def test_unshown_candidate_cannot_be_selected(self):
        card=self.start()
        with self.assertRaises(ContractError):self.click(card,'候选 5')
        self.assertIsNone(self.b.card_flow.cards.choice(card['card_id']))

    def test_failed_search_is_visible_and_never_claims_zero_candidates(self):
        (self.f.profile.root/self.other['group']/'metadata.json').write_text('{}')
        self.flow.start(self.store.document(self.a['document_id']));self.flow.report()
        self.assertFalse(self.store.rows('SELECT * FROM daily_cards'))
        self.assertIn('暂缓',next(iter(self.notices.values())))
        self.assertEqual(self.store.document(self.a['document_id'])['phase'],'notified')

    def test_multiple_relations_keep_independent_correction_cards(self):
        third=self.f.material(3)
        self.choose_type('仅主题相关');self.flow.report()
        self.flow.report();self.flow.report()  # Remaining candidates and first manage card.
        first=self.card('relation_manage')
        self.click(self.card('relation_candidates'),'候选 1');self.flow.report();self.flow.report()
        self.click(self.card('relation_type'),'仅主题相关');self.flow.report();self.flow.report()
        second=self.card('relation_manage')
        self.assertNotEqual(first['binding']['pair_id'],second['binding']['pair_id'])
        self.assertEqual(self.b.card_flow.cards.get(first['card_id'])['state'],'sent')
        self.click(first,'撤销关联');self.flow.report()
        self.assertEqual(sum(e['active'] for e in self.f.graph()['edges'].values()),1)
        self.assertEqual(self.b.card_flow.cards.get(second['card_id'])['state'],'sent')

    def test_relation_file_change_is_reported_and_does_not_overwrite_data(self):
        self.choose_type();self.flow.report();self.flow.report()
        graph=self.f.profile.root/GRAPH;raw=graph.read_text();value=json.loads(raw)
        value['history'][0]['command']='manual edit';graph.write_text(encoded(value))
        self.click(self.card('relation_manage'),'撤销关联');self.flow.report()
        self.assertEqual(graph.read_text(),encoded(value))
        self.assertTrue(next(iter(json.loads(graph.read_text())['edges'].values()))['active'])

    def test_failed_problem_notice_remains_pending_and_is_retried(self):
        (self.f.profile.root/self.other['group']/'metadata.json').write_text('{}')
        self.flow.start(self.store.document(self.a['document_id']))
        notice=self.b.notice
        def offline(*a,**k):raise Deferred('offline')
        self.b.notice=offline;self.flow.report()
        self.assertIsNotNone(self.store.get('relation-problem-notice-pending'))
        self.b.notice=notice;self.flow.report()
        self.assertIsNone(self.store.get('relation-problem-notice-pending'))
        self.assertEqual(len(self.notices),1)


if __name__=='__main__':unittest.main()
