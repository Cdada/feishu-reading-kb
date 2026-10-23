from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import unittest

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.qa_quality import answer_packet
from kb_custody_v21.qa_quality_subscription import SubscriptionAdapter, file_digest, save_new, MODEL
from test_qa_quality import fixture, answer


class SubscriptionTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name).resolve()/'.dev-runtime';self.root.mkdir()
        now=datetime.now(timezone.utc)
        self.approval=dict(approved=True,model=MODEL,effort='medium',billing='chatgpt_subscription_only',
            account_sha256='a'*64,minimum_remaining_percent=20,starts_at=(now-timedelta(seconds=10)).isoformat(),
            expires_at=(now+timedelta(minutes=5)).isoformat(),wait_seconds=60,max_request_bytes=32000,max_response_bytes=64000)
        self.path=self.root/'transport.json';save_new(self.path,self.approval)
        self.proof={str(self.path):file_digest(self.path)};self.packets=[]
        def exchange(packet,check):
            check();self.packets.append(packet)
            return dict(status='received',text=json.dumps(answer()),provider_model=MODEL,usage=dict(input_tokens=12,output_tokens=8))
        self.wire=SimpleNamespace(exchange=exchange)

    def adapter(self):
        return SubscriptionAdapter(self.root/'calls',approval_path=self.path,approval_sha=file_digest(self.path),wire=self.wire)

    def test_binds_exact_request_and_parent_grant_without_exposing_references(self):
        result=self.adapter()(answer_packet(fixture()),lambda:self.proof)
        self.assertEqual(result['output'],answer());self.assertEqual(len(self.packets),1)
        p=self.packets[0];child=json.loads(Path(p['binding']['approvalPath']).read_text())
        self.assertEqual(child['max_business_turns'],1);self.assertEqual(child['automatic_business_retries'],0)
        self.assertEqual(child['request_sha256'],[p['binding']['requestSha']])
        self.assertNotIn('reference',json.loads(p['request']))
        self.assertEqual(Path(p['binding']['approvalPath']).stat().st_mode & 0o777,0o600)

    def test_changed_approval_is_rejected_before_transport(self):
        adapter=self.adapter();self.path.write_text('{}')
        with self.assertRaises(ContractError):adapter(answer_packet(fixture()),lambda:self.proof)
        self.assertEqual(self.packets,[])

    def test_source_drift_oversize_and_expiry_never_call_transport(self):
        adapter=self.adapter()
        with self.assertRaises(ContractError):adapter(answer_packet(fixture()),lambda:{str(self.path):'b'*64})
        with self.assertRaises(ContractError):adapter(dict(protocol='qa-quality-answer-v1',large='x'*40000),lambda:self.proof)
        self.approval['expires_at']=self.approval['starts_at'];self.path.write_text(json.dumps(self.approval))
        with self.assertRaises(ContractError):self.adapter()
        self.assertEqual(self.packets,[])

    def test_incomplete_changed_model_and_invalid_json_fail_without_retry(self):
        for response in ({'status':'unknown'},dict(status='received',provider_model='another',text='{}'),
                         dict(status='received',provider_model=MODEL,text='bad JSON')):
            count=[]
            self.wire.exchange=lambda p,c:(count.append(p) or response)
            with self.assertRaises(ContractError):self.adapter()(answer_packet(fixture()),lambda:self.proof)
            self.assertEqual(len(count),1)

    def test_existing_receipts_are_never_overwritten(self):
        with self.assertRaises(FileExistsError):save_new(self.path,{})
        self.assertEqual(json.loads(self.path.read_text()),self.approval)
