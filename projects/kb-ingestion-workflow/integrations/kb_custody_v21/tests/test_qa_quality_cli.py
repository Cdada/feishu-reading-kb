"""Offline command integration using temporary synthetic fixtures and a tiny ZIP."""
from contextlib import redirect_stdout
from io import StringIO
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.qa_quality import digest
from kb_custody_v21.qa_quality_cli import main, read_json, prepare, write_new
from test_qa_quality import fixture, record, grade, run_config, calibrated_report
from test_qa_regression import manifest, NOW, SYSTEM


class QualityCLITests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.cases = self.save('cases.json', [fixture()])
        self.run = self.save('run.json', run_config())

    def save(self, name, value):
        path = self.root/name; path.write_text(json.dumps(value, ensure_ascii=False)); return str(path)

    def run_cli(self, *args, expected=0):
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(list(args)), expected)
        return json.loads(output.getvalue())

    def test_prepare_has_no_private_reference_and_never_overwrites(self):
        dest = self.root/'packets'
        result = self.run_cli('prepare', '--cases', self.cases, '--out', str(dest))
        self.assertEqual(result, dict(prepared=1, blocked=0, model_calls=0))
        packet = read_json(next(dest.glob('*.request.json')))
        self.assertNotIn('reference', packet)
        self.assertNotIn('expected', packet)
        with self.assertRaises(FileExistsError): prepare([fixture()], dest)

    def test_prepare_blocks_large_input_instead_of_truncating(self):
        result = self.run_cli('prepare', '--cases', self.cases, '--out', str(self.root/'small'), '--max-input-bytes', '1', expected=2)
        self.assertEqual(result['blocked'], 1)
        self.assertEqual(list((self.root/'small').glob('*.request.json')), [])

    def test_empty_answers_emit_not_run_report(self):
        records = self.save('records.json', [])
        result = self.run_cli('score', '--cases', self.cases, '--records', records, '--run', self.run, '--out', str(self.root/'report'), expected=2)
        self.assertEqual(result['states'], {'not_run': 1})
        self.assertFalse(result['quality_passed'])
        self.assertTrue((self.root/'report/report.md').is_file())

    def test_score_compare_complete_synthetic_pipeline(self):
        cal = self.save('calibration.json', calibrated_report())
        records = self.save('good.json', [record(fixture())])
        common = ['--cases', self.cases, '--run', self.run, '--calibration', cal]
        self.run_cli('score', *common, '--records', records, '--out', str(self.root/'old'))
        bad = grade(); bad['forbidden'][0]['verdict'] = 'present'
        records = self.save('bad.json', [record(fixture(), g=bad)])
        self.run_cli('score', *common, '--records', records, '--out', str(self.root/'new'), expected=2)
        diff = self.run_cli('compare', '--baseline', str(self.root/'old/report.json'),
            '--candidate', str(self.root/'new/report.json'), '--out', str(self.root/'diff.json'), expected=2)
        self.assertTrue(diff['changes'][0]['new_failure'])
        self.assertFalse(diff['release_allowed'])

    def test_plan_check_resume_and_local_notification(self):
        current = self.save('snapshot.json', manifest())
        plan = str(self.root/'plan.json')
        self.run_cli('plan', '--cases', self.cases, '--current', current, '--system-fingerprint', SYSTEM,
            '--now', NOW.isoformat(), '--core', 'plan', '--enable-offline', '--out', plan)
        result = self.run_cli('check', '--cases', self.cases, '--plan', plan, '--state-db', str(self.root/'state.sqlite3'), '--out', str(self.root/'checks'))
        self.assertTrue(result['offline_checks_passed'])
        self.assertFalse(result['semantic_quality_tested'])
        task = read_json(self.root/'checks/offline-task.json')
        self.assertEqual(task['results'][0]['check'], 'frozen_fixture_integrity')
        result = self.run_cli('check', '--cases', self.cases, '--plan', plan, '--state-db', str(self.root/'state.sqlite3'), '--out', str(self.root/'repeat'))
        self.assertIsNone(read_json(self.root/'repeat/notification.json'))

    def test_plan_defaults_disabled_and_cannot_be_run(self):
        current = self.save('snapshot.json', manifest())
        plan = str(self.root/'disabled.json')
        result = self.run_cli('plan', '--cases', self.cases, '--current', current, '--system-fingerprint', SYSTEM,
            '--now', NOW.isoformat(), '--out', plan)
        self.assertEqual(result['state'], 'disabled')
        with self.assertRaises(ContractError):
            self.run_cli('check', '--cases', self.cases, '--plan', plan, '--state-db', str(self.root/'state.sqlite3'), '--out', str(self.root/'checks'))

    def test_cli_retrieval_builds_only_an_isolated_index(self):
        body = '合成园林调查记录：花园调查仍处于计划阶段。'
        archive = self.root/'synthetic.zip'
        with zipfile.ZipFile(archive, 'w') as z: z.writestr('garden.md', body)
        before = archive.read_bytes()
        selection = self.save('selection.json', [dict(id='garden', path='garden.md', title='合成园林调查', role='record',
            canonical_id='garden', body_sha256=hashlib.sha256(body.encode()).hexdigest())])
        cases = self.save('retrieval.json', [dict(id='garden', question='查文献 园林调查', expected_mode='search',
            evidence_groups=[[dict(material_id='garden', quote='计划阶段')]])])
        result = self.run_cli('retrieve', '--archive', str(archive), '--selection', selection,
            '--cases', cases, '--now', NOW.isoformat(), '--out', str(self.root/'retrieval'))
        self.assertEqual(result['model_calls'], 0)
        report = read_json(self.root/'retrieval/retrieval-report.json')
        self.assertEqual(report['results'][0]['metrics']['evidence_coverage_at_12'], 1)
        self.assertEqual(archive.read_bytes(), before)

    def test_nonfinite_duplicate_and_symlink_inputs_rejected(self):
        bad = self.root/'bad.json'
        for raw in ('{"x": NaN}', '{"x": 1, "x": 2}'):
            bad.write_text(raw)
            with self.assertRaises(ContractError): read_json(bad)
        link = self.root/'link.json'; link.symlink_to(self.cases)
        with self.assertRaises(ContractError): read_json(link)

    def test_result_file_is_exclusive_and_private(self):
        path = self.root/'private.json'
        write_new(path, {'x': 1})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError): write_new(path, {'x': 2})
        self.assertEqual(read_json(path), {'x': 1})


if __name__ == '__main__':
    unittest.main()
