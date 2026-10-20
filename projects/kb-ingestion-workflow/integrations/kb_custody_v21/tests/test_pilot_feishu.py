"""Reuse the F05 CLI receipts and F02 group fixtures; no sockets or secrets."""
from datetime import datetime, timedelta, timezone
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.f01 import encoded,sha
from kb_custody_v21.f02 import administration
from kb_custody_v21.f05 import file_sha
from kb_custody_v21.pilot_feishu import PilotReadSession,LIMITS
from test_custody import BASE
from test_f02 import group,members


class PilotFeishuTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='pilot-feishu-',dir=BASE/'test-output')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()/'n10-requests';self.root.mkdir()
        self.now=datetime.now(timezone.utc).replace(microsecond=0)
        self.approval=dict(reference='N10',approved=True,root=str(self.root),app_id='cli_synthetic',
            bot_id='ou_synthetic_bot',account_id='ou_synthetic',chat_id='oc_synthetic',cli_profile='synthetic',
            limits=LIMITS,starts_at=(self.now-timedelta(minutes=1)).isoformat(),
            expires_at=(self.now+timedelta(hours=9)).isoformat(),automatic_retries=0,redirects=0,
            publish_scope='only_bound_account',administration=administration(group()))
        self.session=self.root/'approval.local.json';self.session.write_text(encoded(self.approval))
        self.digest=file_sha(self.session)
        self.source=self.root/'source.synthetic';self.source.write_text('synthetic source')
        self.frozen={str(self.source):file_sha(self.source)}
        self.checked_source=Mock(side_effect=lambda:dict(self.frozen))
        self.runner=Mock(side_effect=self.respond)
        self.hook=lambda c,v:None
        self.message=dict(message_id='om_synthetic',chat_id='oc_synthetic',msg_type='file',deleted=False,
            sender=dict(id='ou_synthetic',sender_type='user',id_type='open_id'),
            body=dict(content=encoded(dict(file_key='file_synthetic',file_name='synthetic.md'))))
        self.pages=[dict(items=[self.message],has_more=False)]

    def make(self):
        c=PilotReadSession(self.root,self.digest,runner=self.runner,checked_source=self.checked_source,clock=lambda:self.now)
        self.addCleanup(c.close)
        return c

    def respond(self,c):
        if c['mode']=='profile':return dict(ok=True,mode='profile')
        path=c['path'];kind=c['kind'];key=c['key']
        if kind=='list':value=dict(code=0,data=copy.deepcopy(self.pages.pop(0)))
        elif kind=='message':value=dict(code=0,data=dict(items=[copy.deepcopy(self.message)]))
        elif '/members?' in path:value=dict(code=0,data=members())
        elif '/chats/' in path:value=dict(code=0,data=group())
        else:value=dict(code=0,bot=dict(open_id='ou_synthetic_bot'))
        self.hook(c,value)
        raw=encoded(value).encode();(self.root/(key+'.response.local')).write_bytes(raw)
        with (self.root/'http.local.jsonl').open('a') as f:
            for category in ('auth',kind):
                f.write(encoded(dict(type='intent',key=key+':'+category,kind=category))+'\n')
                f.write(encoded(dict(type='complete',key=key+':'+category,kind=category,http_status=200,
                    **({'response_sha256':sha(raw)} if category!='auth' else {})))+'\n')
        return dict(ok=True,mode='read')

    def scan(self,c,**opts):
        return c.scan('scan_synthetic',start=self.now-timedelta(hours=1),end=self.now,**opts)

    def test_fixed_identity_scan_and_exact_message_replay_without_new_requests(self):
        c=self.make();c.verify_isolation();result=self.scan(c)
        self.assertEqual(result['status'],'COMPLETE_INTERVAL')
        self.assertFalse(result['permanent_backfill_verified'])
        self.assertEqual(result['message_ids'],['om_synthetic'])
        self.assertEqual(c.message('scan_synthetic','om_synthetic'),self.message)
        self.assertEqual(len(c.http_events()),20) # five physical reads + five auth
        self.assertEqual(self.runner.call_count,6)
        c.close();c=self.make();c.verify_isolation()
        self.assertEqual(self.scan(c),result)
        self.assertEqual(c.message('scan_synthetic','om_synthetic'),self.message)
        self.assertEqual(self.runner.call_count,6)

    def test_identity_failure_stops_before_any_history(self):
        def hook(c,v):
            if '/members?' in c['path']:v['data']['has_more']=True
        self.hook=hook;c=self.make()
        with self.assertRaises(ContractError):c.verify_isolation()
        with self.assertRaises(ContractError):self.scan(c)
        self.assertEqual(self.runner.call_count,4)

    def test_pagination_limit_reports_incomplete_without_unbounded_next_page(self):
        self.pages=[dict(items=[self.message],has_more=True,page_token='synthetic-next')]
        c=self.make();c.verify_isolation();r=self.scan(c)
        self.assertEqual(r['status'],'INCOMPLETE_PAGE_LIMIT')
        self.assertEqual(r['next_page_token'],'synthetic-next')
        self.assertEqual(self.runner.call_count,5)

    def test_pagination_continues_with_fixed_interval_and_rejects_repeated_cursor(self):
        self.pages=[dict(items=[],has_more=True,page_token='same'),dict(items=[],has_more=True,page_token='same')]
        c=self.make();c.verify_isolation()
        with self.assertRaises(ContractError):self.scan(c,max_pages=3)
        paths=[x.args[0]['path'] for x in self.runner.call_args_list if x.args[0]['kind']=='list']
        self.assertEqual(len(paths),2);self.assertIn('page_token=same',paths[-1])
        c.close()
        with self.assertRaises(ContractError):self.make()

    def test_other_sender_is_not_eligible_for_detail_and_foreign_group_stops(self):
        self.pages[0]['items']=[dict(self.message,sender=dict(id='ou_other',id_type='open_id',sender_type='user'))]
        c=self.make();c.verify_isolation();r=self.scan(c)
        self.assertEqual(r['message_ids'],[])
        with self.assertRaises(ContractError):c.message('scan_synthetic','om_synthetic')
        self.pages=[dict(items=[dict(self.message,chat_id='oc_other')],has_more=False)]
        with self.assertRaises(ContractError):c.scan('scan_foreign',start=self.now-timedelta(hours=1),end=self.now)

    def test_final_source_change_preserves_receipt_but_stops_before_success(self):
        def hook(c,v):
            if c['kind']=='list':self.source.write_text('changed')
        self.hook=hook;c=self.make();c.verify_isolation()
        with self.assertRaises(ContractError):self.scan(c)
        self.assertIsNone(c.value('scan_synthetic:result'))
        self.assertTrue(list(self.root.glob('*-list.response.local')))
        self.assertEqual(self.runner.call_count,5)

    def test_deadline_crossing_in_checked_source_makes_no_cli_call(self):
        c=self.make()
        def cross():
            self.now=datetime.fromisoformat(self.approval['expires_at'])
            return dict(self.frozen)
        self.checked_source.side_effect=cross
        with self.assertRaises(ContractError):c.verify_isolation()
        self.runner.assert_not_called()

    def test_completed_page_before_process_exit_is_reused_after_restart(self):
        c=self.make();c.verify_isolation();original=c._call
        def crash(*args,**kwargs):
            result=original(*args,**kwargs)
            if args[0]=='list':raise SystemExit(77)
            return result
        c._call=crash
        with self.assertRaises(SystemExit):self.scan(c)
        self.assertEqual(self.runner.call_count,5)
        c.close();c=self.make();result=self.scan(c)
        self.assertEqual(result['status'],'COMPLETE_INTERVAL')
        self.assertEqual(self.runner.call_count,5)

    def test_unknown_physical_attempt_cannot_restart_under_another_scan(self):
        c=self.make();c.verify_isolation()
        with (self.root/'http.local.jsonl').open('a') as f:
            f.write(encoded(dict(type='intent',key='p005-list:list',kind='list'))+'\n')
        with self.assertRaises(ContractError):self.scan(c)
        c.close()
        with self.assertRaises(ContractError):self.make()
        self.assertEqual(self.runner.call_count,4)

    def test_tampered_list_receipt_prevents_cached_success_and_detail(self):
        c=self.make();c.verify_isolation();r=self.scan(c)
        Path(next(iter(r['receipts']))).write_text('{}')
        with self.assertRaises(ContractError):self.scan(c)
        # Even a wrapper claiming success cannot manufacture physical evidence.
        self.runner.side_effect=None;self.runner.return_value=dict(ok=True,mode='read')
        with self.assertRaises((ContractError,OSError)):c.message('scan_synthetic','om_synthetic')

    def test_long_integer_json_normalizes_contract_error(self):
        from kb_custody_v21.pilot_feishu import read_json
        p=self.root/'synthetic.json';p.write_text('{"n":'+'1'*10000+'}')
        with self.assertRaises(ContractError):read_json(p)
