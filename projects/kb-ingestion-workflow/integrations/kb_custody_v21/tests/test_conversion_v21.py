import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kb_custody_v21.contracts import ContractError, ENTRY_SCHEMA
from kb_custody_v21.conversion import CustodyConversion
from kb_custody_v21.intake import FeishuMockIntake, MockAttachment
from kb_custody_v21.notifications import OutboxWorker
from kb_custody_v21.registry import Registry
from kb_custody_v21.runtime import Runtime, DEFAULTS
from kb_custody_v21.staging import Staging, IntegrityError
from test_custody import WORKTREE, BASE


class ConversionTests(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp(prefix='conversion-', dir=BASE / 'test-output'))
        self.runtime = Runtime(WORKTREE, DEFAULTS, root)
        self.registry = Registry(self.runtime)
        self.staging = Staging(self.registry)
        self.intake = FeishuMockIntake(self.registry, self.staging,
                                      OutboxWorker(self.registry, self.runtime.make_sender()))
        self.conversion = CustodyConversion(self.registry)

    def receive(self, data=None, **overrides):
        self.content = data or '# 合成资料\n\n城市😀研究。\n第二段。\n'.encode()
        contract = dict(schema_version=ENTRY_SCHEMA, intake_key='synthetic-conversion',
                        entry_key='1', received_filename='synthetic.md')
        contract.update(overrides)
        return self.intake.receive(contract, MockAttachment(self.content))['entry_id']

    def convert(self, entry, key='convert-0001', action='not_applicable'):
        inspected = self.conversion.inspect(entry)
        task = self.conversion.submit(inspected['inspection_id'], key, action)
        self.conversion.worker.run_once()
        row = self.conversion.service.ledger.get_task(task['task_id'])
        self.assertEqual(row['status'], 'ready_candidate', row)
        return task, json.loads(row['quality_json'])['extraction']

    def test_actual_worker_from_accepted_original_without_inbound(self):
        entry = self.receive()
        # The mock download object is gone before conversion. The existing real
        # worker executes prepare/process/check, not a canned conversion result.
        with patch.object(self.conversion.worker, '_resolve_source', side_effect=AssertionError('path fallback')):
            task, mapping = self.convert(entry)
        row, actual, root = self.conversion.extraction(mapping['extraction_id'])
        self.assertEqual((root / 'source.md').read_bytes(), self.content)
        self.assertEqual(mapping['text_length'], len(self.content.decode()))
        self.assertEqual(mapping['output_sha256'], hashlib.sha256(self.content).hexdigest())
        self.assertEqual(mapping['page_mappings'], [])
        self.assertEqual(mapping['locator_precision'], 'document')
        inspection = self.conversion.service.ledger.get_inspection(
            self.conversion.service.ledger.get_task(task['task_id'])['inspection_id'])
        self.assertEqual(inspection['source_relpath'], entry)
        self.assertFalse(self.registry.rows('SELECT * FROM asset_leases WHERE released_at IS NULL'))

    def test_pending_and_damaged_original_never_convert(self):
        entry = self.receive()
        path = self.runtime.root / self.registry.entry(entry)['relative_path']
        path.write_bytes(b'damaged')
        with self.assertRaises(IntegrityError):
            self.conversion.inspect(entry)
        self.assertEqual(self.registry.receipt(entry)['source_staging_status'], 'accepted')

    def test_lease_spans_worker_and_failure_releases(self):
        entry = self.receive()
        inspected = self.conversion.inspect(entry)
        self.conversion.submit(inspected['inspection_id'], 'failure-0001')
        def fail(*args, **kwargs):
            active = self.registry.rows("SELECT * FROM asset_leases WHERE released_at IS NULL AND purpose='conversion'")
            self.assertEqual(len(active), 1)
            raise RuntimeError('synthetic worker failure')
        with patch.object(self.conversion.worker, '_run_stage', side_effect=fail):
            self.conversion.worker.run_once()
        self.assertFalse(self.registry.rows('SELECT * FROM asset_leases WHERE released_at IS NULL'))
        self.assertFalse(self.registry.rows('SELECT * FROM extraction_snapshots'))

    def test_reextract_creates_new_ids_preserving_previous_bytes(self):
        entry = self.receive()
        _, first = self.convert(entry)
        _, second = self.convert(entry, 'convert-0002', 'convert_as_new_version')
        self.assertEqual(first['source_asset_id'], second['source_asset_id'])
        self.assertNotEqual(first['extraction_id'], second['extraction_id'])
        self.assertNotEqual(first['output_asset_id'], second['output_asset_id'])
        self.assertEqual(self.conversion.extraction(first['extraction_id'])[1], first)

    def test_group_waits_for_seal_and_all_required(self):
        group = self.registry.create_group()
        entry = self.receive(group={**group, 'required': True})
        with self.assertRaisesRegex(ContractError, 'sealed group'):
            self.conversion.inspect(entry)

    def test_map_tampering_blocks_read(self):
        entry = self.receive()
        _, mapping = self.convert(entry)
        _, _, root = self.conversion.extraction(mapping['extraction_id'])
        (root / 'extraction-map.json').write_text('{}')
        with self.assertRaisesRegex(ContractError, 'map changed'):
            self.conversion.extraction(mapping['extraction_id'])
