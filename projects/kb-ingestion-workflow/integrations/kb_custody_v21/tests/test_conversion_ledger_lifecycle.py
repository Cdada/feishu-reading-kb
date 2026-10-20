"""Conversion ledger transactions finish and close before backup observation."""
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from local_document_conversion_mcp.ledger import Ledger


class ConversionLedgerLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='ledger-lifecycle-')
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'conversion.sqlite3'
        self.connections = []
        connect = sqlite3.connect

        def tracked_connect(*args, **kwargs):
            db = connect(*args, **kwargs)
            self.connections.append(db)
            self.addCleanup(db.close)
            return db

        tracked = patch('local_document_conversion_mcp.ledger.sqlite3.connect', side_effect=tracked_connect)
        tracked.start()
        self.addCleanup(tracked.stop)

    def assert_connections_closed(self):
        self.assertTrue(self.connections)
        for db in self.connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                db.execute('SELECT 1')

    def test_initialization_write_and_read_close_connections_and_commit(self):
        ledger = Ledger(self.path)
        ledger.record_document(dict(task_id='synthetic-task', filename='sample.pdf',
            normalized_filename='sample.pdf', sha256='a' * 64, status='candidate',
            candidate_location_label='synthetic candidate', archive_location_label=None,
            updated_at='2026-10-20'))
        rows = ledger.find_documents(normalized_filename='sample.pdf', sha256='a' * 64)
        self.assertEqual([row['task_id'] for row in rows], ['synthetic-task'])
        self.assert_connections_closed()

    def test_failed_transaction_rolls_back_and_closes_connection(self):
        ledger = Ledger(self.path)
        with self.assertRaisesRegex(RuntimeError, 'synthetic interruption'):
            with ledger._connect() as db:
                db.execute('INSERT INTO documents VALUES (?,?,?,?,?,?,?,?)',
                    ('interrupted', 'sample.pdf', 'sample.pdf', 'b' * 64,
                     'candidate', 'synthetic candidate', None, '2026-10-20'))
                raise RuntimeError('synthetic interruption')
        self.assertEqual(ledger.find_documents(normalized_filename='sample.pdf', sha256='b' * 64), [])
        self.assert_connections_closed()
