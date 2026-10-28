"""Known-message continuation reuses F01-F03 mocks and frozen files."""
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.f01 import PREFIX, encoded, sha
from kb_custody_v21.f01_flow import NOTIFICATIONS, completed_steps, run
from kb_custody_v21.f04 import KnownMessageExperiment, LIMITS, REUSE, EVIDENCE_FILES
from test_custody import WORKTREE
from test_f01 import message_item
from test_f02 import BOT, response, group, members
import test_f03 as previous_fixture


def crash_download(approval_path):
    path = Path(approval_path)
    approval = json.loads(path.read_text())
    source = Path(approval['f03_root'])/(sha(b'detail:fixture.txt')+'.response.local')
    answers = iter([BOT, response(group()), response(members()), (200, source.read_bytes())])
    def wire(method, target, body, token, content_type):
        if '/resources/' in target:
            with path.with_suffix('.download-attempt').open('xb') as marker:
                marker.write(b'one synthetic download request')
                marker.flush()
                os.fsync(marker.fileno())
            os._exit(77)
        return next(answers)
    client = KnownMessageExperiment(approval['predecessor_root'], approval, wire=Mock(request=wire),
        sleep=lambda _:None, clock=lambda:datetime.fromisoformat(approval['starts_at'])+timedelta(seconds=1))
    client.token = 'synthetic-token'
    client.establish()
    client.download('fixture.txt')


class F04Tests(unittest.TestCase):
    def setUp(self):
        seed = previous_fixture.F03Tests('test_legacy_state_one_put_then_strict_verification_preserves_failed_history')
        seed.setUp()
        self.addCleanup(seed.doCleanups)
        self.root, self.prior = seed.root, seed.prior
        self.frozen, self.font = seed.frozen, seed.font
        self.now = seed.now + timedelta(minutes=2)
        self.item = message_item('om_synthetic_known', 'file',
            dict(file_key='file_synthetic_message', file_name='fixture.txt'))
        # Generate the exact stopped legacy shape, using the existing wire.
        seed.wire.request.side_effect = [(200, b'{"code":0,"tenant_access_token":"synthetic-token"}')]+seed.answers()+[
            response(group()), response(members()), response({'file_key':'file_synthetic_upload'}),
            response(self.item), response({'items':[self.item]})]
        seed.client.authenticate('cli_synthetic', 'synthetic-secret')
        seed.client.establish()
        seed.client.verify('input-0')
        seed.client.upload('fixture.txt', self.frozen['fixture.txt'])
        seed.client.request('send','POST',PREFIX+'/im/v1/messages?receive_id_type=chat_id',
            dict(receive_id='oc_synthetic',msg_type='file',content=encoded({'file_key':'file_synthetic_upload'})),
            key='send:fixture.txt')
        seed.client.bind('message:fixture.txt', dict(message_id=self.item['message_id'],
            **seed.client.value('upload:fixture.txt')))
        seed.client.request('message','GET',PREFIX+'/im/v1/messages/'+self.item['message_id'],key='detail:fixture.txt')
        seed.client.append({'stopped':'resource identity mismatch'})
        seed.client.append({'stopped':'orchestration','error_type':'ContractError'})
        old = seed.client.root
        source = dict(head=seed.approval['source_commit'], **seed.approval['source_snapshot'])
        (old/'executed-source.local.json').write_text(json.dumps(source))
        (old/'result.local.json').write_text(json.dumps(dict(status='STOPPED',unfinished_requests=0,
            physical_requests={k:sum(e.get('kind')==k for e in seed.client.events) for k in LIMITS},
            ended_at=(seed.now+timedelta(seconds=1)).isoformat(),group_state_reconciliation='updated_and_verified')))
        seed.client.close()
        self.old = old
        self.old_bytes = {name:(old/name).read_bytes() for name in EVIDENCE_FILES}
        self.approval = dict(seed.approval,reference='F04',limits=LIMITS,
            starts_at=self.now.isoformat(),expires_at=(self.now+timedelta(hours=2)).isoformat(),
            f03_root=str(old),f03_evidence_sha256=self.hashes(),known_message_reuse=REUSE,
            group_update=False,history_list=False)
        self.wire = Mock()
        self.client = self.make()
        self.addCleanup(self.client.close)
        self.client.token = 'synthetic-token'

    def hashes(self):
        return {name:sha((self.old/name).read_bytes()) for name in EVIDENCE_FILES}

    def make(self, approval=None):
        return KnownMessageExperiment(self.prior, approval or self.approval, wire=self.wire,
                                      clock=lambda:self.now, sleep=lambda _:None)

    def establish(self):
        self.wire.request.side_effect = [BOT,response(group()),response(members())]
        self.client.establish()

    def test_known_mapping_import_preserves_old_bad_binding_and_cannot_send_or_upload_again(self):
        self.establish()
        self.assertEqual(self.client.value('message:fixture.txt')['resource_key'],'file_synthetic_message')
        self.assertEqual(self.client.value('message:fixture.txt')['upload_resource_key'],'file_synthetic_upload')
        with self.assertRaises(ContractError): self.client.upload('fixture.txt',self.frozen['fixture.txt'])
        with self.assertRaises(ContractError): self.client.send('fixture.txt')
        with self.assertRaises(ContractError):
            self.client.request('send','POST',PREFIX+'/im/v1/messages?receive_id_type=chat_id',
                dict(receive_id='oc_synthetic',content=encoded({'file_key':'file_synthetic_upload'})),key='new-txt-key')
        with self.assertRaises(ContractError):
            self.client.request('update','PUT',PREFIX+'/im/v1/chats/oc_synthetic',{},key='update:1')
        with self.assertRaises(ContractError):
            self.client.request('list','GET',self.client.list_path(),key='list:1')
        self.assertEqual(self.wire.request.call_count,3)
        self.assertEqual({n:(self.old/n).read_bytes() for n in EVIDENCE_FILES},self.old_bytes)

    def full_flow_answers(self):
        answers=[(200,b'{"code":0,"tenant_access_token":"synthetic-token"}'),BOT,response(group()),response(members())]
        notifications=[]
        for index,name in enumerate(('fixture.txt','fixture.md','geometry.png','preview.pdf')):
            image=name.endswith('.png')
            key=('img_' if image else 'file_')+'synthetic_new_'+str(index)
            content={('image_key' if image else 'file_key'):key+'_message'}
            if not image: content['file_name']=name
            item=self.item if index==0 else message_item('om_synthetic_new_'+str(index),'image' if image else 'file',content)
            answers += [response(group()),response(members())]
            if index:
                answers += [response({('image_key' if image else 'file_key'):key}),response(item)]
            answers += [response({'items':[item]}),(200,self.frozen[name])]
            if index<2:
                notice=message_item('om_synthetic_notice_'+str(index),'text',{'text':NOTIFICATIONS[index]})
                answers += [response(notice)]
                notifications.append(notice)
        answers += [response(group()),response(members())]+[response({'items':[n]}) for n in notifications]
        return answers

    def test_full_flow_32_requests_no_history_or_txt_resend_and_actual_conversion(self):
        self.wire.request.side_effect=self.full_flow_answers()
        self.client.authenticate('cli_synthetic','synthetic-secret')
        result=run(self.client,WORKTREE,self.root/'conversion',self.frozen,self.font,reuse_known_txt=True)
        self.assertEqual(self.wire.request.call_count,32)
        self.assertEqual(sum(LIMITS.values()),32)
        self.assertEqual(result['transport_sha256'],{k:sha(v) for k,v in self.frozen.items()})
        self.assertEqual(result['conversions']['fixture.txt']['status'],'ready_candidate')
        self.assertEqual(result['conversions']['fixture.md']['status'],'ready_candidate')
        progress = completed_steps(self.client)
        self.assertEqual(set(progress['downloads']), set(self.frozen))
        self.assertEqual(progress['conversions'], result['conversions'])
        self.assertEqual(result['end_to_end'],'NOT_RUN')
        self.assertEqual(result['unknown_recovery'],'NOT_RUN')
        keys={e['intent'] for e in self.client.events if 'intent' in e}
        self.assertFalse(keys & {'upload:fixture.txt','send:fixture.txt','list:1','update:1'})
        self.assertEqual({n:(self.old/n).read_bytes() for n in EVIDENCE_FILES},self.old_bytes)

    def test_updated_notification_stops_after_31_requests_preserving_verified_milestones(self):
        answers = self.full_flow_answers()
        item = json.loads(answers[-2][1])['data']['items'][0]
        answers[-2] = response({'items': [dict(item, updated=True)]})
        self.wire.request.side_effect = answers
        self.client.authenticate('cli_synthetic', 'synthetic-secret')
        with self.assertRaises(ContractError):
            run(self.client, WORKTREE, self.root/'conversion', self.frozen, self.font, reuse_known_txt=True)
        self.assertEqual(self.wire.request.call_count, 31)
        progress = completed_steps(self.client)
        self.assertEqual(progress['downloads'], {n:dict(sha256=sha(b), bytes=len(b)) for n,b in self.frozen.items()})
        self.assertEqual(progress['conversions']['fixture.txt']['status'], 'ready_candidate')
        self.assertEqual(progress['conversions']['fixture.md']['status'], 'ready_candidate')
        with self.assertRaises(ContractError): self.client.list_sent()
        self.assertEqual(self.wire.request.call_count, 31)
        self.client.close()
        saved = [json.loads(line) for line in self.client.path.read_text().splitlines()]
        for kind, values in progress.items():
            for name, value in values.items():
                self.assertIn({'binding': 'verified-'+kind+':'+name, 'value': value}, saved)
        with self.assertRaises(ContractError): self.make()

    def test_conversion_failure_records_only_verified_download_and_never_notifies(self):
        self.wire.request.side_effect = self.full_flow_answers()[1:]
        with patch('kb_custody_v21.f01_flow.convert_download', side_effect=ContractError('synthetic conversion failure')):
            with self.assertRaisesRegex(ContractError, 'synthetic conversion failure'):
                run(self.client, WORKTREE, self.root/'conversion', self.frozen, self.font, reuse_known_txt=True)
        progress = completed_steps(self.client)
        self.assertEqual(set(progress['downloads']), {'fixture.txt'})
        self.assertEqual(progress['conversions'], {})
        self.assertFalse(any(e.get('kind') == 'send' for e in self.client.events))

    def test_milestone_write_error_prevents_conversion_or_further_network(self):
        self.wire.request.side_effect = self.full_flow_answers()[1:]
        bind = self.client.bind
        def fail_milestone(name, value):
            if name.startswith('verified-downloads:'):
                raise OSError('synthetic evidence disk failure')
            return bind(name, value)
        with patch.object(self.client, 'bind', side_effect=fail_milestone):
            with self.assertRaisesRegex(OSError, 'synthetic evidence disk failure'):
                run(self.client, WORKTREE, self.root/'conversion', self.frozen, self.font, reuse_known_txt=True)
        self.assertEqual(completed_steps(self.client), {'downloads': {}, 'conversions': {}})
        self.assertFalse((self.root/'conversion').exists())
        self.assertEqual(self.wire.request.call_count, 7)

    def test_executor_preserves_partial_progress_without_changing_stopped_status(self):
        import f02_execute
        prior = self.root/'.dev-runtime/kb-v21/real-validation/f01-v1'
        prior.mkdir(parents=True)
        (prior/'f04-approval.local.json').write_text(json.dumps(self.approval))
        self.client.bind('verified-downloads:fixture.txt', {'sha256':sha(self.frozen['fixture.txt']), 'bytes':len(self.frozen['fixture.txt'])})
        with patch.object(f02_execute.subprocess, 'check_output', return_value=str(self.root)), \
                patch.object(f02_execute, 'checked_source', return_value={'head':'synthetic-test'}), \
                patch.object(f02_execute, 'frozen_files', return_value=self.frozen), \
                patch.object(f02_execute.sys.stdin, 'isatty', return_value=True), \
                patch.object(f02_execute.getpass, 'getpass', return_value='synthetic-test-secret'), \
                patch.object(self.client, 'authenticate'), \
                patch.object(f02_execute, 'run', side_effect=ContractError('synthetic later stop')):
            self.assertEqual(f02_execute.execute(experiment_class=lambda *a:self.client,
                reference='f04', reuse_known_txt=True), 1)
        outcome = json.loads((self.client.root/'result.local.json').read_text())
        self.assertEqual(outcome['status'], 'STOPPED')
        self.assertEqual(outcome['completed_steps'], completed_steps(self.client))
        self.assertEqual(set(outcome['completed_steps']['downloads']), {'fixture.txt'})
        self.assertEqual(outcome['completed_steps']['conversions'], {})
        self.assertEqual(outcome['end_to_end'], 'NOT_RUN')
        self.wire.request.assert_not_called()

    def test_changed_or_incomplete_historical_evidence_rejected_before_network(self):
        self.client.close()
        for changed in [dict(approved=False),dict(known_message_reuse={}),dict(unknown_lookup=1),
                dict(group_update=True),dict(history_list=True),dict(f03_root=str(self.root/'other')),
                dict(f03_evidence_sha256={}),dict(account_id='ou_other')]:
            with self.subTest(changed=changed),self.assertRaises(ContractError):
                self.make(dict(self.approval,**changed))
        self.wire.request.assert_not_called()

    def test_historical_receipt_mutation_even_rebound_file_hash_rejected(self):
        self.client.close()
        path=self.old/(sha(b'send:fixture.txt')+'.response.local')
        path.write_bytes(response(dict(self.item,chat_id='oc_other'))[1])
        with self.assertRaisesRegex(ContractError,'receipt differs'):
            self.make(dict(self.approval,f03_evidence_sha256=self.hashes()))
        self.wire.request.assert_not_called()

    def test_unknown_old_send_cannot_be_misrepresented_as_known(self):
        self.client.close()
        path=self.old/'journal.local.jsonl'
        rows=[json.loads(line) for line in path.read_text().splitlines()]
        rows=[e for e in rows if e.get('complete')!='send:fixture.txt']
        path.write_text(''.join(json.dumps(e)+'\n' for e in rows))
        with self.assertRaisesRegex(ContractError,'twelve-request'):
            self.make(dict(self.approval,f03_evidence_sha256=self.hashes()))
        self.wire.request.assert_not_called()

    def test_rehashed_foreign_historical_sender_still_rejected(self):
        self.client.close()
        key='send:fixture.txt'
        path=self.old/(sha(key.encode())+'.response.local')
        raw=response(dict(self.item,sender={'id':'cli_other','id_type':'app_id','sender_type':'app'}))[1]
        path.write_bytes(raw)
        journal=self.old/'journal.local.jsonl'
        rows=[json.loads(line) for line in journal.read_text().splitlines()]
        for row in rows:
            if row.get('complete')==key: row['response_sha256']=sha(raw)
        journal.write_text(''.join(json.dumps(e)+'\n' for e in rows))
        with self.assertRaisesRegex(ContractError,'message identity'):
            self.make(dict(self.approval,f03_evidence_sha256=self.hashes()))
        self.wire.request.assert_not_called()

    def test_executed_source_mismatch_not_accepted_with_new_inventory_digest(self):
        self.client.close()
        path=self.old/'executed-source.local.json'
        value=json.loads(path.read_text()); value['head']='different-synthetic-source'
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ContractError,'executed source'):
            self.make(dict(self.approval,f03_evidence_sha256=self.hashes()))
        self.wire.request.assert_not_called()

    def test_evidence_change_after_setup_stops_before_authentication(self):
        (self.old/'result.local.json').write_bytes(b'{}')
        with self.assertRaisesRegex(ContractError,'evidence changed'):
            self.client.authenticate('cli_synthetic','synthetic-secret')
        self.wire.request.assert_not_called()

    def test_group_isolation_drift_stops_without_repair_or_resource_access(self):
        self.wire.request.side_effect=[BOT,response(group(False))]
        with self.assertRaises(ContractError): self.client.establish()
        self.assertIsNone(self.client.value('message:fixture.txt'))
        self.assertEqual(self.wire.request.call_count,2)

    def test_current_detail_mutation_stops_without_downloading_or_resending(self):
        self.establish()
        self.wire.request.side_effect=[response({'items':[dict(self.item,updated=True)]})]
        with self.assertRaises(ContractError): self.client.download('fixture.txt')
        self.assertEqual(self.wire.request.call_count,4)
        with self.assertRaises(ContractError): self.client.send('fixture.txt')

    def test_wrong_download_bytes_stops_before_conversion_or_notification(self):
        self.wire.request.side_effect=[BOT,response(group()),response(members()),
            response(group()),response(members()),response({'items':[self.item]}),(200,b'wrong')]
        with self.assertRaisesRegex(ContractError,'download differs'):
            run(self.client,WORKTREE,self.root/'conversion',self.frozen,self.font,reuse_known_txt=True)
        self.assertFalse((self.root/'conversion').exists())
        self.assertFalse(any(e.get('kind')=='send' for e in self.client.events))
        self.assertEqual(self.wire.request.call_count,7)
        self.assertEqual(completed_steps(self.client), {'downloads': {}, 'conversions': {}})

    def test_download_timeout_is_not_retryable_in_new_session(self):
        self.establish()
        self.wire.request.side_effect=[response({'items':[self.item]}),TimeoutError('synthetic')]
        with self.assertRaises(TimeoutError): self.client.download('fixture.txt')
        self.client.close()
        with self.assertRaises(ContractError): self.make()
        self.assertEqual(self.wire.request.call_count,5)

    def test_real_child_death_during_download_refuses_restart_or_new_budget(self):
        self.client.close()
        path=self.root/'f04.synthetic.json'; path.write_text(json.dumps(self.approval))
        env={**os.environ,'PYTHONPATH':os.pathsep.join([os.environ.get('PYTHONPATH',''),
            str(Path(__file__).parent),str(Path(__file__).parents[1]),str(Path(__file__).parents[1]/'src'),
            str(Path(__file__).parents[2]/'local_conversion_mcp/src')])}
        result=subprocess.run([sys.executable,'-c','import sys; from test_f04 import crash_download; crash_download(sys.argv[1])',
            str(path)],env=env,capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,77,result.stderr)
        self.assertTrue(path.with_suffix('.download-attempt').exists())
        with self.assertRaises(ContractError): self.make()
        self.wire.request.assert_not_called()
        self.assertEqual(self.client.root,self.root/'f04-requests')

    def test_expired_window_cannot_use_remaining_budget(self):
        self.establish()
        self.client.clock=lambda:self.now+timedelta(hours=2)
        with self.assertRaisesRegex(ContractError,'outside approved'):
            self.client.download('fixture.txt')
        self.assertEqual(self.wire.request.call_count,3)
