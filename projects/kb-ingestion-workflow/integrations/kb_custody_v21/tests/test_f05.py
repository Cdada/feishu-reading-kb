"""F05 reuses synthetic notification receipts; no credentials or sockets."""
import copy
from datetime import datetime, timedelta, timezone
import json
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.f01 import encoded, sha
from kb_custody_v21.f02 import SCOPES, administration
from kb_custody_v21.f05 import NotificationReadExperiment, CliProcess, EVIDENCE_FILES, LIMITS, file_sha
from test_custody import BASE
from test_f02 import group, members
import test_notification_review


class F05Tests(unittest.TestCase):
    def setUp(self):
        seed = test_notification_review.NotificationReviewTests()
        seed.setUp()
        self.addCleanup(seed.doCleanups)
        self.root = Path(tempfile.mkdtemp(prefix='f05-',dir=BASE/'test-output')).resolve()
        self.prior = self.root/'f04-requests'
        self.prior.mkdir()
        self.items = copy.deepcopy(seed.items)
        self.now = datetime.now(timezone.utc)
        old = seed.events[0]['approval']
        old.update(account_id='ou_synthetic', administration=administration(group()), scopes=SCOPES,
            published_version='0.1.1',publish_scope='only_bound_account', limits={'send':2},
            source_commit='synthetic-original-head',source_snapshot={'sha256':'synthetic','files':{}})
        seed.save()
        for p in seed.root.iterdir():
            (self.prior/p.name).write_bytes(p.read_bytes())
        (self.prior/'executed-source.local.json').write_text(encoded(dict(head=old['source_commit'],**old['source_snapshot'])))
        (self.prior/'result.local.json').write_text(encoded(dict(status='STOPPED',unfinished_requests=0,
            physical_requests={'send':2},ended_at=(self.now-timedelta(days=1)).isoformat())))
        self.approval = dict(old, reference='F05',limits=LIMITS, f04_root=str(self.prior),
            starts_at=(self.now-timedelta(seconds=1)).isoformat(),expires_at=(self.now+timedelta(hours=1)).isoformat(),
            cli_profile='synthetic', automatic_retries=0,redirects=0,unknown_lookup=0,history_list=False,writes=0,
            f04_evidence_sha256={n:file_sha(self.prior/n) for n in EVIDENCE_FILES})
        self.runner=Mock(side_effect=self.cli_response)
        self.hook=lambda control,value:None
        self.before={p.name:p.read_bytes() for p in self.prior.iterdir()}

    def make(self,approval=None):
        client=NotificationReadExperiment(self.prior,approval or self.approval,runner=self.runner,clock=lambda:self.now)
        self.addCleanup(client.close)
        return client

    def cli_response(self,control):
        if control['mode']=='profile':
            self.hook(control,None)
            return {'ok':True,'mode':'profile'}
        key,kind=control['key'],control['kind']
        values={'bot':dict(code=0,bot={'open_id':'ou_synthetic_bot'}),
            'chat':dict(code=0,data=group()),'members':dict(code=0,data=members()),
            **{'notification-'+str(i):dict(code=0,data={'items':[dict(item,updated=i==0)]})
               for i,item in enumerate(self.items)}}
        value=copy.deepcopy(values[key])
        self.hook(control,value)
        raw=encoded(value).encode()
        root=Path(control['root'])
        (root/(key+'.response.local')).write_bytes(raw)
        with (root/'http.local.jsonl').open('a') as stream:
            for category in ('auth',kind):
                stream.write(encoded(dict(type='intent',key=key+':'+category,kind=category))+'\n')
                stream.write(encoded(dict(type='complete',key=key+':'+category,kind=category,http_status=200,
                    **({'response_sha256':sha(raw)} if category!='auth' else {})))+'\n')
        return {'ok':True,'mode':'read'}

    def test_exact_two_known_reads_full_chain_with_auth_budget_and_old_evidence_unchanged(self):
        client=self.make(); result=client.run()
        self.assertEqual(result['0']['status'],'MATCH_WITH_UPDATE_FLAG')
        self.assertEqual(result['1']['status'],'MATCH')
        self.assertEqual(result['0']['unknown_recovery'],'NOT_TESTED')
        self.assertEqual(self.runner.call_count,6)
        calls=[x.args[0] for x in self.runner.call_args_list]
        self.assertEqual([c['key'] for c in calls],['','bot','chat','members','notification-0','notification-1'])
        self.assertEqual([c['path'].split('/')[-1] for c in calls[-2:]], [x['message_id'] for x in self.items])
        self.assertEqual({k:sum(e['type']=='intent' and e['kind']==k for e in client.http_events()) for k in LIMITS}, LIMITS)
        self.assertEqual({p.name:p.read_bytes() for p in self.prior.iterdir()},self.before)

    def test_no_direct_send_auth_search_or_out_of_order_query(self):
        client=self.make()
        with self.assertRaises(ContractError):client.authenticate('cli_synthetic','synthetic-secret')
        for key in client.paths():
            kind,path=client.paths()[key]
            with self.subTest(key=key),self.assertRaises(ContractError):client.call(kind,key,path)
        with self.assertRaises(ContractError):client.call('message','notification-0','/open-apis/im/v1/messages/om_other')
        self.runner.assert_not_called()

    def test_receipt_tamper_is_rejected_before_any_cli(self):
        p=self.prior/'journal.local.jsonl';p.write_text(p.read_text()+'{}\n')
        with self.assertRaises(ContractError):self.make()
        self.runner.assert_not_called()

    def test_changed_identity_source_or_old_stop_cannot_be_approved_by_new_hash_alone(self):
        for filename,field,value in [('executed-source.local.json','head','other'),
                                     ('result.local.json','status','PASS'),
                                     ('result.local.json','unfinished_requests',1)]:
            p=self.prior/filename; original=p.read_bytes(); obj=json.loads(original);obj[field]=value;p.write_text(encoded(obj))
            approval=copy.deepcopy(self.approval);approval['f04_evidence_sha256'][filename]=file_sha(p)
            with self.subTest(field=field),self.assertRaises(ContractError):self.make(approval)
            p.write_bytes(original)
        with self.assertRaises(ContractError):self.make(dict(self.approval,chat_id='oc_other'))
        self.runner.assert_not_called()

    def test_unapproved_expired_or_expanded_scope_denied(self):
        for change in [dict(approved=False),dict(expires_at=(self.now-timedelta(microseconds=1)).isoformat()),
                dict(automatic_retries=1),dict(redirects=1),dict(writes=1),dict(unknown_lookup=1),
                dict(history_list=True),dict(limits=dict(LIMITS,auth=6)),dict(scopes=SCOPES+['unapproved'])]:
            with self.subTest(change=change),self.assertRaises(ContractError):self.make(dict(self.approval,**change))
        self.runner.assert_not_called()

    def test_profile_failure_has_zero_physical_requests_and_restart_denied(self):
        self.runner.side_effect=None;self.runner.return_value={'ok':False}
        client=self.make()
        with self.assertRaises(ContractError):client.run()
        self.assertEqual(client.http_events(),[])
        client.close()
        with self.assertRaises(ContractError):self.make()
        self.assertEqual(self.runner.call_count,1)

    def test_foreign_bot_stops_before_group_query(self):
        def hook(c,v):
            if c['key']=='bot':v['bot']['open_id']='ou_other'
        self.hook=hook;client=self.make()
        with self.assertRaises(ContractError):client.run()
        self.assertEqual(self.runner.call_count,2)

    def test_membership_change_stops_before_either_notification(self):
        def hook(c,v):
            if c['key']=='members':v['data']['has_more']=True
        self.hook=hook;client=self.make()
        with self.assertRaises(ContractError):client.run()
        self.assertEqual(self.runner.call_count,4)
        self.assertEqual(client.observations(),{'0':None,'1':None})

    def test_second_detail_failure_preserves_first_observation_and_prevents_retry(self):
        def hook(c,v):
            if c['key']=='notification-1':v['data']['items'][0]['body']['content']=encoded({'text':'changed'})
        self.hook=hook;client=self.make()
        with self.assertRaises(ContractError):client.run()
        self.assertEqual(client.observations()['0']['status'],'MATCH_WITH_UPDATE_FLAG')
        self.assertIsNone(client.observations()['1'])
        with self.assertRaises(ContractError):client.run()
        client.close()
        with self.assertRaises(ContractError):self.make()
        self.assertEqual(self.runner.call_count,6)

    def test_missing_http_proof_or_unknown_attempt_cannot_be_cli_success(self):
        self.runner.side_effect=None;self.runner.return_value={'ok':True}
        client=self.make()
        with self.assertRaises((ContractError,OSError)):client.run()
        self.assertFalse(client.value('bot_verified'))
        self.assertEqual(self.runner.call_count,2)

    def test_evidence_change_after_profile_blocks_first_http(self):
        def hook(c,v):
            if c['mode']=='profile':(self.prior/'result.local.json').write_text('{}')
        self.hook=hook;client=self.make()
        with self.assertRaises(ContractError):client.run()
        self.assertEqual(self.runner.call_count,1)

    def test_cli_binary_hash_output_and_timeout_no_secret_fallback(self):
        p=self.root/'guard.synthetic';p.write_bytes(b'synthetic binary')
        process=CliProcess(p,file_sha(p))
        control=dict(mode='profile',expires_at=(self.now+timedelta(minutes=1)).isoformat())
        with patch('kb_custody_v21.f05.subprocess.run',return_value=Mock(returncode=0,stdout='{"ok":true,"mode":"profile"}')) as run:
            self.assertTrue(process(control)['ok']);self.assertEqual(run.call_count,1)
            self.assertNotIn('LARKSUITE_CLI_APP_SECRET',run.call_args.kwargs['env'])
            p.write_bytes(b'changed')
            with self.assertRaises(ContractError):process(control)
            self.assertEqual(run.call_count,1)

    def test_slow_download_can_finish_without_retry_but_ordinary_reads_stay_short(self):
        p=self.root/'guard.synthetic';p.write_bytes(b'synthetic binary')
        process=CliProcess(p,file_sha(p))
        control=dict(mode='read',kind='download',expires_at=(self.now+timedelta(hours=1)).isoformat())
        def slow_call(*args,**kwargs):
            if kwargs['timeout']<90:
                raise subprocess.TimeoutExpired(args[0],kwargs['timeout'])
            return Mock(returncode=0,stdout='{"ok":true,"mode":"read"}')
        with patch('kb_custody_v21.f05.datetime') as clock, patch('kb_custody_v21.f05.subprocess.run',side_effect=slow_call) as run:
            clock.now.return_value=self.now
            self.assertTrue(process(control)['ok'])
            self.assertEqual(run.call_count,1)
            self.assertEqual(run.call_args.kwargs['timeout'],1200)
            for kind in ('message','list','identity','upload','send'):
                with self.subTest(kind=kind),self.assertRaises(subprocess.TimeoutExpired):
                    process(dict(control,kind=kind))
                self.assertEqual(run.call_args.kwargs['timeout'],45)

    def test_download_obeys_shorter_authorization_and_expiry_prevents_dispatch(self):
        p=self.root/'guard.synthetic';p.write_bytes(b'synthetic binary')
        process=CliProcess(p,file_sha(p))
        control=dict(mode='read',kind='download',expires_at=(self.now+timedelta(seconds=20)).isoformat())
        with patch('kb_custody_v21.f05.datetime') as clock, patch('kb_custody_v21.f05.subprocess.run',return_value=Mock(returncode=0,stdout='{"ok":true,"mode":"read"}')) as run:
            clock.now.return_value=self.now
            self.assertTrue(process(control)['ok'])
            self.assertEqual(run.call_args.kwargs['timeout'],20)
            clock.now.return_value=self.now+timedelta(seconds=20)
            with self.assertRaises(ContractError):process(control)
            self.assertEqual(run.call_count,1)
