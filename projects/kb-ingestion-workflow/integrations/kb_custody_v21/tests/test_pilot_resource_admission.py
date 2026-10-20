"""Small orchestration checks: no renderer or conversion worker is executed."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kb_custody_v21.intake import MockAttachment
from kb_custody_v21.pilot_intake import PilotIntake
from kb_custody_v21.pilot_resources import PilotResources
from kb_custody_v21.registry import Registry
from kb_custody_v21.runtime import Runtime, DEFAULTS
from test_custody import WORKTREE, BASE
from test_pilot_intake import SCOPE, CONTENT, message
from test_pilot_resources import sample


class ResourceAdmissionTests(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp(prefix='pilot-resource-admit-', dir=BASE/'test-output'))
        self.registry = Registry(Runtime(WORKTREE, DEFAULTS, root))
        self.flow = self.reopen()
        self.eid = self.flow.receive(message(), 0, MockAttachment(CONTENT).download)['entry_id']

    def reopen(self):
        return PilotIntake(self.registry, SCOPE, max_attachments=5, max_pending=8)

    def resources(self, *values):
        it=iter(values)
        return PilotResources(self.registry, probe=lambda _: next(it))

    def test_warning_before_inspection_preserves_original_and_queue(self):
        with patch.object(self.flow.conversion, 'inspect') as inspect:
            result=self.flow.work_guarded_once(resources=self.resources(sample(60,'warning')))
        inspect.assert_not_called()
        self.assertEqual(result['state'], 'paused_resources')
        state=self.flow.status(self.eid)
        self.assertEqual((state['original_status'],state['custody_health'],state['task_id']),
                         ('accepted','healthy',None))
        self.assertFalse(self.registry.rows('SELECT * FROM conversion_bindings'))

    def test_warning_at_inspection_boundary_stops_before_heavy_work(self):
        with patch.object(self.flow.conversion, 'inspect') as inspect:
            self.flow.work_guarded_once(resources=self.resources(sample(),sample(60,'warning')))
        inspect.assert_not_called()

    def test_queued_task_survives_pause_then_resume_uses_same_task(self):
        with patch.object(self.flow.conversion.worker, 'run_once') as worker:
            stopped=self.flow.work_guarded_once(resources=self.resources(sample(),sample(),sample(60,'critical')))
        worker.assert_not_called()
        task=stopped['task_id']
        self.assertEqual(self.flow.conversion.service.ledger.get_task(task)['status'],'queued')
        before=self.registry.rows('SELECT * FROM asset_leases')
        restarted=self.reopen()
        self.assertEqual(restarted.work_guarded_once(resources=self.resources(sample(24)))['state'],'paused_resources')
        self.assertEqual(self.registry.rows('SELECT * FROM asset_leases'),before)
        with patch.object(restarted.conversion, 'submit') as submit, \
             patch.object(restarted.conversion.worker, 'run_once') as worker:
            result=restarted.work_guarded_once(resources=self.resources(sample(25),sample(25)))
        submit.assert_not_called()
        worker.assert_called_once_with(task_id=task,blocking=False)
        self.assertEqual(result['task_id'],task)
        self.assertEqual(len(self.registry.rows('SELECT * FROM conversion_bindings')),1)

    def test_busy_controller_does_not_probe_or_start_a_second_worker(self):
        resource=self.resources()
        with patch.object(resource,'check') as check, self.flow._serial():
            self.assertEqual(self.reopen().work_guarded_once(resources=resource), {'state':'busy'})
        check.assert_not_called()

    def test_default_guard_uses_host_probe_instead_of_assuming_safe_memory(self):
        with patch('kb_custody_v21.pilot_resources.macos_sample',return_value=sample(70,'warning')) as probe:
            self.assertEqual(self.flow.work_guarded_once()['state'],'paused_resources')
        probe.assert_called_once_with(self.registry.runtime.root)
