"""Physical read evidence -> direct owner reply -> existing exact publication."""
import copy
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.f01 import encoded
from kb_custody_v21.material_review import MaterialReviews
from kb_custody_v21.pilot_decision import apply_owner_reply,reply_text,observed_reply
from kb_custody_v21.pilot_preview import PilotScope
import test_material_review as review_fixture
import test_pilot_delivery as delivery_fixture
import test_pilot_feishu as session_fixture


def owner_reply(text='确认入库'):
    return dict(message_id='om_owner_reply',chat_id='oc_synthetic',msg_type='text',
        deleted=False,updated=False,sender=dict(id='ou_synthetic',sender_type='user',id_type='open_id'),
        body=dict(content=encoded(dict(text=text))),parent_id='om_preview')


class ReplyParsingTests(unittest.TestCase):
    def test_exact_raw_text_and_direct_parent(self):
        self.assertEqual(reply_text(owner_reply()),('om_preview','确认入库'))

    def test_root_only_deleted_updated_other_type_and_malformed_body_rejected(self):
        cases=[{'parent_id':None,'root_id':'om_preview'},{'parent_id':''},{'updated':True},
            {'updated':None},{'deleted':True},{'msg_type':'post'},{'body':None},
            {'body':{'content':encoded({'text':['确认入库']})}},
            {'body':{'content':'{"text":"暂不入库","text":"确认入库"}'}},
            {'body':{'content':encoded({'text':'确认入库','extra':1})}},
            {'body':{'content':'1'*10000}},
            {'body':{'content':'['*1500+']'*1500}},
            {'body':{'content':encoded({'text':'图'*1800})}}]
        for changes in cases:
            with self.subTest(fields=list(changes)),self.assertRaises(ContractError):
                reply_text(owner_reply()|changes)


class ObservedReplyTests(unittest.TestCase):
    def item(self):
        return owner_reply()|dict(updated=True,create_time='1000',update_time='1098',
            body=dict(content=encoded(dict(text='@_user_1 确认入库'))),
            mentions=[dict(key='@_user_1',id='ou_synthetic_bot',id_type='open_id')])

    def test_exact_bot_mention_and_update_flag_are_preserved(self):
        parent,text,version=observed_reply(self.item(),'ou_synthetic_bot')
        self.assertEqual((parent,text),('om_preview','确认入库'));self.assertTrue(version['updated'])
        self.assertEqual(version['body'],self.item()['body'])
        with self.assertRaises(ContractError):reply_text(self.item())

    def test_spoofed_unknown_repeated_and_embedded_mentions_rejected(self):
        cases=[dict(mentions=[]),dict(mentions=[dict(key='@_user_1',id='ou_other',id_type='open_id')]),
            dict(mentions=self.item()['mentions']*2),dict(body=dict(content=encoded(dict(text='确认入库 @_user_1'))))]
        for changes in cases:
            with self.subTest(changes=changes),self.assertRaises(ContractError):
                observed_reply(self.item()|changes,'ou_synthetic_bot')

    def test_version_requires_boolean_flags_and_valid_update_times(self):
        for changes in (dict(updated=1),dict(deleted=True),dict(update_time='999'),dict(update_time=None)):
            with self.subTest(changes=changes),self.assertRaises(ContractError):
                observed_reply(self.item()|changes,'ou_synthetic_bot')


class PilotDecisionTests(unittest.TestCase):
    setUp=delivery_fixture.PilotDeliveryTests.setUp
    make=delivery_fixture.PilotDeliveryTests.make
    respond=delivery_fixture.PilotDeliveryTests.respond
    ready=delivery_fixture.PilotDeliveryTests.ready
    deliver=delivery_fixture.PilotDeliveryTests.deliver

    def delivered(self,*,limitation=False,text=None):
        if limitation:
            report=review_fixture.report_for(self.fixture.runtime,self.fixture.mapping,limitation=True)
            MaterialReviews(self.registry).record(self.fixture.entry,self.fixture.mapping['extraction_id'],report)
            self.preview=self.fixture.prepare();self.pid=self.preview['publication_id']
        self.pdf=Path(self.preview['preview_path']).read_bytes()
        self.message=owner_reply(text or ('确认当前版本入库' if limitation else '确认入库'))
        self.pages=[dict(items=[copy.deepcopy(self.message)],has_more=False)]
        s=self.ready();self.deliver(s)
        session_fixture.PilotFeishuTests.scan(self,s)
        return s

    def apply(self,s):
        return apply_owner_reply(s,self.pilot,'scan_synthetic','om_owner_reply')

    def assert_unconfirmed(self):
        self.assertFalse(self.registry.rows('SELECT * FROM publication_confirmations'))
        self.assertFalse(self.registry.rows('SELECT * FROM pilot_preview_decisions'))
        self.assertEqual(self.pilot.publisher._row(self.pid)['state'],'prepared')
        self.assertFalse((self.pilot.publisher.root/self.pid).exists())

    def expire(self):
        self.now=datetime.fromisoformat(self.approval['expires_at'])

    def test_complete_byte_delivery_verified_reply_publication_and_restart_no_new_http(self):
        s=self.delivered();result=self.apply(s)
        self.assertEqual(result['status'],'published')
        self.assertEqual((Path(result['path'])/'发布记录/已确认入库预览.pdf').read_bytes(),self.pdf)
        proof=json.loads(self.registry.rows("SELECT payload_json FROM custody_events WHERE event_type='pilot_decision_observed'")[0]['payload_json'])
        self.assertEqual(proof['message_id'],'om_owner_reply')
        self.assertEqual(len(proof['receipts']),5) # list, detail, upload, send, download
        self.assertEqual(proof['pdf_sha256'],self.preview['preview_sha256'])
        n=self.runner.call_count;s.close();s=self.make()
        self.assertEqual(self.apply(s),result);self.assertEqual(self.runner.call_count,n)
        self.assertEqual(len(self.registry.rows("SELECT * FROM custody_events WHERE event_type='publication_published'")),1)
        self.assertEqual(len(self.registry.rows('SELECT * FROM publication_confirmations')),1)

    def test_exact_limitation_choice_required_and_recorded(self):
        s=self.delivered(limitation=True)
        self.assertEqual(self.apply(s)['status'],'published')
        self.assertEqual(len(self.registry.rows("SELECT * FROM custody_events WHERE event_type='material_limitations_accepted'")),1)

    def test_generic_confirmation_cannot_accept_material_limitations(self):
        s=self.delivered(limitation=True,text='确认入库')
        with self.assertRaisesRegex(ContractError,'limitations'):self.apply(s)
        self.assert_unconfirmed()

    def test_defer_records_provenance_without_publishing(self):
        s=self.delivered(text='暂不入库')
        self.assertEqual(self.apply(s)['status'],'defer')
        self.assertFalse(self.registry.rows('SELECT * FROM publication_confirmations'))
        self.assertEqual(self.pilot.publisher._row(self.pid)['state'],'prepared')
        self.assertEqual(len(self.registry.rows('SELECT * FROM pilot_preview_decisions')),1)

    def test_fuzzy_yes_is_never_an_approval(self):
        s=self.delivered(text='好的')
        with self.assertRaises(ContractError):self.apply(s)
        self.assert_unconfirmed()

    def test_direct_parent_must_be_the_byte_checked_preview_even_with_matching_root(self):
        s=self.delivered();self.message.update(parent_id='om_unbound',root_id='om_preview')
        with self.assertRaisesRegex(ContractError,'direct reply'):self.apply(s)
        self.assert_unconfirmed()

    def test_superseded_preview_reply_does_not_approve_new_candidate(self):
        s=self.delivered();new=self.fixture.prepare();self.pilot.stage(new['publication_id'])
        with self.assertRaisesRegex(ContractError,'old preview'):self.apply(s)
        self.assert_unconfirmed()
        self.assertEqual(self.pilot.publisher._row(new['publication_id'])['state'],'prepared')

    def test_changed_material_review_rejects_approval(self):
        s=self.delivered()
        report=review_fixture.report_for(self.fixture.runtime,self.fixture.mapping)
        MaterialReviews(self.registry).record(self.fixture.entry,self.fixture.mapping['extraction_id'],report)
        with self.assertRaisesRegex(ContractError,'review changed'):self.apply(s)
        self.assert_unconfirmed()

    def test_wrong_configured_scope_does_not_query_and_wrong_platform_sender_stops(self):
        s=self.delivered();n=self.runner.call_count
        with patch.object(self.pilot,'scope',PilotScope('cli_other','oc_synthetic','ou_synthetic')):
            with self.assertRaises(ContractError):self.apply(s)
        self.assertEqual(self.runner.call_count,n)
        self.message['sender']['id']='ou_other'
        with self.assertRaisesRegex(ContractError,'identity'):self.apply(s)
        self.assert_unconfirmed();n=self.runner.call_count;s.close()
        with self.assertRaises(ContractError):self.make()
        self.assertEqual(self.runner.call_count,n)

    def test_known_id_without_verified_scan_never_queries(self):
        s=self.ready();n=self.runner.call_count
        with self.assertRaisesRegex(ContractError,'verified fixed-group scan'):self.apply(s)
        self.assertEqual(self.runner.call_count,n);self.assert_unconfirmed()

    def test_updated_reply_cannot_record_a_decision(self):
        s=self.delivered();self.message['updated']=True
        with self.assertRaises(ContractError):self.apply(s)
        self.assert_unconfirmed()

    def test_missing_physical_delivery_provenance_blocks_even_with_local_binding(self):
        s=self.delivered()
        # Drop in-memory provenance only; do not forge a physical receipt.
        s.events=[e for e in s.events if e.get('operation')!='preview:'+self.pid+':upload']
        with self.assertRaises(ContractError):self.apply(s)
        self.assert_unconfirmed()

    def test_tampered_detail_receipt_blocks_cached_decision_without_extra_http(self):
        s=self.delivered();s.message('scan_synthetic','om_owner_reply')
        next(self.root.glob('*-message.response.local')).write_text('{}')
        n=self.runner.call_count
        with self.assertRaises(ContractError):self.apply(s)
        self.assert_unconfirmed();self.assertEqual(self.runner.call_count,n)

    def test_expiry_during_confirmation_tree_hash_rolls_back_all_decision_rows(self):
        s=self.delivered();tree=self.pilot.publisher._tree
        def cross(*args):
            value=tree(*args);self.expire();return value
        with patch.object(self.pilot.publisher,'_tree',side_effect=cross),self.assertRaises(ContractError):self.apply(s)
        self.assert_unconfirmed()
        self.assertFalse(self.registry.rows("SELECT * FROM custody_events WHERE event_type='pilot_decision_observed'"))

    def test_receipt_mutation_during_confirmation_rolls_back_approval(self):
        s=self.delivered();tree=self.pilot.publisher._tree
        def change(*args):
            value=tree(*args);next(self.root.glob('*-list.response.local')).write_text('{}');return value
        with patch.object(self.pilot.publisher,'_tree',side_effect=change),self.assertRaises(ContractError):self.apply(s)
        self.assert_unconfirmed()

    def test_source_change_during_confirmation_rolls_back_approval(self):
        s=self.delivered();tree=self.pilot.publisher._tree
        def change(*args):
            value=tree(*args);self.source.write_text('changed code');return value
        with patch.object(self.pilot.publisher,'_tree',side_effect=change),self.assertRaises(ContractError):self.apply(s)
        self.assert_unconfirmed()

    def test_expiry_before_atomic_rename_retains_confirmation_and_publishing_intent(self):
        s=self.delivered();tree=self.pilot.publisher._tree;calls=0
        def cross(*args):
            nonlocal calls
            value=tree(*args);calls+=1
            if calls==3:self.expire() # confirmation, publish intent, pre-rename hash
            return value
        with patch.object(self.pilot.publisher,'_tree',side_effect=cross),self.assertRaises(ContractError):self.apply(s)
        self.assertEqual(self.pilot.publisher._row(self.pid)['state'],'publishing')
        self.assertEqual(len(self.registry.rows('SELECT * FROM publication_confirmations')),1)
        self.assertFalse((self.pilot.publisher.root/self.pid).exists())
        self.assertTrue((self.pilot.publisher.root/(self.pid+'.candidate')).exists())

    def test_expiry_after_rename_keeps_recoverable_files_and_does_not_claim_published(self):
        s=self.delivered();tree=self.pilot.publisher._tree
        def cross(row,name):
            value=tree(row,name)
            if name==self.pid:self.expire()
            return value
        with patch.object(self.pilot.publisher,'_tree',side_effect=cross),self.assertRaises(ContractError):self.apply(s)
        self.assertEqual(self.pilot.publisher._row(self.pid)['state'],'publishing')
        self.assertTrue((self.pilot.publisher.root/self.pid).exists())
        self.assertFalse(self.registry.rows('SELECT * FROM active_publications_v21'))
        self.assertFalse(self.registry.rows("SELECT * FROM custody_events WHERE event_type='publication_published'"))

    def test_hard_exit_after_rename_reuses_saved_reply_and_preserves_orphan_lease(self):
        s=self.delivered();s.message('scan_synthetic','om_owner_reply');n=self.runner.call_count;s.close()
        config=self.root/'decision-worker.local.json'
        config.write_text(encoded(dict(root=str(self.root),digest=self.digest,frozen=self.frozen,
            now=self.now.isoformat(),runtime=str(self.fixture.runtime.root),pid=self.pid)))
        result=subprocess.run([sys.executable,str(Path(__file__).with_name('pilot_decision_crash_worker.py')),str(config)],
            capture_output=True,text=True,timeout=40)
        self.assertEqual(result.returncode,89,result.stderr)
        self.assertEqual(self.pilot.publisher._row(self.pid)['state'],'publishing')
        leases=self.registry.rows("SELECT lease_id FROM asset_leases WHERE released_at IS NULL")
        self.assertTrue(leases)
        s=self.make();self.assertEqual(self.apply(s)['status'],'published')
        self.assertEqual(self.runner.call_count,n)
        self.assertEqual(self.registry.rows("SELECT lease_id FROM asset_leases WHERE released_at IS NULL"),leases)
        self.assertEqual(len(self.registry.rows("SELECT * FROM custody_events WHERE event_type='publication_published'")),1)
