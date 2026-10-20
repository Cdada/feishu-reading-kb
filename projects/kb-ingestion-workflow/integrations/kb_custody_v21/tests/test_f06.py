"""F06 uses the existing synthetic CLI/Feishu fixtures and real local outbox."""
import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.f01 import encoded, sha
from kb_custody_v21.f05 import file_sha
from kb_custody_v21.f06 import OutboxExperiment, prepare, binding, LIMITS, TEXT
from kb_custody_v21.notifications import OutboxWorker
from kb_custody_v21.registry import Registry
from kb_custody_v21.s6 import Sender
from test_custody import BASE, WORKTREE
from test_f02 import group, members
import test_f05


class F06Tests(unittest.TestCase):
    def setUp(self):
        seed=test_f05.F05Tests();seed.setUp();self.addCleanup(seed.doCleanups)
        self.root=Path(tempfile.mkdtemp(prefix='f06-',dir=BASE/'test-output')).resolve()
        self.base=self.root/'f01-v1';self.base.mkdir();(self.base/'f05-requests').mkdir()
        self.old=copy.deepcopy(seed.approval)
        self.now=datetime.now(timezone.utc)
        prior={'f05-approval.local.json':self.old,
            'f05-requests/result.local.json':dict(status='KNOWN_NOTIFICATION_CONTENT_VERIFIED',unfinished_requests=0),
            'f05-requests/executed-source.local.json':{'synthetic':True}}
        for n,v in prior.items():(self.base/n).write_text(encoded(v))
        (self.base/'f05-requests/http.local.jsonl').write_text('')
        self.before={str(p.relative_to(self.base)):p.read_bytes() for p in self.base.rglob('*') if p.is_file()}
        self.registry,frozen=prepare(WORKTREE,self.old['chat_id'],root=self.root/'runtime')
        self.approval=dict(self.old,reference='F06',base=str(self.base),limits=LIMITS,
            starts_at=(self.now-timedelta(seconds=1)).isoformat(),expires_at=(self.now+timedelta(hours=1)).isoformat(),
            outbox_binding=frozen,runtime_relative=str(self.registry.runtime.root.relative_to(WORKTREE)),
            prior_evidence_sha256={n:sha(b) for n,b in self.before.items()},logical_notifications=1,same_key_repeat=0)
        self.receipt=copy.deepcopy(seed.items[0]);self.receipt['body']['content']=encoded({'text':TEXT})
        self.runner=Mock(side_effect=self.cli_response);self.hook=lambda c,v:None

    def make(self,approval=None):
        c=OutboxExperiment(self.base,approval or self.approval,self.registry,runner=self.runner,clock=lambda:self.now)
        self.addCleanup(c.close);return c

    def cli_response(self,c):
        if c['mode']=='profile':return {'ok':True,'mode':'profile'}
        key,kind=c['key'],c['kind'];root=Path(c['root'])
        values={'bot':dict(code=0,bot={'open_id':self.old['bot_id']}),'chat':dict(code=0,data=group()),
            'members':dict(code=0,data=members()),'send':dict(code=0,data=self.receipt),
            'notification-0':dict(code=0,data={'items':[dict(self.receipt,updated=True)]})}
        value=copy.deepcopy(values[key])
        def event(v):
            with (root/'http.local.jsonl').open('a') as f:f.write(encoded(v)+'\n')
        event(dict(type='intent',key=key+':auth',kind='auth'))
        event(dict(type='complete',key=key+':auth',kind='auth',http_status=200))
        event(dict(type='intent',key=key+':'+kind,kind=kind))
        if key=='send':
            self.assertEqual(c['mode'],'send')
            self.assertEqual(json.loads(c['body'])['uuid'],self.approval['outbox_binding']['item']['provider_request_key'])
        self.hook(c,value)
        raw=encoded(value).encode();(root/(key+'.response.local')).write_bytes(raw)
        event(dict(type='complete',key=key+':'+kind,kind=kind,http_status=200,response_sha256=sha(raw)))
        return {'ok':True,'mode':c['mode']}

    def test_normal_send_uses_transactional_outbox_and_known_detail_without_duplicate_on_reopen(self):
        c=self.make();r=c.run()
        self.assertEqual(r['status'],'MATCH_WITH_UPDATE_FLAG')
        self.assertEqual(r['unknown_recovery'],'NOT_TESTED')
        item=self.registry.rows('SELECT * FROM notification_outbox')[0]
        self.assertEqual(item['delivery_state'],'delivered')
        self.assertEqual(item['provider_message_id'],self.receipt['message_id'])
        self.assertEqual(len(self.registry.rows('SELECT * FROM notification_attempts')),1)
        self.assertTrue(c.value('reopened_worker_skipped_delivered'))
        self.assertEqual(self.runner.call_count,6)
        self.assertEqual({k:sum(e['type']=='intent' and e['kind']==k for e in c.http_events()) for k in LIMITS},LIMITS)
        for n,b in self.before.items():self.assertEqual((self.base/n).read_bytes(),b)
        c.close()
        with self.assertRaises(ContractError):self.make()

    def test_missing_or_expanded_approval_and_wrong_objects_have_zero_calls(self):
        for change in [dict(approved=False),dict(logical_notifications=2),dict(same_key_repeat=1),
                dict(unknown_lookup=1),dict(automatic_retries=1),dict(redirects=1),dict(history_list=True),
                dict(limits=dict(LIMITS,send=2)),dict(chat_id='oc_other'),dict(cli_profile='other')]:
            with self.subTest(change=change),self.assertRaises(ContractError):self.make(dict(self.approval,**change))
        self.runner.assert_not_called()

    def test_preparation_does_not_replace_an_existing_stable_key(self):
        old=binding(self.registry)
        with self.assertRaises(ContractError):prepare(WORKTREE,self.old['chat_id'],root=self.registry.runtime.root)
        self.assertEqual(binding(self.registry),old)
        self.assertEqual(self.registry.rows('SELECT * FROM notification_attempts'),[])

    def test_prior_evidence_or_frozen_notice_tamper_stops_before_network(self):
        p=self.base/'f05-requests/result.local.json';p.write_text('{}')
        with self.assertRaises(ContractError):self.make()
        p.write_bytes(self.before['f05-requests/result.local.json'])
        self.approval['outbox_binding']['item']['provider_request_key']='custody-'+'f'*40
        with self.assertRaises(ContractError):self.make()
        self.runner.assert_not_called()

    def test_send_cannot_bypass_worker_claim_or_isolation(self):
        c=self.make()
        with self.assertRaises(ContractError):Sender(c).send(self.approval['outbox_binding']['item'],'invented')
        self.runner.assert_not_called()
        c.verify_isolation()
        with self.assertRaises(ContractError):c.call('send','send',c.paths()['send'][1])
        self.assertEqual(self.runner.call_count,4)

    def test_membership_change_prevents_claim_and_send(self):
        def hook(c,v):
            if c['key']=='members':v['data']['has_more']=True
        self.hook=hook;c=self.make()
        with self.assertRaises(ContractError):c.run()
        self.assertEqual(self.registry.rows('SELECT * FROM notification_attempts'),[])
        self.assertEqual(self.registry.rows('SELECT * FROM notification_outbox')[0]['delivery_state'],'pending')

    def test_http_success_with_wrong_send_body_does_not_mark_outbox_delivered(self):
        def hook(c,v):
            if c['key']=='send':v['data']['body']['content']=encoded({'text':'wrong'})
        self.hook=hook;c=self.make()
        with self.assertRaises(ContractError):c.run()
        self.assertEqual(self.registry.rows('SELECT * FROM notification_outbox')[0]['delivery_state'],'sending')
        self.assertIsNone(c.value('send_receipt'))
        self.assertEqual(self.runner.call_count,5)

    def test_detail_mismatch_preserves_confirmed_send_but_stops_verification(self):
        def hook(c,v):
            if c['key']=='notification-0':v['data']['items'][0]['body']['content']=encoded({'text':'changed'})
        self.hook=hook;c=self.make()
        with self.assertRaises(ContractError):c.run()
        self.assertEqual(self.registry.rows('SELECT * FROM notification_outbox')[0]['delivery_state'],'delivered')
        self.assertIsNotNone(c.value('outbox_delivered'));self.assertIsNone(c.observations()['0'])
        with self.assertRaises(ContractError):c.run()
        self.assertEqual(self.runner.call_count,6)

    @unittest.skipUnless(hasattr(os,'fork'),'requires POSIX child process')
    def test_process_death_after_remote_effect_has_no_second_send_or_false_recovery(self):
        effect=self.root/'synthetic-remote-effect'
        pid=os.fork()
        if pid==0:
            def hook(c,v):
                if c['key']=='send':
                    with effect.open('xb') as f:f.write(c['body'].encode());f.flush();os.fsync(f.fileno())
                    os._exit(71)
            self.hook=hook
            try:self.make().run()
            except Exception:os._exit(72)
            os._exit(73)
        _,status=os.waitpid(pid,0);self.assertEqual(os.waitstatus_to_exitcode(status),71)
        saved=effect.read_bytes()
        self.assertEqual(self.registry.rows('SELECT * FROM notification_outbox')[0]['delivery_state'],'sending')
        with self.assertRaises(ContractError):self.make()
        with self.registry.transaction() as db:db.execute('UPDATE notification_outbox SET lease_until=0')
        # Existing S6 lookup deliberately returns unknown without network.
        worker=OutboxWorker(Registry(self.registry.runtime),Sender(object()))
        self.assertEqual(worker.drain(limit=1),1)
        self.assertEqual(worker.drain(limit=1),1)
        self.assertEqual(self.registry.rows('SELECT * FROM notification_outbox')[0]['delivery_state'],'delivery_unknown')
        attempts=self.registry.rows('SELECT * FROM notification_attempts')
        self.assertEqual(sum(a['operation']=='send' for a in attempts),1)
        self.assertEqual(effect.read_bytes(),saved)

    @unittest.skipUnless(hasattr(os,'fork'),'requires POSIX child process')
    def test_new_process_skips_delivered_notice_without_sender_or_http(self):
        self.make().run()
        pid=os.fork()
        if pid==0:
            class NoCalls:
                def send(self,*args):os._exit(81)
                def lookup(self,*args):os._exit(82)
            try:
                count=OutboxWorker(Registry(self.registry.runtime),NoCalls()).drain()
                os._exit(0 if count==0 else 83)
            except Exception:os._exit(84)
        _,status=os.waitpid(pid,0);self.assertEqual(os.waitstatus_to_exitcode(status),0)
        self.assertEqual(self.runner.call_count,6)
