"""Real HTTP stack with patched connections; no socket, keychain or OAuth."""
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import BytesIO
import http.client
import json
from pathlib import Path
import subprocess
import sys
import ssl
import unittest
from unittest.mock import Mock, patch

from kb_custody_v21.baidu import CHUNK
from kb_custody_v21.baidu_http import BaiduHttpsWire
from kb_custody_v21.baidu_protocol import HttpRequest, JSON_LIMIT, PAN, PCS, BaiduProtocolTransport
from kb_custody_v21.contracts import ContractError
from kb_custody_v21.registry import encoded
import test_baidu_protocol
from test_custody import WORKTREE


def http_response(body=b'{}', status=200, headers=None):
    response = Mock(status=status)
    response.getheaders.return_value = headers if headers is not None else [('Content-Length', str(len(body)))]
    response.read.side_effect = BytesIO(body).read
    return response


class HttpsBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.check = Mock(return_value=True)
        self.wire = BaiduHttpsWire(self.check)
        self.req = HttpRequest('quota','GET',PAN,'/api/quota?access_token=synthetic-token',
                               (('User-Agent',PAN),),b'',JSON_LIMIT)
        self.connection = Mock()
        self.factory = patch('kb_custody_v21.baidu_http.http.client.HTTPSConnection',return_value=self.connection).start()
        self.addCleanup(patch.stopall)
        self.connection.getresponse.return_value = http_response()

    def test_https_verified_context_and_one_request_only(self):
        result = self.wire.request(self.req)
        self.assertEqual(result.body,b'{}')
        self.assertEqual([call.args for call in self.check.call_args_list], [(self.req,), (self.req,)])
        self.factory.assert_called_once()
        context=self.factory.call_args.kwargs['context']
        self.assertEqual(context.verify_mode,ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertEqual(self.factory.call_args.kwargs['timeout'],20)
        self.connection.request.assert_called_once_with('GET',self.req.target,body=b'',headers={'User-Agent':PAN})
        self.connection.close.assert_called_once()

    def test_authorization_failure_before_connection(self):
        self.check.side_effect=ContractError('not approved')
        with self.assertRaisesRegex(ContractError,'not approved'): self.wire.request(self.req)
        self.factory.assert_not_called()
        self.check.side_effect=None
        for result in (False,None,1):
            self.check.return_value=result
            with self.assertRaisesRegex(ContractError,'explicitly confirm'): self.wire.request(self.req)
        self.factory.assert_not_called()

    def test_route_header_and_limit_errors_before_connection(self):
        cases=[replace(self.req,host=PAN+'.example.invalid'),replace(self.req,method='POST'),
               replace(self.req,target='https://'+PAN+'/api/quota'),replace(self.req,target='/api/quota\r\nX: y'),
               replace(self.req,target='/api/quota#fragment'),replace(self.req,target='/api/other'),
               replace(self.req,response_limit=CHUNK),replace(self.req,body=b'x'),
               replace(self.req,headers=(('User-Agent',PAN),('Authorization','synthetic-secret'))),
               replace(self.req,headers=(('User-Agent',PAN),('User-Agent',PAN))),
               replace(self.req,label='delete')]
        for req in cases:
            with self.subTest(req=req):
                with self.assertRaises(ContractError): self.wire.request(req)
        self.factory.assert_not_called()

    def test_post_requires_exact_length_and_rejects_wrong_api_method(self):
        req=HttpRequest('create','POST',PAN,'/rest/2.0/xpan/file?method=create',
                        (('User-Agent',PAN),('Content-Type','application/x-www-form-urlencoded'),
                         ('Content-Length','3')),b'a=b',JSON_LIMIT)
        for bad in (replace(req,headers=req.headers[:-1]),replace(req,body=b'a=bc'),
                    replace(req,target='/rest/2.0/xpan/file?method=create&method=delete')):
            with self.assertRaises(ContractError): self.wire.request(bad)
        self.factory.assert_not_called()
        self.wire.request(req)
        self.connection.request.assert_called_once()

    def test_redirect_and_error_bodies_never_read_or_followed(self):
        for status in (302,429,500):
            with self.subTest(status=status):
                response=http_response(b'synthetic secret error body',status=status)
                self.connection.getresponse.return_value=response
                self.assertEqual(self.wire.request(self.req).status,status)
                response.read.assert_not_called()
        self.assertEqual(self.factory.call_count,3)
        self.assertEqual(self.connection.request.call_count,3)

    def test_stream_without_length_stops_at_limit_plus_one(self):
        req=replace(self.req,label='download',host=PCS,target='/file/synthetic',response_limit=7)
        response=http_response(b'x'*100,headers=[])
        self.connection.getresponse.return_value=response
        with self.assertRaisesRegex(OSError,'no automatic retry'): self.wire.request(req)
        self.assertEqual(response.read.call_args_list[0].args,(8,))
        self.assertEqual(response.read.call_count,1)
        self.connection.close.assert_called_once()

    def test_length_overflow_and_ambiguous_encoding_stop_before_body(self):
        for headers in ([('Content-Length',str(JSON_LIMIT+1))],
                        [('Content-Length','2'),('Content-Length','2')],
                        [('Content-Length','-1')],[('Content-Encoding','gzip')],
                        [('Transfer-Encoding','chunked'),('Content-Length','2')],
                        [('Transfer-Encoding','gzip')]):
            with self.subTest(headers=headers):
                response=http_response(headers=headers)
                self.connection.getresponse.return_value=response
                with self.assertRaises(OSError): self.wire.request(self.req)
                response.read.assert_not_called()

    def test_partial_reads_and_truncation(self):
        response=http_response(headers=[('Content-Length','4')])
        response.read.side_effect=[b'a',b'b',b'cd',b'']
        self.connection.getresponse.return_value=response
        self.assertEqual(self.wire.request(self.req).body,b'abcd')
        self.connection.getresponse.return_value=http_response(b'ab',headers=[('Content-Length','4')])
        with self.assertRaises(OSError): self.wire.request(self.req)

    def test_transport_errors_are_uncertain_redacted_and_never_retried(self):
        for error in (TimeoutError('synthetic-secret-in-url'),http.client.BadStatusLine('synthetic-secret-in-url'),
                      http.client.IncompleteRead(b'synthetic-secret-in-url')):
            with self.subTest(kind=type(error).__name__):
                self.connection.request.reset_mock()
                self.connection.getresponse.side_effect=error
                with self.assertRaises(OSError) as raised: self.wire.request(self.req)
                self.assertNotIn('synthetic-secret',str(raised.exception))
                self.connection.request.assert_called_once()

    def test_default_protocol_still_refuses_live_wire(self):
        with self.assertRaisesRegex(ContractError,'live Baidu'):
            BaiduProtocolTransport(self.wire,'1234567','/apps/synthetic/test',
                                   '/apps/synthetic/test/report.md',token='synthetic-token')
        self.factory.assert_not_called()


class BudgetBoundaryTests(unittest.TestCase):
    setUp=test_baidu_protocol.BaiduProtocolTests.setUp
    receive=test_baidu_protocol.BaiduProtocolTests.receive
    setup_protocol=test_baidu_protocol.BaiduProtocolTests.setup_protocol
    attempts=test_baidu_protocol.BaiduProtocolTests.attempts

    def bounded(self, limits=None, *, ref='synthetic-bounded'):
        self.start=(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat()
        self.end=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()
        self.limits=dict(limits or Counter(label for label,_ in self.steps))
        return self.archive.session(ref,[self.oid],self.end,11,starts_at=self.start,physical_limits=self.limits)

    def test_protocol_to_http_uses_every_category_and_persistent_records(self):
        self.setup_protocol(auth=True)
        checks=Mock(return_value=True)
        wire=BaiduHttpsWire(checks)
        # Reuse existing Mock boundary. Real HTTP primitive receives all actual
        # requests, while its lowest connection is patched and network guarded.
        self.wire.request.side_effect=wire.request
        replies=iter(self.steps)
        def connect(*args,**kwargs):
            # Physical connections, not guard callbacks, consume the replies.
            label,result=next(replies)
            request=checks.call_args.args[0]
            self.assertEqual(request.label,label)
            connection=Mock()
            connection.getresponse.return_value=http_response(result.body,result.status)
            return connection
        session=self.bounded()
        with patch('kb_custody_v21.baidu_http.http.client.HTTPSConnection',side_effect=connect) as factory:
            result=session.execute(self.oid)
        self.assertEqual(result['verification_level'],'content_sha256_verified')
        self.assertEqual(factory.call_count,11)
        records=self.registry.rows("SELECT payload_json FROM custody_events WHERE event_type='archive_http_reserved'")
        self.assertEqual(Counter(json.loads(r['payload_json'])['method'] for r in records),Counter(self.limits))
        self.assertNotIn('synthetic-token',json.dumps(records))

    def test_method_budget_cannot_borrow_remaining_total(self):
        self.setup_protocol(auth=True)
        limits=dict(Counter(label for label,_ in self.steps))
        limits['oauth_token']=0
        limits['list']=2
        session=self.bounded(limits)
        with self.assertRaisesRegex(ContractError,'method budget'): session.execute(self.oid)
        self.wire.request.assert_not_called()
        self.assertEqual(self.registry.rows('SELECT used_calls FROM archive_windows')[0]['used_calls'],0)

    def test_restart_cannot_remove_change_or_expand_policy(self):
        self.setup_protocol(auth=True)
        session=self.bounded()
        for options in ({},{'starts_at':self.start,'physical_limits':{**self.limits,'list':2}},
                        {'starts_at':self.start,'physical_limits':{**self.limits,'list':2},'max_calls':12}):
            args=dict(max_calls=11,**{k:v for k,v in options.items() if k!='max_calls'})
            if 'max_calls' in options: args['max_calls']=options['max_calls']
            with self.assertRaisesRegex(ContractError,'immutable'):
                self.archive.session(session.ref,[self.oid],self.end,**args)
        self.assertEqual(len(self.attempts()),0)

    def test_input_policy_mutation_does_not_expand_snapshot(self):
        self.setup_protocol(auth=True)
        limits=dict(Counter(label for label,_ in self.steps));limits['oauth_token']=0;limits['list']=2
        session=self.bounded(limits)
        self.limits['oauth_token']=11
        with self.assertRaisesRegex(ContractError,'method budget'): session.execute(self.oid)
        self.wire.request.assert_not_called()

    def test_hard_process_exit_preserves_method_budget_and_unresolved_protection(self):
        self.setup_protocol()
        second=self.archive.plan(self.aid,target=self.root+'/second.md')
        child=subprocess.run([sys.executable,str(Path(__file__).with_name('archive_crash_worker.py')),
                              str(WORKTREE),str(self.runtime.root),self.oid,'protocol-bounded'],
                             capture_output=True,text=True,timeout=30)
        self.assertEqual(child.returncode,73,child.stderr)
        self.assertEqual((self.runtime.root/'test-output/remote-created.bin').read_bytes(),self.content)
        window=self.registry.rows('SELECT * FROM archive_windows')[0]
        self.assertEqual(window['used_calls'],6)
        records=self.registry.rows("SELECT payload_json FROM custody_events WHERE event_type='archive_http_reserved'")
        self.assertEqual(Counter(json.loads(r['payload_json'])['method'] for r in records),
                         Counter({'uinfo':1,'quota':1,'list':1,'precreate':1,'slice':1,'create':1}))
        policy=json.loads(self.registry.rows("SELECT payload_json FROM custody_events WHERE event_type='archive_window_policy'")[0]['payload_json'])
        reopened=self.archive.session(window['confirmation_ref'],[self.oid],window['expires_at'],10,**policy)
        reopened.current=self.oid
        with self.assertRaisesRegex(ContractError,'method budget'):
            reopened.physical_request('create',Mock(side_effect=AssertionError('second create')))
        with self.assertRaisesRegex(ContractError,'unresolved upload'):
            self.archive.session('synthetic-second',[second],window['expires_at'],10).execute(second)
        self.wire.request.assert_not_called()

    def test_not_before_and_invalid_limits_never_attempt_http(self):
        self.setup_protocol(auth=True)
        now=datetime.now(timezone.utc);end=(now+timedelta(hours=2)).isoformat()
        with self.assertRaisesRegex(ContractError,'not started'):
            self.archive.session('synthetic-future',[self.oid],end,1,
                                 starts_at=(now+timedelta(hours=1)).isoformat(),physical_limits={'oauth_token':1})
        for limits in ({},{'oauth_token':True},{'oauth_token':-1},{'oauth_token':12},{'oauth_token':1},{'bad label':11}):
            with self.assertRaises(ContractError):
                self.archive.session('synthetic-invalid',[self.oid],end,11,physical_limits=limits)
        self.wire.request.assert_not_called()

    def test_expiry_rechecked_after_transaction_wait(self):
        self.setup_protocol(auth=True);session=self.bounded();session.current=self.oid
        before=datetime.now(timezone.utc);after=session.expires+timedelta(seconds=1)
        with patch('kb_custody_v21.archive.datetime') as clock:
            clock.now.side_effect=[before,after]
            with self.assertRaisesRegex(ContractError,'outside approved time'):
                session.physical_request('oauth_token',Mock(side_effect=AssertionError('unexpected HTTP')))
        self.assertEqual(len(self.attempts()),0)
        self.assertEqual(self.registry.rows('SELECT used_calls FROM archive_windows')[0]['used_calls'],0)

    def test_used_legacy_window_cannot_receive_new_policy(self):
        self.setup_protocol(auth=True)
        end=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()
        with self.registry.transaction() as db:
            db.execute('INSERT INTO archive_windows VALUES (?,?,?,?,?,?)',
                       ('synthetic-legacy',encoded([self.oid]),end,11,1,0))
        with self.assertRaisesRegex(ContractError,'legacy window'):
            self.archive.session('synthetic-legacy',[self.oid],end,11,
                                 physical_limits=dict(Counter(label for label,_ in self.steps)))
        self.wire.request.assert_not_called()
