"""Pending PDF threads remain discoverable independently of old root timestamps."""
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from urllib.parse import parse_qs,urlsplit
import unittest

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.daily_collector import DailyCollector
from kb_custody_v21.daily_store import DailyStore
from kb_custody_v21.f05 import file_sha
from kb_custody_v21.registry import encoded
from test_daily_store import policy,message


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()
        self.now=1000000.
        self.policy=policy()
        self.store=DailyStore(self.root/'queue.sqlite3',self.policy,clock=lambda:self.now)
        self.calls=[]
        self.thread='omt_known'
        self.a=SimpleNamespace(policy=self.policy,config=dict(initial_history_start=self.now-30))
        self.collector=DailyCollector(self.a,self.store,self)
        self.collector.verify_isolation=lambda:None
        self.items=[]
        self.next_page=False
        self.refreshed_reply=None

    def _checked_result(self,value):
        for path,digest in value['evidence'].items():
            if file_sha(path)!=digest:raise ContractError('changed synthetic evidence')
        return value

    def proof(self,key,value):
        path=self.root/(hashlib.sha256(key.encode()).hexdigest()+'.json')
        path.write_text(encoded(value))
        return dict(value=value,receipt=str(path),evidence={str(path):file_sha(path)})

    def request(self,key,kind,path,**kwargs):
        self.calls.append((kind,path))
        if kind=='message' and self.refreshed_reply and path.endswith('/om_reply'):
            return self.proof(key,dict(code=0,data=dict(items=[self.refreshed_reply])))
        if kind=='message':
            item=dict(message_id='om_pdf',chat_id='oc_synthetic',thread_id=self.thread,deleted=False,
                sender=dict(id='ou_bot',id_type='open_id',sender_type='app'))
            return self.proof(key,dict(code=0,data=dict(items=[item])))
        q=parse_qs(urlsplit(path).query)
        if q['container_id_type']==['thread']:
            self.assertNotIn('start_time',q)
            self.assertNotIn('end_time',q)
            item=message('reply',kind='text')
            item.update(parent_id='om_pdf',thread_id='omt_known')
            return self.proof(key,dict(code=0,data=dict(items=[item],has_more=False)))
        return self.proof(key,dict(code=0,data=dict(items=self.items,has_more=self.next_page,page_token='page-two' if self.next_page else None)))

    def pending(self):
        scan=self.store.scan('chat',initial_start=self.now-60)
        self.store.save_page(scan,[message('file')],has_more=False)
        phases=['registered','original_saved','converted','generating','guide_ready','preview_ready','awaiting_confirmation']
        for a,b in zip(phases,phases[1:]):self.store.advance('om_file',a,b)
        proof=self.proof('send',dict(code=0,data=dict(message_id='om_pdf')))
        with self.store.transaction() as db:
            row=self.store.document('om_file')
            data=dict(row['data'],preview=dict(sent=proof))
            db.execute('UPDATE daily_documents SET preview_message_id=?,data=? WHERE message_id=?',('om_pdf',encoded(data),'om_file'))

    def test_old_pdf_root_is_queried_for_thread_and_owner_reply_saved(self):
        self.pending()
        self.now+=86400
        self.collector.collect()
        self.assertEqual([k for k,_ in self.calls],['list','message','list'])
        observed,proof=self.collector.observed('om_reply')
        self.assertEqual(observed['parent_id'],'om_pdf')
        self._checked_result(proof)
        self.now+=20
        self.collector.collect()
        self.assertEqual([k for k,_ in self.calls[-2:]],['list','list'])

    def test_foreign_thread_response_never_becomes_owner_decision(self):
        self.pending();self.thread='omt_other'
        with self.assertRaises(ContractError):self.collector.collect()
        self.assertFalse(self.store.rows("SELECT 1 FROM daily_messages WHERE id='om_reply'"))

    def test_edited_old_chat_reply_is_refreshed_without_reupload(self):
        self.pending();self.thread=None
        original=message('reply',kind='text',text='@确认入库')
        original.update(parent_id='om_pdf')
        self.items=[original]
        corrected=message('reply',kind='text')
        corrected.update(parent_id='om_pdf',updated=True,update_time='1001000')
        self.refreshed_reply=corrected
        self.collector.collect()
        self.assertEqual(self.collector.observed('om_reply')[0],corrected)
        self.items=[];self.now+=3600
        deleted=dict(corrected,deleted=True,update_time='4601000')
        self.refreshed_reply=deleted
        self.collector.collect()
        self.assertTrue(self.collector.observed('om_reply')[0]['deleted'])

    def test_continuous_uploads_are_registered_without_heavy_worker(self):
        self.items=[message(n) for n in range(12)]
        self.collector.collect()
        self.assertEqual(self.store.summary()['continuing_count'],12)
        self.assertEqual(len(self.calls),1)


if __name__=='__main__':unittest.main()
