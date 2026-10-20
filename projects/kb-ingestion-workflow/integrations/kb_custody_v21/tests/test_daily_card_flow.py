"""Synthetic platform receipts around the real custody/publication pipeline."""
import copy
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.daily_card_events import EventIngress, CardListener
from kb_custody_v21.daily_card_flow import CardFlow
from kb_custody_v21.daily_cards import render_card
from kb_custody_v21.daily_scheduler import MaterialProblem
from kb_custody_v21.daily_store import Deferred
from kb_custody_v21.f05 import file_sha
from kb_custody_v21.registry import encoded
import test_daily_backend as backend_fixture
import test_daily_images as image_fixture_module
from test_embedded_markdown import embedded
from test_markdown_bundle import png
from test_daily_card_readback import platform_view


class CardTransport(backend_fixture.LocalTransport):
    def __init__(self, old):
        super().__init__(old.a)
        self.authority = old.a
        self.calls, self.results = old.calls, old.results
        self.messages = {}
        self.platform_readback = False

    def request(self, operation, kind, path, **kwargs):
        if not kwargs.get('card'):
            return super().request(operation, kind, path, **kwargs)
        if operation in self.results:
            return self._checked_result(self.results[operation])
        self.calls.append((operation, kind))
        card = kwargs['card']
        p = self.a.policy
        if kwargs.get('mode') == 'send':
            body = json.loads(kwargs['body'])
            mid = 'om_card'+str(len(self.calls))
            item = dict(message_id=mid, chat_id=p.chat_id, parent_id=card['binding']['target_message_id'],
                msg_type='interactive', deleted=False, updated=False,
                sender=dict(id=p.bot_id, id_type='open_id', sender_type='app'), body=dict(content=body['content']))
            self.messages[mid] = item
            value = dict(code=0, data=copy.deepcopy(item))
        elif kwargs.get('mode') == 'card_update':
            self.messages[card['message_id']]['body'] = dict(content=json.loads(kwargs['body'])['content'])
            self.messages[card['message_id']]['updated'] = True
            value = dict(code=0, data={})
        else:
            value = dict(code=0, data=dict(items=[copy.deepcopy(self.messages[card['message_id']])]))
        if self.platform_readback and kwargs.get('mode','read')=='read':
            for item in value['data']['items']:
                item['body']['content']=encoded(platform_view(json.loads(item['body']['content'])))
        receipt = self.a.root/('card-http-'+str(len(self.calls))+'.local.json')
        receipt.write_text(encoded(value))
        result = dict(value=value, receipt=str(receipt), evidence={str(receipt):file_sha(receipt)})
        self.results[operation] = result
        return result


class CardFlowTests(unittest.TestCase):
    def case(self, images=False):
        if images:
            image_fixture = image_fixture_module.DailyImageTests()
            raw = ('# Synthetic\n\nSource.\n'+embedded(png())+'\n![missing](absent.png)').encode()
            f = image_fixture.case('partial.md', raw)
            self.addCleanup(image_fixture.doCleanups)
            f.advance(); f.advance()
        else:
            f = backend_fixture.DailyBackendTests()
            f.setUp()
            self.addCleanup(f.doCleanups)
            f.preview()
        f.transport = CardTransport(f.transport)
        f.backend.transport = f.transport
        f.authority.config['cards'] = dict(reference='P40-cards-v1', enabled=True)
        f.backend.card_flow = CardFlow(f.backend)
        f.backend.card_listener = SimpleNamespace(ready=True)
        return f

    def sent(self, f):
        f.backend.advance_light(f.store.document(f.mid))
        return f.backend.card_flow.cards.get(f.store.rows('SELECT card_id FROM daily_cards')[-1]['card_id'])

    def event(self, f, card, command='确认入库', eid='synthetic_event'):
        return dict(type='card.action.trigger', event_id=eid, timestamp='1000000000',
            operator_id=f.policy.account_id, chat_id=f.policy.chat_id, host='im_message',
            message_id=card['message_id'], action_tag='button',
            action_value=encoded(dict(reference='P40-card-v1', card_id=card['card_id'], command=command)))

    def ingress(self, f, event):
        session = f.root/'listener.local.json'
        session.write_text(encoded(dict(reference='synthetic-private-listener')))
        queue = EventIngress(f.backend)
        queue.persist(event, app_id=f.policy.app_id, session_path=session)
        return queue

    def test_persist_then_restart_then_button_publishes_once_and_removes_actions(self):
        f = self.case()
        card = self.sent(f)
        event = self.event(f, card)
        self.ingress(f, event)
        self.assertIsNone(f.backend.card_flow.cards.choice(card['card_id']))
        self.assertEqual(f.store.document(f.mid)['phase'], 'awaiting_confirmation')
        EventIngress(f.backend).drain()  # New ingress instance after a restart.
        f.backend.advance_light(f.store.document(f.mid))
        self.assertEqual(f.store.document(f.mid)['phase'], 'notified')
        f.backend.card_flow.report()
        calls = len(f.transport.calls)
        self.ingress(f, event).drain()
        f.backend.card_flow.report()
        self.assertEqual(len(f.transport.calls), calls)
        self.assertEqual(len(f.registry.rows('SELECT * FROM publication_confirmations')), 1)
        self.assertEqual(f.g.wire.calls, 1)
        actual = json.loads(f.transport.messages[card['message_id']]['body']['content'])
        self.assertEqual(actual, render_card(card, result='已确认入库。'))
        self.assertNotIn('button', encoded(actual))

    def test_platform_normalized_callback_and_result_publish_once(self):
        f=self.case();f.transport.platform_readback=True
        card=self.sent(f)
        event=self.event(f,card)
        self.ingress(f,event).drain()
        f.backend.advance_light(f.store.document(f.mid))
        f.backend.card_flow.report()
        self.ingress(f,event).drain()
        f.backend.card_flow.report()
        self.assertEqual(f.store.document(f.mid)['phase'],'notified')
        self.assertEqual(len(f.registry.rows('SELECT * FROM publication_confirmations')),1)
        self.assertEqual(f.g.wire.calls,1)
        actual=json.loads(f.transport.messages[card['message_id']]['body']['content'])
        self.assertNotIn('button',encoded(actual))

    def test_click_arriving_before_delivery_readback_waits_without_losing_event(self):
        f = self.case()
        binding, target = f.backend.card_flow.material(f.store.document(f.mid))
        card = f.backend.card_flow.cards.prepare(binding, f.store.document(f.mid)['filename'])
        request = f.transport.request
        saved = []
        def interrupt(operation, kind, path, **kwargs):
            result = request(operation, kind, path, **kwargs)
            if kwargs.get('card') and kwargs.get('mode') == 'send' and not saved:
                event = self.event(f, dict(card, message_id=result['value']['data']['message_id']))
                queue = self.ingress(f, event)
                queue.drain()
                self.assertIsNone(f.backend.card_flow.cards.choice(card['card_id']))
                saved.append(queue)
            return result
        f.transport.request = interrupt
        f.backend.card_flow.delivery.send(card, target)
        saved[0].drain()
        f.backend.advance_light(f.store.document(f.mid))
        self.assertEqual(f.store.document(f.mid)['phase'], 'notified')
        self.assertEqual(len(f.registry.rows('SELECT * FROM publication_confirmations')), 1)

    def test_listener_backoff_does_not_block_text_collection(self):
        f = self.case()
        f.backend.card_listener = SimpleNamespace(tick=lambda: (_ for _ in ()).throw(Deferred('retry_wait', 10)))
        with patch.object(f.backend.collector, 'collect', return_value='collected') as collect:
            self.assertEqual(f.backend.collect(), 'collected')
        collect.assert_called_once_with()
        self.assertEqual(f.store.get('card_listener_wait')['reason'], 'retry_wait')

    def test_saved_button_wins_over_conflicting_text_without_second_publication(self):
        f = self.case(); card = self.sent(f)
        self.ingress(f, self.event(f, card)).drain()
        f.owner_reply(f.store.document(f.mid), '暂不入库')
        f.backend.advance_light(f.store.document(f.mid))
        self.assertEqual(f.store.document(f.mid)['phase'], 'notified')
        self.assertIsNotNone(f.store.get('card-conflict:'+card['card_id']))
        self.assertEqual(len(f.registry.rows('SELECT * FROM publication_confirmations')), 1)

    def test_defer_and_supplement_do_not_publish(self):
        for command in ('暂不入库', '补充材料后再入库'):
            with self.subTest(command=command):
                f = self.case(); card = self.sent(f)
                self.ingress(f, self.event(f, card, command)).drain()
                f.backend.advance_light(f.store.document(f.mid))
                f.backend.card_flow.report()
                self.assertEqual(f.store.document(f.mid)['phase'], 'rejected')
                self.assertEqual(len(f.registry.rows('SELECT * FROM publication_confirmations')), 0)

    def test_verified_text_works_before_listener_and_card_delivery(self):
        f = self.case()
        f.backend.card_listener.ready = False
        with self.assertRaisesRegex(Deferred, 'card_listener_not_ready'):
            f.backend.advance_light(f.store.document(f.mid))
        f.owner_reply(f.store.document(f.mid), '确认当前版本入库')
        f.backend.advance_light(f.store.document(f.mid))
        self.assertEqual(f.store.document(f.mid)['phase'], 'notified')
        self.assertFalse(f.transport.messages)

    def test_missing_image_buttons_continue_or_request_new_original_without_model(self):
        for command, phase in (('按缺图版本继续', 'converted'), ('重新发文件', 'needs_upload')):
            with self.subTest(command=command):
                f = self.case(images=True); card = self.sent(f)
                self.ingress(f, self.event(f, card, command)).drain()
                f.backend.advance_light(f.store.document(f.mid))
                f.backend.card_flow.report()
                self.assertEqual(f.store.document(f.mid)['phase'], phase)
                self.assertEqual(f.g.wire.calls, 0)
                self.assertEqual(len(f.registry.rows('SELECT * FROM publication_confirmations')), 0)

    def test_event_evidence_mutation_fails_before_decision(self):
        f = self.case(); card = self.sent(f)
        q = self.ingress(f, self.event(f, card))
        (f.root/'listener.local.json').write_text('{}')
        with self.assertRaisesRegex(ContractError, 'evidence changed'): q.drain()
        self.assertIsNone(f.backend.card_flow.cards.choice(card['card_id']))

    def test_foreign_actor_and_changed_remote_card_cannot_confirm(self):
        f = self.case(); card = self.sent(f)
        event = self.event(f, card); event['operator_id'] = 'ou_other'
        with self.assertRaisesRegex(ContractError, 'scope differs'): self.ingress(f, event)
        q = self.ingress(f, self.event(f, card))
        f.transport.messages[card['message_id']]['body']['content'] = '{}'
        with self.assertRaisesRegex(ContractError, 'registered definition'): q.drain()
        self.assertIsNone(f.backend.card_flow.cards.choice(card['card_id']))

    def test_display_failure_fallback_survives_interruption_without_republishing(self):
        f = self.case(); card = self.sent(f)
        self.ingress(f, self.event(f, card)).drain()
        f.backend.advance_light(f.store.document(f.mid))
        with patch.object(f.backend.card_flow.delivery, 'update', side_effect=MaterialProblem('edit expired')):
            with patch.object(f.backend, 'notice', side_effect=Deferred('offline')):
                with self.assertRaises(Deferred): f.backend.card_flow.report()
            f.backend.card_flow.report()
        self.assertEqual(f.store.get('card-display:'+card['card_id'])['status'], 'fallback_verified')
        self.assertEqual(len(f.registry.rows('SELECT * FROM publication_confirmations')), 1)
        self.assertEqual(f.g.wire.calls, 1)

    def test_listener_reserves_bootstrap_and_upgrade_before_ready_and_persists_before_ack(self):
        f = self.case(); card = self.sent(f)
        listener = CardListener(f.backend)
        listener.session = 'synthetic'
        listener.session_path = f.root/'listener.local.json'
        listener.session_path.write_text('{}')
        values = [dict(type='reserve', kind='event_bootstrap', path='/callback/ws/endpoint'),
            dict(type='http_complete', kind='event_bootstrap', status=200),
            dict(type='reserve', kind='event_upgrade', path='websocket'), dict(type='ready'),
            dict(type='event', app_id=f.policy.app_id, event=self.event(f, card))]
        raw = ''.join(encoded(dict(v, sequence=i+1))+'\n' for i,v in enumerate(values)).encode()
        listener.process = SimpleNamespace(stdout=io.BytesIO(raw), stdin=io.BytesIO(), poll=lambda:None)
        before = f.store.rows('SELECT COALESCE(sum(count),0) AS n FROM daily_requests')[0]['n']
        listener._read()
        self.assertIsNone(listener.failure)
        self.assertTrue(listener.ready)
        self.assertEqual(f.store.rows('SELECT COALESCE(sum(count),0) AS n FROM daily_requests')[0]['n']-before, 2)
        self.assertEqual(len(f.store.rows('SELECT * FROM daily_card_inbox')), 1)
        acks = [json.loads(v) for v in listener.process.stdin.getvalue().splitlines()]
        self.assertEqual(acks[-1], dict(sequence=5, ok=True))
        self.assertIsNone(f.backend.card_flow.cards.choice(card['card_id']))


if __name__ == '__main__': unittest.main()
