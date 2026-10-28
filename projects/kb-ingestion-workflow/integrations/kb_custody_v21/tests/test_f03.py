"""Reuse F02 fixtures/wire; no simulated platform or live authentication."""
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.f01 import PREFIX, encoded, sha
from kb_custody_v21.f01_flow import run, NOTIFICATIONS
from kb_custody_v21.f02 import ISOLATION
from kb_custody_v21.f03 import ReconciledGroupExperiment, LIMITS, RECONCILIATION, EVIDENCE_FILES
import test_f02 as previous_fixture
from test_f02 import BOT, group, members, response
from test_custody import WORKTREE
from test_f01 import message_item


def crash_reconcile(approval_path):
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
    client = ReconciledGroupExperiment(approval['predecessor_root'], approval,
        wire=Mock(request=wire), sleep=lambda _:None,
        clock=lambda:datetime.fromisoformat(approval['starts_at'])+timedelta(seconds=1))
    client.token = 'synthetic-token'
    client.establish()


class F03Tests(unittest.TestCase):
    def setUp(self):
        # Reuse preparation without inheriting and duplicating F02's test suite.
        seed = previous_fixture.F02Tests('test_wrong_bot_stops_before_reading_group')
        seed.setUp()
        self.addCleanup(seed.doCleanups)
        seed.client.close()
        self.root, self.prior, self.now = seed.root, seed.prior, seed.now
        self.frozen, self.font = seed.frozen, seed.font
        self.failed = self.root/'f02-requests'
        snapshot = dict(files={'synthetic.py':sha(b'synthetic')}, sha256='synthetic-source')
        previous = dict(seed.approval, source_commit='synthetic-executed', source_snapshot=snapshot,
            starts_at=(self.now-timedelta(hours=1)).isoformat(), expires_at=(self.now+timedelta(hours=1)).isoformat())
        previous.pop('update_method')  # The actual consumed F02 did not bind a method.
        events = [dict(approval=previous),dict(binding='chat',value='oc_synthetic')]
        for key,kind,answer in [('auth:1','auth',(200,b'synthetic-auth')),
                ('bot:1','bot',BOT),('chat:before-update','chat',response(group(False))),
                ('members:before-update','members',response(members()))]:
            events += [dict(intent=key,kind=kind,method='POST' if kind=='auth' else 'GET'),
                       dict(complete=key,http_status=200,response_sha256=sha(answer[1]))]
            if kind != 'auth':
                (self.failed/(sha(key.encode())+'.response.local')).write_bytes(answer[1])
        events += [dict(binding='bot',value='ou_synthetic_bot'),dict(binding='prechecked',value=True),
            dict(intent='update:1',kind='update',method='PATCH',payload_sha256=sha(encoded(ISOLATION).encode())),
            dict(stopped='update:1',error_type='ContractError'),dict(stopped='orchestration',error_type='ContractError')]
        (self.failed/'journal.local.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in events))
        (self.failed/(sha(b'update:1')+'.response.local')).write_bytes(b'404 page not found')
        (self.failed/'result.local.json').write_text(json.dumps(dict(status='STOPPED',unfinished_requests=1,
            physical_requests={k:int(k in ('auth','bot','chat','members','update')) for k in LIMITS},
            ended_at=(self.now-timedelta(minutes=59)).isoformat())))
        (self.failed/'executed-source.local.json').write_text(json.dumps(dict(head='synthetic-executed',**snapshot)))
        self.approval = dict(previous, reference='F03', limits=LIMITS, update_method='PUT', unknown_lookup=0,
            starts_at=(self.now-timedelta(minutes=1)).isoformat(),expires_at=(self.now+timedelta(minutes=119)).isoformat(),
            f02_root=str(self.failed),group_state_reconciliation=RECONCILIATION,f02_evidence_sha256=self.hashes())
        self.old_bytes={name:(self.failed/name).read_bytes() for name in EVIDENCE_FILES}
        self.wire = Mock()
        self.client = self.make()
        self.addCleanup(self.client.close)
        self.client.token = 'synthetic-token'

    def hashes(self):
        return {name:sha((self.failed/name).read_bytes()) for name in EVIDENCE_FILES}

    def make(self, approval=None):
        return ReconciledGroupExperiment(self.prior, approval or self.approval,
            wire=self.wire,clock=lambda:self.now,sleep=lambda _:None)

    def answers(self, strict=False):
        return [BOT,response(group(strict)),response(members())]+([] if strict else [response({})])+[
            response(group()),response(members())]

    def assert_old_unchanged(self):
        self.assertEqual({name:(self.failed/name).read_bytes() for name in EVIDENCE_FILES},self.old_bytes)

    def test_legacy_state_one_put_then_strict_verification_preserves_failed_history(self):
        self.wire.request.side_effect=self.answers()
        self.client.establish()
        calls=self.wire.request.call_args_list
        self.assertEqual([c.args[0] for c in calls],['GET','GET','GET','PUT','GET','GET'])
        self.assertEqual(json.loads(calls[3].args[2]),ISOLATION)
        self.assertEqual(self.client.value('group_state_reconciliation'),'updated_and_verified')
        self.assert_old_unchanged()

    def test_target_state_skips_put_and_cannot_later_use_unused_update_budget(self):
        self.wire.request.side_effect=self.answers(True)
        self.client.establish()
        self.assertEqual(self.wire.request.call_count,5)
        self.assertTrue(all(c.args[0]=='GET' for c in self.wire.request.call_args_list))
        self.assertEqual(self.client.value('group_state_reconciliation'),'already_target_verified')
        with self.assertRaises(ContractError):
            self.client.request('update','PUT',PREFIX+'/im/v1/chats/oc_synthetic',ISOLATION,key='update:1')
        self.assertEqual(self.wire.request.call_count,5)
        self.assert_old_unchanged()

    def test_mixed_current_settings_stop_before_any_write(self):
        self.wire.request.side_effect=[BOT,response(dict(group(),share_card_permission='allowed')),response(members())]
        with self.assertRaisesRegex(ContractError,'conditional branches'):
            self.client.establish()
        self.assertEqual(self.wire.request.call_count,3)
        self.assertFalse(self.client.value('hardened'))

    def test_missing_observation_authority_or_changed_objects_rejected_without_network(self):
        self.client.close()
        for changed in [dict(approved=False),dict(group_state_reconciliation={}),dict(unknown_lookup=1),
                dict(update_method='PATCH'),dict(f02_root=str(self.root/'other')),
                dict(account_id='ou_other'),dict(f02_evidence_sha256={})]:
            with self.subTest(changed=changed),self.assertRaises(ContractError):
                self.make(dict(self.approval,**changed))
        self.wire.request.assert_not_called()

    def test_f02_receipt_mutation_stops_current_session_before_network(self):
        (self.failed/(sha(b'update:1')+'.response.local')).write_bytes(b'other')
        with self.assertRaisesRegex(ContractError,'evidence changed'):
            self.client.bot()
        self.wire.request.assert_not_called()

    def test_rebound_digest_cannot_make_different_failure_eligible(self):
        self.client.close()
        (self.failed/(sha(b'update:1')+'.response.local')).write_bytes(b'{"code":0}')
        with self.assertRaisesRegex(ContractError,'unexpected unresolved'):
            self.make(dict(self.approval,f02_evidence_sha256=self.hashes()))
        self.wire.request.assert_not_called()

    def test_changed_executed_source_manifest_rejected_even_with_rebound_file_digest(self):
        self.client.close()
        p=self.failed/'executed-source.local.json'
        source=json.loads(p.read_text()); source['head']='other'
        p.write_text(json.dumps(source))
        with self.assertRaisesRegex(ContractError,'executed source'):
            self.make(dict(self.approval,f02_evidence_sha256=self.hashes()))
        self.wire.request.assert_not_called()

    def test_successful_or_additional_old_request_is_not_eligible_for_continuation(self):
        self.client.close()
        p=self.failed/'journal.local.jsonl'
        p.write_bytes(p.read_bytes()+b'{"complete":"update:1","http_status":200}\n')
        with self.assertRaisesRegex(ContractError,'five-request'):
            self.make(dict(self.approval,f02_evidence_sha256=self.hashes()))
        self.wire.request.assert_not_called()

    def test_old_evidence_change_during_current_reads_prevents_update(self):
        answers=iter(self.answers())
        def mutate_after_members(*args):
            value=next(answers)
            if '/members?' in args[1]:
                p=self.failed/'journal.local.jsonl'; p.write_bytes(p.read_bytes()+b'\n')
            return value
        self.wire.request.side_effect=mutate_after_members
        with self.assertRaisesRegex(ContractError,'evidence changed'):
            self.client.establish()
        self.assertEqual(self.wire.request.call_count,3)

    def test_unknown_state_read_cannot_restart_or_use_alternate_budget_location(self):
        self.wire.request.side_effect=[BOT,TimeoutError('synthetic')]
        with self.assertRaises(TimeoutError): self.client.establish()
        self.client.close()
        with self.assertRaises(ContractError): self.make()
        self.assertEqual(self.wire.request.call_count,2)
        self.assertEqual(self.client.root,self.root/'f03-requests')
        self.assert_old_unchanged()

    def test_unknown_put_stops_without_post_read_or_upload(self):
        self.wire.request.side_effect=self.answers()[:3]+[TimeoutError('synthetic')]
        with self.assertRaises(TimeoutError): self.client.establish()
        with self.assertRaises(ContractError): self.client.upload('fixture.txt',self.frozen['fixture.txt'])
        self.client.close()
        with self.assertRaises(ContractError): self.make()
        self.assertEqual(self.wire.request.call_count,4)
        self.assert_old_unchanged()

    def test_real_child_death_after_put_effect_never_repeats_operation(self):
        self.client.close()
        path=self.root/'f03.synthetic.json'; path.write_text(json.dumps(self.approval))
        env={**os.environ,'PYTHONPATH':os.pathsep.join([os.environ.get('PYTHONPATH',''),
            str(Path(__file__).parent),str(Path(__file__).parents[1]),str(Path(__file__).parents[1]/'src'),
            str(Path(__file__).parents[2]/'local_conversion_mcp/src')])}
        result=subprocess.run([sys.executable,'-c','import sys; from test_f03 import crash_reconcile; crash_reconcile(sys.argv[1])',
            str(path)],env=env,capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,77,result.stderr)
        self.assertEqual(json.loads(path.with_suffix('.remote-effect').read_bytes()),ISOLATION)
        with self.assertRaises(ContractError): self.make()
        self.assert_old_unchanged()

    def test_both_branches_use_exact_35_or_36_requests_and_actual_local_conversion(self):
        self.client.close()
        for strict in (False,True):
            with self.subTest(strict=strict):
                # Each branch gets a separate synthetic history, never a new real run location.
                helper=F03Tests('test_legacy_state_one_put_then_strict_verification_preserves_failed_history')
                helper.setUp()
                try:
                    answers=[(200,b'{"code":0,"tenant_access_token":"synthetic-token"}')]+helper.answers(strict)
                    sent=[]
                    for index,name in enumerate(('fixture.txt','fixture.md','geometry.png','preview.pdf')):
                        image=name.endswith('.png'); key=('img_' if image else 'file_')+'synthetic_'+str(index)
                        mid='om_synthetic_'+str(index); sent.append(mid)
                        content={('image_key' if image else 'file_key'):key+'_message'}
                        if not image:
                            content['file_name']=name
                        item=message_item(mid,'image' if image else 'file',content)
                        answers += [response(group()),response(members()),response({('image_key' if image else 'file_key'):key}),
                            response(item),response({'items':[item]}),
                            (200,helper.frozen[name])]
                        if index<2:
                            mid='om_notice_'+str(index); sent.append(mid)
                            answers += [response(message_item(mid,'text',{'text':NOTIFICATIONS[index]}))]
                    answers += [response(group()),response(members()),response(dict(has_more=False,
                        items=[dict(message_id=m,chat_id='oc_synthetic') for m in sent]))]
                    helper.wire.request.side_effect=answers
                    helper.client.authenticate('cli_synthetic','synthetic-secret')
                    result=run(helper.client,WORKTREE,helper.root/'conversion',helper.frozen,helper.font)
                    self.assertEqual(helper.wire.request.call_count,35 if strict else 36)
                    self.assertEqual(sum(LIMITS.values()),36)
                    self.assertEqual(result['transport_sha256'],{k:sha(v) for k,v in helper.frozen.items()})
                    self.assertEqual(result['conversions']['fixture.txt']['status'],'ready_candidate')
                    self.assertEqual(result['end_to_end'],'NOT_RUN')
                    helper.assert_old_unchanged()
                finally:
                    helper.doCleanups()
