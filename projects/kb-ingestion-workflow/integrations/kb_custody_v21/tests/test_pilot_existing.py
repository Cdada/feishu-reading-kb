"""Synthetic V1 metadata schemas; no production service or material bodies."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.f05 import file_sha
from kb_custody_v21.pilot_existing import lookup_existing, catalog_reader
from kb_custody_v21.pilot_inbox import receive_inbox
from kb_custody_v21.f01 import encoded, sha
import test_pilot_inbox as inbox_fixture


class ExistingCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.curation=self.root/'curation.sqlite3';self.conversion=self.root/'conversion.sqlite3'
        self.catalog=dict(curation_ledger_path=str(self.curation),conversion_ledger_path=str(self.conversion))
        for path,sql in [(self.curation, '''
            CREATE TABLE plans (plan_id TEXT,source_task_id TEXT,source_filename TEXT,source_sha256 TEXT,status TEXT,updated_at TEXT);
            CREATE TABLE curation_runs (run_id TEXT,plan_id TEXT,draft_json TEXT);
            CREATE TABLE publications (publication_id TEXT,run_id TEXT,state TEXT,source_sha256 TEXT);
            CREATE TABLE workflow_intakes (filename TEXT,filename_key TEXT,payload_json TEXT);
            '''),(self.conversion, '''
            CREATE TABLE inspections (inspection_id TEXT,filename TEXT,sha256 TEXT);
            CREATE TABLE tasks (task_id TEXT,inspection_id TEXT,status TEXT,updated_at TEXT);
            ''')]:
            db=sqlite3.connect(path);db.executescript(sql);db.close()

    def add_plan(self, name='01-Article.md',digest='a'*64,number=1,state='ready',publication='committed'):
        db=sqlite3.connect(self.curation)
        db.execute('INSERT INTO plans VALUES (?,?,?,?,?,?)',(f'plan_{number}',f'task_{number}',name,digest,state,str(number)))
        # This deliberately invalid body must never be parsed by metadata lookup.
        db.execute('INSERT INTO curation_runs VALUES (?,?,?)',(f'run_{number}',f'plan_{number}','DO NOT READ OR PARSE THIS BODY'))
        if publication:db.execute('INSERT INTO publications VALUES (?,?,?,?)',(f'publication_{number}',f'run_{number}',publication,digest))
        db.commit();db.close()

    def add_conversion(self,name='Article.md',digest='a'*64,state='ready_candidate'):
        db=sqlite3.connect(self.conversion)
        db.execute('INSERT INTO inspections VALUES (?,?,?)',('inspect_1',name,digest))
        db.execute('INSERT INTO tasks VALUES (?,?,?,?)',('task_conversion','inspect_1',state,'2026'))
        db.commit();db.close()

    def lookup(self,name='Article.md',digest='a'*64,**kwargs):
        return lookup_existing(name,digest,self.catalog,**kwargs)

    def test_v1_name_rule_and_byte_identity_distinguish_renames_and_revisions(self):
        self.add_plan()
        same=self.lookup();self.assertEqual(same['matches'][0]['relationship'],'same_name_same_bytes')
        rename=self.lookup('new-name.md');self.assertEqual(rename['matches'][0]['relationship'],'same_bytes')
        revision=self.lookup(digest='b'*64);self.assertEqual(revision['matches'][0]['relationship'],'same_name_different_bytes')
        for result in (same,rename,revision):
            self.assertTrue(result['requires_review']);self.assertFalse(result['matches'][0]['reusable_verified'])

    def test_published_record_is_distinct_from_withdrawn_or_unfinished_record(self):
        self.add_plan();self.add_plan('02-Other.md','b'*64,2,publication='withdrawn')
        self.assertEqual(self.lookup()['matches'][0]['publication_id'],'publication_1')
        self.assertIsNone(self.lookup('Other.md','b'*64)['matches'][0]['publication_id'])

    def test_conversion_failure_or_running_state_remains_known_history(self):
        self.add_conversion(state='processing')
        result=self.lookup()
        self.assertTrue(result['requires_review']);self.assertEqual(result['matches'][0]['recorded_state'],'processing')

    def test_original_upload_alias_is_only_a_name_hint(self):
        self.add_plan('converted.md','b'*64)
        db=sqlite3.connect(self.curation)
        db.execute('INSERT INTO workflow_intakes VALUES (?,?,?)',('Original.pdf','original.pdf',json.dumps(dict(source_task_id='task_1',converted_handoff=dict(task_id='task_1')))))
        db.commit();db.close()
        result=self.lookup('Original.pdf')
        self.assertTrue(result['received_before'])
        self.assertEqual(result['matches'][0]['relationship'],'name_only')
        self.assertIsNone(result['matches'][0]['source_sha256'])

    def test_received_without_conversion_is_not_reported_as_new(self):
        db=sqlite3.connect(self.curation)
        db.execute('INSERT INTO workflow_intakes VALUES (?,?,?)',('Article.md','article.md','{}'));db.commit();db.close()
        result=self.lookup();self.assertTrue(result['requires_review']);self.assertFalse(result['matches'])

    def test_no_match_states_registered_scope_and_does_not_claim_all_files_checked(self):
        result=self.lookup()
        self.assertTrue(result['complete']);self.assertFalse(result['requires_review'])
        self.assertEqual(result['scope'],'v1_registered_metadata')
        self.assertFalse(result['unindexed_files_checked']);self.assertFalse(result['bodies_read']);self.assertFalse(result['formal_write'])

    def test_truncated_scan_or_match_list_requires_review_even_when_no_visible_match(self):
        self.add_plan(number=1);self.add_plan('Other.md','b'*64,number=2)
        result=self.lookup(max_records=1)
        self.assertFalse(result['complete']);self.assertTrue(result['requires_review']);self.assertFalse(result['matches'])
        self.add_plan(number=3)
        result=self.lookup(max_matches=1)
        self.assertFalse(result['complete']);self.assertEqual(len(result['matches']),1)

    def test_missing_or_wrong_database_is_a_visible_error_and_never_created(self):
        missing=self.root/'absent.sqlite3';self.catalog['curation_ledger_path']=str(missing)
        with self.assertRaises(ContractError):self.lookup()
        self.assertFalse(missing.exists())
        self.catalog['curation_ledger_path']=str(self.conversion)
        with self.assertRaises(ContractError):self.lookup()

    def test_read_only_connection_rejects_writes_and_keeps_database_bytes(self):
        self.add_plan();before=[file_sha(p) for p in (self.curation,self.conversion)]
        self.lookup()
        with self.assertRaises(ContractError):
            with catalog_reader(self.curation) as db:db.execute('DELETE FROM plans')
        self.assertEqual(before,[file_sha(p) for p in (self.curation,self.conversion)])

    def test_live_wal_is_observed_instead_of_ignored_as_immutable(self):
        db=sqlite3.connect(self.conversion);self.addCleanup(db.close)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('INSERT INTO inspections VALUES (?,?,?)',('inspect_1','Article.md','a'*64))
        db.execute('INSERT INTO tasks VALUES (?,?,?,?)',('task_wal','inspect_1','ready_candidate','2026'));db.commit()
        before=file_sha(Path(str(self.conversion)+'-wal'))
        result=self.lookup();self.assertEqual(result['matches'][0]['source_task_id'],'task_wal')
        self.assertEqual(file_sha(Path(str(self.conversion)+'-wal')),before)

    def test_inbox_plan_selects_legacy_lookup_before_next_conversion_action(self):
        self.add_plan('synthetic.md',sha(inbox_fixture.CONTENT))
        f=inbox_fixture.PilotInboxTests();f.setUp();self.addCleanup(f.doCleanups)
        path=f.scan_plan();path.write_text(encoded(dict(reference='P20-receive-inbox-v2',lookback_seconds=3600,legacy_catalog=self.catalog)))
        f.f.frozen[str(path)]=file_sha(path);c=f.make();gate=Mock();gate.check.return_value=dict(state='resources_ready')
        result=receive_inbox(c,f.f.flow,path,resources=gate)
        self.assertEqual(result['accepted'][0]['next_action'],'review_existing')
        self.assertEqual(result['accepted'][0]['existing']['matches'][0]['publication_id'],'publication_1')
        self.assertFalse(result['conversion_started']);self.assertFalse(result['model_started'])

    def test_same_title_different_format_is_a_hint_not_an_exact_match(self):
        self.add_plan()
        result=self.lookup('Article.pdf','b'*64)
        self.assertEqual(result['matches'][0]['relationship'],'possible_same_article_different_format')
        self.assertTrue(result['requires_review']);self.assertFalse(result['matches'][0]['reusable_verified'])

    def test_bad_legacy_configuration_blocks_before_platform_access(self):
        f=inbox_fixture.PilotInboxTests();f.setUp();self.addCleanup(f.doCleanups)
        path=f.scan_plan();path.write_text(encoded(dict(reference='P20-receive-inbox-v2',lookback_seconds=3600,
            legacy_catalog=dict(self.catalog,curation_ledger_path=str(self.root/'missing.sqlite3')))))
        f.f.frozen[str(path)]=file_sha(path);c=f.make()
        with self.assertRaises(ContractError):receive_inbox(c,f.f.flow,path)
        f.f.runner.assert_not_called()

    def test_quiescent_wal_database_does_not_create_source_sidecars(self):
        db=sqlite3.connect(self.conversion);db.execute('PRAGMA journal_mode=WAL');db.close()
        before={p.name for p in self.root.iterdir()};self.lookup()
        self.assertEqual(before,{p.name for p in self.root.iterdir()})

    def test_catalog_change_during_read_is_not_returned_as_a_complete_result(self):
        with self.assertRaisesRegex(ContractError,'changed during lookup'):
            with catalog_reader(self.conversion):
                self.add_conversion()
