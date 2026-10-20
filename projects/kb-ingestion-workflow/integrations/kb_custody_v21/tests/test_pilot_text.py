from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import shutil
import subprocess
import unittest
from unittest.mock import patch

from fixtures_guide import SyntheticGuide, SyntheticQuoteGuide
from kb_custody_v21.content import ReadingGuideAdapter, empty_record
from kb_custody_v21.contracts import ContractError
from kb_custody_v21.conversion import sha
from kb_custody_v21.pilot_text import MODEL, PilotTextSession, CodexTextWire, load_validated_guide
from kb_custody_v21.pilot_text_grounding import EVIDENCE_PROTOCOL
from kb_custody_v21.registry import encoded


class Wire:
    def __init__(self):
        self.calls = 0
        self.mutate = lambda result: result

    def exchange(self, packet, check):
        check()
        self.calls += 1
        self.packet = packet
        response = SyntheticQuoteGuide().generate(json.loads(packet['request']))
        return self.mutate(dict(status='received', text=encoded(response)))


class PilotTextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()/'.dev-runtime/pilot-text-synthetic'; self.root.mkdir(parents=True)
        self.clock = datetime.now(timezone.utc)
        self.body = '街道树荫影响步行路线。仅供合成测试。'.encode()
        self.mapping = dict(extraction_id='extract-test', output_asset_id='asset-test', output_sha256=sha(self.body))
        self.record = empty_record('合成街道研究', 'synthetic-title-confirmation')
        self.consent = dict(event_id='synthetic-consent', asset_id='asset-test', sha256=sha(self.body),
            extraction_id='extract-test', ranges=[[0,len(self.body.decode())]], model=MODEL,
            expires_at=(self.clock+timedelta(minutes=10)).isoformat())
        request = ReadingGuideAdapter(PilotTextSession).prepare_request(self.record,self.body,self.mapping,self.consent)
        self.key = sha(encoded(request).encode())
        self.approval = dict(approved=True, root=str(self.root), model=MODEL, effort='medium',
            evidence_protocol=EVIDENCE_PROTOCOL,
            billing='chatgpt_subscription_only', automatic_business_retries=0, max_business_turns=2,
            wait_seconds=600, starts_at=(self.clock-timedelta(minutes=1)).isoformat(),
            expires_at=(self.clock+timedelta(minutes=20)).isoformat(), request_sha256=[self.key],
            account_sha256=sha(b'synthetic-account@example.invalid'))
        self.source = self.root/'synthetic-source.txt'; self.source.write_bytes(self.body)
        self.source_check = lambda: {str(self.source): sha(self.body)}
        self.wire = Wire()
        self.session = self.open_session()

    def open_session(self):
        p = self.root/'approval.local.json'
        if not p.exists(): p.write_text(encoded(self.approval))
        session = PilotTextSession(self.root,sha(p.read_bytes()),
            checked_source=lambda: self.source_check(), wire=self.wire,clock=lambda: self.clock)
        self.addCleanup(session.close)
        return session

    def generate(self):
        return self.session.generate_guide(self.record,self.body,self.mapping,self.consent)

    def state(self):
        return self.session.events[-1].get('state')

    def load(self):
        self.session.close()
        return load_validated_guide(self.root,sha((self.root/'approval.local.json').read_bytes()),
            self.record,self.body,self.mapping,self.consent,checked_source=self.source_check)

    def test_saved_success_reused_after_expiry_without_new_identity_or_call(self):
        expected=self.generate()
        self.clock+=timedelta(hours=2)
        self.session.close()
        with self.assertRaisesRegex(ContractError,'outside'):self.open_session()
        with patch('kb_custody_v21.content.datetime',wraps=datetime) as current, patch.object(
                self.wire,'exchange',side_effect=AssertionError('must not send')):
            current.now.return_value=self.clock
            self.assertEqual(self.load(),expected)

    def test_local_reuse_requires_terminal_validation_event(self):
        self.wire.mutate=lambda _: (_ for _ in ()).throw(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt): self.generate()
        with self.assertRaisesRegex(ContractError,'complete validated'): self.load()

    def test_raw_quotes_and_computed_grounding_are_separately_preserved(self):
        expected=self.generate()
        raw=json.loads((self.root/(self.key+'.response.local.json')).read_bytes())
        grounded=json.loads((self.root/(self.key+'.grounded.local.json')).read_bytes())
        self.assertEqual(raw['evidence'][0]['source_index'],0)
        self.assertNotIn('locator',raw['evidence'][0])
        self.assertEqual(grounded['evidence'][0]['locator'],dict(char_start=0,char_end=len(self.body.decode())))
        self.assertEqual(self.session.events[-1]['evidence_protocol'],EVIDENCE_PROTOCOL)
        self.assertEqual(self.load(),expected)

    def test_small_provider_response_cannot_expand_beyond_saved_receipt_limit(self):
        def many(v):
            value=json.loads(v['text']);first=value['evidence'][0]
            value['evidence']=[dict(first,evidence_id='e'+str(i)) for i in range(10000)]
            value['claims'][0]['evidence_refs']=['e0']
            value['evidence_links'][0]['evidence_refs']=['e0']
            raw=encoded(value)
            self.assertLess(len(raw.encode()),2*1024**2)
            return dict(v,text=raw)
        self.wire.mutate=many
        with self.assertRaisesRegex(ContractError,'received_rejected'):self.generate()
        self.assertTrue((self.root/(self.key+'.response.local.json')).exists())
        self.assertFalse((self.root/(self.key+'.validated.local.json')).exists())
        self.assertEqual(self.wire.calls,1)

    def test_local_reuse_recomputes_grounding_even_if_saved_hash_was_changed(self):
        self.generate()
        path=self.root/(self.key+'.grounded.local.json')
        value=json.loads(path.read_bytes());value['evidence'][0]['locator']['char_start']=1
        raw=encoded(value).encode();path.write_bytes(raw)
        self.session.events[-1]['grounded_sha256']=sha(raw)
        self.session.path.write_text(''.join(encoded(v)+'\n' for v in self.session.events))
        with self.assertRaisesRegex(ContractError,'grounding changed'):self.load()

    def test_old_validated_receipt_remains_readable_without_new_model_call(self):
        from types import SimpleNamespace
        self.session.close()
        approval=dict(self.approval);approval.pop('evidence_protocol')
        (self.root/'approval.local.json').write_text(encoded(approval))
        provider=SimpleNamespace(model=MODEL,version='pilot-text/v1',is_mock=False)
        adapter=ReadingGuideAdapter(provider)
        request=adapter.prepare_request(self.record,self.body,self.mapping,self.consent)
        response=SyntheticGuide().generate(request)
        record,curation,guide=adapter.validate_response(self.record,self.body,self.mapping,self.consent,request,response)
        for suffix,value in [('request',request),('response',response),('validated',dict(record=record,curation=curation,guide=guide))]:
            (self.root/(self.key+'.'+suffix+'.local.json')).write_text(encoded(value))
        events=[dict(approval_sha256=sha((self.root/'approval.local.json').read_bytes())),
            dict(request=self.key,state='intent',at=self.clock.isoformat()),
            dict(request=self.key,state='received',response_sha256=sha(encoded(response).encode())),
            dict(request=self.key,state='validated',output_sha256=sha(encoded(dict(record=record,curation=curation,guide=guide)).encode()))]
        self.session.path.write_text(''.join(encoded(v)+'\n' for v in events))
        self.assertEqual(self.load(),(record,curation,guide))
        self.assertEqual(self.wire.calls,0)

    def test_received_rejected_legacy_response_cannot_be_promoted_or_resubmitted(self):
        def old_response(_):
            request=json.loads(self.wire.packet['request'])
            value=SyntheticGuide().generate(request)
            value['evidence'][0]['locator']['char_start']=1
            value['evidence'][0]['source_id']='synthetic-wrong-source'
            return dict(status='received',text=encoded(value))
        self.wire.mutate=old_response
        with self.assertRaisesRegex(ContractError,'received_rejected'):self.generate()
        with self.assertRaisesRegex(ContractError,'complete validated'):self.load()
        self.session=self.open_session()
        with self.assertRaisesRegex(ContractError,'existing'):self.generate()
        self.assertEqual(self.wire.calls,1)

    def test_local_reuse_rejects_changed_raw_response(self):
        self.generate()
        (self.root/(self.key+'.response.local.json')).write_bytes(b'{}')
        with self.assertRaisesRegex(ContractError,'changed'): self.load()

    def test_local_reuse_revalidates_bound_output_not_just_its_hash(self):
        self.generate()
        path=self.root/(self.key+'.validated.local.json')
        saved=json.loads(path.read_bytes());saved['guide']='invented guide'
        raw=encoded(saved).encode();path.write_bytes(raw)
        events=self.session.events;events[-1]['output_sha256']=sha(raw)
        self.session.path.write_text(''.join(encoded(v)+'\n' for v in events))
        with self.assertRaisesRegex(ContractError,'no longer agrees'): self.load()

    def test_local_reuse_rejects_changed_input_metadata(self):
        self.generate(); self.record['work']['title']='changed'
        with self.assertRaisesRegex(ContractError,'complete validated'): self.load()

    def test_mock_gate_remains_and_explicit_route_validates_evidence(self):
        with self.assertRaisesRegex(ContractError,'DEV'):
            ReadingGuideAdapter(self.session).generate(self.record,self.body,self.mapping,self.consent)
        _, curation, _ = self.generate()
        self.assertEqual(curation['guide_generation']['validation_mode'],'real_content_pending_review')
        self.assertEqual(self.state(),'validated')
        self.assertEqual(self.wire.calls,1)
        self.assertEqual(self.wire.packet['model'],MODEL)
        self.assertEqual(self.session.events[-1]['internal_http_attempts'],'not_observable')
        self.assertTrue((self.root/(self.key+'.validated.local.json')).exists())

    def test_exact_metadata_and_slice_changes_rejected_without_send(self):
        self.record['work']['title'] = 'Changed synthetic title'
        with self.assertRaisesRegex(ContractError,'differs'): self.generate()
        self.assertEqual(self.wire.calls,0)

    def test_repeat_after_restart_never_submits_again(self):
        self.generate(); self.session.close(); self.session = self.open_session()
        with self.assertRaisesRegex(ContractError,'existing'): self.generate()
        self.assertEqual(self.wire.calls,1)

    def test_interruption_keeps_intent_and_restart_no_resend(self):
        self.wire.mutate = lambda _: (_ for _ in ()).throw(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt): self.generate()
        self.assertEqual(self.state(),'intent')
        self.session.close(); self.session = self.open_session()
        with self.assertRaisesRegex(ContractError,'existing'): self.generate()
        self.assertEqual(self.wire.calls,1)

    def test_transport_unknown_is_not_received_or_success(self):
        self.wire.mutate = lambda _: (_ for _ in ()).throw(OSError('synthetic timeout'))
        with self.assertRaisesRegex(ContractError,'unknown'): self.generate()
        self.assertEqual(self.state(),'unknown')
        self.assertFalse((self.root/(self.key+'.response.local.json')).exists())

    def test_invalid_quote_retains_received_evidence_without_validation(self):
        def mutate(v):
            result=json.loads(v['text']); result['evidence'][0]['quote']='not in source'
            return dict(v,text=encoded(result))
        self.wire.mutate=mutate
        with self.assertRaisesRegex(ContractError,'received_rejected'): self.generate()
        self.assertEqual(self.state(),'received_rejected')
        self.assertTrue((self.root/(self.key+'.response.local.json')).exists())
        self.assertFalse((self.root/(self.key+'.validated.local.json')).exists())

    def test_long_integer_json_is_redacted_contract_error(self):
        self.wire.mutate=lambda _: dict(status='received',text='{"huge":'+('1'*5000)+'}')
        with self.assertRaisesRegex(ContractError,'received_rejected'): self.generate()

    def test_source_check_crosses_window_before_access(self):
        def expired():
            self.clock += timedelta(hours=1)
            return {str(self.source):sha(self.body)}
        self.source_check=expired
        with self.assertRaisesRegex(ContractError,'outside'): self.generate()
        self.assertEqual(self.wire.calls,0)

    def test_shorter_content_consent_expiry_during_response(self):
        def expired(v):
            self.clock += timedelta(minutes=11)
            return v
        self.wire.mutate=expired
        with self.assertRaisesRegex(ContractError,'received_rejected'): self.generate()
        self.assertNotEqual(self.state(),'validated')

    def test_final_validation_source_change_prevents_success(self):
        original=ReadingGuideAdapter.validate_response
        def corrupt(*args):
            result=original(*args); self.source.write_bytes(b'corrupt'); return result
        with patch.object(ReadingGuideAdapter,'validate_response',corrupt):
            with self.assertRaisesRegex(ContractError,'received_rejected'): self.generate()
        self.assertNotEqual(self.state(),'validated')

    def test_preflight_block_recorded_without_fabricated_unknown(self):
        self.wire.mutate=lambda _: dict(status='blocked_before_turn',stage='preflight')
        with self.assertRaisesRegex(ContractError,'blocked_before_turn'): self.generate()
        self.assertEqual(self.state(),'blocked_before_turn')

    def test_safe_preflight_diagnosis_persists_without_reopening_budget(self):
        self.wire.mutate=lambda _: dict(status='blocked_before_turn',stage='preflight',
            checkpoint='subscription_check',reason='quota_threshold_reached',
            message='synthetic-private-token',account={'email':'synthetic@example.invalid'})
        with self.assertRaisesRegex(ContractError,'blocked_before_turn'): self.generate()
        event=self.session.events[-1]
        self.assertEqual(event['diagnostic'],dict(stage='preflight',
            checkpoint='subscription_check',reason='quota_threshold_reached'))
        self.assertNotIn('synthetic-private',self.session.path.read_text())
        self.session.close();self.session=self.open_session()
        with self.assertRaisesRegex(ContractError,'no automatic resubmission'): self.generate()
        self.assertEqual(self.wire.calls,1)

    def test_untrusted_diagnostic_fields_are_not_persisted(self):
        self.wire.mutate=lambda _: dict(status='blocked_before_turn',stage='preflight',
            checkpoint={'secret':'synthetic-private'},reason='synthetic-private',
            cleanup=['synthetic-private'],text='synthetic-private')
        with self.assertRaisesRegex(ContractError,'blocked_before_turn'): self.generate()
        self.assertEqual(self.session.events[-1]['diagnostic'],dict(stage='preflight'))
        self.assertNotIn('synthetic-private',self.session.path.read_text())
        self.assertEqual(self.state(),'blocked_before_turn')

    def test_budget_persists_and_does_not_reset(self):
        self.session.append(dict(state='intent',request='1'*64))
        self.session.append(dict(state='received_rejected',request='1'*64))
        self.session.append(dict(state='intent',request='2'*64))
        self.session.append(dict(state='blocked_before_turn',request='2'*64))
        self.session.close(); self.session=self.open_session()
        with self.assertRaisesRegex(ContractError,'budget'): self.generate()
        self.assertEqual(self.wire.calls,0)

    def test_real_wire_offline_gate_precedes_spawn(self):
        with patch.dict('os.environ',KB_V21_OFFLINE_TEST='1'), patch('subprocess.Popen',side_effect=AssertionError()):
            with self.assertRaisesRegex(ContractError,'offline'):
                CodexTextWire('node','unused','codex','unused').exchange({},lambda:None)

    def test_first_node_start_filters_preload_and_environment(self):
        marker=self.root/'unexpected-preload.txt'
        preload=self.root/'synthetic-preload.cjs'
        preload.write_text("require('node:fs').writeFileSync("+json.dumps(str(marker))+",'preloaded');")
        script=self.root/'synthetic-environment.mjs'
        script.write_text("process.stdout.write(JSON.stringify({environment:process.env}));")
        environment=dict(HOME=str(self.root),PATH='/usr/bin:/bin',TMPDIR=str(self.root),
            LANG='C',LC_ALL='C',USER='synthetic',LOGNAME='synthetic',CODEX_HOME=str(self.root/'codex'),
            NODE_OPTIONS='--require='+str(preload),NODE_PATH=str(self.root),
            OPENAI_API_KEY='synthetic-placeholder',HTTPS_PROXY='https://example.invalid',
            KB_V21_OFFLINE_TEST='0')
        node=shutil.which('node')
        self.assertIsNotNone(node)
        # Only a fixed local environment-reporting fixture is launched; no client/model.
        with patch.dict('os.environ',environment,clear=True), patch('subprocess.Popen',wraps=subprocess.Popen) as popen:
            response=CodexTextWire(node,script,'unused',self.root).exchange({'waitMs':1000},lambda:None)
        self.assertFalse(marker.exists(),'NODE_OPTIONS ran before the JavaScript filter')
        allowed=('HOME','PATH','TMPDIR','LANG','LC_ALL','USER','LOGNAME','CODEX_HOME')
        self.assertEqual(popen.call_args.kwargs['env'],{k:environment[k] for k in allowed})
        # The OS may add runtime metadata (for example macOS text encoding).
        self.assertEqual({k:response['environment'][k] for k in allowed},{k:environment[k] for k in allowed})
        for key in set(environment)-set(allowed):
            self.assertNotIn(key,response['environment'])

    def test_failed_final_window_after_validation_has_no_success_event(self):
        original=ReadingGuideAdapter.validate_response
        def expired(*args):
            result=original(*args); self.clock+=timedelta(hours=1); return result
        with patch.object(ReadingGuideAdapter,'validate_response',expired):
            with self.assertRaisesRegex(ContractError,'received_rejected'): self.generate()
        self.assertNotEqual(self.state(),'validated')

    def node_wire(self, mode):
        marker=self.root/'synthetic-submission.txt'
        def exchange(packet,check):
            check()
            response=SyntheticQuoteGuide().generate(json.loads(packet['request']))
            child=subprocess.run([shutil.which('node'),str(Path(__file__).with_name('pilot_text_wire_fixture.mjs'))],
                input=encoded(dict(packet=packet,response=encoded(response),mode=mode,marker=str(marker))),
                text=True,capture_output=True,timeout=10)
            if child.returncode: raise OSError('synthetic child exited')
            return json.loads(child.stdout)
        self.wire.exchange=exchange
        return marker

    def test_python_node_synthetic_transport_validates_chinese_payload(self):
        marker=self.node_wire('success')
        self.generate()
        self.assertEqual(marker.read_text(),'synthetic submission')
        self.assertEqual(self.state(),'validated')

    def test_real_child_death_after_synthetic_submission_is_not_retried(self):
        marker=self.node_wire('crash')
        with self.assertRaisesRegex(ContractError,'unknown'): self.generate()
        self.assertEqual(marker.read_text(),'synthetic submission')
        self.session.close();self.session=self.open_session()
        with patch.object(self.wire,'exchange',side_effect=AssertionError('must not run')):
            with self.assertRaisesRegex(ContractError,'existing'): self.generate()
