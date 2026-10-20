"""Public-upload regressions, all values and repositories are synthetic."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'check_public_source.py'
spec = importlib.util.spec_from_file_location('public_guard', SCRIPT)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class ContentTests(unittest.TestCase):
    def test_raw_and_escaped_home_paths(self):
        home = '/' + 'Users' + '/synthetic/person.txt'
        for text in (home, home.replace('/', '\\/'), home.replace('s', '\\u0073'),
                     home.replace('s', '\\x73')):
            with self.subTest(text=text):
                self.assertIn('personal_home', {x['category'] for x in guard.text_findings(text)})

    def test_secret_values_are_not_returned(self):
        token = 'ghp_' + 's' * 35
        result = guard.check_blob('code.py', ('token="' + token + '"').encode())
        self.assertTrue(result)
        self.assertNotIn(token, json.dumps(result))

    def test_private_artifacts_and_unknown_binary_are_rejected(self):
        for name in ('settings.local.yaml', '.env', 'credentials/value.txt', 'article.pdf', 'state.sqlite3'):
            with self.subTest(name=name):
                self.assertTrue(guard.check_blob(name, b'plain text'))
        self.assertTrue(guard.check_blob('picture.png', b'\x89PNG\x00'))
        self.assertTrue(guard.check_blob('link', b'target', '120000'))

    def test_public_email_and_synthetic_fixtures_are_accepted(self):
        self.assertFalse(guard.check_blob('.env.example', b'TOKEN=\n'))
        self.assertFalse(guard.text_findings('demo@example.invalid\nname@users.noreply.github.com'))
        email = 'person@' + 'mail.test'
        self.assertTrue(guard.text_findings(email))
        self.assertFalse(guard.check_blob('LICENSES/upstream.txt', ('Copyright ' + email).encode()))

    def test_reviewed_binary_hash_is_enforced(self):
        for name in guard.REVIEWED_BINARIES:
            self.assertTrue(guard.check_blob(name, b'\x89PNG changed'))


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.git('init', '-q')
        self.git('config', 'user.name', 'Synthetic Tester')
        self.git('config', 'user.email', 'tester@users.noreply.github.com')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], stderr=subprocess.PIPE)

    def run_guard(self, *args):
        result = subprocess.run(['python3', str(SCRIPT), *args], cwd=self.repo, capture_output=True, text=True)
        return result.returncode, json.loads(result.stdout)

    def test_deleted_secret_still_blocks_history(self):
        path = self.repo / 'source.txt'
        path.write_text('ghp_' + 'x' * 35)
        self.git('add', 'source.txt')
        self.git('commit', '-qm', 'synthetic initial')
        path.write_text('clean')
        self.git('commit', '-qam', 'remove synthetic secret')
        self.assertEqual(self.run_guard()[0], 0)
        code, report = self.run_guard('--history')
        self.assertEqual(code, 1)
        self.assertEqual(report['scanned_commits'], 2)

    def test_commit_identity_blocks_even_when_tree_is_clean(self):
        self.git('config', 'user.email', 'person@' + 'mail.test')
        (self.repo / 'source.txt').write_text('clean')
        self.git('add', 'source.txt')
        self.git('commit', '-qm', 'synthetic initial')
        code, report = self.run_guard('--history')
        self.assertEqual(code, 1)
        self.assertIn('author_email_not_noreply', {item['category'] for item in report['findings']})

    def test_index_scans_all_tracked_files_and_staged_content(self):
        path = self.repo / 'source.txt'
        path.write_text('clean')
        self.git('add', 'source.txt')
        self.git('commit', '-qm', 'synthetic initial')
        path.write_text('ghp_' + 'x' * 35)
        self.git('add', 'source.txt')
        path.write_text('clean unstaged')
        self.assertEqual(self.run_guard()[0], 1)


if __name__ == '__main__':
    unittest.main()
