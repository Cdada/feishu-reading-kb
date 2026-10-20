"""Reuse publication and physical receipt fixtures; no chat simulation or socket."""
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.f01 import encoded,sha
from kb_custody_v21.f05 import file_sha
from kb_custody_v21.pilot_delivery import deliver_preview
from kb_custody_v21.pilot_preview import PilotScope,PilotPreviews
import test_pilot_feishu as session_fixture
import test_publication_v21 as publication_fixture


def delivery_response(c):
    root=Path(c['root']);p=c['preview'];kind=c['kind'];key=c['key']
    if kind=='upload':value=dict(code=0,data=dict(file_key='file_upload'))
    elif kind=='send':value=dict(code=0,data=dict(message_id='om_preview',chat_id='oc_synthetic',
        msg_type='file',deleted=False,updated=False,sender=dict(id='cli_synthetic',id_type='app_id',sender_type='app'),
        body=dict(content=encoded(dict(file_key='file_message',file_name=p['filename'])))))
    else:value=None
    raw=encoded(value).encode() if value else Path(p['pdf_path']).read_bytes()
    (root/(key+'.response.local')).write_bytes(raw)
    with (root/'http.local.jsonl').open('a') as f:
        for category in ('auth',kind):
            f.write(encoded(dict(type='intent',key=key+':'+category,kind=category))+'\n')
            f.write(encoded(dict(type='complete',key=key+':'+category,kind=category,http_status=200,
                **({'response_sha256':sha(raw)} if category!='auth' else {})))+'\n')
    return dict(ok=True,mode=c['mode'])


class PilotDeliveryTests(unittest.TestCase):
    make=session_fixture.PilotFeishuTests.make
    def setUp(self):
        session_fixture.PilotFeishuTests.setUp(self)
        self.fixture=publication_fixture.PublicationTests();self.fixture.setUp()
        self.registry=self.fixture.registry
        self.pilot=PilotPreviews(self.registry,PilotScope('cli_synthetic','oc_synthetic','ou_synthetic'))
        self.preview=self.fixture.prepare();self.pid=self.preview['publication_id']
        entry=self.registry.entry(self.fixture.entry)
        self.original=self.fixture.runtime.root/entry['relative_path']
        self.frozen[str(self.original)]=entry['sha256']
        self.effect=lambda c:None

    def respond(self,c):
        if c['kind'] not in ('upload','send','download'):return session_fixture.PilotFeishuTests.respond(self,c)
        result=delivery_response(c);self.effect(c);return result

    def ready(self):
        s=self.make();s.verify_isolation();return s

    def deliver(self,s):return deliver_preview(s,self.pilot,self.pid)

    def test_exact_pdf_chain_distinct_keys_restart_and_no_owner_approval(self):
        s=self.ready();result=self.deliver(s)
        self.assertEqual(result['status'],'awaiting_owner_decision')
        self.assertFalse(result['published'])
        self.assertEqual(len(self.registry.rows('SELECT * FROM pilot_preview_deliveries')),1)
        self.assertFalse(self.registry.rows('SELECT * FROM publication_confirmations'))
        calls=[x.args[0] for x in self.runner.call_args_list if x.args[0]['kind'] in ('upload','send','download')]
        self.assertEqual([x['kind'] for x in calls],['upload','send','download'])
        self.assertIn('file_upload',calls[1]['body']);self.assertIn('file_message',calls[2]['path'])
        self.assertEqual(next(self.root.glob('*-download.response.local')).read_bytes(),Path(self.preview['preview_path']).read_bytes())
        n=self.runner.call_count;s.close();s=self.make()
        self.assertEqual(self.deliver(s),result);self.assertEqual(self.runner.call_count,n)

    def test_scope_unapproved_original_and_changed_pdf_block_before_upload(self):
        s=self.ready();n=self.runner.call_count
        with patch.object(self.pilot,'scope',PilotScope('cli_other','oc_synthetic','ou_synthetic')):
            with self.assertRaises(ContractError):self.deliver(s)
        self.frozen.pop(str(self.original))
        with self.assertRaises(ContractError):self.deliver(s)
        self.frozen[str(self.original)]=file_sha(self.original)
        Path(self.preview['preview_path']).write_bytes(b'changed')
        with self.assertRaises(ContractError):self.deliver(s)
        self.assertEqual(self.runner.call_count,n)

    def test_superseded_candidate_cannot_deliver_even_if_old_pdf_unchanged(self):
        s=self.ready();self.pilot.stage(self.pid)
        new=self.fixture.prepare();self.pilot.stage(new['publication_id'])
        n=self.runner.call_count
        with self.assertRaises(ContractError):self.deliver(s)
        self.assertEqual(self.runner.call_count,n)

    def test_changed_pdf_after_upload_stops_before_send_preserving_receipt(self):
        s=self.ready()
        def change(c):
            if c['kind']=='upload':Path(c['preview']['pdf_path']).write_bytes(b'changed')
        self.effect=change
        with self.assertRaises(ContractError):self.deliver(s)
        self.assertEqual(len(list(self.root.glob('*-upload.response.local'))),1)
        self.assertFalse(list(self.root.glob('*-send.response.local')))
        self.assertFalse(self.registry.rows('SELECT * FROM pilot_preview_deliveries'))

    def test_source_or_window_change_after_download_blocks_final_binding(self):
        s=self.ready()
        def change(c):
            if c['kind']=='download':self.source.write_text('changed code')
        self.effect=change
        with self.assertRaises(ContractError):self.deliver(s)
        self.assertTrue(list(self.root.glob('*-download.response.local')))
        self.assertFalse(self.registry.rows('SELECT * FROM pilot_preview_deliveries'))

    def test_expiry_after_send_blocks_download_and_bind(self):
        s=self.ready()
        def change(c):
            if c['kind']=='send':self.now=__import__('datetime').datetime.fromisoformat(self.approval['expires_at'])
        self.effect=change
        with self.assertRaises(ContractError):self.deliver(s)
        self.assertFalse(list(self.root.glob('*-download.response.local')))
        self.assertFalse(self.registry.rows('SELECT * FROM pilot_preview_deliveries'))

    def test_wrong_message_identity_or_updated_flag_never_binds(self):
        s=self.ready()
        def change(c):
            if c['kind']=='send':
                # A transport-successful response can still fail message validation.
                import json
                p=self.root/(c['key']+'.response.local');v=json.loads(p.read_bytes());v['data']['updated']=True
                raw=encoded(v).encode();p.write_bytes(raw)
                journal=self.root/'http.local.jsonl';events=[json.loads(x) for x in journal.read_text().splitlines()]
                events[-1]['response_sha256']=sha(raw);journal.write_text(''.join(encoded(x)+'\n' for x in events))
        self.effect=change
        with self.assertRaises(ContractError):self.deliver(s)
        self.assertFalse(list(self.root.glob('*-download.response.local')))
        self.assertFalse(self.registry.rows('SELECT * FROM pilot_preview_deliveries'))

    def test_byte_mismatch_never_creates_approvable_delivery(self):
        s=self.ready()
        def change(c):
            if c['kind']=='download':(self.root/(c['key']+'.response.local')).write_bytes(b'wrong')
        self.effect=change
        with self.assertRaises(ContractError):self.deliver(s)
        self.assertFalse(self.registry.rows('SELECT * FROM pilot_preview_deliveries'))

    def test_unknown_send_prevents_second_send_and_new_candidate(self):
        s=self.ready()
        def fail(c):
            if c['kind']=='send':
                with (self.root/'http.local.jsonl').open('a') as f:
                    f.write(encoded(dict(type='intent',key=c['key']+':send',kind='send'))+'\n')
                raise OSError('synthetic unknown send')
            return self.respond(c)
        self.runner.side_effect=fail
        with self.assertRaises(OSError):self.deliver(s)
        n=self.runner.call_count
        with self.assertRaises(ContractError):self.deliver(s)
        self.pid=self.fixture.prepare()['publication_id']
        with self.assertRaises(ContractError):self.deliver(s)
        s.close()
        with self.assertRaises(ContractError):self.make()
        self.assertEqual(self.runner.call_count,n)

    def test_hard_exit_after_send_receipt_is_never_retried(self):
        s=self.ready();s.close()
        config=self.root/'worker.local.json';config.write_text(encoded(dict(root=str(self.root),digest=self.digest,
            frozen=self.frozen,now=self.now.isoformat(),runtime=str(self.fixture.runtime.root),pid=self.pid)))
        result=subprocess.run([sys.executable,str(Path(__file__).with_name('pilot_delivery_crash_worker.py')),str(config)],
            capture_output=True,text=True,timeout=40)
        self.assertEqual(result.returncode,88,result.stderr)
        self.assertTrue(list(self.root.glob('*-send.response.local')))
        with self.assertRaises(ContractError):self.make()
        self.assertFalse(self.registry.rows('SELECT * FROM pilot_preview_deliveries'))

    def test_expiry_during_final_tree_hash_rolls_back_delivery_binding(self):
        s=self.ready();original=self.pilot.bind_delivery;tree=self.pilot.publisher._tree
        def late(*args,**kwargs):
            def crossed(*a,**k):
                out=tree(*a,**k)
                self.now=__import__('datetime').datetime.fromisoformat(self.approval['expires_at'])
                return out
            with patch.object(self.pilot.publisher,'_tree',side_effect=crossed):return original(*args,**kwargs)
        with patch.object(self.pilot,'bind_delivery',side_effect=late),self.assertRaises(ContractError):self.deliver(s)
        self.assertTrue(list(self.root.glob('*-download.response.local')))
        self.assertFalse(self.registry.rows('SELECT * FROM pilot_preview_deliveries'))

    def test_finished_receipts_resume_local_binding_without_another_request(self):
        s=self.ready()
        with patch.object(self.pilot,'bind_delivery',side_effect=SystemExit(88)),self.assertRaises(SystemExit):self.deliver(s)
        n=self.runner.call_count;s.close();s=self.make()
        self.assertEqual(self.deliver(s)['status'],'awaiting_owner_decision')
        self.assertEqual(self.runner.call_count,n)
