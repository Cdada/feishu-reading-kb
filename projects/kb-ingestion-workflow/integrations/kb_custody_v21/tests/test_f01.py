from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.f01 import Experiment, LIMITS, PREFIX, sha
from test_custody import BASE


def message_item(mid, kind, content):
    """Synthetic full message envelope, independent of upload receipts."""
    return dict(message_id=mid, chat_id='oc_synthetic', msg_type=kind,
        deleted=False, updated=False,
        sender=dict(id='cli_synthetic', id_type='app_id', sender_type='app'),
        body=dict(content=json.dumps(content)))


class F01Tests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='f01-', dir=BASE / 'test-output'))
        self.now = datetime.now(timezone.utc)
        self.approval = dict(reference='F01', approved=True, synthetic_only=True, limits=LIMITS,
            app_id='cli_synthetic', account_id='ou_synthetic',
            starts_at=(self.now-timedelta(minutes=1)).isoformat(),
            expires_at=(self.now+timedelta(minutes=119)).isoformat(),
            fixtures={'fixture.txt': {'sha256':sha(b'synthetic'), 'bytes':9}}, notifications=['synthetic accepted'])
        self.wire = Mock()
        self.client = Experiment(self.root, self.approval, wire=self.wire, clock=lambda:self.now, sleep=lambda _:None)
        self.addCleanup(self.client.close)

    def ready(self):
        self.client.token = 'synthetic-token'
        self.client.bind('bot','ou_synthetic_bot')
        self.client.bind('chat','oc_synthetic')
        self.client.bind('verified', True)

    def response(self, data):
        return (200, json.dumps({'code':0,'data':data}).encode())

    def test_unknown_intent_survives_restart_and_stops_before_network(self):
        self.ready()
        self.wire.request.side_effect = TimeoutError('synthetic timeout')
        with self.assertRaises(TimeoutError):
            self.client.upload('fixture.txt',b'synthetic')
        self.client.close()
        with self.assertRaisesRegex(ContractError, 'unfinished/failed'):
            Experiment(self.root,self.approval,wire=self.wire,clock=lambda:self.now,sleep=lambda _:None)
        self.assertEqual(self.wire.request.call_count,1)

    def test_expired_or_wrong_app_never_reads_network(self):
        with self.assertRaises(ContractError):
            self.client.authenticate('cli_unapproved','synthetic-secret')
        self.now += timedelta(hours=3)
        with self.assertRaises(ContractError):
            self.client.authenticate('cli_synthetic','synthetic-secret')
        self.wire.request.assert_not_called()

    def test_changed_approval_cannot_reset_budget(self):
        self.client.close()
        changed = dict(self.approval,account_id='ou_other')
        with self.assertRaisesRegex(ContractError,'approval changed'):
            Experiment(self.root,changed,wire=self.wire,clock=lambda:self.now,sleep=lambda _:None)

    def test_fixed_fixture_and_foreign_endpoint_rejected_before_request(self):
        self.ready()
        with self.assertRaises(ContractError):
            self.client.upload('fixture.txt',b'private-other-content')
        with self.assertRaises(ContractError):
            self.client.request('chat','GET',PREFIX+'/im/v1/chats/oc_other',key='unapproved')
        self.wire.request.assert_not_called()

    def test_upload_send_download_same_bytes_and_no_resend(self):
        self.ready()
        self.wire.request.return_value=self.response({'file_key':'file_synthetic'})
        self.client.upload('fixture.txt',b'synthetic')
        item=message_item('om_synthetic','file',{'file_key':'file_synthetic','file_name':'fixture.txt'})
        self.wire.request.return_value=self.response(item)
        self.client.send('fixture.txt')
        with self.assertRaisesRegex(ContractError,'already attempted'):
            self.client.send('fixture.txt')
        self.wire.request.side_effect=[self.response({'items':[item]}),(200,b'synthetic')]
        self.assertEqual(self.client.download('fixture.txt'),b'synthetic')
        self.assertEqual(self.wire.request.call_count,4)

    def test_group_extra_member_or_truncation_stops_channel(self):
        self.ready()
        group=dict(chat_mode='group',chat_type='private',external=False,user_count='1',bot_count='1',
                   share_card_permission='not_allowed',add_member_permission='only_owner')
        self.wire.request.side_effect=[self.response(group),self.response({'has_more':True,'items':[]})]
        with self.assertRaises(ContractError):
            self.client.verify('synthetic-batch')
        with self.assertRaises(ContractError):
            self.client.upload('fixture.txt',b'synthetic')
        self.assertEqual(self.wire.request.call_count,2)

    def test_endpoint_budget_not_fungible(self):
        self.ready()
        self.wire.request.return_value=self.response({})
        for index in range(8):
            self.client.request('chat','GET',PREFIX+'/im/v1/chats/oc_synthetic',key='chat:'+str(index))
        with self.assertRaisesRegex(ContractError,'budget exhausted'):
            self.client.request('chat','GET',PREFIX+'/im/v1/chats/oc_synthetic',key='chat:extra')
        self.assertEqual(self.wire.request.call_count,8)

    def test_auth_response_token_is_not_written_to_evidence(self):
        self.wire.request.return_value=(200,json.dumps({'code':0,'tenant_access_token':'synthetic-secret-token'}).encode())
        self.client.authenticate('cli_synthetic','synthetic-app-secret')
        for path in self.root.iterdir():
            if path.is_file():
                self.assertNotIn(b'synthetic-secret-token',path.read_bytes())
                self.assertNotIn(b'synthetic-app-secret',path.read_bytes())

    def test_listing_requires_exact_confirmed_message_set(self):
        self.ready()
        self.client.bind('message:fixture.txt', {'message_id':'om_synthetic'})
        self.wire.request.return_value=self.response({'has_more':False,'items':[
            {'message_id':'om_other','chat_id':'oc_synthetic'}]})
        with self.assertRaisesRegex(ContractError,'message list outside'):
            self.client.list_sent()
        with self.assertRaises(ContractError):
            self.client.upload('fixture.txt',b'synthetic')
        self.assertEqual(self.wire.request.call_count,1)

    def test_failure_response_preserved_privately_without_retry(self):
        self.ready()
        raw=b'{"code":99991672,"msg":"synthetic scope failure"}'
        self.wire.request.return_value=(403,raw)
        with self.assertRaises(ContractError):
            self.client.upload('fixture.txt',b'synthetic')
        evidence=self.root/(sha(b'upload:fixture.txt')+'.response.local')
        self.assertEqual(evidence.read_bytes(),raw)
        self.assertEqual(evidence.stat().st_mode & 0o777,0o600)
        with self.assertRaises(ContractError):
            self.client.upload('fixture.txt',b'synthetic')
        self.assertEqual(self.wire.request.call_count,1)

    def test_incomplete_create_permission_plan_cannot_create_another_group(self):
        self.client.token='synthetic-token'
        self.client.bind('bot','ou_synthetic_bot')
        with self.assertRaisesRegex(ContractError,'separately approved chat update'):
            self.client.create()
        with self.assertRaises(ContractError):
            self.client.request('create','POST',PREFIX+'/im/v1/chats?user_id_type=open_id',{},key='create:1')
        self.wire.request.assert_not_called()


class F01FlowTests(unittest.TestCase):
    def test_preview_is_reproducible_and_rejects_other_material(self):
        from kb_custody_v21.f01_flow import synthetic_preview, MD
        import os
        font=os.environ.get('KB_V21_PREVIEW_FONT','/System/Library/Fonts/STHeiti Light.ttc')
        self.assertEqual(synthetic_preview(MD,font),synthetic_preview(MD,font))
        with self.assertRaises(ContractError):
            synthetic_preview(b'other material',font)

    def test_verified_synthetic_download_runs_actual_custody_conversion(self):
        from kb_custody_v21.f01_flow import convert_download, TXT, MD
        from test_custody import WORKTREE
        root=Path(tempfile.mkdtemp(prefix='f01-conversion-',dir=BASE/'test-output'))
        for name,data in [('fixture.txt',TXT),('fixture.md',MD)]:
            body,evidence=convert_download(WORKTREE,root,name,data,{'message_id':'om_synthetic'})
            self.assertEqual(body,b'# fixture\n\n'+TXT if name.endswith('.txt') else MD)
            self.assertEqual(evidence['status'],'needs_review' if name.endswith('.txt') else 'ready_candidate')
            self.assertEqual(evidence['body_sha256'],sha(body))
        with self.assertRaises(ContractError):
            convert_download(WORKTREE,root,'fixture.md',b'changed',{'message_id':'om_synthetic'})
