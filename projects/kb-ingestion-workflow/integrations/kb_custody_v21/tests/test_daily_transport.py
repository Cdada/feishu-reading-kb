"""Fault injection at the one-shot CLI boundary, with no network capability."""
from datetime import datetime,timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.daily_policy import DailyPolicy
from kb_custody_v21.daily_scheduler import MaterialProblem
from kb_custody_v21.daily_store import DailyStore,Deferred
from kb_custody_v21.daily_transport import DailyTransport
from kb_custody_v21.f05 import file_sha
from kb_custody_v21.registry import encoded


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()
        self.now=1000000.
        self.policy=DailyPolicy('cli_synthetic','oc_synthetic','ou_owner','ou_bot',3)
        rules=self.root/'rules.local.json';rules.write_text('{}')
        self.a=SimpleNamespace(root=self.root,path=rules,digest=file_sha(rules),policy=self.policy,
            config=dict(cli_profile='synthetic'),check=lambda:{str(rules):file_sha(rules)})
        self.store=DailyStore(self.root/'queue.sqlite3',self.policy,clock=lambda:self.now)
        self.calls=[]
        self.failure=None
        self.items=[]
        self.transport=DailyTransport(self.a,self.store,runner=self.exchange)

    def exchange(self,control):
        self.calls.append(control)
        root=Path(control['root']);kind=control['kind'];key=control['key']+':'+kind
        fail=self.failure
        self.failure=None
        actual='auth' if fail=='auth-offline' else kind
        events=[dict(type='intent',kind=actual,key=control['key']+':'+actual)]
        if fail and fail!='completed-then-crashed':
            status=429 if fail=='rate' else 0
            events.append(dict(type='stop',kind=actual,key=control['key']+':'+actual,http_status=status,retry_after_seconds=120 if fail=='rate' else 0))
        else:
            if kind=='send':
                payload=json.loads(control['body'])
                content=json.loads(payload['content'])
                value=dict(code=0,data=self.bot(content))
            elif kind=='list':
                value=dict(code=0,data=dict(items=self.items,has_more=False))
            else:
                value=dict(code=0,data={})
            response=root/(control['key']+'.response.local')
            response.write_text(encoded(value))
            events.append(dict(type='complete',kind=kind,key=key,http_status=200,response_sha256=file_sha(response)))
        (root/'http.local.jsonl').write_text(''.join(encoded(e)+'\n' for e in events))
        if fail:
            raise ContractError('synthetic interrupted CLI')
        return dict(ok=True)

    def bot(self,content):
        return dict(message_id='om_sent',chat_id='oc_synthetic',msg_type='text',deleted=False,updated=False,
            create_time=str(int(self.now*1000)),sender=dict(id='ou_bot',id_type='open_id',sender_type='app'),
            body=dict(content=encoded(content)))

    def read(self):
        return self.transport.request('history','list','/open-apis/im/v1/messages?synthetic')

    def send(self):
        return self.transport.request('notice','send','/open-apis/im/v1/messages?receive_id_type=chat_id',mode='send',
            body=encoded(dict(receive_id='oc_synthetic',msg_type='text',uuid='stable-synthetic',content=encoded(dict(text='合成完成通知')))))

    def test_transient_read_retains_failed_attempt_and_retries_after_delay(self):
        self.failure='offline'
        with self.assertRaises(Deferred):self.read()
        with self.assertRaises(Deferred):self.read()
        self.assertEqual(len(self.calls),1)
        self.now+=5
        result=self.read()
        self.assertEqual(len(self.calls),2)
        self.assertEqual(result['value']['code'],0)
        self.assertTrue((Path(self.calls[0]['root'])/'http.local.jsonl').exists())
        self.assertEqual(self.store.rows('SELECT attempts FROM daily_operations')[0]['attempts'],2)

    def test_auth_offline_does_not_misclassify_unsent_notice(self):
        self.failure='auth-offline'
        with self.assertRaises(Deferred):self.send()
        self.assertEqual(self.store.rows('SELECT state FROM daily_operations')[0]['state'],'retry')
        self.now+=5
        self.send()
        self.assertEqual(len(self.calls),2)

    def test_rate_limit_uses_provider_wait(self):
        self.failure='rate'
        with self.assertRaises(Deferred):self.read()
        self.now+=119
        with self.assertRaises(Deferred):self.read()
        self.now+=1
        self.read()
        self.assertEqual(len(self.calls),2)

    def test_completed_write_receipt_survives_crashed_parent_without_resend(self):
        self.failure='completed-then-crashed'
        with self.assertRaises(Deferred):self.send()
        self.send()
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.store.rows('SELECT state FROM daily_operations')[0]['state'],'complete')

    def test_unknown_notice_found_in_history_is_not_resent(self):
        self.failure='offline'
        with self.assertRaises(Deferred):self.send()
        self.items=[self.bot(dict(text='合成完成通知'))]
        self.now+=3601
        result=self.send()
        self.assertEqual(result['reconciled_from'],'verified_chat_history')
        self.assertTrue(result['receipt'].endswith('-list.response.local'))
        self.assertEqual([c['kind'] for c in self.calls],['send','list'])

    def test_unknown_absent_notice_reuses_same_uuid_only_within_hour(self):
        self.failure='offline'
        with self.assertRaises(Deferred):self.send()
        self.now+=30
        self.send()
        self.assertEqual([c['kind'] for c in self.calls],['send','list','send'])
        self.assertEqual(self.calls[0]['body'],self.calls[-1]['body'])
        deadline=datetime.fromisoformat(self.calls[-1]['expires_at']).timestamp()
        self.assertLess(deadline,1000000+3600)

    def test_absent_notice_after_dedup_period_remains_unresolved(self):
        self.failure='offline'
        with self.assertRaises(Deferred):self.send()
        self.now+=3601
        with self.assertRaises(MaterialProblem):self.send()
        self.assertEqual([c['kind'] for c in self.calls],['send','list'])
        self.assertEqual(self.store.rows("SELECT state FROM daily_operations WHERE op_key='notice'")[0]['state'],'unknown')

    def test_fifteenth_failure_stops_attempting(self):
        for n in range(1,16):
            self.failure='rate'
            with self.assertRaises(Deferred):self.read()
            self.now+=max(120,self.policy.retry_delay(n))
        with self.assertRaises(MaterialProblem):self.read()
        self.assertEqual(len(self.calls),15)

    def test_changed_cached_response_is_not_a_success(self):
        result=self.read()
        Path(result['receipt']).write_text('{}')
        with self.assertRaises(ContractError):self.read()
        self.assertEqual(len(self.calls),1)

    def test_pdf_download_grant_requires_explicit_parent_profile(self):
        captured=[]
        self.transport.runner=lambda control:captured.append(json.loads(Path(control['session']).read_text()))
        self.transport._receipt=lambda *args:dict(value=dict(synthetic=True))
        self.transport._checked_result=lambda result:result
        for enabled in (False,True):
            if enabled:self.a.config['pdf']={'synthetic':True}
            self.transport.request('download-'+str(enabled),'download','/open-apis/im/v1/messages/om_synthetic/resources/file_synthetic?type=file',
                download=dict(reference='P19-inbox-download-v1',message_id='om_synthetic',file_key='file_synthetic',
                              filename='paper.pdf',max_asset_bytes=self.policy.max_asset_bytes))
            self.assertEqual('.pdf' in captured[-1]['inbox']['formats'],enabled)
            self.assertEqual(captured[-1]['daily_parent']['rules_sha256'],self.a.digest)
            self.assertEqual(datetime.fromisoformat(captured[-1]['expires_at']).timestamp()-self.now,1200)
            self.assertEqual(captured[-1]['maximum_total'],2)
            self.assertEqual(captured[-1]['automatic_retries'],0)
        self.read()
        self.assertEqual(datetime.fromisoformat(captured[-1]['expires_at']).timestamp()-self.now,600)


if __name__=='__main__':unittest.main()
