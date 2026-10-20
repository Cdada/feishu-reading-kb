"""Distinct B03 reuses the B01 wire and B02 predecessor fixtures."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from kb_custody_v21.b03 import CONTENT, PURPOSE, DistinctArchiveExperiment, fixture, prepare
from kb_custody_v21.b01 import ORDER, LIMITS
from kb_custody_v21.baidu import RemoteFailure
from kb_custody_v21.contracts import ContractError
from kb_custody_v21.f01 import encoded, sha
from kb_custody_v21.f01_flow import TXT
from b01_fixture import approval, callbacks, steps
import test_b02
from test_baidu_protocol import response
from test_custody import BASE, WORKTREE, PACKAGE
import b03_execute


class B03Tests(unittest.TestCase):
    def setUp(self):
        prior = test_b02.B02Tests(); prior.setUp(); self.addCleanup(prior.doCleanups)
        self.prior = prior
        prior.a.update(source_commit='c'*40, source_snapshot={'sha256':'d'*64,'files':{}})
        ap = prior.old.root/'frozen/b02-approval.json'; ap.write_text(encoded(prior.a))
        prior.steps[2] = ('list',response(dict(errno=0,list=[])))
        exp = prior.make()
        exp.append({'binding':'approved_source','value':dict(commit=prior.a['source_commit'],
            source_sha256=prior.a['source_snapshot']['sha256'],approval_sha256=sha(ap.read_bytes()))})
        c = callbacks(); c.pop('confirm_account')
        self.assertEqual(exp.run(**c)['status'],'NO_FINAL_FILE_OBSERVED_UNKNOWN_REMAINS')
        cp = prior.old.root/'frozen/consent.json'
        cp.write_text(encoded(dict(approved_sha256=sha(ap.read_bytes()),source_commit=prior.a['source_commit'])))
        self.files = dict(approval=ap,journal=exp.path,consent=cp)
        self.before = {k:p.read_bytes() for k,p in self.files.items()}
        self.root = Path(tempfile.mkdtemp(prefix='b03-',dir=BASE/'test-output'))
        self.registry, bound = prepare(WORKTREE,root=self.root)
        self.a = approval(self.registry,bound)
        self.a.update(reference='B03',purpose=PURPOSE,fixture=fixture(),
            first_account='must_match_previously_confirmed_uk',account_ref='1234567',
            root='/apps/synthetic/KB-V21-B03',target='/apps/synthetic/KB-V21-B03/fixture.txt',
            predecessor_commit=prior.a['source_commit'],prior_observation={
                k:dict(relative_path=str(p.relative_to(WORKTREE)),sha256=sha(p.read_bytes())) for k,p in self.files.items()})
        self.steps = steps(self.a,CONTENT); self.calls=[]; self.hook=lambda request:None
        self.check=Mock(return_value=True)
        def request(req):
            kind,result=self.steps[len(self.calls)]
            self.assertEqual(req.label,kind); self.calls.append(req); self.hook(req)
            if isinstance(result,Exception): raise result
            return result
        self.wire=Mock(is_mock=True,request=request)

    def make(self):
        return DistinctArchiveExperiment(self.registry,self.a,checked_source=self.check,wire=self.wire)

    def run_experiment(self):
        c=callbacks(); c.pop('confirm_account')
        return self.make().run(**c)

    def assert_old_unchanged(self):
        self.prior.assert_old_unchanged()
        for k,p in self.files.items(): self.assertEqual(p.read_bytes(),self.before[k],k)

    def test_distinct_bytes_full13_strong_sha_without_touching_old_unknown(self):
        self.steps[7]=('slice',response(dict(md5=hashlib.md5(CONTENT).hexdigest())))
        result=self.run_experiment()
        self.assertEqual(result['status'],'OFFLINE_B03_VERIFIED',result)
        self.assertEqual(result['physical_attempts'],LIMITS)
        self.assertEqual(result['archive_result']['verification_level'],'content_sha256_verified')
        self.assertEqual(result['archive_result']['integrity_hold'],0)
        self.assertEqual(self.registry.entry(self.a['fixture_binding']['entry_id'])['sha256'],sha(CONTENT))
        self.assertNotEqual(sha(CONTENT),sha(TXT))
        self.assertIn(CONTENT,self.calls[7].body)
        self.assertNotIn(TXT,self.calls[7].body)
        self.assert_old_unchanged()
        with self.assertRaises(FileExistsError): self.make()
        journal=(self.root/'b03-requests/journal.local.jsonl').read_text()
        for secret in ('synthetic-token','synthetic-refresh','synthetic-code','synthetic-secret','synthetic-upload','synthetic-signed'):
            self.assertNotIn(secret,journal)

    def test_old_bytes_or_identifiers_cannot_be_rewrapped_in_new_runtime(self):
        for field in ('asset_id','entry_id','acceptance_id','sha256'):
            with self.subTest(field=field):
                original=copy.deepcopy(self.a)
                self.a['fixture_binding'][field]=self.prior.old.a['fixture_binding'][field]
                with self.assertRaises(ContractError): self.make()
                self.a=original
        self.assertEqual(self.calls,[])
        self.assertFalse((self.root/'b03-requests').exists())

    def test_unapproved_wider_scope_old_directory_and_fixture_rejected(self):
        for change in (dict(approved=False),dict(max_calls=50),dict(root=self.prior.a['root']),
                       dict(purpose='retry_old'),dict(first_account='operator_confirms_observed_uk'),
                       dict(fixture=self.prior.a['fixture'])):
            with self.subTest(change=change):
                old=self.a; self.a=dict(old,**change)
                with self.assertRaises(ContractError): self.make()
                self.a=old
        self.assertFalse((self.root/'b03-requests').exists()); self.assert_old_unchanged()

    def test_live_account_mismatch_stops_before_mkdir_without_operator_override(self):
        self.steps[1]=('uinfo',response(dict(errno=0,uk=7654321)))
        self.assertEqual(self.run_experiment()['status'],'STOPPED')
        self.assertEqual(len(self.calls),2); self.assert_old_unchanged()

    def test_prior_account_or_observation_provenance_cannot_be_rebound(self):
        for field,value in (('account_ref','7654321'),('predecessor_commit','e'*40)):
            original=self.a; self.a=dict(original,**{field:value})
            with self.assertRaises(ContractError): self.make()
            self.a=original
        # Rehashing a changed prior result is insufficient.
        path=self.files['journal']; lines=[json.loads(s) for s in path.read_bytes().splitlines()]
        lines[-1]['result']['old_archive_state']='verified'
        path.write_text(''.join(encoded(e)+'\n' for e in lines))
        self.a['prior_observation']['journal']['sha256']=sha(path.read_bytes())
        with self.assertRaises(ContractError): self.make()
        self.assertEqual(self.calls,[])

    def test_prior_evidence_change_on_last_response_stops_and_holds_new_asset(self):
        def hook(req):
            if len(self.calls)==13: self.files['consent'].write_text('{}')
        self.hook=hook
        result=self.run_experiment()
        self.assertEqual(result['status'],'STOPPED'); self.assertEqual(len(self.calls),13)
        self.assertEqual(self.registry.rows('SELECT state FROM archive_operations')[0]['state'],'unknown')
        self.assertEqual(self.registry.rows('SELECT integrity_hold FROM asset_locations')[0]['integrity_hold'],1)

    def test_download_corruption_preserves_both_originals(self):
        self.steps[11]=('download',response(b'X'*len(CONTENT)))
        self.assertEqual(self.run_experiment()['status'],'STOPPED')
        self.assertEqual(len(self.calls),12); self.assert_old_unchanged()
        self.assertEqual(self.registry.rows('SELECT integrity_hold FROM asset_locations')[0]['integrity_hold'],1)

    def test_precreate_instant_path_never_blindly_creates_or_retries(self):
        self.steps[6]=('precreate',response(dict(errno=0,return_type=2,uploadid='',block_list=[])))
        self.assertEqual(self.run_experiment()['status'],'STOPPED')
        self.assertEqual(len(self.calls),7); self.assert_old_unchanged()
        with self.assertRaises(FileExistsError): self.make()

    def test_reusable_block_skips_slice_and_still_requires_download_sha(self):
        self.steps[6]=('precreate',response(dict(errno=0,return_type=1,uploadid='synthetic-upload',block_list=[])))
        self.steps.pop(7)
        result=self.run_experiment()
        self.assertEqual(result['status'],'OFFLINE_B03_VERIFIED',result)
        self.assertEqual(len(self.calls),12); self.assertEqual(result['physical_attempts']['slice'],0)
        self.assert_old_unchanged()

    def test_redirect_does_not_add_requests_or_change_predecessor(self):
        self.steps[8]=('create',response(b'',status=302))
        result=self.run_experiment()
        self.assertEqual(result['status'],'STOPPED'); self.assertEqual(len(self.calls),9)
        self.assert_old_unchanged()

    def assert_stopped_attempt(self, outcome, calls, unfinished):
        result=self.run_experiment()
        self.assertEqual(result['status'],'STOPPED')
        self.assertEqual(len(self.calls),calls)
        self.assertEqual(result['unfinished_attempts'],unfinished)
        attempts=self.registry.rows('SELECT * FROM archive_attempts ORDER BY rowid')
        self.assertEqual(attempts[-1]['outcome'],outcome)
        self.assertIsNotNone(attempts[-1]['finished_at'])
        self.assertTrue(all(a['finished_at'] is not None for a in attempts))
        self.assertEqual(self.registry.rows('SELECT state FROM archive_operations')[0]['state'],'unknown')
        self.assertEqual(self.registry.rows('SELECT stopped FROM archive_windows')[0]['stopped'],1)
        self.assertEqual(self.registry.rows('SELECT integrity_hold FROM asset_locations')[0]['integrity_hold'],1)
        entry=self.registry.entry(self.a['fixture_binding']['entry_id'])
        self.assertEqual(entry['custody_health'],'healthy')
        self.assertEqual(entry['acceptance_id'],self.a['fixture_binding']['acceptance_id'])
        self.assert_old_unchanged()
        # Restoring a valid source cannot bypass the durable consumed window.
        self.check.side_effect=None
        self.check.return_value=True
        with self.assertRaises(FileExistsError): self.make()
        self.assertEqual(len(self.calls),calls)

    def test_received_malformed_create_json_finishes_attempt_without_resolving_unknown(self):
        self.steps[8]=('create',response(b'{synthetic-invalid-json'))
        self.assert_stopped_attempt('response_rejected',9,0)
        events=[json.loads(s) for s in (self.root/'b03-requests/journal.local.jsonl').read_text().splitlines()]
        receipt=next(e for e in events if e.get('received')==9)
        self.assertEqual(receipt['response_sha256'],sha(b'{synthetic-invalid-json'))
        self.assertNotIn('api_errno',receipt)

    def test_received_final_response_then_source_check_fails_finishes_attempt(self):
        self.check.side_effect=lambda:len(self.calls)<13
        self.assert_stopped_attempt('response_rejected',13,0)

    def test_remote_failure_without_received_response_keeps_uncertainty(self):
        self.steps[8]=('create',RemoteFailure('synthetic transport rejection without receipt'))
        self.assert_stopped_attempt('unknown',9,1)

    def test_contract_error_without_received_response_keeps_uncertainty(self):
        self.steps[8]=('create',ContractError('synthetic pre-connect check failure'))
        self.assert_stopped_attempt('unknown',9,1)

    def test_real_child_death_after_create_cannot_restart_or_clear_predecessor(self):
        (self.root/'approval-fixture.json').write_text(encoded(self.a))
        env=dict(os.environ,PYTHONPATH=os.pathsep.join(p for p in os.environ.get('PYTHONPATH','').split(os.pathsep)
            if p and Path(p).resolve() not in {PACKAGE/'src',PACKAGE/'tests',PACKAGE.parent/'local_conversion_mcp/src'}))
        child=subprocess.run([sys.executable,str(Path(__file__).with_name('b03_crash_worker.py')),str(WORKTREE),str(self.root)],
            cwd=self.root,env=env,capture_output=True,timeout=30)
        self.assertEqual(child.returncode,74,child.stderr.decode())
        self.assertEqual((self.root/'synthetic-remote-effect').read_text(),'create')
        self.assertEqual(self.registry.rows('SELECT state FROM archive_operations')[0]['state'],'running')
        # Hard exit cannot run exception bookkeeping. Durable running intent,
        # absent strong verification and custody hold must still protect it.
        loc=self.registry.rows('SELECT * FROM asset_locations')[0]
        self.assertEqual(loc['archive_status'],'uploading')
        self.assertEqual(loc['verification_level'],'none')
        self.assertEqual(self.registry.rows('SELECT cleanup_hold FROM staging_files')[0]['cleanup_hold'],1)
        with self.assertRaises(FileExistsError): self.make()
        self.assertEqual(self.calls,[]); self.assert_old_unchanged()

    def test_entry_rejects_unapproved_before_preparation_or_keychain(self):
        with patch.object(b03_execute,'private_approval',return_value=encoded(dict(approved=False)).encode()), \
             patch.object(b03_execute,'prepare') as prepare_mock, patch.object(b03_execute,'LoginKeychainReader') as keychain:
            with self.assertRaises(ContractError): b03_execute.execute()
            prepare_mock.assert_not_called(); keychain.assert_not_called()
