import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from kb_custody_v21.archive_retention import preview, GRACE_SECONDS
from kb_custody_v21.contracts import ContractError
from kb_custody_v21.daily_store import DailyStore
from test_daily_store import policy, message

class PreviewTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name).resolve()/'conversion-work/daily-service'
        self.time=1000000.0
        self.store=DailyStore(self.root/'queue.sqlite3',policy(),clock=lambda:self.time)
        scan=self.store.scan('chat',initial_start=self.time-60)
        self.store.save_page(scan,[message(1)],has_more=False)
        self.temp=self.root/('process.local.json.'+'a'*32+'.tmp')
        self.temp.write_text('{}');os.utime(self.temp,(1,1))
        (self.root/'process.local.json').write_text('{}')

    def test_preview_never_deletes_or_modifies_ledger_and_protects_waiting_tasks(self):
        before=self.store.rows('SELECT * FROM daily_documents')
        result=preview(self.root,now=self.time+GRACE_SECONDS+1)
        self.assertEqual(result['candidates'],0)
        self.assertTrue(self.temp.exists());self.assertEqual(result['deleted_files'],0)
        self.assertEqual(self.store.rows('SELECT * FROM daily_documents'),before)

    def test_completed_old_atomic_temp_only_becomes_review_candidate(self):
        self.store.advance('om_1','registered','held')
        # A held job is unresolved even if old.
        self.assertEqual(preview(self.root,now=self.time+GRACE_SECONDS+1)['candidates'],0)
        with self.store.transaction() as db:
            db.execute("UPDATE daily_documents SET phase='notified'")
        self.assertEqual(preview(self.root,now=self.time+1)['candidates'],0)
        result=preview(self.root,now=self.time+GRACE_SECONDS+1)
        self.assertEqual(result['candidates'],1)
        self.assertTrue(self.temp.exists());self.assertFalse(result['grace_policy_approved'])
        self.store.put('defer:om_1',dict(reason='synthetic_review'))
        self.assertEqual(preview(self.root,now=self.time+GRACE_SECONDS+1)['candidates'],0)

    def test_result_unknown_retains_temps_and_all_model_evidence(self):
        with self.store.transaction() as db:db.execute("UPDATE daily_documents SET phase='notified'")
        self.store.operation('synthetic','send',{});self.store.begin('synthetic',physical_requests=2)
        path=self.root/'requests/synthetic';path.mkdir(parents=True);(path/'response.local').write_text('synthetic')
        result=preview(self.root,now=self.time+GRACE_SECONDS+1)
        self.assertEqual(result['candidates'],0)
        row=next(r for r in result['rows'] if r['area']=='requests')
        self.assertTrue(row['rollback_dependency']);self.assertEqual(row['classification'],'retain')

    def test_mineru_completed_manifest_and_failed_outputs_are_both_retained(self):
        root=self.root.parent/'pdf-chunks/synthetic'
        root.mkdir(parents=True);(root/'manifest.json').write_text('{}')
        (root/'failed.local').write_text('synthetic');(root/'result.md').write_text('synthetic')
        result=preview(self.root,now=self.time+GRACE_SECONDS+1)
        row=next(r for r in result['rows'] if r['area']=='pdf-chunks')
        self.assertEqual(row['file_count'],3);self.assertEqual(row['classification'],'retain')
        self.assertIn('不可变清单',row['reason'])

    def test_symlink_and_outside_roots_are_not_followed(self):
        outside=self.root.parent/'private';outside.write_text('do not read')
        jobs=self.root/'reading-jobs';jobs.mkdir()
        (jobs/'synthetic').symlink_to(outside)
        result=preview(self.root)
        row=next(r for r in result['rows'] if r['area']=='reading-jobs')
        self.assertEqual(row['logical_bytes'],0);self.assertFalse(row['complete'])
        self.assertFalse(result['complete']);self.assertNotIn('do not read',str(result))
        with self.assertRaises(ContractError):preview(self.root.parent)

    def test_missing_database_is_not_created(self):
        missing=self.root.parent/'another/conversion-work/daily-service'
        with self.assertRaises(ContractError):preview(missing)
        self.assertFalse(missing.exists())
