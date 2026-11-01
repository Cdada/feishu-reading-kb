import json
from types import SimpleNamespace
import unittest

from kb_custody_v21.archive_handoff import ArchiveHandoff, safe_text
from kb_custody_v21.archive_main import MainCards
from kb_custody_v21.daily_card_flow import CardFlow
from kb_custody_v21.daily_scheduler import DailyScheduler
from kb_custody_v21.f05 import file_sha
from kb_custody_v21.registry import encoded
import test_archive_main as main_fixture
import test_daily_card_flow as card_fixture
import test_reading_flow as reading_fixture
from copy import deepcopy


class HandoffTests(unittest.TestCase):
    def case(self):
        f=main_fixture.MainCardTests().case()
        self.addCleanup(f.doCleanups)
        return f

    def test_stable_section_preserves_operator_notes_and_compacts_resolved_status(self):
        f=self.case();handoff=f.backend.main_cards.handoff
        f.store.put('defer:'+f.mid,dict(reason='paused_resources',until=f.store.now()+60))
        facts=handoff.observe(f.store.document(f.mid));identity=facts['id']
        handoff.path.write_text(handoff.path.read_text()+'\n已人工核对：保留原件和现有页，不重复识别。\n')
        f.store.put('defer:'+f.mid,None)
        handoff.observe(f.store.document(f.mid))
        text=handoff.path.read_text()
        self.assertEqual(text.count('## '+identity),1)
        self.assertIn('已人工核对',text)
        self.assertIn('已恢复或已完成',text)
        self.assertEqual(handoff.path.stat().st_mode & 0o777,0o600)
        before=handoff.path.stat().st_mtime_ns
        handoff.observe(f.store.document(f.mid))
        self.assertEqual(before,handoff.path.stat().st_mtime_ns)

    def test_expired_wait_does_not_fabricate_resource_recovery(self):
        f=self.case();handoff=f.backend.main_cards.handoff
        f.store.put('defer:'+f.mid,dict(reason='paused_resources',until=f.store.now()-1))
        # The saved task predates this wait; reaching its due time proves no recovery.
        with f.store.transaction() as db:db.execute('UPDATE daily_documents SET updated=? WHERE message_id=?',(f.store.now()-10,f.mid))
        self.assertEqual(handoff.observe(f.store.document(f.mid))['category'],'resource')

    def test_damaged_machine_markers_are_reported_without_overwriting_human_text(self):
        f=self.case();handoff=f.backend.main_cards.handoff
        f.store.advance(f.mid,'original_saved','held',reason='合成解析未完成')
        f.backend.main_cards.record_handoffs()
        text=handoff.path.read_text().replace('END MACHINE','BROKEN MACHINE')+'\n人工备注必须保留\n'
        handoff.path.write_text(text)
        f.backend.main_cards.record_handoffs()
        self.assertEqual(handoff.path.read_text(),text)
        self.assertIsNotNone(f.store.get('archive-handoff-error'))
        self.assertEqual(f.store.document(f.mid)['phase'],'held')

    def test_secret_shaped_diagnostic_values_are_removed(self):
        for text in ('api_key=synthetic-sensitive-value', 'Authorization: Bearer synthetic-sensitive-value',
                     'password: synthetic-sensitive-value', 'Bearer synthetic-sensitive-value'):
            self.assertNotIn('synthetic-sensitive-value',safe_text(text))

    def test_status_locates_handoff_without_including_its_body(self):
        from kb_custody_v21.daily_status import local_status,user_summary
        f=self.case();f.store.advance(f.mid,'original_saved','held',reason='合成解析未完成')
        f.backend.main_cards.record_handoffs()
        path=f.backend.main_cards.handoff.path
        path.write_text(path.read_text()+'\n仅供本机人工复核的合成备注\n')
        before=f.store.rows('SELECT * FROM daily_models')
        status=local_status(f.authority.root)
        self.assertEqual(status['archive_handoff']['path'],str(path))
        self.assertNotIn('仅供本机人工复核',encoded(status))
        self.assertNotIn(str(path),user_summary(status))
        self.assertEqual(before,f.store.rows('SELECT * FROM daily_models'))


class MainReadingTests(unittest.TestCase):
    def case(self):
        r=reading_fixture.ReadingFlowTests();r.setUp();self.addCleanup(r.doCleanups)
        f=r.f
        # The fixture seeds a synthetic new-task flag before its first card.
        document=f.store.document(f.mid)
        with f.store.transaction() as db:
            db.execute('UPDATE daily_documents SET data=? WHERE message_id=?',
                (encoded(dict(document['data'],archive_main=True)),f.mid))
        f.transport=card_fixture.CardTransport(f.transport);r.b.transport=f.transport
        r.b.card_flow=CardFlow(r.b);r.b.card_listener=SimpleNamespace(ready=True)
        r.b.main_cards=MainCards(r.b)
        raw=f.g.f.message;path=f.root/'synthetic-main-original.local.json';path.write_text(encoded(raw))
        proof=dict(value=dict(code=0,data=dict(items=[raw])),receipt=str(path),evidence={str(path):file_sha(path)})
        r.b.collector.observed=lambda mid:(raw,proof)
        r.b.main_cards.report_one(f.mid,force=True)
        return r

    def test_complete_reading_and_review_update_one_card_without_stage_texts(self):
        r=self.case();r.approve();seen=[]
        for _ in range(8):
            r.b.main_cards.record_handoffs()
            r.b.main_cards.report_one(r.f.mid,force=True)
            seen.append(r.b.main_cards.state(r.f.mid)['binding']['details'])
            r.flow.report()
            if r.document()['phase']!='reading':break
            r.step()
        r.b.advance_light(r.document())
        self.assertEqual(r.document()['phase'],'notified')
        state=r.b.main_cards.state(r.f.mid)
        self.assertIn('知识整理已完成',state['binding']['details'])
        self.assertIn('未核验问答索引',state['binding']['details'])
        self.assertTrue(any('独立复核' in t for t in seen))
        self.assertEqual(len(r.f.transport.messages),1)
        self.assertEqual(r.daily.summary()['model_turns'],3)
        self.assertFalse(any(op.startswith(('notice:reading-progress:','notice:published:')) for op,_ in r.f.transport.calls))
        r.b.advance_light(r.document())
        self.assertEqual(len(r.wire.calls),2)

    def test_rejected_review_remains_incomplete_with_private_handoff(self):
        r=self.case()
        def transform(purpose,value):
            if purpose=='review':value.update(status='reject',findings=['合成覆盖缺失'])
            return value
        r.wire.transform=transform;r.approve()
        for _ in range(5):
            if r.document()['phase']=='held':break
            DailyScheduler._isolated(SimpleNamespace(store=r.daily),r.document(),r.b.advance_heavy)
        r.b.main_cards.record_handoffs();r.b.main_cards.report_one(r.f.mid,force=True)
        self.assertEqual(r.document()['phase'],'held')
        self.assertFalse(r.document()['data'].get('reading_completed'))
        text=r.b.main_cards.state(r.f.mid)['binding']['details']
        self.assertIn('复核未通过',text);self.assertIn('KB-EX-',text)
        report=r.b.main_cards.handoff.path.read_text()
        self.assertIn('暂无通用自动恢复入口',report)
        self.assertNotIn('completion_notice',r.document()['data'])
        self.assertEqual(len(r.wire.calls),2)

    def test_unknown_model_result_is_visible_and_not_replayed(self):
        r=self.case();r.approve();r.step();r.wire.fail=True
        for _ in range(2):DailyScheduler._isolated(SimpleNamespace(store=r.daily),r.document(),r.b.advance_heavy)
        r.b.main_cards.record_handoffs()
        report=r.b.main_cards.handoff.path.read_text()
        self.assertIn('unknown',report)
        self.assertIn('先查回执',report)
        self.assertEqual(len(r.wire.calls),1)
        self.assertEqual(r.daily.summary()['model_turns'],2)

    def test_completed_tasks_do_not_delay_the_next_main_card(self):
        r=self.case();r.approve();r.finish()
        main=r.b.main_cards;main.report_one(r.f.mid,force=True)
        r.daily.archive_cards_enabled=True
        raw=deepcopy(r.f.g.f.message);raw['message_id']='om_next_main'
        with r.daily.transaction() as db:r.daily._observe(db,raw)
        seen=[];main.report_one=lambda mid:seen.append(mid)
        main.report()
        self.assertEqual(seen,['om_next_main'])
