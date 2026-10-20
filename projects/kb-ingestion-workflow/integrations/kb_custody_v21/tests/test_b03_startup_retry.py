"""One separately approved startup attempt is allowed only after proven zero HTTP."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch
import test_b01
import test_b03
from b01_fixture import callbacks
import b01_execute
import b03_execute
import kb_custody_v21.b03 as b03
from kb_custody_v21.contracts import ContractError
from kb_custody_v21.f01 import encoded, sha
from test_custody import WORKTREE


class StartupDiagnosisTests(unittest.TestCase):
    def seed(self):
        seed=test_b01.B01Tests();seed.setUp();self.addCleanup(seed.doCleanups);return seed

    def check_stop(self, key, callback, stage):
        seed=self.seed(); exp=seed.experiment()
        result=exp.run(**dict(callbacks(),**{key:callback}))
        self.assertEqual(result['status'],'STOPPED')
        self.assertEqual(result['error_stage'],stage)
        self.assertEqual(seed.calls,[])
        self.assertNotIn('sensitive-synthetic-input',exp.path.read_text())
        output=io.StringIO()
        with redirect_stdout(output):b01_execute.report_result(result)
        self.assertNotIn('sensitive-synthetic-input',output.getvalue())
        with self.assertRaises(FileExistsError):seed.experiment()
        return output.getvalue()

    def test_credential_failure_has_stage_and_does_not_prompt_for_code(self):
        self.check_stop('load_credentials',Mock(side_effect=ContractError('sensitive-synthetic-input')),'load_credentials')

    def test_empty_code_has_actionable_hidden_input_guidance_without_http(self):
        text=self.check_stop('obtain_code',lambda *_:'','validate_oauth_code')
        self.assertIn('Chrome',text);self.assertIn('BAIDU CODE',text)

    def test_cancelled_hidden_input_has_its_own_stage(self):
        self.check_stop('obtain_code',Mock(side_effect=KeyboardInterrupt),'obtain_oauth_code')

    def test_source_change_after_code_is_not_misreported_as_code_format(self):
        seed=self.seed();exp=seed.experiment()
        def obtain(*_):seed.check.return_value=False;return 'sensitive-synthetic-input'
        result=exp.run(**dict(callbacks(),obtain_code=obtain))
        self.assertEqual(result['error_stage'],'after_oauth_check');self.assertEqual(seed.calls,[])
        self.assertNotIn('sensitive-synthetic-input',exp.path.read_text())


class StartupRetryTests(unittest.TestCase):
    def setUp(self):
        seed=test_b03.B03Tests();seed.setUp();self.addCleanup(seed.doCleanups);self.seed=seed
        seed.a.update(source_commit='1'*40,source_snapshot={'sha256':'2'*64,'files':{}})
        (WORKTREE/'tmp').mkdir(exist_ok=True)
        self.proofroot=Path(tempfile.mkdtemp(prefix='b03-r1-',dir=WORKTREE/'tmp'))
        ap=self.proofroot/'approval.local.json';ap.write_text(encoded(seed.a))
        consent=self.proofroot/'consent.local.json'
        consent.write_text(encoded(dict(approved_sha256=sha(ap.read_bytes()),source_commit=seed.a['source_commit'])))
        exp=seed.make()
        exp.append(dict(binding='approved_source',value=dict(commit=seed.a['source_commit'],
            source_sha256=seed.a['source_snapshot']['sha256'],approval_sha256=sha(ap.read_bytes()))))
        c=callbacks();c.pop('confirm_account');c['obtain_code']=lambda *_:''
        self.assertEqual(exp.run(**c)['status'],'STOPPED');self.assertEqual(seed.calls,[])
        self.prior_files=dict(approval=ap,journal=exp.path,consent=consent)
        self.prior_bytes={k:p.read_bytes() for k,p in self.prior_files.items()}
        snapshot=self.proofroot/'registry.sqlite3';shutil.copyfile(seed.registry.runtime.db,snapshot)
        self.a=dict(seed.a,attempt='B03-R1',source_commit='3'*40,
            retry_of={k:dict(relative_path=str(p.relative_to(WORKTREE)),sha256=sha(p.read_bytes())) for k,p in self.prior_files.items()},
            prior_database=dict(relative_path=str(snapshot.relative_to(WORKTREE)),sha256=sha(snapshot.read_bytes())))
        for key,value in (('APPROVAL',str(ap.relative_to(WORKTREE))),('RUNTIME',str(seed.root.relative_to(WORKTREE)))):
            item=patch.object(b03,key,value);item.start();self.addCleanup(item.stop)

    def make(self):
        return b03.StartupRetryExperiment(self.seed.registry,self.a,checked_source=self.seed.check,wire=self.seed.wire)

    def test_same_asset_new_claim_full13_and_old_stop_unchanged(self):
        exp=self.make();c=callbacks();c.pop('confirm_account')
        result=exp.run(**c)
        self.assertEqual(result['status'],'OFFLINE_B03_VERIFIED',result)
        self.assertEqual(len(self.seed.calls),13)
        self.assertEqual(result['archive_result']['verification_level'],'content_sha256_verified')
        self.assertEqual(self.seed.registry.entry(self.a['fixture_binding']['entry_id'])['asset_id'],self.a['fixture_binding']['asset_id'])
        for k,p in self.prior_files.items():self.assertEqual(p.read_bytes(),self.prior_bytes[k])
        self.seed.assert_old_unchanged()
        with self.assertRaises(ContractError):self.make()

    def test_new_attempt_failure_cannot_be_started_again_even_with_zero_calls(self):
        exp=self.make();c=callbacks();c.pop('confirm_account');c['obtain_code']=lambda *_:''
        self.assertEqual(exp.run(**c)['error_stage'],'validate_oauth_code')
        with self.assertRaises(FileExistsError):self.make()
        self.assertEqual(self.seed.calls,[])

    def test_any_prior_http_intent_prevents_retry_even_if_result_claims_zero(self):
        p=self.prior_files['journal'];p.write_text(p.read_text()+encoded(dict(intent=1,kind='oauth_token'))+'\n')
        self.a['retry_of']['journal']['sha256']=sha(p.read_bytes())
        with self.assertRaises(ContractError):self.make()
        self.assertEqual(self.seed.calls,[])

    def test_scope_window_or_same_asset_change_is_rejected_before_claim(self):
        for change in (dict(max_calls=50),dict(expires_at='2099-01-01T00:00:00+00:00'),
                       dict(root='/apps/synthetic/another'),dict(fixture_binding={} ),dict(approved=False)):
            old=self.a;self.a=dict(old,**change)
            with self.subTest(change=change),self.assertRaises(ContractError):self.make()
            self.a=old
        self.assertFalse((self.seed.root/'b03-r1-requests').exists())

    def test_frozen_database_change_and_current_archive_activity_refuse_retry(self):
        proof=WORKTREE/self.a['prior_database']['relative_path'];before=proof.read_bytes()
        proof.write_bytes(before+b'changed')
        with self.assertRaises(ContractError):self.make()
        proof.write_bytes(before)
        with self.seed.registry.transaction() as db:
            db.execute('INSERT INTO archive_windows VALUES (?,?,?,?,?,?)',('synthetic-other','[]',self.a['expires_at'],1,0,0))
        with self.assertRaises(ContractError):self.make()
        self.assertEqual(self.seed.calls,[])

    def test_old_journal_change_after_claim_stops_before_any_http(self):
        exp=self.make();self.prior_files['journal'].write_text('{}\n')
        c=callbacks();c.pop('confirm_account')
        result=exp.run(**c)
        self.assertEqual(result['error_stage'],'initial_check');self.assertEqual(self.seed.calls,[])

    def test_retry_entry_unapproved_never_prepares_or_reads_credentials(self):
        with patch.object(b03_execute,'private_approval',return_value=encoded(dict(approved=False)).encode()), \
             patch.object(b03_execute,'prepare') as prepare,patch.object(b03_execute,'LoginKeychainReader') as keys:
            with self.assertRaises(ContractError):b03_execute.execute(startup_retry=True)
            prepare.assert_not_called();keys.assert_not_called()
