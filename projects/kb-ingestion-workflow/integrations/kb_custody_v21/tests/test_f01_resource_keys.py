"""Observed API shape, with purely synthetic IDs and file bytes."""
import copy
import json
import unittest

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.f01 import PREFIX, sha
import test_f01 as prior


class ResourceKeyTests(unittest.TestCase):
    def setUp(self):
        self.new_client()

    def new_client(self):
        seed=prior.F01Tests('test_upload_send_download_same_bytes_and_no_resend')
        seed.setUp()
        self.addCleanup(seed.doCleanups)
        seed.ready()
        self.client,self.wire,self.response=seed.client,seed.wire,seed.response
        self.wire.request.return_value=self.response({'file_key':'file_synthetic_upload'})
        self.client.upload('fixture.txt',b'synthetic')
        self.item=prior.message_item('om_synthetic','file',
            dict(file_key='file_synthetic_message',file_name='fixture.txt'))
        return self.client

    def send_ok(self):
        self.wire.request.return_value=self.response(self.item)
        return self.client.send('fixture.txt')

    def test_upload_key_and_message_key_preserved_and_only_bound_message_pair_downloaded(self):
        message=self.send_ok()
        sent=json.loads(self.wire.request.call_args.args[2])
        self.assertEqual(json.loads(sent['content'])['file_key'],'file_synthetic_upload')
        self.wire.request.side_effect=[self.response({'items':[self.item]}),(200,b'synthetic')]
        self.assertEqual(self.client.download('fixture.txt'),b'synthetic')
        self.assertTrue(self.wire.request.call_args.args[1].endswith('/resources/file_synthetic_message?type=file'))
        self.assertEqual(message['upload_resource_key'],'file_synthetic_upload')
        self.assertEqual(message['resource_key'],'file_synthetic_message')
        with self.assertRaises(ContractError):
            self.client.request('download','GET',PREFIX+'/im/v1/messages/om_synthetic/resources/file_synthetic_upload?type=file',
                key='unapproved-old-upload-key',binary=True)
        self.assertEqual(self.client.value('upload:fixture.txt')['resource_key'],'file_synthetic_upload')
        self.assertEqual(message['sha256'],sha(b'synthetic'))
        self.assertEqual(self.wire.request.call_count,4)

    def test_foreign_or_incomplete_send_receipt_stops_before_binding_or_download(self):
        cases=[dict(chat_id='oc_other'),dict(msg_type='image'),dict(deleted=True),dict(updated=True),
            dict(sender={'id':'cli_other','id_type':'app_id','sender_type':'app'}),
            dict(sender={'id':'cli_synthetic','id_type':'open_id','sender_type':'app'}),
            dict(sender={'id':'ou_synthetic','id_type':'open_id','sender_type':'user'}),
            dict(body={}),dict(body={'content':'not json'}),dict(body={'content':'[]'}),
            dict(body={'content':json.dumps({'file_key':'file_wrong/name','file_name':'fixture.txt'})}),
            dict(body={'content':json.dumps({'file_key':'file_synthetic_message','file_name':'other.txt'})})]
        for patch in cases:
            with self.subTest(patch=patch):
                self.new_client()
                self.wire.request.return_value=self.response(dict(self.item,**patch))
                with self.assertRaises(ContractError): self.client.send('fixture.txt')
                self.assertIsNone(self.client.value('message:fixture.txt'))
                self.assertTrue(any('stopped' in e for e in self.client.events))
                with self.assertRaises(ContractError): self.client.upload('fixture.txt',b'synthetic')
                self.assertEqual(self.wire.request.call_count,2)

    def test_detail_must_match_validated_send_identity_type_and_resource_key(self):
        for patch in [dict(chat_id='oc_other'),dict(message_id='om_other'),dict(msg_type='image'),
            dict(deleted=True),dict(updated=True),
            dict(sender={'id':'cli_other','id_type':'app_id','sender_type':'app'}),
            dict(body={'content':json.dumps({'file_key':'file_synthetic_upload','file_name':'fixture.txt'})}),
            dict(body={'content':json.dumps({'file_key':'file_other','file_name':'fixture.txt'})}),
            dict(body={'content':json.dumps({'file_key':'file_synthetic_message','file_name':'other.txt'})})]:
            with self.subTest(patch=patch):
                self.new_client()
                self.send_ok()
                self.wire.request.return_value=self.response({'items':[dict(self.item,**patch)]})
                with self.assertRaises(ContractError): self.client.download('fixture.txt')
                self.assertEqual(self.wire.request.call_count,3)
                self.assertFalse(any(e.get('kind')=='download' for e in self.client.events))

    def test_correct_message_pair_with_wrong_download_bytes_stays_failed(self):
        message=self.send_ok()
        self.wire.request.side_effect=[self.response({'items':[self.item]}),(200,b'other-content')]
        with self.assertRaisesRegex(ContractError,'download differs'):
            self.client.download('fixture.txt')
        self.assertEqual(message['sha256'],sha(b'synthetic'))
        self.assertTrue(any(e.get('stopped')=='download bytes mismatch' for e in self.client.events))
        with self.assertRaises(ContractError): self.client.download('fixture.txt')
        self.assertEqual(self.wire.request.call_count,4)

    def test_legacy_binding_without_validated_send_resource_is_not_inferred_or_migrated(self):
        self.client.bind('message:fixture.txt',dict(message_id='om_synthetic',
            **self.client.value('upload:fixture.txt')))
        with self.assertRaises(ContractError): self.client.download('fixture.txt')
        self.assertEqual(self.wire.request.call_count,1)

    def test_send_unknown_keeps_intent_and_never_looks_up_or_resends(self):
        self.wire.request.side_effect=TimeoutError('synthetic send outcome unknown')
        with self.assertRaises(TimeoutError): self.client.send('fixture.txt')
        self.assertIsNone(self.client.value('message:fixture.txt'))
        with self.assertRaises(ContractError): self.client.send('fixture.txt')
        with self.assertRaises(ContractError): self.client.download('fixture.txt')
        self.assertEqual(self.wire.request.call_count,2)
        self.assertNotIn('send:fixture.txt',{e['complete'] for e in self.client.events if 'complete' in e})

    def test_notification_receipt_must_preserve_exact_approved_text(self):
        self.wire.request.return_value=self.response(prior.message_item('om_notice','text',{'text':'other text'}))
        with self.assertRaises(ContractError): self.client.send('notification',text='synthetic accepted')
        self.assertIsNone(self.client.value('message:notification'))
        self.assertTrue(any('stopped' in e for e in self.client.events))

    def test_missing_or_multiple_detail_items_blocks_download_and_channel(self):
        for items in ([],[self.item,self.item]):
            with self.subTest(count=len(items)):
                self.new_client()
                self.send_ok()
                self.wire.request.return_value=self.response({'items':copy.deepcopy(items)})
                with self.assertRaises(ContractError): self.client.download('fixture.txt')
                with self.assertRaises(ContractError): self.client.upload('fixture.txt',b'synthetic')
                self.assertEqual(self.wire.request.call_count,3)
