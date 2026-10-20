"""B02 reuses B01 fixtures and only reads a protected synthetic predecessor."""
import copy
from contextlib import closing
from datetime import timedelta
import json
import os
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from kb_custody_v21.b02 import BaiduObservation, LIMITS, ORDER
from kb_custody_v21.b01 import fixture
from kb_custody_v21.baidu_protocol import HttpResponse
from kb_custody_v21.contracts import ContractError
from kb_custody_v21.f01 import encoded, sha
from kb_custody_v21.f01_flow import TXT
from b01_fixture import callbacks, steps
from test_baidu_protocol import response
from test_baidu_boundaries import http_response
from test_custody import WORKTREE
import test_b01
import b02_execute


class B02Tests(unittest.TestCase):
    def setUp(self):
        old=test_b01.B01Tests(); old.setUp(); self.addCleanup(old.doCleanups)
        self.old=old; self.root=old.root/'observation'
        frozen=old.root/'frozen'; frozen.mkdir()
        old.a.update(source_commit='a'*40,source_snapshot={'sha256':'b'*64,'files':{}})
        approval_path=frozen/'old-approval.json'; approval_path.write_text(encoded(old.a))
        exp=old.experiment()
        exp.append({'binding':'approved_source','value':dict(commit=old.a['source_commit'],
            source_sha256=old.a['source_snapshot']['sha256'],approval_sha256=sha(approval_path.read_bytes()))})
        old.fail_at('slice',response({'errno':0}))  # response exists, MD5 missing: protected unknown
        self.assertEqual(exp.run(**callbacks())['status'],'STOPPED')
        database=frozen/'registry-consistent.sqlite3'
        with closing(sqlite3.connect(database)) as target, old.registry.connect() as source: source.backup(target)
        journal=frozen/'journal.jsonl'; journal.write_bytes(exp.path.read_bytes())
        entry=old.registry.entry(old.a['fixture_binding']['entry_id'])
        original=old.registry.runtime.root/entry['relative_path']
        frozen_original=frozen/'fixture.txt'; frozen_original.write_bytes(original.read_bytes())
        files=dict(approval=approval_path,journal=journal,database=database,original=frozen_original,
                   current_original=original,current_journal=exp.path,current_database=old.registry.runtime.db)
        self.before={k:p.read_bytes() for k,p in files.items()}; self.files=files
        self.a=dict(old.a,reference='B02',limits=LIMITS,max_calls=7,writes=[],
            purpose='observe_existing_unknown_without_resolving_it',account_ref='1234567',
            prior_commit=old.a['source_commit'],runtime_relative=str(self.root.relative_to(WORKTREE)),
            prior_files={k:dict(relative_path=str(p.relative_to(WORKTREE)),sha256=sha(p.read_bytes())) for k,p in files.items()})
        originals=steps(old.a)
        item=json.loads(originals[9][1].body)['list'][0]
        self.steps=[originals[0],originals[1],('list',response(dict(errno=0,list=[item]))),*originals[9:]]
        self.calls=[]; self.hook=lambda req:None; self.check=Mock(return_value=True)
        def request(req):
            name,result=self.steps[len(self.calls)]
            self.assertEqual(req.label,name); self.calls.append(req); self.hook(req)
            if isinstance(result,Exception): raise result
            return result
        self.wire=Mock(is_mock=True,request=request)

    def make(self, **kwargs):
        return BaiduObservation(WORKTREE,self.a,checked_source=self.check,root=self.root,
                                wire=kwargs.pop('wire',self.wire),**kwargs)

    def run_observation(self, **kwargs):
        c=callbacks(); c.pop('confirm_account')
        return self.make(**kwargs).run(**c)

    def assert_old_unchanged(self):
        for k,p in self.files.items(): self.assertEqual(p.read_bytes(),self.before[k],k)
        self.assertFalse(Path(str(self.files['database'])+'-wal').exists())
        self.assertEqual(self.old.registry.rows('SELECT state FROM archive_operations')[0]['state'],'unknown')
        self.assertEqual(self.old.registry.rows('SELECT integrity_hold FROM asset_locations')[0]['integrity_hold'],1)

    def test_unique_exact_candidate_download_sha_and_unchanged_old_unknown(self):
        r=self.run_observation()
        self.assertEqual(r['status'],'CANDIDATE_BYTES_VERIFIED_UNKNOWN_REMAINS',r)
        self.assertEqual(r['physical_attempts'],LIMITS)
        self.assertEqual(r['unknown_recovery'],'NOT_RUN')
        self.assertEqual(r['observed_sha256'],sha(TXT))
        self.assertEqual([q.label for q in self.calls],ORDER)
        self.assertTrue(all(q.method=='GET' and not q.body for q in self.calls))
        self.assert_old_unchanged()
        with self.assertRaises(FileExistsError): self.make()
        self.assertEqual(len(self.calls),7)
        journal=(self.root/'b02-requests/journal.local.jsonl').read_text()
        for secret in ('synthetic-token','synthetic-refresh','synthetic-code','synthetic-secret','synthetic-signed'):
            self.assertNotIn(secret,journal)

    def test_no_final_file_is_three_reads_and_never_authorizes_retry(self):
        self.steps[2]=('list',response(dict(errno=0,list=[])))
        r=self.run_observation()
        self.assertEqual(r['status'],'NO_FINAL_FILE_OBSERVED_UNKNOWN_REMAINS')
        self.assertEqual(len(self.calls),3)
        self.assert_old_unchanged()
        with self.assertRaises(FileExistsError): self.make()

    def test_wrong_account_stops_before_any_directory_query(self):
        self.steps[1]=('uinfo',response(dict(errno=0,uk=7654321)))
        self.assertEqual(self.run_observation()['status'],'STOPPED')
        self.assertEqual(len(self.calls),2); self.assert_old_unchanged()

    def test_missing_directory_is_not_absence_and_never_created(self):
        self.steps[2]=('list',response(dict(errno=-9)))
        self.assertEqual(self.run_observation()['status'],'STOPPED')
        self.assertEqual(len(self.calls),3); self.assert_old_unchanged()

    def test_ambiguous_truncated_and_out_of_scope_candidates_stop(self):
        variants=('duplicate','full_page','has_more','outside','wrong_size','directory')
        for variant in variants:
            with self.subTest(variant=variant):
                seed=B02Tests(); seed.setUp(); self.addCleanup(seed.doCleanups)
                item=json.loads(seed.steps[2][1].body)['list'][0]
                body=dict(errno=0,list=[item])
                if variant=='duplicate': body['list'].append(dict(item,fs_id=987654321))
                elif variant=='full_page': body['list']=[item]*100
                elif variant=='has_more': body['has_more']=True
                elif variant=='outside': item['path']='/apps/synthetic/other/fixture.txt'
                elif variant=='wrong_size': item['size']=False
                else: item['isdir']=1
                seed.steps[2]=('list',response(body))
                self.assertEqual(seed.run_observation()['status'],'STOPPED')
                self.assertEqual(len(seed.calls),3); seed.assert_old_unchanged()

    def test_bad_detail_or_download_hash_does_not_release_unknown(self):
        for position in (3,5,6):
            with self.subTest(position=position):
                seed=B02Tests(); seed.setUp(); self.addCleanup(seed.doCleanups)
                name,result=seed.steps[position]
                if name=='download': result=response(b'X'*len(TXT))
                else:
                    body=json.loads(result.body); body['list'][0]['path']=seed.a['root']+'/other.txt'; result=response(body)
                seed.steps[position]=(name,result)
                self.assertEqual(seed.run_observation()['status'],'STOPPED')
                self.assertEqual(len(seed.calls),position+1); seed.assert_old_unchanged()

    def test_timeout_and_redirect_do_not_retry_or_restart(self):
        for failure in (TimeoutError('synthetic-secret'),HttpResponse(302,b'')):
            with self.subTest(kind=type(failure).__name__):
                seed=B02Tests(); seed.setUp(); self.addCleanup(seed.doCleanups)
                seed.steps[2]=('list',failure)
                r=seed.run_observation(); self.assertEqual(r['status'],'STOPPED')
                self.assertEqual(len(seed.calls),3)
                with self.assertRaises(FileExistsError): seed.make()
                seed.assert_old_unchanged()

    def test_prior_source_and_registry_provenance_cannot_be_rebound_by_hash_alone(self):
        for kind in ('approval','journal','database'):
            with self.subTest(kind=kind):
                seed=B02Tests(); seed.setUp(); self.addCleanup(seed.doCleanups)
                path=seed.files[kind]
                if kind=='approval':
                    obj=json.loads(path.read_bytes()); obj['app_name']='other'; path.write_text(encoded(obj))
                elif kind=='journal':
                    lines=[json.loads(line) for line in path.read_text().splitlines()]
                    next(e for e in lines if e.get('binding')=='account_ref')['value']='7654321'
                    path.write_text(''.join(encoded(e)+'\n' for e in lines))
                    seed.files['current_journal'].write_bytes(path.read_bytes())
                    seed.a['prior_files']['current_journal']['sha256']=sha(path.read_bytes())
                else:
                    with closing(sqlite3.connect(path)) as db:
                        with db: db.execute("UPDATE archive_operations SET state='succeeded'")
                seed.a['prior_files'][kind]['sha256']=sha(path.read_bytes())
                with self.assertRaises(ContractError): seed.make()
                self.assertEqual(seed.calls,[])

    def test_nonempty_current_or_snapshot_wal_is_rejected_before_network(self):
        for kind in ('database','current_database'):
            with self.subTest(kind=kind):
                seed=B02Tests(); seed.setUp(); self.addCleanup(seed.doCleanups)
                Path(str(seed.files[kind])+'-wal').write_bytes(b'synthetic-uncheckpointed-wal')
                with self.assertRaises(ContractError): seed.make()
                self.assertEqual(seed.calls,[])

    def test_unapproved_expired_or_expanded_scope_has_zero_calls(self):
        original=copy.deepcopy(self.a)
        for change in (dict(approved=False),dict(max_calls=13),dict(writes=['upload']),dict(automatic_retries=1),
                       dict(redirects=1),dict(account_ref='7654321'),dict(expires_at=self.a['starts_at'])):
            with self.subTest(change=change):
                self.a=dict(original,**change)
                with self.assertRaises(ContractError): self.make()
        self.assertEqual(self.calls,[])

    def test_source_change_during_code_input_has_zero_calls(self):
        client=self.make(); c=callbacks(); c.pop('confirm_account')
        def code(*args): self.check.return_value=False; return 'synthetic-code'
        c['obtain_code']=code
        self.assertEqual(client.run(**c)['status'],'STOPPED')
        self.assertEqual(self.calls,[])

    def test_last_reply_source_or_old_evidence_change_rejects_success(self):
        for change in ('source','evidence','current_original','expiry'):
            with self.subTest(change=change):
                seed=B02Tests(); seed.setUp(); self.addCleanup(seed.doCleanups)
                from datetime import datetime,timezone
                clock=Mock(return_value=datetime.now(timezone.utc))
                def hook(req):
                    if len(seed.calls)==7:
                        if change=='source': seed.check.return_value=False
                        elif change=='expiry': clock.return_value+=timedelta(hours=3)
                        else: seed.files['journal' if change=='evidence' else change].write_bytes(b'changed synthetic')
                seed.hook=hook
                self.assertEqual(seed.run_observation(clock=clock)['status'],'STOPPED')
                self.assertEqual(len(seed.calls),7)

    @unittest.skipUnless(hasattr(os,'fork'),'POSIX child death')
    def test_child_death_after_read_intent_keeps_single_claim_and_no_replay(self):
        pid=os.fork()
        if pid==0:
            self.hook=lambda req: os._exit(71) if req.label=='uinfo' else None
            try: self.run_observation()
            except BaseException: os._exit(72)
            os._exit(73)
        _,status=os.waitpid(pid,0)
        self.assertEqual(os.waitstatus_to_exitcode(status),71)
        events=[json.loads(line) for line in (self.root/'b02-requests/journal.local.jsonl').read_text().splitlines()]
        self.assertEqual([e['kind'] for e in events if 'intent' in e],ORDER[:2])
        with self.assertRaises(FileExistsError): self.make()
        self.assertEqual(self.calls,[]); self.assert_old_unchanged()

    def test_real_composition_uses_existing_guarded_wire_but_tls_is_mocked(self):
        connection=Mock()
        connection.getresponse.side_effect=[http_response(r.body) for _,r in self.steps]
        with patch('kb_custody_v21.baidu_http.http.client.HTTPSConnection',return_value=connection):
            r=self.run_observation(wire=None)
        self.assertEqual(r['status'],'CANDIDATE_BYTES_VERIFIED_UNKNOWN_REMAINS')
        self.assertEqual(r['validation_mode'],'real_bounded')  # TLS mock remains offline evidence.
        self.assertEqual(connection.request.call_count,7); self.assert_old_unchanged()

    def test_forbidden_write_and_out_of_order_category_have_zero_physical_calls(self):
        c=self.make()
        for label in ('precreate','slice','create','mkdir','download'):
            callback=Mock()
            with self.assertRaises(ContractError): c.perform(label,callback)
            callback.assert_not_called()

    def test_entry_unapproved_stops_before_keychain_or_oauth(self):
        with patch.object(b02_execute,'private_approval',return_value=b'{"approved":false}'), \
             patch.object(b02_execute,'LoginKeychainReader') as keychain:
            with self.assertRaises(ContractError): b02_execute.execute()
            keychain.assert_not_called()
