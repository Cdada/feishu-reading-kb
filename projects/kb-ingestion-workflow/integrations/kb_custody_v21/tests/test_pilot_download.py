"""Bounded raw receipts -> existing custody/conversion; synthetic data only."""
import json
from pathlib import Path
import subprocess
import sys
import unittest

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.f01 import encoded, sha
from kb_custody_v21.f05 import file_sha
from kb_custody_v21.pilot_feishu import PilotReadSession, FILE_LIMIT
from kb_custody_v21.pilot_intake import PilotIntake
from kb_custody_v21.pilot_preview import PilotScope
from kb_custody_v21.registry import Registry
from kb_custody_v21.runtime import Runtime, DEFAULTS
from test_custody import WORKTREE
import test_pilot_feishu as fixture

CONTENT='# 合成收件\n\n仅用于有界下载与本地接管测试。\n'.encode()


def download_response(c, raw):
    root=Path(c['root']);key=c['key']
    (root/(key+'.response.local')).write_bytes(raw)
    with (root/'http.local.jsonl').open('a') as stream:
        for kind in ('auth','download'):
            stream.write(encoded(dict(type='intent',key=key+':'+kind,kind=kind))+'\n')
            stream.write(encoded(dict(type='complete',key=key+':'+kind,kind=kind,http_status=200,
                **({'response_sha256':sha(raw)} if kind=='download' else {})))+'\n')
    return dict(ok=True,mode='read')


class PilotDownloadTests(unittest.TestCase):
    make=fixture.PilotFeishuTests.make
    scan=fixture.PilotFeishuTests.scan

    def setUp(self):
        fixture.PilotFeishuTests.setUp(self)
        self.sample=self.root/'synthetic.md';self.sample.write_bytes(CONTENT)
        self.frozen[str(self.sample)]=file_sha(self.sample)
        self.runtime=Runtime(WORKTREE,DEFAULTS,self.root.parent/'custody')
        self.registry=Registry(self.runtime)
        self.flow=PilotIntake(self.registry,PilotScope('cli_synthetic','oc_synthetic','ou_synthetic'),
                              max_attachments=1,max_pending=3)
        self.binary=CONTENT

    def respond(self,c):
        if c['kind']=='download':return download_response(c,self.binary)
        return fixture.PilotFeishuTests.respond(self,c)

    def ready(self):
        c=self.make();c.verify_isolation();self.scan(c);return c

    def receive(self,c):
        return c.receive_file('scan_synthetic','om_synthetic',self.flow,sample_path=self.sample)

    def test_complete_chain_and_restart_reuse_one_download_then_one_conversion(self):
        c=self.ready();result=self.receive(c);eid=result['receipt']['entry_id']
        self.assertEqual(result['status']['processing_state'],'waiting_conversion')
        self.assertFalse(result['permanent_backfill_verified'])
        self.assertEqual(self.registry.entry(eid)['sha256'],sha(CONTENT))
        self.assertEqual(json.loads(self.registry.entry(eid)['expected_json'])['sha256'],sha(CONTENT))
        c.close();c=self.make()
        self.assertEqual(self.receive(c)['receipt']['entry_id'],eid)
        self.assertEqual(sum(x.args[0]['kind']=='download' for x in self.runner.call_args_list),1)
        self.assertEqual(self.flow.work_once()['processing_state'],'ready_candidate')
        self.assertEqual(self.flow.work_once()['state'],'idle')
        self.assertFalse(self.registry.rows('SELECT * FROM notification_attempts'))
        self.assertEqual(len(self.registry.rows('SELECT * FROM conversion_bindings')),1)

    def test_non_frozen_sample_rejects_before_detail_download_and_registration(self):
        c=self.ready();self.frozen.pop(str(self.sample));count=self.runner.call_count
        with self.assertRaises(ContractError):self.receive(c)
        self.assertEqual(self.runner.call_count,count)
        self.assertFalse(self.registry.rows('SELECT * FROM intake_entries'))

    def test_oversize_sparse_sample_rejects_without_download(self):
        with self.sample.open('wb') as f:f.truncate(FILE_LIMIT+1)
        self.frozen[str(self.sample)]=file_sha(self.sample);c=self.ready()
        with self.assertRaises(ContractError):self.receive(c)
        self.assertFalse(self.registry.rows('SELECT * FROM intake_entries'))

    def test_other_scope_rejects_before_reading_message(self):
        c=self.ready();self.flow.scope=PilotScope('cli_other','oc_synthetic','ou_synthetic')
        count=self.runner.call_count
        with self.assertRaises(ContractError):self.receive(c)
        self.assertEqual(self.runner.call_count,count)

    def test_name_or_type_or_deleted_or_humanized_content_never_downloads(self):
        for mode in ('name','type','deleted','humanized','integer'):
            with self.subTest(mode=mode):
                # Distinct sessions avoid editing previously successful receipts.
                case=PilotDownloadTests('test_other_scope_rejects_before_reading_message');case.setUp()
                try:
                    if mode=='name':case.message['body']['content']=encoded(dict(file_key='file_synthetic',file_name='other.md'))
                    if mode=='type':case.message['msg_type']='post'
                    if mode=='deleted':case.message['deleted']=True
                    if mode=='humanized':case.message['body']['content']='[file: file_synthetic]'
                    if mode=='integer':case.message['body']['content']='{"n":'+'1'*10000+'}'
                    c=case.ready()
                    with self.assertRaises(ContractError):case.receive(c)
                    self.assertFalse(case.registry.rows('SELECT * FROM intake_entries'))
                    self.assertFalse(any(x.args[0]['kind']=='download' for x in case.runner.call_args_list))
                finally:case.doCleanups()

    def test_wrong_bytes_never_accept_and_stop_without_restart_or_retry(self):
        c=self.ready();self.binary=b'x'*len(CONTENT)
        with self.assertRaises(ContractError):self.receive(c)
        entries=self.registry.rows('SELECT entry_id FROM intake_entries')
        self.assertNotEqual(self.registry.entry(entries[0]['entry_id'])['status'],'accepted')
        c.close();count=self.runner.call_count
        with self.assertRaises(ContractError):self.make()
        self.assertEqual(self.runner.call_count,count)

    def test_unknown_download_keeps_intent_and_blocks_another_message(self):
        c=self.ready()
        def fail(control):
            if control['kind']=='download':
                with (self.root/'http.local.jsonl').open('a') as f:
                    f.write(encoded(dict(type='intent',key=control['key']+':download',kind='download'))+'\n')
                raise OSError('synthetic disconnected transport')
            return self.respond(control)
        self.runner.side_effect=fail
        with self.assertRaises(OSError):self.receive(c)
        count=self.runner.call_count;c.close()
        with self.assertRaises(ContractError):self.make()
        self.assertEqual(self.runner.call_count,count)

    def test_tampered_exact_message_receipt_prevents_download(self):
        c=self.ready();c.message('scan_synthetic','om_synthetic')
        next(self.root.glob('*-message.response.local')).write_text('{}')
        count=self.runner.call_count
        with self.assertRaises(ContractError):self.receive(c)
        self.assertEqual(self.runner.call_count,count)

    def test_original_changed_during_download_keeps_original_unaccepted(self):
        c=self.ready()
        def change(control):
            out=self.respond(control)
            if control['kind']=='download':self.sample.write_bytes(b'changed')
            return out
        self.runner.side_effect=change
        with self.assertRaises(ContractError):self.receive(c)
        self.assertFalse(self.registry.rows("SELECT * FROM staging_receipts WHERE status='accepted'"))

    def test_deadline_crosses_after_binary_response_before_custody_acceptance(self):
        c=self.ready()
        def expire(control):
            out=self.respond(control)
            if control['kind']=='download':self.now=__import__('datetime').datetime.fromisoformat(self.approval['expires_at'])
            return out
        self.runner.side_effect=expire
        with self.assertRaises(ContractError):self.receive(c)
        self.assertFalse(self.registry.rows("SELECT * FROM staging_receipts WHERE status='accepted'"))

    def test_process_death_after_receive_baseline_recovers_locally_without_download(self):
        c=self.ready();c.message('scan_synthetic','om_synthetic');c.close()
        config=self.root/'child.local.json'
        config.write_text(encoded(dict(root=str(self.root),approval_sha=self.digest,frozen=self.frozen,
            runtime=str(self.runtime.root),sample=str(self.sample),now=self.now.isoformat())))
        child=Path(__file__).with_name('pilot_download_crash_worker.py')
        result=subprocess.run([sys.executable,str(child),str(config)],capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,87,result.stderr)
        c=self.make();count=self.runner.call_count
        received=self.receive(c)
        self.assertEqual(received['status']['original_status'],'accepted')
        self.assertEqual(self.runner.call_count,count)
        self.assertEqual(len([e for e in c.http_events() if e['type']=='intent' and e['kind']=='download']),1)
        self.assertEqual(self.flow.work_once()['processing_state'],'ready_candidate')
