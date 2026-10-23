"""Real local ZIP changes, absent disks and repeated scheduling, synthetic text."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from kb_custody_v21.f05 import file_sha
from kb_custody_v21.qa_regression_monitor import monitor
from test_qa_quality import fixture


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.archive=self.root/'source.zip'
        self.case=fixture();self.doc=self.case['model_input']['documents'][0]
        self.text=self.doc['text'];self.source_id=self.doc['material_id']
        self.write_zip(self.text)
        self.selection=self.root/'selection.json';self.selection.write_text(json.dumps([dict(id=self.source_id,path='source.md',title='Synthetic',role='unknown',canonical_id='synthetic-family')]))
        self.cases=self.root/'cases.json';self.cases.write_text(json.dumps([self.case]))
        self.system=self.root/'system.py';self.system.write_text('# synthetic')
        self.config=self.root/'config.json';self.state=self.root/'.dev-runtime/monitor'
        self.config.write_text(json.dumps(dict(version='qa-offline-monitor-v1',archive=str(self.archive),selection=str(self.selection),cases=str(self.cases),corpus_id='synthetic',system_files=[str(self.system)],state_root=str(self.state))))
        self.now=datetime(2030,1,6,12,tzinfo=timezone.utc)

    def write_zip(self,text,extra=False):
        with zipfile.ZipFile(self.archive,'w') as z:
            z.writestr('source.md',text)
            if extra:z.writestr('new.md','New unreviewed source')

    def run_tick(self):return monitor(self.config,file_sha(self.config),now=self.now)

    def test_initial_real_read_and_duplicate_trigger(self):
        result=self.run_tick();self.assertTrue(result['offline_checks_passed'])
        self.assertEqual(result['model_calls'],0);self.assertFalse(result['semantic_quality_tested'])
        again=self.run_tick();self.assertEqual(again['state'],'unchanged');self.assertFalse(again['notify'])

    def test_changed_source_invalidates_old_reference_and_is_not_passed(self):
        self.run_tick();self.write_zip(self.text+' Changed source.')
        result=self.run_tick();self.assertEqual(result['needs_review'],1)
        self.assertFalse(result['offline_checks_passed']);self.assertTrue(result['notify'])
        self.assertEqual(self.run_tick()['state'],'unchanged')

    def test_new_unreviewed_file_and_weekly_due(self):
        self.run_tick();self.write_zip(self.text,extra=True)
        result=self.run_tick();self.assertEqual(result['unreviewed_member_changes'],1)
        self.assertFalse(result['offline_checks_passed'])
        self.now+=timedelta(days=7);result=self.run_tick()
        self.assertEqual(result['checked'],1)
        self.assertFalse(result['offline_checks_passed'])

    def test_missing_disk_preserves_last_completed_state(self):
        self.run_tick();before=(self.state/'state.local.json').read_bytes()
        self.archive.unlink()
        with self.assertRaises(FileNotFoundError):self.run_tick()
        self.assertEqual((self.state/'state.local.json').read_bytes(),before)

    def test_explicit_synthetic_sources_are_separate_from_real_library(self):
        synthetic=fixture('boundary',body='Invented boundary test. No real project.')
        source_id='synthetic-boundary'
        from kb_custody_v21.qa_test_fixtures import compile_fixture
        text='Invented boundary test. No real project.'
        synthetic=compile_fixture(dict(id='boundary',question='Is this real?',selectors=[dict(material_id=source_id,
            body_sha256=hashlib.sha256(text.encode()).hexdigest(),start=0,end=len(text))],constraints=['Use only the synthetic control.'],conversation=[],
            expected=dict(answerability='supported',required_claims=['Invented.'],forbidden_claims=['Real.'])),
            {source_id:dict(title='Synthetic control',role='record',text=text)})
        self.cases.write_text(json.dumps([self.case,synthetic]))
        controls=self.root/'controls.json';controls.write_text(json.dumps([dict(id=source_id,text=text)]))
        config=json.loads(self.config.read_bytes());config['synthetic_sources']=dict(path=str(controls),sha256=file_sha(controls))
        self.config.write_text(json.dumps(config))
        result=self.run_tick()
        self.assertTrue(result['offline_checks_passed'])
        self.assertEqual(result['selected_sources'],1);self.assertEqual(result['synthetic_sources'],1)
        self.assertEqual(result['synthetic_cases'],1);self.assertEqual(result['model_calls'],0)
        controls.write_text('[]')
        from kb_custody_v21.contracts import ContractError
        with self.assertRaisesRegex(ContractError,'manifest changed'):self.run_tick()
