"""Late integrity/frozen-input changes must not publish experiment success."""
from contextlib import ExitStack, redirect_stdout
import copy
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

from kb_custody_v21.baidu import RemoteFailure
from kb_custody_v21.baidu_protocol import JSON_LIMIT, json_object
from kb_custody_v21.contracts import ContractError
from kb_custody_v21.f05 import CliProcess, file_sha
from kb_custody_v21.staging import IntegrityError
from b01_fixture import callbacks
import test_b01
import test_f05
import test_f06
import f05_execute
import f06_execute


class FinalizationTests(unittest.TestCase):
    def seed(self, cls):
        seed = cls()
        seed.setUp()
        self.addCleanup(seed.doCleanups)
        return seed

    def corrupt_original_at(self, request_number):
        seed = self.seed(test_b01.B01Tests)
        original_request = seed.wire.request
        entry_id = seed.a['fixture_binding']['entry_id']
        entry = seed.registry.entry(entry_id)
        original = seed.registry.runtime.root / entry['relative_path']
        def request(req):
            result = original_request(req)
            if len(seed.calls) == request_number:
                original.write_bytes(b'synthetic-damaged-original')
            return result
        seed.wire.request = request
        experiment = seed.experiment()
        result = experiment.run(**callbacks())
        self.assertEqual(result['status'], 'STOPPED')
        self.assertEqual(result['error_type'], 'IntegrityError')
        self.assertEqual(len(seed.calls), request_number)
        entry = seed.registry.entry(entry_id)
        self.assertEqual(entry['status'], 'accepted')
        self.assertEqual(entry['custody_health'], 'suspect')
        self.assertEqual(seed.registry.rows('SELECT cleanup_hold FROM staging_files')[0]['cleanup_hold'], 1)
        op = experiment.archive.operation(experiment.operation_id)
        self.assertEqual(op['error_code'], 'IntegrityError')
        self.assertIsNotNone(op['finished_at'])
        loc = seed.registry.rows('SELECT * FROM asset_locations')[0]
        self.assertEqual(loc['integrity_hold'], 1)
        self.assertEqual(loc['availability'], 'unknown')
        self.assertEqual(loc['verification_level'], 'none')
        window = seed.registry.rows('SELECT * FROM archive_windows')[0]
        self.assertEqual(window['stopped'], 1)
        self.assertEqual(window['used_calls'], request_number - 3)
        attempts = seed.registry.rows('SELECT * FROM archive_attempts ORDER BY started_at, rowid')
        self.assertTrue(all(a['finished_at'] is not None for a in attempts))
        self.assertEqual(attempts[-1]['outcome'], 'integrity_error')
        with self.assertRaises((IntegrityError, ContractError, FileExistsError)):
            seed.experiment().run(**callbacks())
        self.assertEqual(len(seed.calls), request_number)
        return op

    def test_b01_last_filemetas_corruption_finalizes_unknown_and_never_restarts_upload(self):
        self.assertEqual(self.corrupt_original_at(13)['state'], 'unknown')

    def test_b01_preflight_corruption_without_file_write_intent_finalizes_failed(self):
        self.assertEqual(self.corrupt_original_at(4)['state'], 'failed')

    def test_integer_digit_limit_is_sanitized_without_disabling_the_limit(self):
        limit = sys.get_int_max_str_digits()
        self.assertGreater(limit, 0, 'boundary test requires the interpreter safety limit')
        payload = b'{"n":' + b'9' * (limit + 1) + b'}'
        self.assertLess(len(payload), JSON_LIMIT)
        with self.assertRaisesRegex(RemoteFailure, '^invalid HTTP JSON response$'):
            json_object(payload)
        self.assertEqual(sys.get_int_max_str_digits(), limit)
        self.assertEqual(json_object(b'{"n":' + b'9' * limit + b'}')['n'] > 0, True)
        with self.assertRaisesRegex(RemoteFailure, '^duplicate JSON field$'):
            json_object(b'{"n":1,"n":2}')

    def test_f05_final_reply_cannot_bypass_frozen_evidence_check(self):
        seed = self.seed(test_f05.F05Tests)
        client = seed.make()
        def hook(control, value):
            if control.get('key') == 'notification-1':
                (seed.prior/'result.local.json').write_text('{}')
        seed.hook = hook
        with self.assertRaises(ContractError):
            client.run()
        self.assertEqual(seed.runner.call_count, 6)

    def test_f06_final_reply_stops_but_keeps_confirmed_delivery(self):
        seed = self.seed(test_f06.F06Tests)
        client = seed.make()
        def hook(control, value):
            if control.get('key') == 'notification-0':
                (seed.base/'f05-requests/result.local.json').write_text('{}')
        seed.hook = hook
        with self.assertRaises(ContractError):
            client.run()
        self.assertEqual(seed.runner.call_count, 6)
        self.assert_delivery_preserved(seed)

    def assert_delivery_preserved(self, seed):
        self.assertEqual(seed.registry.rows('SELECT delivery_state FROM notification_outbox')[0]['delivery_state'], 'delivered')
        self.assertEqual(len(seed.registry.rows('SELECT * FROM notification_attempts')), 1)

    def test_cli_binary_changed_during_process_is_rejected_after_return(self):
        seed = self.seed(test_f05.F05Tests)
        binary = seed.root/'synthetic-guard'
        binary.write_bytes(b'synthetic-binary')
        process = CliProcess(binary, file_sha(binary))
        def finished(*args, **kwargs):
            binary.write_bytes(b'changed-binary')
            return Mock(returncode=0, stdout=b'{"ok":true,"mode":"profile"}')
        control = dict(mode='profile', expires_at=seed.approval['expires_at'])
        with patch('kb_custody_v21.f05.subprocess.run', side_effect=finished) as run:
            with self.assertRaises(ContractError):
                process(control)
            self.assertEqual(run.call_count, 1)

    def execute_final_gate(self, kind, change):
        seed = self.seed(test_f05.F05Tests if kind == 'f05' else test_f06.F06Tests)
        module = f05_execute if kind == 'f05' else f06_execute
        client = seed.make()
        # A new synthetic workspace: no live approval, executable or credentials.
        worktree = seed.root/'entry-worktree'
        base = worktree/'.dev-runtime/kb-v21/real-validation/f01-v1'
        base.mkdir(parents=True)
        binary = worktree/f'.dev-runtime/kb-v21/{kind}-cli-guard'
        binary.write_bytes(b'synthetic-binary')
        source_file = worktree/'source.py'
        source_file.write_text('synthetic_source = True\n')
        source_hash = file_sha(source_file)
        approval = copy.deepcopy(seed.approval)
        approval['cli_guard_sha256'] = file_sha(binary)
        if kind == 'f06':
            approval['runtime_relative'] = module.RUNTIME
        approval_path = base/f'{kind}-approval.local.json'
        approval_path.write_text(json.dumps(approval))
        def checked_source(root, approved):
            if file_sha(source_file) != source_hash:
                raise ContractError('synthetic source changed')
            return dict(head='synthetic-head', files={'source.py': source_hash})
        evidence = (seed.prior/'result.local.json' if kind == 'f05' else
                    seed.base/'f05-requests/result.local.json')
        original_run = client.run
        def completed():
            result = original_run()
            targets = dict(source=source_file, approval=approval_path, binary=binary, evidence=evidence)
            if change in targets:
                targets[change].write_text('changed after observations returned')
            return result
        original_check_output = module.subprocess.check_output
        entry_query = ['git', '-C', str(module.PACKAGE), 'rev-parse', '--show-toplevel']
        def check_output(command, *args, **kwargs):
            if command == entry_query:
                return str(worktree)
            return original_check_output(command, *args, **kwargs)
        with ExitStack() as stack:
            stack.enter_context(patch.object(module.subprocess, 'check_output', side_effect=check_output))
            stack.enter_context(patch.object(module, 'checked_source', side_effect=checked_source))
            stack.enter_context(patch.object(module, 'NotificationReadExperiment' if kind == 'f05' else 'OutboxExperiment', return_value=client))
            if kind == 'f06':
                stack.enter_context(patch.object(module, 'Registry', return_value=seed.registry))
                stack.enter_context(patch.object(module, 'Runtime'))
            stack.enter_context(patch.object(client, 'run', side_effect=completed))
            stack.enter_context(redirect_stdout(io.StringIO()))
            code = module.execute()
        result = json.loads((client.root/'result.local.json').read_text())
        self.assertEqual(seed.runner.call_count, 6)
        self.assertEqual(sum(result['physical_requests'].values()), 10)
        self.assertEqual(result['unfinished_requests'], 0)
        if change == 'none':
            self.assertEqual(code, 0)
            self.assertEqual(result['status'], 'KNOWN_NOTIFICATION_CONTENT_VERIFIED' if kind == 'f05' else 'NORMAL_OUTBOX_VERIFIED')
        else:
            self.assertEqual(code, 1)
            self.assertEqual(result['status'], 'STOPPED')
            self.assertEqual(result['final_validation_error_type'], 'ContractError')
        if kind == 'f06':
            self.assert_delivery_preserved(seed)
            self.assertEqual(result['outbox_state'], 'delivered')
        self.assertTrue(all(result['observations'].values()))

    def test_f05_before_result_write_rechecks_source_approval_binary_and_evidence(self):
        for change in ('none', 'source', 'approval', 'binary', 'evidence'):
            with self.subTest(change=change):
                self.execute_final_gate('f05', change)

    def test_f06_before_result_write_rechecks_source_approval_binary_and_evidence(self):
        for change in ('none', 'source', 'approval', 'binary', 'evidence'):
            with self.subTest(change=change):
                self.execute_final_gate('f06', change)
