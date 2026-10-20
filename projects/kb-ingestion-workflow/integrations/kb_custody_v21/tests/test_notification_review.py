"""Known receipt verification never sends, searches, or changes old evidence."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.f01 import PREFIX, encoded, sha, validated_message_content
from kb_custody_v21.notification_review import acknowledged_notifications, observe_notification
from test_f01 import message_item


class NotificationReviewTests(unittest.TestCase):
    def setUp(self):
        self.options = dict(app_id='cli_synthetic', bot_id='ou_synthetic_bot', chat_id='oc_synthetic')
        self.texts = ['synthetic accepted', 'synthetic ready for review']
        self.items = [message_item('om_synthetic_notice_'+str(i), 'text', {'text': t})
                      for i,t in enumerate(self.texts)]
        self.receipt = self.items[0]
        self.receipt.update(create_time='1000', update_time='1000')
        self.current = dict(self.receipt, updated=True, update_time='1125')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.events = [dict(approval=dict(self.options, reference='F04', approved=True,
            synthetic_only=True, notifications=self.texts, starts_at='2026-01-01T00:00:00Z'))]
        for i,item in enumerate(self.items):
            name = 'notification-'+str(i)
            key = 'send:'+name
            payload = encoded(dict(code=0, data=item)).encode()
            body = dict(receive_id=self.options['chat_id'], msg_type='text',
                content=encoded({'text': self.texts[i]}),
                uuid=sha(('2026-01-01T00:00:00Z'+name).encode())[:48])
            self.events += [dict(intent=key, kind='send', method='POST',
                path=PREFIX+'/im/v1/messages?receive_id_type=chat_id', payload_sha256=sha(encoded(body).encode())),
                dict(complete=key, http_status=200, response_sha256=sha(payload)),
                dict(binding='message:'+name, value={'message_id':item['message_id']})]
            (self.root/(sha(key.encode())+'.response.local')).write_bytes(payload)
        self.save()

    def save(self):
        (self.root/'journal.local.jsonl').write_text('\n'.join(encoded(e) for e in self.events)+'\n')
        self.hashes = {p.name:sha(p.read_bytes()) for p in self.root.iterdir()}

    def load(self, **changes):
        return acknowledged_notifications(self.root, self.hashes, **dict(self.options, texts=self.texts, **changes))

    def observe(self, current=None, receipt=None):
        return observe_notification(self.receipt if receipt is None else receipt,
            [self.current if current is None else current], text=self.texts[0], **self.options)

    def test_updated_but_exact_content_preserves_confirmed_fact_and_explicit_anomaly(self):
        before = copy.deepcopy([self.receipt, self.current])
        result = self.observe()
        self.assertEqual(result['send_receipt'], 'CONFIRMED')
        self.assertEqual(result['current_content'], 'MATCH')
        self.assertEqual(result['status'], 'MATCH_WITH_UPDATE_FLAG')
        self.assertEqual(result['update_cause'], 'UNDETERMINED')
        self.assertEqual(result['current_update_time'], '1125')
        self.assertEqual(result['edit_history'], 'NOT_VERIFIED')
        self.assertEqual(result['unknown_recovery'], 'NOT_TESTED')
        self.assertEqual([self.receipt, self.current], before)

    def test_unchanged_exact_message_has_no_invented_update(self):
        result = self.observe(current=self.receipt)
        self.assertEqual(result['status'], 'MATCH')
        self.assertEqual(result['update_cause'], 'NOT_OBSERVED')

    def test_changed_text_is_rejected_even_with_successful_receipt(self):
        for text in ('other synthetic text', self.texts[0]+' ', self.texts[0].upper()):
            with self.subTest(text=text), self.assertRaises(ContractError):
                self.observe(dict(self.current, body={'content':encoded({'text':text})}))
        self.assertEqual(self.receipt['body']['content'], json.dumps({'text':self.texts[0]}))

    def test_foreign_sender_chat_id_type_deleted_or_nonboolean_flag_rejected(self):
        variants = [dict(message_id='om_other'), dict(chat_id='oc_other'), dict(msg_type='file'),
            dict(deleted=True), dict(deleted=0), dict(updated='true'), dict(updated=1), dict(updated=None),
            dict(sender={'id':'cli_other','id_type':'app_id','sender_type':'app'}),
            dict(sender={'id':'cli_synthetic','id_type':'open_id','sender_type':'app'}),
            dict(sender={'id':'cli_synthetic','id_type':'app_id','sender_type':'user'})]
        for changes in variants:
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                self.observe(dict(self.current, **changes))

    def test_zero_multiple_or_malformed_detail_cannot_be_success_or_retry_permission(self):
        for items in ([], [self.current,self.current], {}, [None]):
            with self.subTest(items=items), self.assertRaises(ContractError):
                observe_notification(self.receipt, items, text=self.texts[0], **self.options)
        without_flag = dict(self.current)
        del without_flag['updated']
        with self.assertRaises(ContractError): self.observe(without_flag)

    def test_missing_or_changed_original_send_cannot_be_treated_as_known(self):
        for receipt in ({}, dict(self.receipt, updated=True), dict(self.receipt, deleted=True)):
            with self.subTest(receipt=receipt), self.assertRaises(ContractError):
                self.observe(receipt=receipt)

    def test_legacy_and_file_validator_remain_strict(self):
        with self.assertRaises(ContractError):
            validated_message_content(self.current, kind='text', name='notice', text=self.texts[0], **self.options)
        item = message_item('om_synthetic_file','file',{'file_key':'file_synthetic','file_name':'fixture.txt'})
        with self.assertRaises(ContractError):
            validated_message_content(dict(item, updated=True),kind='file',name='fixture.txt', **self.options)

    def test_completed_sends_load_without_modifying_stopped_history(self):
        self.events.append({'stopped':'message receipt validation failed'})
        self.save()
        before = {p.name:p.read_bytes() for p in self.root.iterdir()}
        self.assertEqual(self.load(), self.items)
        self.assertEqual({p.name:p.read_bytes() for p in self.root.iterdir()}, before)

    def test_unresolved_send_or_unrelated_request_and_duplicate_completion_rejected(self):
        before = copy.deepcopy(self.events)
        variants = [[e for e in before if e.get('complete')!='send:notification-1'],
            before+[{'intent':'unknown:synthetic','kind':'send'}], before+[before[2]]]
        for events in variants:
            self.events = events; self.save()
            with self.subTest(events=events), self.assertRaises(ContractError): self.load()

    def test_manifest_and_receipt_digest_tampering_rejected(self):
        path = self.root/(sha(b'send:notification-0')+'.response.local')
        path.write_text(encoded({'code':0,'data':dict(self.receipt,chat_id='oc_other')}))
        with self.assertRaises(ContractError): self.load()
        self.save()  # Rebinding a manifest alone cannot change the original completed receipt.
        with self.assertRaises(ContractError): self.load()

    def test_changed_recipient_payload_binding_or_old_approval_is_rejected(self):
        before = copy.deepcopy(self.events)
        for index, field, value in [(0,'approval',{}), (1,'payload_sha256',sha(b'other')),
                (1,'method','PUT'), (1,'path',PREFIX+'/im/v1/messages/om_other'),
                (2,'http_status',500), (3,'value',{'message_id':'om_other'})]:
            self.events = copy.deepcopy(before)
            self.events[index][field] = value; self.save()
            with self.subTest(field=field), self.assertRaises(ContractError): self.load()

    def test_missing_or_extra_evidence_and_non_synthetic_approval_rejected(self):
        original = dict(self.hashes)
        for changes in ({}, dict(original, extra='digest')):
            self.hashes = changes
            with self.assertRaises(ContractError): self.load()
        self.events[0]['approval']['synthetic_only'] = False
        self.save()
        with self.assertRaises(ContractError): self.load()
