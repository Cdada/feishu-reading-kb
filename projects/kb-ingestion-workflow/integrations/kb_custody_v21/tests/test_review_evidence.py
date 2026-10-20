import subprocess
import tempfile
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from review_evidence import source_snapshot, redact_junit


class ReviewEvidenceTests(unittest.TestCase):
    def test_junit_redacts_attribute_text_and_tail_without_changing_structure(self):
        source = b'<testsuite tests="1"><testcase name="case" file="/synthetic/repo/test.py"><failure message="/synthetic/repo &amp; error">bad &lt;input&gt; at /synthetic/repo</failure>/synthetic/repo</testcase></testsuite>'
        rendered = redact_junit(source, lambda text: text.replace('/synthetic/repo', '<DEV_WORKTREE>'))
        root = ET.fromstring(rendered)
        self.assertEqual(root.attrib['tests'], '1')
        self.assertEqual([e.tag for e in root.iter()], ['testsuite', 'testcase', 'failure'])
        case = root.find('testcase')
        self.assertEqual(case.attrib['file'], '<DEV_WORKTREE>/test.py')
        self.assertEqual(case.find('failure').attrib['message'], '<DEV_WORKTREE> & error')
        self.assertEqual(case.find('failure').text, 'bad <input> at <DEV_WORKTREE>')
        self.assertEqual(case.find('failure').tail, '<DEV_WORKTREE>')
        self.assertNotIn(b'/synthetic/repo', rendered)
        self.assertIn(b'&lt;DEV_WORKTREE&gt;', rendered)
        with self.assertRaises(ET.ParseError):
            redact_junit(b'<broken', str)

    def test_snapshot_includes_schemas_converter_untracked_and_all_file_types(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            (root / '.gitignore').write_text('runtime/\n')
            names = ['schemas/conversion.schema.json', 'integrations/dify_converter/client.py',
                     'fixtures/sample.bin', 'new_test.py']
            for name in names + ['runtime/private.json']:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'synthetic')
            subprocess.run(['git', '-C', str(root), 'add', '.gitignore', *names[:-1]], check=True)
            before = source_snapshot(root)
            self.assertEqual(set(before['files']), {'.gitignore', *names})
            for name in names:
                original = (root / name).read_bytes()
                (root / name).write_bytes(original + b' change')
                after = source_snapshot(root)
                self.assertNotEqual(before['sha256'], after['sha256'])
                self.assertNotEqual(before['files'][name], after['files'][name])
                (root / name).write_bytes(original)
            self.assertEqual(before, source_snapshot(root))
            (root / names[0]).unlink()
            with self.assertRaisesRegex(ValueError, 'missing/nonregular'):
                source_snapshot(root)
