"""Owned child lifecycle and request budgeting without a running process."""
import io
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.daily_card_events import CardListener
from kb_custody_v21.daily_store import Deferred
import test_daily_transport as fixture


class ListenerTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.TransportTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.backend=SimpleNamespace(authority=self.f.a,store=self.f.store,
            check_authorization=lambda:None,stop=threading.Event())
        self.listener=CardListener(self.backend)
        self.listener.session='synthetic'
        self.listener.session_path=self.f.root/'listener.local.json'
        self.listener.session_path.write_text('{}')

    def test_ready_and_event_require_successful_reserved_handshake(self):
        for message in (dict(type='ready'),dict(type='event',event={})):
            with self.assertRaises(ContractError):self.listener._handle(message)
        self.assertFalse(self.listener.ready)

    def test_stop_and_exhausted_http_budget_prevent_grant(self):
        message=dict(type='reserve',kind='event_bootstrap',path='/callback/ws/endpoint',sequence=1)
        self.backend.stop.set()
        with self.assertRaisesRegex(Deferred,'stopping'):self.listener._handle(message)
        self.backend.stop.clear()
        s=self.f.store;s.operation('occupied','read',{});s.begin('occupied',physical_requests=60)
        with self.assertRaisesRegex(Deferred,'request_rate'):self.listener._handle(message)
        self.assertFalse(self.listener.pending_http)
        self.assertEqual(s.rows('SELECT sum(count) AS n FROM daily_requests')[0]['n'],60)

    def test_close_terminates_only_owned_child_and_preserves_charged_attempt(self):
        listener=self.listener;s=self.f.store
        listener.operation='owned';s.operation('owned','events',{});listener.attempt=s.begin('owned')
        listener._handle(dict(type='reserve',kind='event_bootstrap',path='/callback/ws/endpoint',sequence=1))
        process=Mock(stdin=io.BytesIO(),stdout=io.BytesIO())
        process.wait.return_value=1
        listener.process=process
        listener.reader=Mock();listener.reader.is_alive.return_value=False
        listener.close()
        process.terminate.assert_called_once_with();process.wait.assert_called_once_with(timeout=5)
        process.kill.assert_not_called()
        self.assertIsNone(listener.process)
        self.assertEqual(s.rows('SELECT sum(count) AS n FROM daily_requests')[0]['n'],1)
        self.assertEqual(s.get('card_listener_status')['state'],'stopped')
        self.assertEqual(s.rows("SELECT attempts FROM daily_operations WHERE op_key='owned'")[0]['attempts'],1)

    def test_storage_pause_keeps_listener_start_and_renewal_without_processing(self):
        listener=self.listener
        listener.ingress.drain=Mock(side_effect=AssertionError('content processing must wait'))
        listener._start=Mock();listener._finish=Mock()
        listener.tick(process_events=False)
        listener._start.assert_called_once_with()
        listener.process=Mock();listener.process.poll.return_value=0
        listener.tick(process_events=False)
        listener._finish.assert_called_once_with(0)
        self.assertEqual(listener._start.call_count,2)
        listener.ingress.drain.assert_not_called()

    def test_changed_or_out_of_order_protocol_cannot_receive_ack(self):
        process=SimpleNamespace(stdin=io.BytesIO(),stdout=io.BytesIO(b'{"type":"check","sequence":2}\n'))
        self.listener.process=process;self.listener._read()
        self.assertIsInstance(self.listener.failure,ContractError)
        self.assertTrue(process.stdin.closed)
        self.assertEqual(self.f.store.get('card_listener_status')['state'],'failed')


if __name__=='__main__':unittest.main()
