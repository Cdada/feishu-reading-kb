"""Full new-format/main-card/compact-input integration with synthetic I/O."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from kb_custody_v21.archive_main import MainCards
from kb_custody_v21.document_formats import OFFICE_FORMATS, OFFICE_PROFILE
from kb_custody_v21.f05 import file_sha
from kb_custody_v21.qa_index import QAIndex
from kb_custody_v21.qa_sources import QASources
from kb_custody_v21.reading_input_storage import FORMAT
from kb_custody_v21.daily_transport import durable_file
from kb_custody_v21.registry import encoded
import test_archive_reading_pipeline as pipeline_fixture
import test_office_documents as office_fixture
import test_reading_flow as reading_fixture


class FormatReleaseTests(unittest.TestCase):
    def complete(self, extension, raw):
        f = pipeline_fixture.ArchiveReadingPipelineTests()
        f.setUp(); self.addCleanup(f.doCleanups)
        f.g.plan.update(reference=OFFICE_PROFILE, formats=OFFICE_FORMATS,
                        max_characters=20000, max_body_bytes=60000)
        f.g.save()
        f.original['body']['content'] = encoded(dict(file_key='file_synthetic', file_name='synthetic'+extension))
        f.transport.messages[f.mid] = f.original
        request = f.transport.request
        def receive(operation, kind, path, **kwargs):
            result = request(operation, kind, path, **kwargs)
            if kind == 'download':
                receipt = Path(result['receipt'])
                receipt.write_bytes(raw)
                result['value'].update(sha256=file_sha(receipt), size_bytes=len(raw))
                result['evidence'] = {str(receipt):file_sha(receipt)}
            return result
        f.transport.request = receive
        f.store.archive_cards_enabled = True
        f.authority.config['archive_cards'] = dict(reference='archive-main-v1', enabled=True)
        f.backend.main_cards = MainCards(f.backend)
        f.mention('new')
        f.backend.main_cards.report_one(f.mid, force=True)
        card_id = f.backend.main_cards.state(f.mid)['card_id']
        for _ in range(8):
            if f.document()['phase'] == 'awaiting_confirmation': break
            f.backend.advance_heavy(f.document())
            f.backend.main_cards.report_one(f.mid, force=True)
        self.assertEqual(f.document()['phase'], 'awaiting_confirmation')
        self.assertFalse(f.authority.reading.root.exists())
        self.assertEqual(f.store.summary()['model_turns'], 1)
        f.backend.advance_light(f.document())
        state = f.backend.main_cards.state(f.mid)
        decision = f.backend.card_flow.cards.get(state['binding']['decision']['card_id'])
        root = f.backend.card_flow.cards.get(card_id)
        f.decide(dict(decision, message_id=root['message_id']), '确认入库')
        for _ in range(8):
            if f.document()['phase'] != 'reading': break
            f.backend.advance_heavy(f.document())
            f.backend.main_cards.report_one(f.mid, force=True)
        self.assertEqual(f.document()['phase'], 'published')
        f.backend.advance_light(f.document())
        f.backend.main_cards.report_one(f.mid, force=True)
        self.assertEqual(f.document()['phase'], 'notified')
        self.assertEqual(f.backend.main_cards.state(f.mid)['card_id'], card_id)
        self.assertEqual(len(f.store.rows("SELECT 1 FROM daily_cards WHERE purpose='archive_main'")), 1)
        self.assertEqual(f.store.summary()['model_turns'], 3)
        self.assertEqual([r['purpose'] for r in f.reading_wire.calls], ['organize', 'review'])
        group = f.authority.reading.root/f.document()['data']['reading_group']
        self.assertEqual((group/('入库依据/原件/source'+extension)).read_bytes(), raw)
        self.assertTrue((group/'综合读书笔记.md').is_file())
        sources = QASources(f.authority, f.store)
        key = sources.ids()[0]
        material = f.backend.reading_flow.store.get(key)
        saved = json.loads(Path(material['detail']['input_path']).read_bytes())
        self.assertEqual(saved['format'], FORMAT)
        source = sources.get(key)
        self.assertEqual(source['text'].encode(), (group/'原文.md').read_bytes())
        index = QAIndex(f.root/'integration-index.sqlite3'); index.sync(sources)
        query = '资料保留' if extension == '.txt' else '合成甲区'
        hits = index.search(query)
        self.assertTrue(hits)
        self.assertTrue(sources.verify(hits))
        if extension in ('.docx', '.xlsx'):
            self.assertTrue((group/'表格目录.md').is_file())
            self.assertTrue(any(h.get('table_context') for h in hits))
        before = f.library_snapshot()
        f.mention('after-completion')
        self.assertEqual(f.store.summary()['model_turns'], 3)
        self.assertEqual(f.library_snapshot(), before)

    def test_txt_main_card_to_reviewed_searchable_original(self):
        self.complete('.txt', '合成资料保留正文与换行。\r\n不得凭空补充数据。'.encode('utf-16'))

    def test_docx_main_card_to_reviewed_tables_and_search(self):
        self.complete('.docx', office_fixture.word())

    def test_xlsx_main_card_to_reviewed_tables_and_search(self):
        self.complete('.xlsx', office_fixture.excel())

    def test_waiting_clarification_survives_new_deployment_without_replay(self):
        f = reading_fixture.ReadingFlowTests(); f.setUp(); self.addCleanup(f.doCleanups)
        question = f.flow._question
        retained = {}
        def switch_after_question(*args):
            question(*args)
            retained.update({p:p.read_bytes() for p in f.a.root.glob('reading-jobs/*/model/*/*.local.json')})
            f.a.digest = 'b'*64
            f.f.g.plan.update(reference=OFFICE_PROFILE, formats=OFFICE_FORMATS)
            f.f.g.save()
        f.flow._question = switch_after_question
        f.test_question_keeps_original_and_resumes_only_from_owner_reply()
        self.assertTrue(retained)
        self.assertTrue(all(p.read_bytes() == raw for p,raw in retained.items()))
        self.assertEqual([r['purpose'] for r in f.wire.calls], ['organize','organize','review'])

    def test_preexisting_unpacked_reading_snapshot_remains_searchable(self):
        f = reading_fixture.ReadingFlowTests(); f.setUp(); self.addCleanup(f.doCleanups)
        with patch('kb_custody_v21.reading_flow.save_input', side_effect=durable_file):
            f.approve(); f.finish()
        sources = QASources(f.a, f.daily)
        source = sources.get(sources.ids()[0])
        self.assertTrue(source['text'])
        self.assertEqual(f.daily.summary()['model_turns'], 3)


if __name__ == '__main__': unittest.main()
