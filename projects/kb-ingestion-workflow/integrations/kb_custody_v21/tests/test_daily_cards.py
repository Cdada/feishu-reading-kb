"""Offline card binding, restart, and simultaneous text/button decisions."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.daily_cards import CardStore, REFERENCE, render_card
from kb_custody_v21.daily_policy import DailyPolicy
from kb_custody_v21.daily_store import DailyStore


class CardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name).resolve()/'queue.sqlite3'
        self.policy = DailyPolicy('cli_synthetic','oc_synthetic','ou_owner','ou_bot',8)
        self.store = DailyStore(self.path, self.policy, clock=lambda:1000000)
        self.cards = CardStore(self.store)
        self.binding = dict(purpose='preview', document_id='om_original', source_sha256='a'*64,
                            target_message_id='om_pdf', publication_id='publication_'+'b'*32,
                            pdf_sha256='c'*64, preview_version=1)
        self.card = self.cards.prepare(self.binding, '示例论文')
        self.proof = dict(receipt='synthetic-verified-event', sha256='d'*64)
        self.deliver()

    def deliver(self):
        self.cards.bind_delivery(self.card['card_id'],'om_card',self.proof,checked_access=lambda:None)

    def event(self, command='确认入库', **changes):
        event = dict(type='card.action.trigger', event_id='event_1', operator_id='ou_owner',
                     chat_id='oc_synthetic',host='im_message',message_id='om_card',action_tag='button',
                     action_value=json.dumps(dict(reference=REFERENCE,card_id=self.card['card_id'],command=command)),
                     card_content=json.dumps(render_card(self.card)),timestamp='1000000000')
        event.update(changes)
        return event

    def accept(self, event=None, **changes):
        args = dict(app_id=self.policy.app_id,current_binding=self.binding,checked_access=lambda:None)
        args.update(changes)
        return self.cards.accept_event(json.dumps(event or self.event()),self.proof,**args)

    def test_choice_survives_restart_without_claiming_business_completion(self):
        self.assertEqual(self.accept(),'accepted')
        self.cards = CardStore(DailyStore(self.path,self.policy,clock=lambda:1000000))
        choice = self.cards.choice(self.card['card_id'])
        self.assertEqual(choice['command'],'确认入库')
        self.assertIsNone(choice['result'])
        self.assertEqual(self.cards.get(self.card['card_id'])['state'],'sent')
        self.assertEqual(self.store.summary()['model_turns'],0)

    def test_same_event_and_repeated_clicks_do_not_create_second_choice(self):
        self.assertEqual(self.accept(),'accepted')
        self.assertEqual(self.accept(),'accepted')
        self.assertEqual(self.accept(self.event(event_id='event_2')),'duplicate')
        self.assertEqual(len(self.store.rows('SELECT * FROM daily_card_choices')),1)
        self.assertEqual(len(self.store.rows('SELECT * FROM daily_card_events')),2)

    def test_conflicting_click_does_not_replace_saved_owner_decision(self):
        self.accept()
        self.assertEqual(self.accept(self.event('暂不入库',event_id='event_2')),'conflict')
        self.assertEqual(self.cards.choice(self.card['card_id'])['command'],'确认入库')

    def test_changed_event_identity_is_rejected(self):
        self.accept()
        with self.assertRaises(ContractError):
            self.accept(self.event('暂不入库'))

    def test_wrong_identity_and_non_submit_actions_are_rejected(self):
        variants = [dict(operator_id='ou_other'),dict(chat_id='oc_other'),dict(message_id='om_other'),
                    dict(host='im_top_notice'),dict(action_tag='select_static'),dict(form_value='{}'),
                    dict(option='确认入库'),dict(card_content=''),dict(type='im.message.receive_v1')]
        for changes in variants:
            with self.subTest(changes=changes),self.assertRaises(ContractError):
                self.accept(self.event(**changes))
        with self.assertRaises(ContractError):
            self.accept(app_id='cli_other')
        self.assertIsNone(self.cards.choice(self.card['card_id']))

    def test_callback_cannot_change_material_binding_or_inject_a_command(self):
        for value in [dict(reference=REFERENCE,card_id=self.card['card_id'],command='删除文件'),
                      dict(reference=REFERENCE,card_id=self.card['card_id'],command='确认入库',pdf_sha256='f'*64),
                      dict(reference=REFERENCE,card_id='f'*64,command='确认入库')]:
            with self.subTest(value=value),self.assertRaises(ContractError):
                self.accept(self.event(action_value=json.dumps(value)))
        self.assertIsNone(self.cards.choice(self.card['card_id']))

    def test_new_pdf_version_invalidates_old_card_even_before_new_send(self):
        newer = dict(self.binding,pdf_sha256='e'*64,preview_version=2)
        newcard = self.cards.prepare(newer,'示例论文')
        self.assertNotEqual(newcard['card_id'],self.card['card_id'])
        self.assertEqual(self.accept(current_binding=newer),'stale')
        self.assertIsNone(self.cards.choice(self.card['card_id']))
        self.assertEqual(self.cards.get(self.card['card_id'])['state'],'superseded')
        with self.assertRaises(ContractError):
            self.deliver()

    def test_verified_text_does_not_depend_on_card_delivery(self):
        newcard = self.cards.prepare(dict(self.binding,preview_version=2,pdf_sha256='e'*64),'示例论文')
        self.assertEqual(self.cards.choose_text(newcard['card_id'],'确认入库',self.proof,
                         current_binding=newcard['binding'],checked_access=lambda:None),'accepted')

    def test_text_and_card_use_one_atomic_decision_slot(self):
        def text_choice():
            return self.cards.choose_text(self.card['card_id'],'确认入库',dict(text='verified'),
                                          current_binding=self.binding,checked_access=lambda:None)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(text_choice),pool.submit(self.accept)]
            outcomes = [f.result() for f in futures]
        self.assertCountEqual(outcomes,['accepted','duplicate'])
        self.assertEqual(len(self.store.rows('SELECT * FROM daily_card_choices')),1)

    def test_evidence_check_failure_never_records_a_decision(self):
        def changed():
            raise ContractError('synthetic receipt changed')
        with self.assertRaises(ContractError):
            self.accept(checked_access=changed)
        self.assertIsNone(self.cards.choice(self.card['card_id']))

    def test_business_result_is_explicit_and_immutable(self):
        cid = self.card['card_id']
        with self.assertRaises(ContractError):
            self.cards.finish(cid,dict(status='published'),checked_access=lambda:None)
        self.accept()
        self.cards.finish(cid,dict(status='published'),checked_access=lambda:None)
        self.cards.finish(cid,dict(status='published'),checked_access=lambda:None)
        self.assertEqual(self.cards.get(cid)['state'],'completed')
        with self.assertRaises(ContractError):
            self.cards.finish(cid,dict(status='rejected'),checked_access=lambda:None)
        self.assertEqual(self.cards.choice(cid)['result'],dict(status='published'))

    def test_card_binding_and_delivery_cannot_be_rewritten(self):
        self.assertEqual(self.cards.prepare(self.binding,'示例论文')['card_id'],self.card['card_id'])
        with self.assertRaises(ContractError):
            self.cards.prepare(self.binding,'另一份材料')
        with self.assertRaises(ContractError):
            self.cards.bind_delivery(self.card['card_id'],'om_replaced',self.proof,checked_access=lambda:None)
        for changes in [dict(preview_version=True),dict(pdf_sha256='invalid'),dict(extra='field')]:
            with self.subTest(changes=changes),self.assertRaises(ContractError):
                self.cards.prepare(dict(self.binding,**changes),'示例论文')

    def test_reordered_binding_reuses_the_sent_card_and_saved_choice(self):
        self.accept()
        reordered = dict(reversed(list(self.binding.items())))
        again = self.cards.prepare(reordered, '示例论文')
        self.assertEqual(again['card_id'], self.card['card_id'])
        self.assertEqual(again['state'], 'sent')
        self.assertEqual(again['message_id'], 'om_card')
        self.assertEqual(self.cards.choice(again['card_id'])['command'], '确认入库')
        self.assertEqual(len(self.store.rows('SELECT * FROM daily_cards')), 1)

    def test_titles_are_plain_text_and_result_card_has_no_live_buttons(self):
        card = dict(self.card,title='[确认](https://invalid.example) **文字**')
        rendered = render_card(card)
        self.assertEqual(rendered['body']['elements'][0]['text']['tag'],'plain_text')
        self.assertEqual(rendered['body']['elements'][0]['text']['content'],card['title'])
        done = render_card(card,result='已入库')
        self.assertFalse(any(e['tag']=='button' for e in done['body']['elements']))

    def test_missing_images_choice_is_distinct_from_publication_confirmation(self):
        binding = dict(purpose='images',document_id='om_original',source_sha256='a'*64,
                       target_message_id='om_image_notice',extraction_id='extraction_synthetic',report_sha256='b'*64)
        card = self.cards.prepare(binding,'缺图材料')
        self.cards.bind_delivery(card['card_id'],'om_image_card',self.proof,checked_access=lambda:None)
        self.assertEqual(self.cards.choose_text(card['card_id'],'按缺图版本继续',self.proof,
                         current_binding=binding,checked_access=lambda:None),'accepted')
        with self.assertRaises(ContractError):
            self.cards.choose_text(card['card_id'],'确认入库',self.proof,
                                   current_binding=binding,checked_access=lambda:None)

    def test_duplicate_json_fields_are_not_interpreted_as_approval(self):
        value = '{"reference":"P40-card-v1","card_id":"'+self.card['card_id']+'","command":"暂不入库","command":"确认入库"}'
        with self.assertRaises(ContractError):
            self.accept(self.event(action_value=value))
        self.assertIsNone(self.cards.choice(self.card['card_id']))


if __name__ == '__main__':
    unittest.main()
