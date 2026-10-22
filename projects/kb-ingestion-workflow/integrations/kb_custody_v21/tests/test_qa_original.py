"""Original links retain owner, group and exact attachment bindings."""
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qs,urlsplit

from kb_custody_v21.contracts import ContractError,stable_key
from kb_custody_v21.qa_original import message_link,render
from kb_custody_v21.qa_query import parse
from kb_custody_v21.daily_cards import commands
import test_qa_engine as fixture


class OriginalLinkTests(unittest.TestCase):
    def test_chat_and_thread_links_use_platform_positions(self):
        chat=dict(chat_id='oc_synthetic',message_position=12)
        link=message_link(chat,'oc_synthetic')
        self.assertEqual(parse_qs(urlsplit(link).query),dict(openChatId=['oc_synthetic'],position=['12']))
        thread=dict(chat,thread_id='omt_synthetic',thread_message_position='9')
        query=parse_qs(urlsplit(message_link(thread,'oc_synthetic')).query)
        self.assertEqual(query['thread_position'],['9'])
        self.assertEqual(query['open_chat_id'],['oc_synthetic'])
        self.assertEqual(query['openchatid'],['oc_synthetic'])

    def test_ids_or_user_body_cannot_become_message_positions(self):
        for value in ('om_synthetic','-1',True,1.2,None):
            item=dict(chat_id='oc_synthetic',message_position=value,
                      body=dict(content='https://untrusted.example.invalid/'))
            self.assertIsNone(message_link(item,'oc_synthetic'))
        with self.assertRaises(ContractError):message_link(dict(chat_id='oc_foreign'),'oc_synthetic')

    def test_only_api_opaque_link_on_official_host_allowed(self):
        base=dict(chat_id='oc_synthetic')
        good='https://applink.feishu.cn/client/message/link/open?token=synthetic'
        self.assertEqual(message_link(dict(base,message_app_link=good),'oc_synthetic'),good)
        for url in (good.replace('applink.feishu.cn','evil.example.invalid'),good+'&redirect=https://example.invalid',
                    good.replace('https:','http:'),good+'#fragment'):
            self.assertIsNone(message_link(dict(base,message_app_link=url),'oc_synthetic'))


class OriginalRenderingTests(unittest.TestCase):
    def setUp(self):
        self.hit=dict(material_id='one',document_id='om_source',source_sha256='a'*64,title='合成原件')
        self.document=dict(source_sha='a'*64,data=dict(resource=dict(file_key='file_synthetic',filename='合成原件.pdf')))
        self.item=dict(message_id='om_source',chat_id='oc_synthetic',msg_type='file',deleted=False,message_position=12,
            sender=dict(id='ou_owner',id_type='open_id',sender_type='user'),
            body=dict(content=json.dumps(dict(file_key='file_synthetic',file_name='合成原件.pdf'))))
        self.b=SimpleNamespace(store=SimpleNamespace(document=Mock(return_value=self.document)),
            authority=SimpleNamespace(policy=SimpleNamespace(chat_id='oc_synthetic',account_id='ou_owner')),
            collector=SimpleNamespace(observed=Mock(return_value=({},dict(evidence={})))),
            transport=SimpleNamespace(request=Mock(side_effect=lambda *a,**k:dict(value=dict(data=dict(items=[deepcopy(self.item)]))))))
        self.sources=SimpleNamespace(verify=Mock())
        self.job=dict(id='b'*64,result=dict(hits=[self.hit,self.hit]))

    def test_shared_source_is_read_once_without_upload_or_model(self):
        result=render(self.b,self.sources,self.job)
        self.assertIn('position=12',result);self.assertEqual(self.b.transport.request.call_count,1)
        self.assertEqual(self.b.transport.request.call_args.args[1],'message')
        self.sources.verify.assert_called()

    def test_changed_attachment_or_owner_is_rejected(self):
        original=deepcopy(self.item)
        for change in (dict(chat_id='oc_foreign'),dict(sender=dict(id='ou_other',id_type='open_id',sender_type='user')),
                       dict(body=dict(content=json.dumps(dict(file_key='other',file_name='合成原件.pdf'))))):
            self.item=dict(original,**change)
            with self.assertRaises(ContractError):render(self.b,self.sources,self.job)

    def test_deleted_and_unlocatable_files_are_explicit(self):
        self.item['deleted']=True
        self.assertIn('原飞书消息已不可用',render(self.b,self.sources,self.job))
        self.item['deleted']=False;del self.item['message_position']
        result=render(self.b,self.sources,self.job)
        self.assertIn('没有生成定位链接',result);self.assertNotIn('https://',result)

    def test_wrong_local_original_stops_before_network(self):
        self.document['source_sha']='c'*64
        with self.assertRaises(ContractError):render(self.b,self.sources,self.job)
        self.b.transport.request.assert_not_called()


class OriginalJobTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.QAEngineTests();self.f.setUp();self.addCleanup(self.f.doCleanups)

    def test_selected_original_respects_result_number_and_uses_no_model(self):
        f=self.f;parent=f.run_job('查文献 城市演变')
        expected=f.store.context('chat')['materials'][1]
        result=f.run_job('原件 2')
        self.assertEqual(result['result']['status'],'original')
        self.assertEqual(result['result']['hits'][0]['material_id'],expected)
        self.assertEqual(f.wire.calls,[])
        self.assertEqual(parent['result']['menu'],'result')
        with self.assertRaises(ContractError):parse('原件 1')

    def test_callback_is_pinned_to_its_parent(self):
        f=self.f;parent=f.run_job('查文献 城市演变')
        f.store.delivered(parent['id'],dict(message_id='om_answer'));f.run_job('新问题')
        child=f.store.enqueue(stable_key('original-card','one'),'om_answer','取阅原件',
            dict(receipts={},qa_original_parent=parent['id']))
        f.engine.run(child)
        self.assertEqual(f.store.get(child['id'])['result']['hits'],parent['result']['hits'])
        self.assertEqual(f.wire.calls,[])

    def test_legacy_card_does_not_change(self):
        self.assertEqual(commands(dict(purpose='qa')),('查看原文依据','本周周报','新问题'))
        self.assertIn('取阅原件',commands(dict(purpose='qa',menu='result')))


class OriginalDeliveryTests(unittest.TestCase):
    def test_rejected_original_does_not_resume_an_old_saved_link(self):
        from unittest.mock import patch
        import test_qa_feishu as feishu_fixture
        f=feishu_fixture.QAFeishuTests();f.setUp();self.addCleanup(f.doCleanups)
        hit=dict(material_id='one')
        job=f.flow.store.enqueue('c'*64,'om_question','原件 1',dict(receipts={}))
        f.flow.store.prepare(job['id'],dict(query=dict(mode='original'),hits=[hit]))
        f.flow.store.complete(job['id'],dict(status='original',text='准备原件入口。',hits=[hit]))
        f.daily.put('qa-delivery-text:'+job['id'],'old saved link')
        notice=Mock(return_value=dict(message_id='om_warning',receipt={}))
        with patch.object(f.flow.sources,'verify'),patch.object(f.b,'notice',notice), \
                patch('kb_custody_v21.qa_original.render',side_effect=ContractError('synthetic changed source')):
            f.flow.report()
        self.assertEqual(notice.call_count,1)
        self.assertNotIn('old saved link',notice.call_args.args[1])
        self.assertIn('未提供链接',notice.call_args.args[1])
        self.assertEqual(f.flow.store.get(job['id'])['state'],'sent')
