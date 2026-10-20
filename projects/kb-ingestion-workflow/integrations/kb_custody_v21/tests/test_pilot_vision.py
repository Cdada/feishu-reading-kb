"""Synthetic image, durable budget and one-request transport regressions."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.f01 import encoded, sha
from kb_custody_v21.f05 import file_sha
from kb_custody_v21.pilot_vision import (PilotVisionSession, OfficialVisionWire,
    LIMITS, MAX_OUTPUT, MODEL, reading_png, reading_result, object_json, usage_cost)
from test_custody import BASE


def synthetic_png():
    image = Image.new('RGBA', (12, 8), (0, 0, 0, 0))
    image.putpixel((3, 3), (0, 0, 0, 255))
    stream = BytesIO(); image.save(stream, format='PNG')
    return stream.getvalue()


def model_response():
    return dict(model=MODEL, object='chat.completion',
        usage=dict(prompt_tokens=500, completion_tokens=120, total_tokens=620),
        choices=[dict(finish_reason='stop', message=dict(role='assistant', content=encoded(dict(
            transcribed_text='Synthetic black square.', interpretation='One opaque mark.',
            reading_status='partially_read', unresolved=['Synthetic small label uncertain.']))))])


class PilotVisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pilot-vision-', dir=BASE/'test-output')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()/'n10-vision'; self.root.mkdir()
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.image = self.root/'synthetic.png'; self.image.write_bytes(synthetic_png())
        item = dict(path=str(self.image), sha256=file_sha(self.image), asset_id='asset_synthetic',
            extraction_id='extraction_synthetic', source_sha256='a'*64, page=2, object='/Synthetic')
        self.a = dict(reference='N10', approved=True, root=str(self.root), model=MODEL, limits=LIMITS,
            cap_cny='3', reservation_cny='2.50', max_output_tokens=MAX_OUTPUT,
            automatic_retries=0, redirects=0, input_peak_cny_per_million='2', output_peak_cny_per_million='8',
            starts_at=(self.now-timedelta(minutes=1)).isoformat(),
            expires_at=(self.now+timedelta(hours=9)).isoformat(),
            images={'N2-figure-1':item, 'N2-figure-2':dict(item,asset_id='asset_synthetic_second',page=3)})
        self.approval = self.root/'approval.local.json'; self.approval.write_text(encoded(self.a))
        self.source = self.root/'source.synthetic'; self.source.write_text('synthetic code binding')
        self.frozen = {str(self.source):file_sha(self.source)}
        self.check_source = Mock(side_effect=lambda:dict(self.frozen))
        self.credential = Mock(return_value='synthetic-noncredential')
        self.wire = Mock(); self.wire.exchange.side_effect = self.respond
        self.response = model_response()
        self.hook = lambda kind:None

    def make(self, **options):
        c = PilotVisionSession(self.root, file_sha(self.approval), checked_source=self.check_source,
            credential=self.credential, wire=self.wire, clock=lambda:self.now, **options)
        self.addCleanup(c.close)
        return c

    def respond(self, kind, payload, credential, before_send):
        before_send()
        self.hook(kind)
        value = dict(is_available=True,balance_infos=[dict(currency='CNY',total_balance='7.00')]) if kind=='balance' else self.response
        return 200, encoded(value).encode()

    def test_two_original_aliases_keep_identity_and_white_reading_bytes(self):
        original = self.image.read_bytes(); c = self.make()
        one, two = c.read('N2-figure-1'), c.read('N2-figure-2')
        self.assertNotEqual(one['image_asset_id'], two['image_asset_id'])
        self.assertEqual(one['read_sha256'], two['read_sha256'])
        self.assertEqual(one['source_locator']['physical_page'], 2)
        self.assertEqual(two['source_locator']['physical_page'], 3)
        self.assertEqual(one['read_representation'], 'derived_white_background_png')
        self.assertEqual(one['reading_status'], 'partially_read')
        self.assertEqual(self.image.read_bytes(), original)
        result = Image.open(self.root/'N2-figure-1-white.png')
        self.assertEqual(result.getpixel((0,0)), (255,255,255))
        self.assertEqual(result.getpixel((3,3)), (0,0,0))
        self.assertEqual(c.totals(), dict(paid=2,balance=2,cost_cny=Decimal('0.00392'),reserved_cny=Decimal(0)))
        self.assertEqual(self.wire.exchange.call_count, 4)
        c.close(); c=self.make()
        with self.assertRaises(ContractError): c.read('N2-figure-1')
        self.assertEqual(self.wire.exchange.call_count,4)

    def test_scope_or_changed_input_stops_before_credentials_and_network(self):
        c = self.make()
        with self.assertRaises(ContractError): c.read('N1-image')
        self.image.write_bytes(b'changed')
        with self.assertRaises(ContractError): c.read('N2-figure-1')
        self.wire.exchange.assert_not_called(); self.credential.assert_not_called()

    def test_source_check_crosses_deadline_before_any_credential_access(self):
        c = self.make()
        def cross():
            self.now = datetime.fromisoformat(self.a['expires_at'])
            return dict(self.frozen)
        self.check_source.side_effect = cross
        with self.assertRaises(ContractError): c.read('N2-figure-1')
        self.credential.assert_not_called(); self.wire.exchange.assert_not_called()

    def test_credential_read_crosses_deadline_no_network_and_restart_blocked(self):
        c=self.make()
        def cross():
            self.now = datetime.fromisoformat(self.a['expires_at'])
            return 'synthetic-noncredential'
        self.credential.side_effect = cross
        with self.assertRaises(ContractError): c.read('N2-figure-1')
        self.wire.exchange.assert_not_called()
        self.now -= timedelta(minutes=1); c.close()
        with self.assertRaises(ContractError): self.make()

    def test_source_changes_during_response_keeps_usage_but_no_success(self):
        c = self.make()
        self.hook = lambda kind:self.source.write_text('changed') if kind=='paid' else None
        with self.assertRaises(ContractError): c.read('N2-figure-1')
        self.assertEqual(c.totals()['reserved_cny'], Decimal(0))
        self.assertEqual(c.totals()['cost_cny'], Decimal('0.00196'))
        self.assertFalse((self.root/'02-result.local.json').exists())
        self.assertTrue((self.root/'02-response.local').exists())
        self.assertEqual(self.wire.exchange.call_count,2)

    def test_paid_response_crosses_deadline_no_completed_observation_or_next_call(self):
        c = self.make()
        def cross(kind):
            if kind == 'paid': self.now = datetime.fromisoformat(self.a['expires_at'])
        self.hook = cross
        with self.assertRaises(ContractError): c.read('N2-figure-1')
        self.assertFalse((self.root/'02-result.local.json').exists())
        self.assertTrue((self.root/'02-response.local').exists())
        self.assertEqual(c.totals()['cost_cny'], Decimal('0.00196'))
        self.assertEqual(self.wire.exchange.call_count, 2)

    def test_mutated_approval_or_finished_result_blocks_new_request(self):
        c = self.make(); c.read('N2-figure-1')
        (self.root/'02-result.local.json').write_text('{}')
        with self.assertRaises(ContractError): c.read('N2-figure-2')
        self.assertEqual(self.wire.exchange.call_count, 2)
        self.approval.write_text('{}')
        with self.assertRaises(ContractError): c.balance('after')
        self.assertEqual(self.wire.exchange.call_count, 2)

    def test_timeout_keeps_reservation_and_prevents_next_figure_or_restart(self):
        c=self.make()
        def fail(kind):
            if kind=='paid': raise OSError('synthetic failure')
        self.hook=fail
        with self.assertRaises(ContractError): c.read('N2-figure-1')
        self.assertEqual(c.totals()['reserved_cny'], Decimal('2.50'))
        with self.assertRaises(ContractError): c.read('N2-figure-2')
        c.close()
        with self.assertRaises(ContractError): self.make()
        self.assertEqual(self.wire.exchange.call_count,2)

    def test_missing_usage_or_other_model_retains_unsettled_cost(self):
        c=self.make(); self.response.pop('usage')
        with self.assertRaises(ContractError): c.read('N2-figure-1')
        self.assertEqual(c.totals()['reserved_cny'],Decimal('2.50'))
        for changed in (dict(model_response(),model='different'),
                        dict(model_response(),usage=dict(prompt_tokens=1,completion_tokens=1,total_tokens=9))):
            with self.assertRaises(ContractError): usage_cost(changed)

    def test_length_finish_is_not_success_even_with_complete_billing(self):
        c=self.make(); self.response['choices'][0]['finish_reason']='length'
        with self.assertRaises(ContractError): c.read('N2-figure-1')
        self.assertEqual(c.totals()['cost_cny'], Decimal('0.00196'))
        self.assertFalse((self.root/'02-result.local.json').exists())

    def test_settled_cost_limits_next_reservation_in_same_ledger(self):
        self.response['usage']=dict(prompt_tokens=1_000_000,completion_tokens=120,total_tokens=1_000_120)
        c=self.make(); c.read('N2-figure-1')
        with self.assertRaisesRegex(ContractError,'reservation'): c.read('N2-figure-2')
        self.assertEqual(c.totals()['paid'],1)
        self.assertEqual(self.wire.exchange.call_count,3) # second balance, no paid call

    def test_low_balance_and_category_cap_never_make_a_paid_call(self):
        c=self.make()
        def low(kind,payload,key,send):
            send(); return 200, encoded(dict(is_available=True,
                balance_infos=[dict(currency='CNY',total_balance='2.49')])).encode()
        self.wire.exchange.side_effect=low
        with self.assertRaisesRegex(ContractError,'balance'): c.read('N2-figure-1')
        for i in range(4): c.balance('balance-'+str(i))
        with self.assertRaisesRegex(ContractError,'exhausted'): c.balance('exhausted')
        self.assertEqual(c.totals()['paid'],0); self.assertEqual(self.wire.exchange.call_count,5)

    def test_received_evidence_tamper_prevents_second_request(self):
        c=self.make(); c.read('N2-figure-1')
        (self.root/'02-response.local').write_text('{}')
        with self.assertRaises(ContractError): c.read('N2-figure-2')
        self.assertEqual(self.wire.exchange.call_count,2)

    def test_retrying_wire_cannot_send_twice(self):
        c=self.make()
        def retry(kind,payload,key,send):
            send(); send()
        self.wire.exchange.side_effect=retry
        with self.assertRaises(ContractError): c.read('N2-figure-1')
        self.assertEqual(sum(e.get('type')=='physical' for e in c.events),1)

    def test_real_process_death_retains_paid_intent_no_resend_after_restart(self):
        script = self.root/'crash.py'
        script.write_text('''import os, sys
from pathlib import Path
project=Path(sys.argv[3]);sys.path[:0]=[str(project),str(project/'src'),str(project.parent/'local_conversion_mcp/src')]
from dev import offline_guard
sys.addaudithook(offline_guard)
from kb_custody_v21.pilot_vision import PilotVisionSession
from kb_custody_v21.f01 import encoded
from kb_custody_v21.f05 import file_sha
class Wire:
    def exchange(self,kind,payload,key,send):
        send()
        if kind=='paid':os._exit(77)
        return 200,encoded(dict(is_available=True,balance_infos=[dict(currency='CNY',total_balance='7')])).encode()
root=Path(sys.argv[1]);source=root/'source.synthetic'
c=PilotVisionSession(root,sys.argv[2],checked_source=lambda:{str(source):file_sha(source)},credential=lambda:'synthetic',wire=Wire())
c.read('N2-figure-1')
''')
        result=subprocess.run([sys.executable,'-B',str(script),str(self.root),file_sha(self.approval),
            str(Path(__file__).resolve().parents[1])],capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,77,result.stderr)
        with self.assertRaisesRegex(ContractError,'unfinished'): self.make()
        events=[json.loads(line) for line in (self.root/'journal.local.jsonl').read_text().splitlines()]
        self.assertEqual(len([e for e in events if e.get('type')=='intent' and e['kind']=='paid']),1)
        self.assertFalse(any(e.get('type')=='settled' for e in events))
        self.credential.assert_not_called();self.wire.exchange.assert_not_called()

    def test_second_live_process_cannot_take_the_budget_lock(self):
        c=self.make()
        with self.assertRaises(BlockingIOError): self.make()
        self.wire.exchange.assert_not_called()

    def test_known_marker_failure_revalidates_existing_reply_without_request_or_new_charge(self):
        inner=object_json(self.response['choices'][0]['message']['content'])
        self.response['choices'][0]['message']['content']=encoded(dict(inner,type='json_object'))
        c=self.make()
        with patch('kb_custody_v21.pilot_vision.reading_result',side_effect=ContractError('old exact schema')):
            with self.assertRaises(ContractError):c.read('N2-figure-1')
        raw=(self.root/'02-response.local').read_bytes();before=c.totals();c.close()
        with self.assertRaises(ContractError):self.make() # explicit local revalidation only
        c=self.make(allow_local_revalidation=True)
        observed=c.revalidate_received()
        self.assertEqual(observed['format_normalization'],'type_json_object_marker')
        self.assertEqual(c.totals(),before)
        self.assertEqual((self.root/'02-response.local').read_bytes(),raw)
        self.assertEqual(self.wire.exchange.call_count,2)
        self.assertEqual(self.credential.call_count,2)
        self.assertEqual(len([e for e in c.events if e.get('type')=='stop']),1)
        c.read('N2-figure-2')
        self.assertEqual(self.wire.exchange.call_count,4)
        with self.assertRaises(ContractError):c.read('N2-figure-1')
        c.close();c=self.make();self.assertEqual(c.totals()['paid'],2)

    def test_local_revalidation_cannot_recover_unknown_or_missing_billing(self):
        c=self.make()
        self.response.pop('usage')
        with self.assertRaises(ContractError):c.read('N2-figure-1')
        c.close()
        with self.assertRaises(ContractError):self.make(allow_local_revalidation=True)
        self.assertEqual(self.wire.exchange.call_count,2)

    def test_local_revalidation_cannot_waive_a_different_content_failure(self):
        c=self.make()
        self.response['choices'][0]['finish_reason']='length'
        with self.assertRaises(ContractError):c.read('N2-figure-1')
        c.close()
        with self.assertRaises(ContractError):self.make(allow_local_revalidation=True)
        self.assertEqual(self.wire.exchange.call_count,2)


class VisionContractTests(unittest.TestCase):
    def test_only_exact_observed_metadata_marker_is_compatible(self):
        value=model_response();inner=object_json(value['choices'][0]['message']['content'])
        value['choices'][0]['message']['content']=encoded(dict(inner,type='json_object'))
        self.assertEqual(reading_result(value),inner)
        for changed in (dict(inner,type='other'),dict(inner,unexpected='ignored?'),
                        dict(inner,type='json_object',unexpected='no')):
            value['choices'][0]['message']['content']=encoded(changed)
            with self.assertRaises(ContractError):reading_result(value)

    def test_image_decoder_and_json_numeric_boundaries_fail_closed(self):
        for raw in (b'not-image',b'x'*(2*1024**2+1)):
            with self.assertRaises(ContractError): reading_png(raw)
        for raw in ('{"x":'+('9'*10000)+'}','{"a":1,"a":2}','{"a":NaN}'):
            with self.assertRaises(ContractError): object_json(raw)
        value=model_response()
        value['choices'][0]['message']['content']=encoded(dict(transcribed_text='x',interpretation='x',
            reading_status='read',unresolved=['unread small text']))
        with self.assertRaises(ContractError): reading_result(value)

    def test_wire_fixed_host_no_redirect_retry_and_final_boundary_after_tls_setup(self):
        wire=OfficialVisionWire(); guard=Mock()
        conn=Mock(); response=conn.getresponse.return_value;response.status=302
        with patch('kb_custody_v21.pilot_vision.http.client.HTTPSConnection',return_value=conn) as factory:
            self.assertEqual(wire.exchange('paid',b'{}','synthetic',guard),(302,b''))
            self.assertEqual(factory.call_args.args[0],'api.deepseek.com')
            conn.request.assert_called_once();response.read.assert_not_called();guard.assert_called_once()
        conn.reset_mock();guard.side_effect=ContractError('expired')
        with patch('kb_custody_v21.pilot_vision.http.client.HTTPSConnection',return_value=conn):
            with self.assertRaises(ContractError):wire.exchange('paid',b'{}','synthetic',guard)
        conn.request.assert_not_called();conn.close.assert_called_once()

    def test_wire_rejects_truncation_and_ambiguous_headers_without_retry(self):
        for headers,body in [([('content-length','9')],b'{}'),
                             ([('content-length','2'),('transfer-encoding','chunked')],b'{}')]:
            conn=Mock();response=conn.getresponse.return_value
            response.status=200;response.getheaders.return_value=headers;response.read.return_value=body
            with patch('kb_custody_v21.pilot_vision.http.client.HTTPSConnection',return_value=conn):
                with self.assertRaises(OSError):OfficialVisionWire().exchange('paid',b'{}','synthetic',lambda:None)
            conn.request.assert_called_once()
