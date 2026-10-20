"""B01 composition regression with guarded sockets and synthetic credentials."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from kb_custody_v21.b01 import BaiduExperiment, ORDER, deadline, prepare
from kb_custody_v21.baidu import BaiduAdapter
from kb_custody_v21.baidu_http import BaiduHttpsWire
from kb_custody_v21.baidu_protocol import BaiduProtocolTransport
from kb_custody_v21.contracts import ContractError
from kb_custody_v21.f01_flow import TXT
from b01_fixture import approval, callbacks, steps
from test_baidu_protocol import response
from test_baidu_boundaries import http_response
from test_custody import BASE, WORKTREE, PACKAGE

sys.path.insert(0, str(PACKAGE))
import b01_execute


class B01Tests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='b01-', dir=BASE/'test-output'))
        self.registry, bound = prepare(WORKTREE, root=self.root)
        self.a = approval(self.registry, bound)
        self.steps, self.calls = steps(self.a), []
        def request(req):
            name, result = self.steps[len(self.calls)]
            self.assertEqual(req.label, name)
            self.calls.append(req)
            if isinstance(result, Exception): raise result
            return result
        self.wire = Mock(is_mock=True, request=request)
        self.check = Mock(return_value=True)

    def experiment(self, **options):
        return BaiduExperiment(self.registry, self.a, checked_source=self.check,
                              wire=options.pop('wire', self.wire), **options)

    def fail_at(self, label, result):
        i = next(i for i, (name, _) in enumerate(self.steps) if name == label)
        self.steps[i] = label, result
        return i+1

    def test_full_chain_global13_archive10_sha_and_no_secrets(self):
        experiment = self.experiment()
        result = experiment.run(**callbacks())
        self.assertEqual(result['status'], 'OFFLINE_B01_VERIFIED', result)
        self.assertEqual([r.label for r in self.calls], ORDER)
        self.assertEqual(result['archive_result']['validation_mode'], 'offline_mock')
        self.assertEqual(self.registry.rows('SELECT used_calls FROM archive_windows')[0]['used_calls'], 10)
        self.assertEqual(result['unfinished_attempts'], 0)
        journal = experiment.path.read_text()
        for secret in ('synthetic-token', 'synthetic-refresh', 'synthetic-code',
                       'synthetic-secret', 'synthetic-upload', 'synthetic-signed'):
            self.assertNotIn(secret, journal)
        self.assertIsNone(experiment.protocol._token)
        self.assertIsNone(experiment.protocol._authorization)
        loader = Mock(side_effect=AssertionError('must not read'))
        with self.assertRaises(ContractError):
            experiment.run(**dict(callbacks(), load_credentials=loader))
        loader.assert_not_called()
        with self.assertRaises(FileExistsError): self.experiment()
        self.assertEqual(len(self.calls), 13)

    def test_no_slice_reuse_still_requires_create_and_download_sha(self):
        self.fail_at('precreate', response(dict(errno=0, return_type=1, uploadid='synthetic-upload', block_list=[])))
        self.steps = [s for s in self.steps if s[0] != 'slice']
        result = self.experiment().run(**callbacks())
        self.assertEqual(result['status'], 'OFFLINE_B01_VERIFIED', result)
        self.assertEqual(sum(result['physical_attempts'].values()), 12)
        self.assertEqual(result['physical_attempts']['slice'], 0)

    def test_wrong_operator_account_stops_before_mkdir(self):
        result = self.experiment().run(**dict(callbacks(), confirm_account=lambda uk: '7654321'))
        self.assertEqual(result['status'], 'STOPPED')
        self.assertEqual([r.label for r in self.calls], ORDER[:2])

    def test_source_change_while_credentials_or_account_prompt_waits(self):
        def code(credentials, a):
            self.check.return_value = False
            return 'synthetic-code'
        result = self.experiment().run(**dict(callbacks(), obtain_code=code))
        self.assertEqual(result['status'], 'STOPPED')
        self.assertEqual(self.calls, [])

    def test_expiry_while_account_prompt_waits_preserves_read_only_result(self):
        clock = Mock(return_value=datetime.now(timezone.utc))
        def confirm(uk):
            clock.return_value += timedelta(hours=3)
            return uk
        result = self.experiment(clock=clock).run(**dict(callbacks(), confirm_account=confirm))
        self.assertEqual(result['status'], 'STOPPED')
        self.assertEqual([r.label for r in self.calls], ORDER[:2])

    def test_scope_change_unapproved_or_expired_rejected_before_claim(self):
        original = json.loads(json.dumps(self.a))
        changes = [dict(approved=False), dict(max_calls=50), dict(automatic_retries=1),
                   dict(root='/apps/synthetic/production'), dict(scopes=['basic','netdisk','extra']),
                   dict(expires_at=original['starts_at'])]
        for change in changes:
            self.a = dict(original, **change)
            with self.subTest(change=change), self.assertRaises(ContractError): self.experiment()
            self.assertFalse((self.root/'b01-requests').exists())
        self.assertEqual(self.calls, [])

    def test_source_false_prevents_claim(self):
        self.check.return_value = False
        with self.assertRaises(ContractError): self.experiment()
        self.assertFalse((self.root/'b01-requests').exists())

    def test_atomic_exclusive_claim_rejects_second_live_owner(self):
        first = self.experiment()
        with self.assertRaises(FileExistsError): self.experiment()
        self.assertEqual(first.run(**callbacks())['status'], 'OFFLINE_B01_VERIFIED')
        self.assertEqual(len(self.calls), 13)

    def test_mkdir_conflict_stops_without_rename_or_upload(self):
        self.fail_at('mkdir', response(dict(errno=-8, errmsg='synthetic-secret')))
        result = self.experiment().run(**callbacks())
        self.assertEqual(result['status'], 'STOPPED')
        self.assertEqual([r.label for r in self.calls], ORDER[:3])
        self.assertEqual(self.registry.rows('SELECT * FROM archive_operations'), [])

    def test_create_timeout_retains_unknown_and_no_restart(self):
        count = self.fail_at('create', TimeoutError('synthetic-secret'))
        experiment = self.experiment()
        result = experiment.run(**callbacks())
        self.assertEqual(result['status'], 'STOPPED')
        self.assertEqual(result['unfinished_attempts'], 1)
        self.assertEqual(experiment.archive.operation(experiment.operation_id)['state'], 'unknown')
        with self.assertRaises(FileExistsError): self.experiment()
        self.assertEqual(len(self.calls), count)
        self.assertNotIn('synthetic-secret', experiment.path.read_text())

    def test_instant_completion_stops_without_slice_create_or_second_plan(self):
        self.fail_at('precreate', response(dict(errno=0, return_type=2, uploadid='', block_list=[])))
        experiment = self.experiment()
        result = experiment.run(**callbacks())
        self.assertEqual(result['status'], 'STOPPED')
        self.assertEqual([r.label for r in self.calls], ORDER[:7])
        with self.assertRaises(ContractError):
            experiment.archive.plan(self.a['fixture_binding']['asset_id'], target=self.a['target'])

    def test_redirect_is_recorded_and_not_followed(self):
        expected = self.fail_at('download', response(b'', status=302))
        result = self.experiment().run(**callbacks())
        self.assertEqual(result['status'], 'STOPPED')
        self.assertEqual(result['unfinished_attempts'], 0)
        self.assertEqual(len(self.calls), expected)

    def test_category_and_order_guard_does_not_execute_callback(self):
        experiment = self.experiment()
        forbidden = Mock(side_effect=AssertionError('unexpected HTTP'))
        for kind in ('mkdir','other','download'):
            with self.subTest(kind=kind), self.assertRaises(ContractError):
                experiment.perform(kind, forbidden)
        forbidden.assert_not_called()

    def test_real_composition_provenance_with_mocked_tls_boundary(self):
        # Actual HTTPS class, with connections patched before DNS; not real evidence.
        replies = [http_response(r.body, status=r.status) for _, r in self.steps]
        connection = Mock()
        connection.getresponse.side_effect = replies
        experiment = self.experiment(wire=None)
        with patch('kb_custody_v21.baidu_http.http.client.HTTPSConnection', return_value=connection):
            result = experiment.run(**callbacks())
        self.assertEqual(result['status'], 'BOUNDED_BAIDU_UPLOAD_VERIFIED', result)
        self.assertEqual(result['archive_result']['validation_mode'], 'real_bounded')
        self.assertEqual(connection.request.call_count, 13)
        self.assertEqual(connection.close.call_count, 13)
        events = self.registry.rows("SELECT * FROM custody_events WHERE event_type='archive_operation_completed'")
        self.assertIn('real_bounded', json.dumps(events))
        with self.assertRaises(ContractError):
            BaiduAdapter(experiment.protocol, '1234567', self.a['root'])
        with self.assertRaises(ContractError):
            BaiduProtocolTransport(BaiduHttpsWire(lambda r: True), '1234567', self.a['root'], self.a['target'], token='synthetic-token')

    def test_real_subprocess_death_after_mkdir_and_create_blocks_restart(self):
        for phase in ('mkdir','create'):
            with self.subTest(phase=phase):
                root = Path(tempfile.mkdtemp(prefix='b01-crash-', dir=BASE/'test-output'))
                registry, bound = prepare(WORKTREE, root=root)
                a = approval(registry, bound)
                (root/'approval-fixture.json').write_text(json.dumps(a))
                # Reproduce a clean CI child: project modules must be located
                # by the worker, not inherited from this workstation's env.
                project_paths = {PACKAGE/'src', PACKAGE/'tests', PACKAGE.parent/'local_conversion_mcp/src'}
                env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in os.environ.get('PYTHONPATH','').split(os.pathsep)
                    if p and Path(p).resolve() not in project_paths))
                child = subprocess.run([sys.executable, str(Path(__file__).with_name('b01_crash_worker.py')),
                    str(WORKTREE), str(root), phase], capture_output=True, timeout=30, cwd=root, env=env)
                self.assertEqual(child.returncode, 74, child.stderr.decode())
                self.assertEqual((root/'synthetic-remote-effect').read_text(), phase)
                events = [json.loads(s) for s in (root/'b01-requests/journal.local.jsonl').read_text().splitlines()]
                intents = [e for e in events if 'intent' in e]
                self.assertEqual(intents[-1]['kind'], phase)
                self.assertEqual(sum(e.get('kind') == phase for e in intents), 1)
                wire = Mock(is_mock=True)
                with self.assertRaises(FileExistsError):
                    BaiduExperiment(registry, a, checked_source=lambda: True, wire=wire)
                wire.request.assert_not_called()
                if phase == 'create':
                    self.assertEqual(registry.rows('SELECT state FROM archive_operations')[0]['state'], 'running')

    def test_wall_deadline_restores_signal_and_stops_blocking_work(self):
        previous = signal.getsignal(signal.SIGALRM)
        with self.assertRaises(TimeoutError):
            with deadline(0.02): time.sleep(0.1)
        self.assertEqual(signal.getsignal(signal.SIGALRM), previous)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))

    def test_private_approval_mode_and_frozen_bytes_checked(self):
        path = self.root/'private-approval.json'
        frozen = json.dumps(dict(worktree=str(WORKTREE))).encode()
        path.write_bytes(frozen)
        path.chmod(0o644)
        with self.assertRaises(ContractError): b01_execute.private_approval(path)
        path.chmod(0o600)
        with patch.object(b01_execute, 'checked_source') as check:
            self.assertTrue(b01_execute.source_check(WORKTREE, path, frozen))
            check.assert_called_once()
            path.write_bytes(frozen+b'\n')
            with self.assertRaises(ContractError): b01_execute.source_check(WORKTREE, path, frozen)
            self.assertEqual(check.call_count, 1)

    def test_cli_unapproved_stops_before_keychain_or_preparation(self):
        with patch.object(b01_execute, 'private_approval', return_value=b'{"approved":false}'), \
             patch.object(b01_execute, 'LoginKeychainReader') as keys, patch.object(b01_execute, 'prepare') as prep:
            with self.assertRaises(ContractError): b01_execute.execute()
            keys.assert_not_called()
            prep.assert_not_called()
