"""Exact old receipt resolution never changes the old stopped journal."""
import copy
import json
from pathlib import Path
import unittest

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.f01 import encoded,sha
from kb_custody_v21.f05 import file_sha
from kb_custody_v21.pilot_owner_followup import previous_preview
import test_pilot_owner_followup as owner_fixture


class DownloadResolutionTests(unittest.TestCase):
    def setUp(self):
        self.f=owner_fixture.OwnerFollowupTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        f=self.f;root=f.old
        approval=json.loads((root/'approval.local.json').read_bytes())
        approval.update(operation='inbox-service',maximum_total=68,
            limits=dict(auth=34,identity=3,list=10,message=4,download=4,upload=2,send=11),
            inbox=dict(reference='P23-service-inbox-v1',formats=['.md'],max_asset_bytes=65536,max_files=2))
        (root/'approval.local.json').write_text(encoded(approval))
        events=[json.loads(line) for line in (root/'journal.local.jsonl').read_text().splitlines()]
        events=[e for e in events if 'stopped' not in e];events[0]={'approval':approval}
        http=[json.loads(line) for line in (root/'http.local.jsonl').read_text().splitlines()]
        mid='om_saved_second';key='file_saved_second';filename='second.md'
        detail=dict(code=0,data=dict(items=[dict(message_id=mid,chat_id=approval['chat_id'],msg_type='file',deleted=False,
            sender=dict(id=approval['account_id'],id_type='open_id',sender_type='user'),body=dict(content=encoded(dict(file_key=key,file_name=filename))))]))
        content=b'# Second synthetic material\n\nPreserved original bytes.\n'
        self.download_key='p901-download'
        for request_key,kind,body in [('p900-message','message',encoded(detail).encode()),(self.download_key,'download',content)]:
            (root/(request_key+'.response.local')).write_bytes(body)
            http += [dict(type='intent',key=request_key+':'+kind,kind=kind),
                     dict(type='complete',key=request_key+':'+kind,kind=kind,http_status=200,response_sha256=sha(body))]
        events += [dict(intent='p900-message',kind='message',mode='read',operation='scan_saved:message:'+mid),
            dict(complete='p900-message',response_file='p900-message.response.local'),
            dict(binding='inbox-claim:saved',value=dict(entry_id='entry_'+'c'*32,message_id=mid,file_key=key,filename=filename)),
            dict(intent=self.download_key,kind='download',mode='read',operation='inbox-download:'+mid+':'+key,
                path='/open-apis/im/v1/messages/'+mid+'/resources/'+key+'?type=file',
                download=dict(reference='P19-inbox-download-v1',message_id=mid,file_key=key,filename=filename,max_asset_bytes=65536)),
            dict(stopped=self.download_key,error_type='ContractError'),dict(stopped='operator_entry',error_type='ContractError')]
        (root/(self.download_key+'.cli.local')).write_bytes(content)
        self.events=events;self.http=http;self.save()
        f.binding=dict(f.binding,approval_sha256=file_sha(root/'approval.local.json'),
            resolved_download=dict(reference='P24-complete-download-resolution-v1',key=self.download_key,sha256=sha(content),size_bytes=len(content)))
        self.binding=f.binding

    def save(self):
        for name,events in [('journal.local.jsonl',self.events),('http.local.jsonl',self.http)]:
            (self.f.old/name).write_text(''.join(encoded(e)+'\n' for e in events))

    def test_explicit_resolution_reuses_same_pdf_without_rewriting_old_evidence(self):
        f=self.f;before={p:file_sha(p) for p in f.old.iterdir() if p.is_file()}
        proof=previous_preview(self.binding)
        self.assertEqual(proof['resolved_download']['filename'],'second.md')
        self.assertEqual(proof['resolved_download']['sha256'],self.binding['resolved_download']['sha256'])
        f.p.frozen.update(proof['files']);f.plan['previous_preview']=self.binding;f.save_plan()
        calls=f.f.wire.calls
        result=f.run_followup()
        self.assertEqual(result['status'],'published')
        self.assertEqual(result['notification']['delivery_state'],'delivered')
        self.assertEqual(f.f.wire.calls,calls)
        self.assertEqual(file_sha(Path(result['path'])/'发布记录/已确认入库预览.pdf'),self.binding['pdf_sha256'])
        self.assertTrue(all(file_sha(p)==digest for p,digest in before.items()))
        self.assertEqual(len([e for e in f.session.http_events() if e['type']=='intent']),12)

    def test_missing_explicit_resolution_retains_old_rejection(self):
        binding=dict(self.binding);binding.pop('resolved_download')
        with self.assertRaises(ContractError):previous_preview(binding)
        self.f.p.runner.assert_not_called()

    def test_unknown_http_cannot_be_resolved(self):
        self.http.pop();self.save()
        with self.assertRaises(ContractError):previous_preview(self.binding)

    def test_failed_write_cannot_borrow_download_resolution(self):
        self.events[-3]['kind']='send';self.save()
        with self.assertRaises(ContractError):previous_preview(self.binding)

    def test_changed_saved_bytes_or_selected_hash_cannot_be_resolved(self):
        original=(self.f.old/(self.download_key+'.cli.local')).read_bytes()
        for mode in ('file','hash','key'):
            with self.subTest(mode=mode):
                binding=copy.deepcopy(self.binding)
                if mode=='file':(self.f.old/(self.download_key+'.cli.local')).write_bytes(b'tampered')
                elif mode=='hash':binding['resolved_download']['sha256']='0'*64
                else:binding['resolved_download']['key']='../elsewhere'
                with self.assertRaises(ContractError):previous_preview(binding)
                (self.f.old/(self.download_key+'.cli.local')).write_bytes(original)

    def test_new_approval_must_freeze_resolved_saved_file(self):
        f=self.f;proof=previous_preview(self.binding);f.p.frozen.update(proof['files'])
        f.p.frozen.pop(str(f.old/(self.download_key+'.cli.local')))
        f.plan['previous_preview']=self.binding;f.save_plan()
        with self.assertRaises(ContractError):f.run_followup()
        f.p.runner.assert_not_called();f.assert_unconfirmed()

    def test_additional_stop_cannot_be_hidden(self):
        self.events.append(dict(stopped='other',error_type='ContractError'));self.save()
        with self.assertRaises(ContractError):previous_preview(self.binding)


if __name__=='__main__':unittest.main()
