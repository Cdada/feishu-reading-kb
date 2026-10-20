import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from kb_custody_v21.locking import open_lock
from test_custody import BASE


class LockTests(unittest.TestCase):
    def test_synchronized_process_creation_preserves_one_inode(self):
        root = Path(tempfile.mkdtemp(prefix='lock-regression-', dir=BASE / 'test-output'))
        result = subprocess.run([sys.executable, str(Path(__file__).with_name('lock_creation_probe.py')), str(root)],
                                capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stderr)
        evidence = json.loads(result.stdout)
        (root / 'observed.json').write_text(json.dumps(evidence))
        self.assertEqual(evidence['exit_codes'], [0] * 4)
        self.assertTrue(all(not row['failures'] for row in evidence['results']), evidence)
        self.assertTrue(all(row['inodes'] == evidence['results'][0]['inodes'] for row in evidence['results']))

    def test_existing_symlink_lock_rejected_without_touching_target(self):
        root = Path(tempfile.mkdtemp(prefix='lock-symlink-', dir=BASE / 'test-output'))
        target = root / 'target'
        target.write_bytes(b'synthetic protected bytes')
        (root / 'lock').symlink_to(target)
        with self.assertRaises(OSError):
            os.close(open_lock(root / 'lock'))
        self.assertEqual(target.read_bytes(), b'synthetic protected bytes')
