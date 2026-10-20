import json
from pathlib import Path
import unittest

from pypdf import PdfReader
from kb_custody_v21.contracts import ContractError
from kb_custody_v21.f01 import encoded
from kb_custody_v21.image_availability import availability
from kb_custody_v21.pilot_service import service_limits
from test_pilot_decision import owner_reply
from test_embedded_markdown import embedded
from test_markdown_bundle import png
import test_pilot_service as fixture
from test_pilot_three_formats import profile


class ImageChoiceServiceTests(unittest.TestCase):
    def fixture(self, *, command=None, reply_mutate=lambda r: None, raw=None):
        f = fixture.ServiceTests(); f.setUp(); self.addCleanup(f.doCleanups)
        profile(f.g)
        f.g.path.write_text(encoded(f.g.plan))
        from kb_custody_v21.f05 import file_sha
        f.f.frozen[str(f.g.path)] = file_sha(f.g.path)
        f.f.approval['new_material_guide']['plan_sha256'] = file_sha(f.g.path)
        f.plan.update(reference='P30-image-choice-service-v1', max_scans=4)
        f.save()
        f.f.approval['limits'] = service_limits(2, 4, image_choices=True)
        f.i.save_approval()
        first = f.messages['om_synthetic']
        f.binaries['file_synthetic'] = raw or ('# Partial\n\nSynthetic body.\n\n' + embedded(png()) + '\n\n![missing](absent.png)\n').encode()
        f.f.pages = [dict(items=[first], has_more=False)] + [dict(items=[], has_more=False)] * 3
        sent = False
        def choose():
            nonlocal sent
            waiting = [m for m in f.run.items.values() if m['state'] == 'awaiting_image_choice']
            if waiting and command and not sent:
                item = waiting[0]
                reply = dict(owner_reply(), message_id='om_image_choice', parent_id=item['image_warning_message_id'],
                    body=dict(content=encoded(dict(text=command))))
                reply_mutate(reply)
                f.messages[reply['message_id']] = reply
                f.f.pages = [dict(items=[reply], has_more=False)] + [dict(items=[], has_more=False)] * 2
                sent = True
        f.after_wait = choose
        return f

    def test_no_choice_keeps_original_and_does_not_generate_on_timeout(self):
        f = self.fixture()
        result = f.make().run()
        item = result['materials'][0]
        self.assertEqual(item['state'], 'awaiting_image_choice')
        self.assertEqual(f.g.wire.calls, 0)
        self.assertFalse(f.registry.rows('SELECT * FROM publications_v21'))
        self.assertEqual(f.intake.status(item['entry_id'])['original_status'], 'accepted')

    def test_continue_retains_available_images_and_discloses_missing_before_publication(self):
        f = self.fixture(command='按缺图版本继续')
        result = f.make().run(); item = result['materials'][0]
        self.assertEqual(item['state'], 'awaiting_owner_decision')
        self.assertEqual(f.g.wire.calls, 1)
        self.assertFalse(item['published'])
        self.assertFalse(f.registry.rows('SELECT * FROM publication_confirmations'))
        row = f.run.pipeline.previews.publisher._row(item['publication_id'])
        tree = f.run.pipeline.previews.publisher._tree(row, item['publication_id'] + '.candidate')
        self.assertEqual(len(list((tree/'提取/images').iterdir())), 1)
        self.assertEqual(next((tree/'提取/images').iterdir()).read_bytes(), png())
        self.assertIn('【缺图', (tree/'提取/source.md').read_text())
        self.assertEqual(json.loads((tree/'发布记录/缺图处理决定.json').read_bytes())['choice'], 'continue_without_missing')
        pdf_text = ''.join(p.extract_text() for p in PdfReader(tree/'发布记录/已确认入库预览.pdf').pages)
        self.assertIn('1 处图片缺失或无法读取', pdf_text)
        for category, count in f.counts().items():
            self.assertLessEqual(count, f.session.limits[category])

    def test_resend_stops_old_version_without_spending_a_model_turn(self):
        f = self.fixture(command='重新发文件')
        result = f.make().run()
        self.assertEqual(result['materials'][0]['state'], 'awaiting_replacement_file')
        self.assertEqual(f.g.wire.calls, 0)
        self.assertFalse(f.registry.rows('SELECT * FROM publications_v21'))

    def test_wrong_reply_or_foreign_sender_cannot_authorize_partial_generation(self):
        mutations = [lambda r: r.update(parent_id='om_wrong'),
                     lambda r: r['sender'].update(id='ou_other')]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                f = self.fixture(command='按缺图版本继续', reply_mutate=mutation)
                result = f.make().run()
                self.assertEqual(result['materials'][0]['state'], 'awaiting_image_choice')
                self.assertEqual(f.g.wire.calls, 0)

    def test_malformed_reply_gets_one_correction_without_generation(self):
        f = self.fixture(command='@按缺图版本继续')
        result = f.make().run(); item = result['materials'][0]
        self.assertEqual(item['state'], 'awaiting_image_choice')
        self.assertTrue(item['image_correction_notice'])
        self.assertEqual(f.g.wire.calls, 0)

    def test_changed_receipt_invalidates_saved_choice(self):
        f = self.fixture(command='按缺图版本继续')
        item = f.make().run()['materials'][0]
        decision = item['image_decision']
        Path(next(iter(decision['evidence']['receipts']))).write_text('{}')
        with self.assertRaisesRegex(ContractError, 'receipt changed'):
            availability(f.intake.conversion, decision['binding']['extraction_id'])

    def test_continue_then_separate_pdf_confirmation_publishes_once(self):
        f = self.fixture(command='按缺图版本继续')
        choose = f.after_wait
        def confirm():
            choose()
            if f.deliveries:
                f.f.pages = [dict(items=f.replies(), has_more=False)] * 2
        f.after_wait = confirm
        item = f.make().run()['materials'][0]
        self.assertTrue(item['published'])
        self.assertEqual(item['notification']['delivery_state'], 'delivered')
        self.assertEqual(f.g.wire.calls, 1)
        self.assertEqual(len(f.registry.rows('SELECT * FROM publication_confirmations')), 1)
        for category, count in f.counts().items():
            self.assertLessEqual(count, f.session.limits[category])

    def test_two_conflicting_choices_stop_before_model(self):
        f = self.fixture(command='按缺图版本继续')
        choose = f.after_wait
        def conflict():
            choose()
            if 'om_image_choice' in f.messages:
                reply = dict(f.messages['om_image_choice'], message_id='om_conflict',
                    body=dict(content=encoded(dict(text='重新发文件'))))
                f.messages[reply['message_id']] = reply
                f.f.pages[0]['items'].append(reply)
        f.after_wait = conflict
        with self.assertRaisesRegex(ContractError, 'conflicting image choices'):
            f.make().run()
        self.assertEqual(f.g.wire.calls, 0)

    def test_changed_detail_cannot_reinterpret_the_listed_choice(self):
        f = self.fixture(command='按缺图版本继续')
        def change(control):
            if control['kind'] == 'message' and control['path'].endswith('/om_image_choice'):
                f.messages['om_image_choice']['body'] = dict(content=encoded(dict(text='重新发文件')))
        f.failure = change
        with self.assertRaisesRegex(ContractError, 'exact warning and observed'):
            f.make().run()
        self.assertEqual(f.g.wire.calls, 0)

    def test_pending_check_survives_new_approved_window_without_reconversion(self):
        from kb_custody_v21.pilot_service import continuation_material
        from kb_custody_v21.f05 import file_sha
        f = self.fixture()
        first = f.make().run()['materials'][0]
        f.run.close(); f.session.close()
        binding = continuation_material(f.intake, first['entry_id'])
        original = f.intake.runtime.root / f.registry.entry(first['entry_id'])['relative_path']
        f.f.frozen[str(original)] = file_sha(original)
        f.change_window(); f.plan['previous_materials'] = [binding]; f.save()
        f.f.approval['limits'] = service_limits(2, 4, image_choices=True); f.i.save_approval()
        f.f.pages = [dict(items=[], has_more=False)] * 4
        second = f.make().run()['materials'][0]
        self.assertEqual(first['image_check']['binding'], second['image_check']['binding'])
        self.assertEqual(second['state'], 'awaiting_image_choice')
        self.assertEqual(len(f.registry.rows('SELECT * FROM conversion_bindings')), 1)
        self.assertEqual(f.counts()['download'], 0)
        self.assertEqual(f.g.wire.calls, 0)

    def test_new_plan_requires_separately_bounded_choice_budget(self):
        f = self.fixture()
        f.f.approval['limits'] = service_limits(2, 4)
        f.i.save_approval()
        with self.assertRaisesRegex(ContractError, 'exact category budget'):
            f.make()
        with self.assertRaisesRegex(ContractError, '100 HTTP'):
            service_limits(3, 8, image_choices=True)


if __name__ == '__main__':
    unittest.main()
