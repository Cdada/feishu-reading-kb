"""The installed launcher's process handshake works without platform access."""
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

from kb_custody_v21 import daily_launcher
from kb_custody_v21.daily_launcher import DailyLauncher


class LauncherProcessTests(unittest.TestCase):
    def test_start_stop_and_restart_keep_separate_instances(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary).resolve()
            launcher=DailyLauncher(directory/'service')
            script=directory/'synthetic_worker.py'
            script.write_text('''import sys,time
from kb_custody_v21.daily_launcher import DailyLauncher
launcher=DailyLauncher(sys.argv[1])
with launcher.own(configuration_sha='a'*64) as instance:
    deadline=time.monotonic()+5
    while time.monotonic()<deadline and not launcher.stop_requested(instance):
        time.sleep(.02)
''')
            env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',
                     PYTHONPATH=str(Path(daily_launcher.__file__).parents[1]))
            command=[sys.executable,str(script),str(launcher.root)]
            first_instance=None
            try:
                for _ in range(2):
                    started=launcher.start(command,env=env,readiness_seconds=3)
                    self.assertTrue(started['running'])
                    self.assertFalse(started['already_running'])
                    instance=started['process']['instance']
                    self.assertNotEqual(instance,first_instance)
                    first_instance=instance
                    repeated=launcher.start(command,env=env)
                    self.assertTrue(repeated['already_running'])
                    self.assertEqual(repeated['process']['instance'],instance)
                    self.assertEqual(launcher.request_stop()['state'],'stop_requested')
                    deadline=time.monotonic()+3
                    while launcher.status()['running'] and time.monotonic()<deadline:
                        time.sleep(.02)
                    self.assertFalse(launcher.status()['running'])
            finally:
                launcher.request_stop()
                deadline=time.monotonic()+6
                while launcher.status()['running'] and time.monotonic()<deadline:
                    time.sleep(.02)
                self.assertFalse(launcher.status()['running'],'synthetic worker must exit before cleanup')


if __name__=='__main__':unittest.main()
