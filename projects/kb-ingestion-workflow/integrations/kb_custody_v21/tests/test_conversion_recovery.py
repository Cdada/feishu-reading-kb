from datetime import datetime, timedelta, timezone
from pathlib import Path
import selectors
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from kb_custody_v21.contracts import ContractError, new_id
from kb_custody_v21.conversion import CustodyConversion
from kb_custody_v21.registry import now
from kb_custody_v21.retention import Retention
import test_archive_v21
import test_conversion_v21
from test_custody import WORKTREE


class ConversionRecoveryTests(unittest.TestCase):
    setUp = test_conversion_v21.ConversionTests.setUp
    receive = test_conversion_v21.ConversionTests.receive
    setup_archive = test_archive_v21.ArchiveTests.setup_archive
    script = test_archive_v21.ArchiveTests.script
    preflight = test_archive_v21.ArchiveTests.preflight
    detail = test_archive_v21.ArchiveTests.detail
    upload_steps = test_archive_v21.ArchiveTests.upload_steps
    upload = test_archive_v21.ArchiveTests.upload
    session = test_archive_v21.ArchiveTests.session

    def queued(self, key='recover-0001'):
        inspected = self.conversion.inspect(self.entry)
        return self.conversion.submit(inspected['inspection_id'], key)

    def start(self, mode):
        child = subprocess.Popen([sys.executable, str(Path(__file__).with_name('conversion_crash_worker.py')),
                                  str(WORKTREE), str(self.runtime.root), mode],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop, child)
        return child

    @staticmethod
    def stop(child):
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)
        for stream in (child.stdin, child.stdout, child.stderr):
            stream.close()

    def ready(self, child):
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            self.assertTrue(selector.select(timeout=15), 'child did not reach lease')
        line = child.stdout.readline()
        self.assertEqual(line.strip(), 'leased', child.stderr.read() if child.poll() is not None else line)

    def active_leases(self):
        return self.registry.rows('SELECT * FROM asset_leases WHERE released_at IS NULL')

    def test_queued_dependency_blocks_qualified_cleanup_until_normal_completion(self):
        self.setup_archive()
        self.upload()
        retention = Retention(self.archive, days=0, policy_version='synthetic-now')
        retention.hold(self.aid, False, 'synthetic-confirmation')
        self.assertEqual(retention.eligibility(self.aid)['status'], 'cleanup_eligible')
        task = self.queued()
        self.assertFalse(self.active_leases())  # Queue protection is independent of live readers.
        self.assertIn('conversion_dependency', retention.eligibility(self.aid)['reasons'])
        with self.assertRaisesRegex(ContractError, 'valid dependency'):
            self.conversion.recovery.recover(task['task_id'], 'synthetic-confirmation')
        with self.assertRaises(ContractError):
            retention.plan([self.aid])
        self.conversion.worker.run_once()
        self.assertEqual(retention.eligibility(self.aid)['status'], 'cleanup_eligible')

    def test_dead_worker_releases_only_its_leases_and_preserves_other_task(self):
        self.setup_archive()
        first = self.queued()
        second = self.queued('recover-0002')
        child = self.start('die')
        self.ready(child)
        self.assertEqual(child.wait(timeout=10), 73)
        owned = self.active_leases()
        self.assertEqual(len(owned), 1)
        unowned = new_id('lease')
        with self.registry.transaction() as db:
            db.execute('INSERT INTO asset_leases VALUES (?,?,?,?,NULL)', (unowned, self.aid, 'legacy-reader', now()))
        conversion = CustodyConversion(self.registry)
        self.assertEqual(conversion.recovery.recover(first['task_id'], 'synthetic-confirmation'), 'failed')
        self.assertEqual([row['lease_id'] for row in self.active_leases()], [unowned])
        binding = self.registry.rows('SELECT * FROM conversion_bindings WHERE task_id=?', (second['task_id'],))[0]
        self.assertIsNone(binding['released_at'])
        self.assertEqual(conversion.service.ledger.get_task(second['task_id'])['status'], 'queued')
        self.assertEqual((self.runtime.root / self.registry.entry(self.entry)['relative_path']).read_bytes(), self.content)
        conversion.worker.run_once()
        self.assertEqual(conversion.service.ledger.get_task(second['task_id'])['status'], 'ready_candidate')
        self.assertEqual(len(self.registry.rows('SELECT * FROM extraction_snapshots')), 1)
        self.assertFalse(conversion.worker.run_once())

    def test_live_worker_old_lease_refuses_recovery_without_state_change(self):
        self.setup_archive()
        task = self.queued()
        child = self.start('hold')
        self.ready(child)
        with self.registry.transaction() as db:
            db.execute('UPDATE asset_leases SET acquired_at=? WHERE released_at IS NULL',
                       ((datetime.now(timezone.utc) - timedelta(days=100)).isoformat(),))
        before = self.conversion.service.ledger.get_task(task['task_id'])
        with self.assertRaisesRegex(ContractError, 'still holds'):
            CustodyConversion(self.registry).recovery.recover(task['task_id'], 'synthetic-confirmation')
        self.assertEqual(before, self.conversion.service.ledger.get_task(task['task_id']))
        self.assertEqual(len(self.active_leases()), 1)
        self.assertIsNone(child.poll())

    def test_surviving_pipeline_child_keeps_fence_after_worker_death(self):
        self.setup_archive()
        task = self.queued()
        marker = self.runtime.root / 'stage-child.ready'
        child = self.start('child')
        self.addCleanup(marker.with_suffix('.release').touch)
        deadline = time.monotonic() + 15
        while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(marker.exists(), 'pipeline child did not start')
        child.kill()
        child.wait(timeout=10)
        with self.assertRaisesRegex(ContractError, 'still holds'):
            self.conversion.recovery.recover(task['task_id'], 'synthetic-confirmation')
        self.assertEqual(len(self.active_leases()), 1)
        marker.with_suffix('.release').touch()
        deadline = time.monotonic() + 10
        while True:
            try:
                result = self.conversion.recovery.recover(task['task_id'], 'synthetic-confirmation')
                break
            except ContractError as error:
                if 'still holds' not in str(error) or time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
        self.assertEqual(result, 'failed')
        self.assertFalse(self.active_leases())

    def test_terminal_write_before_dependency_release_recovers_idempotently(self):
        self.setup_archive()
        task = self.queued()
        child = self.start('terminal-gap')
        self.assertEqual(child.wait(timeout=15), 74, child.stderr.read())
        self.assertEqual(self.conversion.service.ledger.get_task(task['task_id'])['status'], 'ready_candidate')
        self.assertIsNone(self.registry.rows('SELECT released_at FROM conversion_bindings')[0]['released_at'])
        before = self.registry.rows('SELECT * FROM extraction_snapshots')
        for _ in range(2):
            self.assertEqual(self.conversion.recovery.recover(task['task_id'], 'synthetic-confirmation'), 'ready_candidate')
        self.assertEqual(before, self.registry.rows('SELECT * FROM extraction_snapshots'))
        self.assertEqual(len(self.registry.rows("SELECT * FROM custody_events WHERE event_type='conversion_task_reconciled'")), 1)
        self.assertFalse(self.active_leases())

    def test_ownership_write_failure_keeps_submission_lease_and_refuses_guessing(self):
        self.setup_archive()
        inspected = self.conversion.inspect(self.entry)
        with patch.object(self.conversion.recovery, 'register', side_effect=RuntimeError('synthetic cross-ledger failure')):
            with self.assertRaisesRegex(RuntimeError, 'cross-ledger'):
                self.conversion.submit(inspected['inspection_id'], 'recover-submit-gap')
        task = self.conversion.service.ledger.get_task_by_idempotency(requested_by='v21-dev', idempotency_key='recover-submit-gap')
        self.assertEqual(task['status'], 'queued')
        self.assertEqual(len(self.active_leases()), 1)
        with self.assertRaisesRegex(ContractError, 'ownership binding'):
            self.conversion.recovery.recover(task['task_id'], 'synthetic-confirmation')
        self.assertEqual(len(self.active_leases()), 1)

    def test_binding_mismatch_preserves_lease(self):
        self.setup_archive()
        task = self.queued()
        child = self.start('die')
        self.ready(child)
        child.wait(timeout=10)
        with self.registry.transaction() as db:
            db.execute("UPDATE conversion_bindings SET inspection_id='synthetic-wrong-inspection'")
        with self.assertRaisesRegex(ContractError, 'ownership changed'):
            self.conversion.recovery.recover(task['task_id'], 'synthetic-confirmation')
        self.assertEqual(len(self.active_leases()), 1)
        self.assertEqual(self.conversion.service.ledger.get_task(task['task_id'])['status'], 'preparing')

    def test_lost_submission_result_preserves_guard_after_queue_commit(self):
        self.setup_archive()
        inspected = self.conversion.inspect(self.entry)
        with patch.object(self.conversion.service, '_task_result', side_effect=RuntimeError('synthetic lost result')):
            with self.assertRaisesRegex(RuntimeError, 'lost result'):
                self.conversion.submit(inspected['inspection_id'], 'recover-result-gap')
        task = self.conversion.service.ledger.get_task_by_idempotency(requested_by='v21-dev', idempotency_key='recover-result-gap')
        self.assertEqual(task['status'], 'queued')
        self.assertEqual(len(self.active_leases()), 1)

    def test_rejected_submission_without_task_releases_transient_lease(self):
        from local_document_conversion_mcp.errors import ServiceError
        self.setup_archive()
        inspected = self.conversion.inspect(self.entry)
        with self.assertRaises(ServiceError):
            self.conversion.submit(inspected['inspection_id'], 'bad')
        self.assertFalse(self.active_leases())

    def test_cancelled_queue_can_release_dependency_without_starting_worker(self):
        self.setup_archive()
        task = self.queued()
        self.conversion.service.cancel_task(task_id=task['task_id'])
        self.assertEqual(self.conversion.recovery.recover(task['task_id'], 'synthetic-confirmation'), 'cancelled')
        self.assertIsNotNone(self.registry.rows('SELECT released_at FROM conversion_bindings')[0]['released_at'])
        self.assertFalse(self.conversion.worker.run_once())
