import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.pilot_resources import (
    GIB, PilotResources, ResourceSample, ResourceUnavailable, macos_sample, parse_memory)
from kb_custody_v21.registry import Registry
from kb_custody_v21.runtime import Runtime, DEFAULTS
from test_custody import WORKTREE, BASE


def sample(free=40, pressure='normal', disk=8 * GIB):
    return ResourceSample(free, pressure, disk)


class ResourcePolicyTests(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp(prefix='pilot-resources-', dir=BASE/'test-output'))
        self.registry = Registry(Runtime(WORKTREE, DEFAULTS, root))

    def check(self, value):
        return PilotResources(self.registry, probe=lambda _: value).check()

    def events(self):
        return self.registry.rows("SELECT * FROM custody_events WHERE event_type='pilot_resource_admission'")

    def test_warning_blocks_even_with_abundant_free_memory(self):
        self.assertEqual(self.check(sample(70, 'warning'))['reason'], 'memory_pressure_warning')
        self.assertEqual(self.check(sample(80, 'critical'))['reason'], 'memory_pressure_critical')

    def test_restart_preserves_hysteresis_until_25_percent(self):
        self.assertEqual(self.check(sample(19))['state'], 'paused_resources')
        # Each check creates a new controller over the existing persisted events.
        self.assertEqual(self.check(sample(24))['reason'], 'memory_recovery_threshold')
        self.assertEqual(self.check(sample(25))['state'], 'resources_ready')
        self.assertEqual(self.check(sample(20))['state'], 'resources_ready')

    def test_disk_floor_and_recovery_require_all_conditions(self):
        self.assertEqual(self.check(sample(disk=5*GIB-1))['reason'], 'disk_free_low')
        self.assertEqual(self.check(sample(24))['state'], 'paused_resources')
        self.assertEqual(self.check(sample(25, disk=5*GIB))['state'], 'resources_ready')
        with self.assertRaises(ContractError):
            PilotResources(self.registry, minimum_free_bytes=5*GIB-1)

    def test_unreadable_probe_pauses_and_never_logs_exception_text(self):
        def unreadable(_): raise ResourceUnavailable('synthetic-private-probe-output')
        result = PilotResources(self.registry, probe=unreadable).check()
        self.assertEqual(result['reason'], 'resource_probe_unavailable')
        self.assertNotIn('synthetic-private-probe-output', str(self.events()))
        self.assertEqual(self.check(sample(24))['state'], 'paused_resources')

    def test_unchanged_state_does_not_append_unbounded_events(self):
        for free in (19, 18, 17):
            self.check(sample(free, 'warning'))
        self.assertEqual(len(self.events()), 1)
        self.check(sample(30))
        self.check(sample(19, 'warning'))
        self.assertEqual(len(self.events()), 3)

    def test_programming_error_is_not_silently_treated_as_low_memory(self):
        def broken(_): raise ValueError('synthetic bug')
        with self.assertRaises(ValueError):
            PilotResources(self.registry, probe=broken).check()
        for value in (sample(True), sample(101), sample(40, 'guessed'), sample(disk=-1)):
            with self.assertRaises(ContractError): self.check(value)
        self.assertFalse(self.events())

    def test_macos_dispatch_mapping_and_invalid_probe_output(self):
        for code, label in (('1','normal'), ('2','warning'), ('4','critical')):
            self.assertEqual(parse_memory('header\nSystem-wide memory free percentage: 48%\n',
                                          code+'\n', 8*GIB), sample(48,label))
        for text, pressure in (('free 48%', '1'), ('System-wide memory free percentage: 101%', '1'),
                               ('System-wide memory free percentage: 48%', '0'),
                               ('System-wide memory free percentage: 48%', '3'),
                               ('System-wide memory free percentage: 48%\nSystem-wide memory free percentage: 50%', '1')):
            with self.assertRaises(ResourceUnavailable): parse_memory(text, pressure, 8*GIB)

    def test_native_probe_is_read_only_and_bounded(self):
        outputs=[subprocess.CompletedProcess([],0,'System-wide memory free percentage: 48%\n',''),
                 subprocess.CompletedProcess([],0,'2\n','')]
        with patch('kb_custody_v21.pilot_resources.sys.platform', 'darwin'), \
             patch('kb_custody_v21.pilot_resources.subprocess.run', side_effect=outputs) as run, \
             patch('kb_custody_v21.pilot_resources.shutil.disk_usage') as disk:
            disk.return_value.free=8*GIB
            self.assertEqual(macos_sample(self.registry.runtime.root), sample(48,'warning'))
        self.assertEqual([c.args[0] for c in run.call_args_list],
                         [['/usr/bin/memory_pressure','-Q'],
                          ['/usr/sbin/sysctl','-n','kern.memorystatus_vm_pressure_level']])
        self.assertTrue(all(c.kwargs['timeout']==5 and c.kwargs['check'] for c in run.call_args_list))

    def test_timeout_nonzero_and_unsupported_host_fail_closed(self):
        for error in (OSError(), subprocess.TimeoutExpired('synthetic',5),
                      subprocess.CalledProcessError(1,'synthetic')):
            with patch('kb_custody_v21.pilot_resources.sys.platform', 'darwin'), \
                 patch('kb_custody_v21.pilot_resources.subprocess.run', side_effect=error), \
                 self.assertRaises(ResourceUnavailable):
                macos_sample(self.registry.runtime.root)
        with patch('kb_custody_v21.pilot_resources.sys.platform','linux'), \
             patch('kb_custody_v21.pilot_resources.subprocess.run') as run, \
             self.assertRaises(ResourceUnavailable):
            macos_sample(self.registry.runtime.root)
        run.assert_not_called()
