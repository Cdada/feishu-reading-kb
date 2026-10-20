"""F02 uses existing Mock Wire, synthetic fixtures and actual local converters."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.f01 import PREFIX, sha
from kb_custody_v21.f01_flow import fixtures, NOTIFICATIONS, run
from kb_custody_v21.f02 import ExistingGroupExperiment, ISOLATION, LIMITS, SCOPES, administration
from test_custody import BASE, WORKTREE
from test_f01 import message_item


def response(data):
    return 200, json.dumps({'code': 0, 'data': data}).encode()


def group(strict=True):
    return dict(chat_mode='group', chat_type='private', external=False,
                bot_manager_id_list=[], user_manager_id_list=[], user_count='1', bot_count='1',
                **(ISOLATION if strict else dict(add_member_permission='all_members',
                                                share_card_permission='allowed')))


def members():
    return dict(has_more=False, items=[dict(member_id='ou_synthetic', member_id_type='open_id')])


BOT = (200, b'{"code":0,"bot":{"open_id":"ou_synthetic_bot"}}')


def crash_update(approval_path):
    """Physical child death after a synthetic remote side effect, before receipt."""
    approval = json.loads(Path(approval_path).read_text())
    answers = iter([BOT, response(group(False)), response(members())])
    def wire(method, path, body, token, content_type):
        if method == 'PUT':
            marker = Path(approval_path).with_suffix('.remote-effect')
            with marker.open('xb') as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            os._exit(77)
        return next(answers)
    client = ExistingGroupExperiment(approval['predecessor_root'], approval,
        wire=Mock(request=wire), sleep=lambda _: None,
        clock=lambda: datetime.fromisoformat(approval['starts_at']) + timedelta(seconds=1))
    client.token = 'synthetic-token'
    client.establish()


class F02Tests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='f02-', dir=BASE / 'test-output'))
        self.prior = self.root / 'requests'
        self.prior.mkdir()
        self.now = datetime.now(timezone.utc)
        self.font = os.environ.get('KB_V21_PREVIEW_FONT', '/System/Library/Fonts/STHeiti Light.ttc')
        # Approval binds PNG bytes, not a platform-dependent re-rendering.
        png = (Path(__file__).parent/'fixtures/f02-geometry.png').read_bytes()
        self.frozen = fixtures(png, self.font)
        self.approval = dict(reference='F02', approved=True, synthetic_only=True, limits=LIMITS,
            app_id='cli_synthetic', account_id='ou_synthetic', bot_id='ou_synthetic_bot',
            chat_id='oc_synthetic', administration=administration(group()),
            starts_at=(self.now-timedelta(minutes=1)).isoformat(),
            expires_at=(self.now+timedelta(minutes=119)).isoformat(),
            fixtures={k: dict(sha256=sha(v), bytes=len(v)) for k, v in self.frozen.items()},
            notifications=NOTIFICATIONS, scopes=SCOPES, published_version='0.1.1',
            publish_scope='only_bound_account', automatic_retries=0, update_payload=ISOLATION, update_method='PUT',
            predecessor_root=str(self.prior))
        previous = dict(self.approval, reference='F01',
            starts_at=(self.now-timedelta(days=1)).isoformat(),
            expires_at=(self.now-timedelta(hours=22)).isoformat())
        events = [{'approval': previous}]
        for kind, answer in [('auth', (200, b'synthetic-auth-not-persisted')), ('bot', BOT),
                ('create', response({'chat_id': 'oc_synthetic'})), ('chat', response(group(False)))]:
            key = kind + ':1'
            events.extend([{'intent': key, 'kind': kind},
                           {'complete': key, 'http_status': 200, 'response_sha256': sha(answer[1])}])
            if kind != 'auth':
                (self.prior / (sha(key.encode()) + '.response.local')).write_bytes(answer[1])
        events.extend([{'binding': 'bot', 'value': 'ou_synthetic_bot'},
                       {'binding': 'chat', 'value': 'oc_synthetic'},
                       {'stopped': 'group identity/membership mismatch'}])
        self.previous_bytes = ('\n'.join(json.dumps(e) for e in events)+'\n').encode()
        (self.prior / 'journal.local.jsonl').write_bytes(self.previous_bytes)
        self.approval['predecessor_sha256'] = sha(self.previous_bytes)
        self.wire = Mock()
        self.client = self.make()
        self.addCleanup(self.client.close)
        self.client.token = 'synthetic-token'

    def make(self, approval=None):
        return ExistingGroupExperiment(self.prior, approval or self.approval, wire=self.wire,
                                        clock=lambda: self.now, sleep=lambda _: None)

    def establish_answers(self, after=None, member_response=None):
        return [BOT, response(group(False)), response(member_response or members()), response({}),
                response(after or group()), response(members())]

    def test_one_exact_update_after_identity_checks_and_prior_journal_unchanged(self):
        self.wire.request.side_effect = self.establish_answers()
        self.client.establish()
        calls = self.wire.request.call_args_list
        self.assertEqual([c.args[0] for c in calls], ['GET', 'GET', 'GET', 'PUT', 'GET', 'GET'])
        self.assertEqual(json.loads(calls[3].args[2]), ISOLATION)
        self.assertTrue(self.client.value('hardened'))
        self.assertEqual((self.prior / 'journal.local.jsonl').read_bytes(), self.previous_bytes)
        with self.assertRaises(ContractError):
            self.client.request('update', 'PUT', PREFIX+'/im/v1/chats/oc_synthetic',
                                ISOLATION, key='update:1')
        self.assertEqual(self.wire.request.call_count, 6)

    def test_official_update_contract_reaches_actual_https_wire(self):
        from kb_custody_v21.s6 import Wire
        contract = json.loads((Path(__file__).parent/'fixtures/feishu-chat-update-contract.json').read_text())
        self.client.wire = Wire()
        self.client.token = 'synthetic-token'
        self.client.bind('bot', 'ou_synthetic_bot')
        self.client.inspect_group = Mock(return_value=group())
        self.client.bot = Mock(return_value={'open_id': 'ou_synthetic_bot'})
        connection = Mock()
        response_mock = connection.getresponse.return_value
        response_mock.status = 200
        response_mock.read.return_value = b'{"code":0,"data":{}}'
        with patch('kb_custody_v21.s6.http.client.HTTPSConnection', return_value=connection):
            self.client.establish()
        args, kwargs = connection.request.call_args
        self.assertEqual(args, (contract['method'], contract['path_template'].format(chat_id='oc_synthetic')))
        self.assertEqual(json.loads(kwargs['body']), contract['approved_body'])
        connection.request.assert_called_once()
        connection.close.assert_called_once()

    def test_non_json_http_rejection_remains_pending_and_blocks_retry_and_restart(self):
        self.client.bind('prechecked', True)
        raw = b'404 page not found\n'
        self.wire.request.return_value = (404, raw)
        with self.assertRaisesRegex(ContractError, 'provider HTTP failure'):
            self.client.request('update', 'PUT', PREFIX+'/im/v1/chats/oc_synthetic', ISOLATION, key='update:1')
        received = [e for e in self.client.events if e.get('received') == 'update:1']
        self.assertEqual(received, [dict(received='update:1', http_status=404, response_sha256=sha(raw))])
        self.assertEqual((self.client.root/(sha(b'update:1')+'.response.local')).read_bytes(), raw)
        self.assertFalse(any(e.get('complete') == 'update:1' for e in self.client.events))
        with self.assertRaises(ContractError):
            self.client.request('update', 'PUT', PREFIX+'/im/v1/chats/oc_synthetic', ISOLATION, key='update:1')
        self.client.close()
        with self.assertRaisesRegex(ContractError, 'unfinished/failed'):
            self.make()
        self.wire.request.assert_called_once()

    def test_unapproved_expanded_scope_or_retry_plan_rejected_without_network(self):
        self.client.close()
        for update in [dict(approved=False), dict(scopes=SCOPES+['im:chat']),
                       dict(automatic_retries=1), dict(publish_scope='all'),
                       dict(update_payload={**ISOLATION, 'name': 'different'})]:
            with self.subTest(update=update), self.assertRaises(ContractError):
                self.make(dict(self.approval, **update))
        self.wire.request.assert_not_called()

    def test_old_or_missing_method_approval_and_patch_request_rejected_without_network(self):
        self.client.close()
        for method in (None, 'PATCH', 'POST'):
            with self.subTest(method=method), self.assertRaises(ContractError):
                self.make(dict(self.approval, update_method=method))
        self.client = self.make()
        self.addCleanup(self.client.close)
        self.client.token = 'synthetic-token'
        self.client.bind('prechecked', True)
        with self.assertRaises(ContractError):
            self.client.request('update', 'PATCH', PREFIX+'/im/v1/chats/oc_synthetic', ISOLATION, key='update:1')
        self.assertFalse(any('intent' in event for event in self.client.events))
        self.wire.request.assert_not_called()

    def test_changed_receipt_or_owner_rejected_before_network(self):
        self.client.close()
        with self.assertRaisesRegex(ContractError, 'receipt identity'):
            self.make(dict(self.approval, administration=dict(administration(group()), owner_id='ou_synthetic')))
        path = self.prior / (sha(b'create:1') + '.response.local')
        path.write_bytes(response({'chat_id': 'oc_other'})[1])
        with self.assertRaisesRegex(ContractError, 'receipt bytes'):
            self.make()
        self.wire.request.assert_not_called()

    def test_changed_predecessor_blocks_current_session_and_new_budget(self):
        (self.prior / 'journal.local.jsonl').write_bytes(self.previous_bytes+b'\n')
        with self.assertRaisesRegex(ContractError, 'evidence changed'):
            self.client.bot()
        self.client.close()
        with self.assertRaises(ContractError):
            self.make()
        with self.assertRaises(ContractError):
            self.make(dict(self.approval, predecessor_root=str(self.root/'other')))
        self.wire.request.assert_not_called()

    def test_forged_verified_flag_does_not_enable_transfer_or_unchecked_update(self):
        self.client.bind('verified', True)
        for action in [lambda: self.client.upload('fixture.txt', self.frozen['fixture.txt']),
                       lambda: self.client.request('update', 'PUT', PREFIX+'/im/v1/chats/oc_synthetic',
                                                   ISOLATION, key='update:1'), self.client.create]:
            with self.assertRaises(ContractError):
                action()
        self.wire.request.assert_not_called()

    def test_update_payload_key_and_target_cannot_expand_authorization(self):
        self.client.bind('prechecked', True)
        for data, key, target in [({**ISOLATION, 'name': 'other'}, 'update:1', 'oc_synthetic'),
                                 (ISOLATION, 'update:2', 'oc_synthetic'),
                                 (ISOLATION, 'update:1', 'oc_other')]:
            with self.assertRaises(ContractError):
                self.client.request('update', 'PUT', PREFIX+'/im/v1/chats/'+target, data, key=key)
        self.wire.request.assert_not_called()

    def test_wrong_bot_stops_before_reading_group(self):
        self.wire.request.return_value = (200, b'{"code":0,"bot":{"open_id":"ou_other"}}')
        with self.assertRaisesRegex(ContractError, 'bot identity'):
            self.client.establish()
        self.assertEqual(self.wire.request.call_count, 1)

    def test_changed_owner_stops_before_members_or_update(self):
        self.wire.request.side_effect = [BOT, response(dict(group(False), owner_id='ou_other'))]
        with self.assertRaises(ContractError):
            self.client.establish()
        self.assertEqual(self.wire.request.call_count, 2)

    def test_added_manager_stops_before_members_or_update(self):
        self.wire.request.side_effect = [BOT, response(dict(group(False), user_manager_id_list=['ou_other']))]
        with self.assertRaises(ContractError):
            self.client.establish()
        self.assertEqual(self.wire.request.call_count, 2)

    def test_truncated_members_stops_before_update(self):
        self.wire.request.side_effect = self.establish_answers(member_response=dict(members(), has_more=True))
        with self.assertRaises(ContractError):
            self.client.establish()
        self.assertEqual(self.wire.request.call_count, 3)

    def test_foreign_member_stops_before_update(self):
        value = dict(has_more=False, items=[dict(member_id='ou_other', member_id_type='open_id')])
        self.wire.request.side_effect = self.establish_answers(member_response=value)
        with self.assertRaises(ContractError):
            self.client.establish()
        self.assertEqual(self.wire.request.call_count, 3)

    def test_update_success_but_ineffective_isolation_stops_all_transfer(self):
        self.wire.request.side_effect = self.establish_answers(after=group(False))
        with self.assertRaises(ContractError):
            self.client.establish()
        with self.assertRaises(ContractError):
            self.client.upload('fixture.txt', self.frozen['fixture.txt'])
        self.assertEqual(self.wire.request.call_count, 5)
        self.assertFalse(self.client.value('hardened'))

    def test_unknown_update_cannot_retry_or_restart(self):
        self.wire.request.side_effect = self.establish_answers()[:3]+[TimeoutError('synthetic')]
        with self.assertRaises(TimeoutError):
            self.client.establish()
        self.client.close()
        with self.assertRaisesRegex(ContractError, 'unfinished/failed'):
            self.make()
        self.assertEqual(self.wire.request.call_count, 4)

    def test_hard_process_exit_after_update_effect_cannot_repeat_update(self):
        self.client.close()
        approval_path = self.root/'approval.synthetic.json'
        approval_path.write_text(json.dumps(self.approval))
        command = [sys.executable, '-c', 'import sys; from test_f02 import crash_update; crash_update(sys.argv[1])',
                   str(approval_path)]
        # Full suite discovery changes sys.path, not the inherited environment.
        # Preserve its offline guard and explicitly export these local modules.
        env = {**os.environ, 'PYTHONPATH': os.pathsep.join([os.environ.get('PYTHONPATH', ''),
            str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1]),
            str(Path(__file__).resolve().parents[1]/'src'),
            str(Path(__file__).resolve().parents[2]/'local_conversion_mcp/src')])}
        result = subprocess.run(command, capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(result.returncode, 77, result.stderr)
        self.assertEqual(json.loads(approval_path.with_suffix('.remote-effect').read_bytes()), ISOLATION)
        with self.assertRaisesRegex(ContractError, 'unfinished/failed'):
            self.make()
        events = [json.loads(line) for line in (self.root/'f02-requests/journal.local.jsonl').read_text().splitlines()]
        self.assertEqual(sum(e.get('kind') == 'update' for e in events), 1)
        self.wire.request.assert_not_called()

    def test_any_attempt_prevents_fresh_auth_budget_on_restart(self):
        self.wire.request.return_value = BOT
        self.client.bot()
        self.client.close()
        with self.assertRaisesRegex(ContractError, 'already attempted'):
            self.make()
        self.assertEqual(self.wire.request.call_count, 1)

    def test_expiry_during_rate_wait_prevents_update_intent(self):
        self.client.bind('prechecked', True)
        self.client.sleep = lambda _: setattr(self, 'now', self.now+timedelta(hours=3))
        with self.assertRaisesRegex(ContractError, 'outside approved F02'):
            self.client.request('update', 'PUT', PREFIX+'/im/v1/chats/oc_synthetic', ISOLATION, key='update:1')
        self.assertFalse(any('intent' in event for event in self.client.events))
        self.wire.request.assert_not_called()

    def test_full_synthetic_transport_uses_36_calls_actual_conversion_and_exact_bytes(self):
        answers = [(200, b'{"code":0,"tenant_access_token":"synthetic-token"}')]+self.establish_answers()
        sent = []
        for index, name in enumerate(('fixture.txt', 'fixture.md', 'geometry.png', 'preview.pdf')):
            image = name.endswith('.png')
            resource = ('img_' if image else 'file_')+'synthetic_'+str(index)
            mid = 'om_synthetic_'+str(index)
            sent.append(mid)
            content={('image_key' if image else 'file_key'):resource+'_message'}
            if not image:
                content['file_name']=name
            item=message_item(mid,'image' if image else 'file',content)
            answers.extend([response(group()), response(members()),
                response({('image_key' if image else 'file_key'): resource}),
                response(item), response({'items':[item]}),
                (200, self.frozen[name])])
            if index < 2:
                notice = 'om_synthetic_notice_'+str(index)
                sent.append(notice)
                answers.append(response(message_item(notice,'text',{'text':NOTIFICATIONS[index]})))
        answers.extend([response(group()), response(members()), response(dict(has_more=False,
            items=[dict(message_id=mid, chat_id='oc_synthetic') for mid in sent]))])
        self.wire.request.side_effect = answers
        self.client.authenticate('cli_synthetic', 'synthetic-secret')
        result = run(self.client, WORKTREE, self.root/'conversion-runtime', self.frozen, self.font)
        self.assertEqual(self.wire.request.call_count, 36)
        self.assertEqual(sum(LIMITS.values()), 38)
        self.assertEqual(result['transport_sha256'], {k: sha(v) for k, v in self.frozen.items()})
        self.assertEqual(result['conversions']['fixture.txt']['status'], 'needs_review')
        self.assertEqual(result['conversions']['fixture.md']['status'], 'ready_candidate')
        self.assertEqual(result['end_to_end'], 'NOT_RUN')
        for kind, maximum in LIMITS.items():
            self.assertLessEqual(sum(e.get('kind') == kind for e in self.client.events), maximum)
        self.assertEqual((self.prior/'journal.local.jsonl').read_bytes(), self.previous_bytes)
        for p in self.client.root.iterdir():
            if p.is_file():
                self.assertNotIn(b'synthetic-token', p.read_bytes())
                self.assertNotIn(b'synthetic-secret', p.read_bytes())

    def test_execution_requires_exact_clean_tested_source_before_credential(self):
        from f02_execute import checked_source
        snapshot = dict(files={'synthetic.py': 'digest'}, sha256='synthetic-digest')
        approval = dict(source_commit='synthetic-head', source_snapshot=snapshot)
        with patch('f02_execute.source_snapshot', return_value=snapshot), \
                patch('f02_execute.subprocess.check_output', side_effect=['synthetic-head\n', b'']):
            self.assertEqual(checked_source(WORKTREE, approval)['head'], 'synthetic-head')
        for head, dirty, candidate in [('synthetic-head', b' M synthetic.py', approval),
                ('other-head', b'', approval),
                ('synthetic-head', b'', dict(approval, source_snapshot={}))]:
            with patch('f02_execute.source_snapshot', return_value=snapshot), \
                    patch('f02_execute.subprocess.check_output', side_effect=[head, dirty]), \
                    self.assertRaises(ContractError):
                checked_source(WORKTREE, candidate)
        self.wire.request.assert_not_called()

    def test_execution_refuses_changed_frozen_pdf_before_credential(self):
        from f02_execute import frozen_files
        for name, data in self.frozen.items():
            (self.root/name).write_bytes(data)
        self.assertEqual(frozen_files(self.root, self.approval, self.font), self.frozen)
        (self.root/'preview.pdf').write_bytes(b'%PDF-changed-synthetic')
        with self.assertRaisesRegex(ContractError, 'fixture bytes changed'):
            frozen_files(self.root, self.approval, self.font)
        self.wire.request.assert_not_called()
