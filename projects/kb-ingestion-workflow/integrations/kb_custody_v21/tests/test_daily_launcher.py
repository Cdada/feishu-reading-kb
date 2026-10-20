"""Local launcher ownership and stop markers; no real service is started."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from kb_custody_v21.daily_launcher import DailyLauncher, write_state


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.launcher = DailyLauncher(Path(self.tmp.name).resolve()/'service')

    def test_state_depends_on_owned_lock_not_stale_pid(self):
        self.assertEqual(self.launcher.status()['state'], 'not_started')
        with self.launcher.own(configuration_sha='a'*64):
            self.assertTrue(self.launcher.status()['running'])
        self.assertFalse(self.launcher.status()['running'])
        self.assertIn('ended_at', self.launcher.status()['process'])

    def test_second_owner_cannot_start_and_stop_targets_current_instance(self):
        with self.launcher.own(configuration_sha='a'*64) as instance:
            with self.assertRaises(BlockingIOError):
                with self.launcher.own(configuration_sha='a'*64):
                    self.fail('second service must not own the lock')
            self.assertEqual(self.launcher.request_stop()['state'], 'stop_requested')
            self.assertTrue(self.launcher.stop_requested(instance))
        with self.launcher.own(configuration_sha='a'*64) as replacement:
            self.assertNotEqual(instance, replacement)
            self.assertFalse(self.launcher.stop_requested(replacement))

    def test_restart_waits_for_child_state_after_it_acquires_lock(self):
        old = dict(instance='old', pid=123, ended_at=1)
        current = dict(instance='new', pid=456)
        child = Mock(pid=456)
        child.poll.return_value = None
        with patch.object(self.launcher, 'status', side_effect=[
                dict(running=False, process=old),
                dict(running=True, process=old),
                dict(running=True, process=current)]) as status, \
                patch('kb_custody_v21.daily_launcher.subprocess.Popen', return_value=child), \
                patch('kb_custody_v21.daily_launcher.time.sleep'):
            result = self.launcher.start(['synthetic-worker'], env={})
        self.assertEqual(status.call_count, 3)
        self.assertEqual(result['process'], current)
        self.assertFalse(result['already_running'])

    def test_repeated_start_of_running_instance_never_spawns(self):
        with self.launcher.own(configuration_sha='a'*64):
            result = self.launcher.start(['/not/an/executable'], env={})
            self.assertTrue(result['already_running'])


if __name__ == '__main__':
    unittest.main()
