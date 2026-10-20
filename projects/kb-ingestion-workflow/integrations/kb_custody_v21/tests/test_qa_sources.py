"""Use the synthetic full ingestion fixture to verify original-only access."""
import unittest

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.qa_sources import QASources
from kb_custody_v21.qa_index import QAIndex
import test_reading_flow as reading_fixture


class QASourceTests(unittest.TestCase):
    def setUp(self):
        self.f = reading_fixture.ReadingFlowTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.sources = QASources(self.f.a, self.f.daily)

    def test_unapproved_and_unfinished_are_not_searchable(self):
        self.assertEqual(self.sources.ids(), [])
        self.f.approve()
        self.assertEqual(self.sources.ids(), [])

    def test_completed_original_is_bound_and_revalidated(self):
        self.f.approve(); self.f.finish()
        key = self.sources.ids()[0]
        source = self.sources.get(key)
        index = QAIndex(self.f.a.root/'qa-index.sqlite3')
        index.sync(self.sources)
        hits = index.search(source['text'])
        self.assertTrue(hits); self.assertTrue(self.sources.verify(hits))
        path = self.f.a.reading.root/source['source_path']
        path.write_text('篡改后的正文')
        with self.assertRaises(ContractError): self.sources.verify(hits)

    def test_reviewed_input_cannot_escape_its_material_directory(self):
        self.f.approve(); self.f.finish()
        key = self.sources.ids()[0]
        import json
        m = self.f.flow.store.get(key); m['detail']['input_path'] = '/private/tmp/unbound-input.json'
        with self.f.daily.transaction() as db:
            db.execute('UPDATE reading_materials SET detail=? WHERE material_id=?', (json.dumps(m['detail']),key))
        with self.assertRaises((ContractError,FileNotFoundError)): self.sources.get(key)
