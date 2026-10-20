"""Fault injection after remote card effects, using the real request ledger."""
import copy
import json
from pathlib import Path
import unittest

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.daily_card_io import CardDelivery
from kb_custody_v21.daily_cards import CardStore, render_card
from kb_custody_v21.daily_scheduler import MaterialProblem
from kb_custody_v21.daily_store import Deferred
from kb_custody_v21.f05 import file_sha
from kb_custody_v21.registry import encoded
import test_daily_transport as fixture
from test_daily_card_readback import platform_view


class CardIOTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.TransportTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        f = self.f
        f.a.config['cards'] = dict(reference='P40-cards-v1', enabled=True)
        self.cards = CardStore(f.store)
        self.card = self.cards.prepare(dict(purpose='preview', document_id='om_original',
            source_sha256='a'*64, target_message_id='om_pdf', publication_id='publication_synthetic',
            pdf_sha256='b'*64, preview_version=1), '合成资料')
        path=f.root/'target.local.json';path.write_text('{}')
        self.target=dict(value={},receipt=str(path),evidence={str(path):file_sha(path)})
        self.delivery = CardDelivery(f.transport, self.cards)
        self.platform_readback = False
        self.remote = None
        self.failure = None
        f.transport.runner = self.exchange

    def exchange(self, control):
        f=self.f; f.calls.append(control)
        root=Path(control['root']);kind=control['kind'];key=control['key']+':'+kind
        events=[dict(type='intent',kind=kind,key=key)]
        failure=self.failure;self.failure=None
        if kind=='send':
            if control['mode']=='send':
                self.remote=dict(f.bot(json.loads(control['card']['content'])),msg_type='interactive',parent_id='om_pdf')
                value=dict(code=0,data=copy.deepcopy(self.remote))
            else:
                if failure not in ('patch-absent','rejected'):
                    self.remote['body']['content']=control['card']['content']
                    self.remote['updated']=True
                value=dict(code=0,data={})
        elif kind=='list':
            value=dict(code=0,data=dict(has_more=False,items=[copy.deepcopy(self.remote)] if self.remote else []))
        else:
            value=dict(code=0,data=dict(items=[copy.deepcopy(self.remote)]))
        if self.platform_readback and kind=='message':
            for item in value['data']['items']:
                item['body']['content']=encoded(platform_view(json.loads(item['body']['content'])))
        response=root/(control['key']+'.response.local')
        if failure=='rejected':
            response.write_text(encoded(dict(code=230075,msg='synthetic expired card')))
            events.append(dict(type='stop',kind=kind,key=key,http_status=400,response_sha256=file_sha(response)))
        elif failure:
            events.append(dict(type='stop',kind=kind,key=key,http_status=0))
        else:
            response.write_text(encoded(value))
            events.append(dict(type='complete',kind=kind,key=key,http_status=200,response_sha256=file_sha(response)))
        (root/'http.local.jsonl').write_text(''.join(encoded(v)+'\n' for v in events))
        if failure: raise ContractError('synthetic disconnected CLI')

    def send(self):
        self.delivery.send(self.card,self.target)
        return self.cards.get(self.card['card_id'])

    def test_unknown_send_is_found_by_original_card_readback_without_duplicate(self):
        self.failure='send-unknown'
        with self.assertRaises(Deferred):self.send()
        self.f.now+=30
        card=self.send()
        self.assertEqual(card['state'],'sent')
        self.assertEqual([c['kind'] for c in self.f.calls],['send','list','message','message'])
        self.assertEqual(sum(c['mode']=='send' for c in self.f.calls),1)

    def test_unknown_patch_already_applied_is_read_back_without_second_write(self):
        card=self.send();self.failure='patch-unknown'
        with self.assertRaises(Deferred):self.delivery.update(card,'已确认入库。')
        self.delivery.update(card,'已确认入库。')
        self.assertEqual(sum(c['mode']=='card_update' for c in self.f.calls),1)
        self.assertEqual(json.loads(self.remote['body']['content']),render_card(card,result='已确认入库。'))

    def test_unknown_unapplied_patch_retries_only_same_body_after_readback(self):
        card=self.send();self.failure='patch-absent'
        with self.assertRaises(Deferred):self.delivery.update(card,'已确认入库。')
        self.delivery.update(card,'已确认入库。')
        writes=[c for c in self.f.calls if c['mode']=='card_update']
        self.assertEqual(len(writes),2)
        self.assertEqual(writes[0]['body'],writes[1]['body'])
        self.assertEqual(self.f.calls[-3]['kind'],'message')

    def test_explicit_platform_rejection_stops_retries_but_keeps_decision(self):
        card=self.send();self.failure='rejected'
        with self.assertRaises(MaterialProblem):self.delivery.update(card,'已确认入库。')
        count=len(self.f.calls)
        with self.assertRaises(MaterialProblem):self.delivery.update(card,'已确认入库。')
        self.assertEqual(len(self.f.calls),count)

    def test_unknown_send_past_dedup_window_is_not_repeated(self):
        self.failure='send-unknown'
        with self.assertRaises(Deferred):self.send()
        self.f.now+=3601
        with self.assertRaises(MaterialProblem):self.send()
        self.assertEqual(len(self.f.calls),1)

    def test_platform_normalized_send_and_result_update(self):
        self.platform_readback=True
        card=self.send()
        self.assertEqual(card['state'],'sent')
        self.delivery.update(card,'已确认入库。')
        self.assertEqual(sum(c['mode']=='send' for c in self.f.calls),1)
        self.assertEqual(sum(c['mode']=='card_update' for c in self.f.calls),1)

    def test_normalized_unknown_send_and_applied_patch_do_not_repeat_writes(self):
        self.platform_readback=True
        self.test_unknown_send_is_found_by_original_card_readback_without_duplicate()
        card=self.cards.get(self.card['card_id']);self.failure='patch-unknown'
        with self.assertRaises(Deferred):self.delivery.update(card,'已确认入库。')
        self.delivery.update(card,'已确认入库。')
        self.assertEqual(sum(c['mode']=='card_update' for c in self.f.calls),1)

    def test_normalized_unapplied_patch_retries_same_body(self):
        self.platform_readback=True
        self.test_unknown_unapplied_patch_retries_only_same_body_after_readback()

    def test_missing_opt_in_rejects_before_request_reservation(self):
        del self.f.a.config['cards']
        with self.assertRaisesRegex(ContractError,'profile'):self.send()
        self.assertEqual(self.f.store.rows('SELECT * FROM daily_requests'),[])


if __name__=='__main__':unittest.main()
