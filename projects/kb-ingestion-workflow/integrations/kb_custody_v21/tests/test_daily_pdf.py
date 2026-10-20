"""Synthetic PDF through actual daily custody, guide, confirmation and publish."""
from dataclasses import asdict
import json
from types import SimpleNamespace
import unittest

from kb_custody_v21.daily_backend import DailyBackend
from kb_custody_v21.daily_policy import DailyPolicy
from kb_custody_v21.daily_scheduler import DailyScheduler, MaterialProblem
from kb_custody_v21.daily_store import DailyStore
from kb_custody_v21.daily_pdf_recovery import resume_repaired_pdf, stopped_service
from kb_custody_v21.contracts import ContractError
from kb_custody_v21.f05 import file_sha
from kb_custody_v21.mineru_chunks import PdfOptions
from kb_custody_v21.registry import encoded
from pilot_pipeline_fixture import ready
from test_daily_backend import LocalTransport
import test_daily_backend as daily_fixture
from test_mineru_chunks import SyntheticMineru, pdf_bytes
import test_pilot_inbox_guide as fixture


class DailyPdfTests(unittest.TestCase):
    owner_reply = daily_fixture.DailyBackendTests.owner_reply
    advance = daily_fixture.DailyBackendTests.advance
    preview = daily_fixture.DailyBackendTests.preview

    def setUp(self):
        self.g = fixture.InboxGuideTests(); self.g.setUp(); self.addCleanup(self.g.doCleanups)
        self.g.f.binary = pdf_bytes()
        self.g.f.message = dict(self.g.f.message, body=dict(content=encoded(dict(file_key='file_synthetic',file_name='synthetic-paper.pdf'))))
        self.g.f.pages = [dict(items=[self.g.f.message],has_more=False)]
        self.g.plan.update(reference='P33-pdf-guide-v1',formats=['.md','.webarchive','.pdf'],
            policy=dict(reference='P27-preserve-originals-v1',reading='not_requested',max_images=200,max_total_bytes=10_000_000))
        self.g.save()
        self.entry = self.g.accept(convert=False)
        self.registry = self.g.registry
        p = self.g.f.approval
        self.policy = DailyPolicy(p['app_id'],p['chat_id'],p['account_id'],p['bot_id'],4)
        self.root = self.registry.runtime.root/'conversion-work/daily-pdf-tests'; self.root.mkdir()
        self.rule = self.root/'rules.local.json'; self.rule.write_text('{}')
        self.authority = SimpleNamespace(policy=self.policy,root=self.root,path=self.rule,digest=file_sha(self.rule),
            runtime=self.registry.runtime,config=dict(guide_plan=str(self.g.path),text_node='',text_script='',codex_binary='',
                pdf=dict(options=asdict(PdfOptions()),api_binary='',client_binary='',dependency_manifest='')))
        self.authority.check = lambda:dict(self.g.f.frozen)
        self.store = DailyStore(self.root/'queue.sqlite3',self.policy)
        self.transport = LocalTransport(self.authority)
        self.converter = SyntheticMineru()
        self.backend = DailyBackend(self.authority,self.store,registry=self.registry,transport=self.transport,
            resources=ready(self.registry),wire=self.g.wire,pdf_converter=self.converter)
        self.backend.collector.verify_isolation = lambda:None
        raw = self.g.f.message; self.mid = raw['message_id']
        scan = self.store.scan('chat',initial_start=self.store.now()-60)
        self.store.save_page(scan,[raw],has_more=False)
        self.store.advance(self.mid,'registered','original_saved',entry_id=self.entry)
        self.store.bind_source(self.mid,self.registry.entry(self.entry)['sha256'])

    def test_complete_pdf_generates_once_and_original_preview_confirms_once(self):
        document = self.preview()
        self.assertEqual(document['phase'],'awaiting_confirmation')
        self.assertEqual(len(self.converter.calls),2)
        self.assertEqual(self.g.wire.calls,1)
        extraction = document['data']['extraction_id']
        _,mapping,_ = self.backend.intake.conversion.extraction(extraction)
        self.assertEqual(mapping['locator_precision'],'page')
        self.owner_reply(document)
        self.backend.advance_light(document)
        self.assertEqual(self.store.document(self.mid)['phase'],'notified')
        self.assertEqual(len(self.registry.rows('SELECT * FROM publication_confirmations')),1)
        self.assertEqual(len(self.registry.rows('SELECT * FROM publications_v21')),1)
        self.assertEqual(self.store.summary()['model_turns'],1)
        self.assertEqual(self.g.wire.calls,1)

    def test_failed_chunk_holds_only_this_document_without_a_model_reservation(self):
        self.converter.fail = lambda pdf, mode:pdf.stem.startswith('p0006')
        scheduler = DailyScheduler(self.store,self.backend)
        try:
            scheduler._isolated(self.store.document(self.mid),self.backend.advance_heavy)
            self.assertEqual(self.store.document(self.mid)['phase'],'held')
            self.assertEqual(self.store.summary()['model_turns'],0)
            self.assertEqual(self.g.wire.calls,0)
            self.assertEqual(len(self.converter.calls),3)
        finally:
            scheduler.close()

    def test_lifecycle_notices_are_durable_and_not_repeated(self):
        self.backend._mineru_event(dict(id='one',kind='starting',text='synthetic starting',instance='owned',reason=None))
        self.backend._mineru_notices(); self.backend._mineru_notices()
        self.assertEqual([operation for operation,kind in self.transport.calls],['notice:mineru-notice:one'])
        self.assertIsNone(self.store.get('mineru-notice:one'))

    def test_oversized_pdf_is_held_before_reserving_a_model(self):
        self.advance()
        profile = json.loads(self.g.path.read_text()); profile['max_characters'] = 20
        self.g.path.write_text(json.dumps(profile))
        with self.assertRaises(MaterialProblem): self.advance()
        self.assertEqual(self.store.summary()['model_turns'],0)
        self.assertEqual(self.g.wire.calls,0)

    def test_operator_repair_then_normal_flow_does_not_repeat_conversion(self):
        self.converter.fail = lambda pdf,mode:pdf.stem.startswith('p0006')
        scheduler = DailyScheduler(self.store,self.backend)
        scheduler._isolated(self.store.document(self.mid),self.backend.advance_heavy)
        scheduler.close()
        raw = self.root/'repair'
        SyntheticMineru().convert(self.root/'p0006-0007.pdf',raw,mode='ocr')
        with stopped_service(self.authority), self.assertRaises(ContractError):
            resume_repaired_pdf(self.authority,self.store,self.registry,self.mid,
                                operator_ref='synthetic',first_page=6,raw_output=raw)
        result = resume_repaired_pdf(self.authority,self.store,self.registry,self.mid,
                                    operator_ref='synthetic',first_page=6,raw_output=raw)
        self.assertEqual(result['state'],'ready_to_resume')
        self.backend.stop.clear()
        document = self.preview()
        self.assertEqual(document['phase'],'awaiting_confirmation')
        self.assertEqual(len(self.converter.calls),3)
        self.assertEqual(self.store.summary()['model_turns'],1)
        with self.assertRaises(ContractError):
            resume_repaired_pdf(self.authority,self.store,self.registry,self.mid,operator_ref='cannot reset started model')

    def test_repaired_first_chunk_allows_later_unattempted_pages_to_continue(self):
        self.converter.fail = lambda pdf,mode:pdf.stem.startswith('p0001')
        scheduler = DailyScheduler(self.store,self.backend)
        scheduler._isolated(self.store.document(self.mid),self.backend.advance_heavy)
        scheduler.close()
        raw = self.root/'repair-first'
        SyntheticMineru().convert(self.root/'p0001-0005.pdf',raw,mode='ocr')
        # Reuse retained output after fixing a parser rejection, without OCR.
        content_path = raw/'paper_content_list.json'
        items = json.loads(content_path.read_text())
        items.insert(0,dict(type='header',text='Synthetic journal heading',page_idx=0))
        items.append(dict(type='footer',text='Synthetic publication details',page_idx=4))
        content_path.write_text(json.dumps(items))
        result = resume_repaired_pdf(self.authority,self.store,self.registry,self.mid,
                                    operator_ref='synthetic',first_page=1,raw_output=raw)
        self.assertEqual(result['state'],'ready_to_resume')
        self.assertEqual(len(self.converter.calls),2)
        self.converter.fail = None
        self.backend.stop.clear()
        self.preview()
        self.assertEqual(self.converter.calls[-1],('p0006-0007','auto'))
        self.assertEqual(len(self.converter.calls),3)


if __name__ == '__main__': unittest.main()
