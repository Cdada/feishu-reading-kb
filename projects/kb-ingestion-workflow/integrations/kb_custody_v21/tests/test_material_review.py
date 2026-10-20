from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from pypdf import PdfReader

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.conversion import sha
from kb_custody_v21.material_review import CHECKS, EVENT, MaterialReviews
from kb_custody_v21.pilot_preview import PilotPreviews, PilotScope
from kb_custody_v21.staging import IntegrityError
import test_conversion_v21 as conversion_fixture
import test_pdf_assets as pdf_fixture
import test_publication_v21 as publication_fixture


def report_for(runtime, mapping, *, limitation=False):
    evidence = runtime.root / 'synthetic-review-evidence.txt'
    if not evidence.exists():
        evidence.write_text('Synthetic fixture review: text order, all pages and image positions checked.')
    common = dict(status='passed', note='合成场景逐项检查通过', evidence_refs=['fixture-review'])
    issues = [dict(issue_id='conversion-' + str(i), category='content_review', status='resolved',
                   note=issue, evidence_refs=['fixture-review']) for i, issue in enumerate(mapping['issues'])]
    if limitation:
        issues.append(dict(issue_id='excerpt', category='source_limitation', status='pending_user',
                           note='当前资料为节选，不能代表完整原文。', evidence_refs=['fixture-review']))
    return dict(operator='synthetic-reviewer', checks=[dict(code=code, **common) for code in sorted(CHECKS)],
        pages=[dict(page_index=p['page_index'], **common) for p in mapping['page_mappings']],
        issues=issues, evidence=[dict(evidence_id='fixture-review',relative_path=evidence.name,
                                     sha256=sha(evidence.read_bytes()))])


class MaterialReviewTests(unittest.TestCase):
    setUp = conversion_fixture.ConversionTests.setUp
    receive = conversion_fixture.ConversionTests.receive
    converted = pdf_fixture.PdfSupplementTests.converted

    def ready(self):
        entry, task, mapping = self.converted()
        service = MaterialReviews(self.registry)
        report = report_for(self.runtime, mapping)
        return entry, task, mapping, service, report

    def test_pdf_review_qualifies_exact_extraction_without_rewriting_converter_or_publishing(self):
        entry, task, mapping, reviews, report = self.ready()
        saved = reviews.record(entry, mapping['extraction_id'], report)
        self.assertEqual(saved['qualification'], 'qualified')
        self.assertEqual(saved['owner_choice'], 'pending')
        self.assertEqual(reviews.record(entry, mapping['extraction_id'], report), saved)
        self.assertEqual(self.conversion.service.ledger.get_task(task['task_id']), task)
        self.assertEqual(self.conversion.publication_extraction(mapping['extraction_id'])[1], mapping)
        self.assertFalse(self.registry.rows('SELECT * FROM publications_v21'))
        self.assertFalse(self.registry.rows('SELECT * FROM publication_confirmations'))

    def test_missing_duplicate_or_false_physical_pages_rejected(self):
        entry, _, mapping, reviews, report = self.ready()
        for pages in (report['pages'][:-1], report['pages'] + report['pages'][:1],
                      [dict(report['pages'][0], page_index=True), report['pages'][1]]):
            with self.subTest(pages=pages), self.assertRaises(ContractError):
                reviews.record(entry, mapping['extraction_id'], dict(report, pages=pages))
        self.assertIsNone(reviews.latest(mapping['extraction_id']))

    def test_pending_failed_checks_and_blocking_issues_cannot_qualify(self):
        entry, _, mapping, reviews, report = self.ready()
        previous = None
        for kind in ('pending', 'failed', 'technical_failure', 'serious_content'):
            changed = deepcopy(report)
            if kind in {'pending', 'failed'}:
                changed['checks'][0]['status'] = kind
            else:
                changed['issues'].append(dict(issue_id=kind, category=kind, status='blocked',
                    note='合成阻断问题', evidence_refs=['fixture-review']))
            saved = reviews.record(entry, mapping['extraction_id'], changed, previous_review_id=previous)
            previous = saved['review_id']
            self.assertEqual(saved['qualification'], 'blocked')
            with self.assertRaisesRegex(ContractError, 'quality review'):
                self.conversion.publication_extraction(mapping['extraction_id'])

    def test_converter_reasons_cannot_be_omitted_or_relabelled_as_limitations(self):
        entry, _, mapping, reviews, report = self.ready()
        for issues in ([], [dict(report['issues'][0], category='source_limitation', status='pending_user')]):
            with self.assertRaises(ContractError):
                reviews.record(entry, mapping['extraction_id'], dict(report, issues=issues))

    def test_material_review_cannot_claim_user_acceptance(self):
        entry, _, mapping, reviews, report = self.ready()
        report['issues'].append(dict(issue_id='limitation', category='source_limitation',
            status='accepted', note='cannot accept for owner', evidence_refs=['fixture-review']))
        with self.assertRaises(ContractError):
            reviews.record(entry, mapping['extraction_id'], report)

    def test_unrelated_or_serious_converter_errors_require_repair(self):
        entry, task, mapping, reviews, report = self.ready()
        for changes in ({'serious_issues': 1}, {'warnings': 2}, {'manual_review_count': 2}):
            quality = json.loads(task['quality_json']) | changes
            with self.conversion.service.ledger._connect() as db:
                db.execute('UPDATE tasks SET quality_json=? WHERE task_id=?', (json.dumps(quality),task['task_id']))
            with self.assertRaisesRegex(ContractError, 'repair'):
                reviews.record(entry, mapping['extraction_id'], report)

    def test_new_expression_does_not_inherit_old_review(self):
        entry, _, mapping, reviews, report = self.ready()
        reviews.record(entry, mapping['extraction_id'], report)
        child = pdf_fixture.PdfAssetSupplement(self.registry).prepare(entry, mapping['extraction_id'])['mapping']
        with self.assertRaisesRegex(ContractError, 'quality review'):
            self.conversion.publication_extraction(child['extraction_id'])

    def test_changed_evidence_revokes_qualification_and_cannot_reuse_record(self):
        entry, _, mapping, reviews, report = self.ready()
        reviews.record(entry, mapping['extraction_id'], report)
        (self.runtime.root / report['evidence'][0]['relative_path']).write_text('changed')
        for call in (lambda: reviews.record(entry, mapping['extraction_id'], report),
                     lambda: self.conversion.publication_extraction(mapping['extraction_id'])):
            with self.assertRaisesRegex(ContractError, 'evidence changed'):
                call()

    def test_changed_mapping_cannot_be_qualified(self):
        entry, _, mapping, reviews, report = self.ready()
        _, _, root = self.conversion.extraction(mapping['extraction_id'])
        (root / 'extraction-map.json').write_text('{}')
        with self.assertRaisesRegex(ContractError, 'map changed'):
            reviews.record(entry, mapping['extraction_id'], report)

    def test_changed_source_during_review_preserves_acceptance_and_sets_suspect(self):
        entry, _, mapping, reviews, report = self.ready()
        validate = reviews._validate
        def damage(*args):
            result = validate(*args)
            (self.runtime.root / self.registry.entry(entry)['relative_path']).write_bytes(b'changed')
            return result
        with patch.object(reviews, '_validate', side_effect=damage), self.assertRaises(IntegrityError):
            reviews.record(entry, mapping['extraction_id'], report)
        self.assertEqual(self.registry.receipt(entry)['custody_health'], 'suspect')
        self.assertEqual(self.registry.receipt(entry)['source_staging_status'], 'accepted')
        self.assertIsNone(reviews.latest(mapping['extraction_id']))

    def test_old_success_cannot_replace_new_blocked_review(self):
        entry, _, mapping, reviews, report = self.ready()
        old = reviews.record(entry, mapping['extraction_id'], report)
        blocked = deepcopy(report)
        blocked['checks'][0]['status'] = 'pending'
        new = reviews.record(entry, mapping['extraction_id'], blocked, previous_review_id=old['review_id'])
        with self.assertRaisesRegex(ContractError, 'current review'):
            reviews.record(entry, mapping['extraction_id'], report)
        self.assertEqual(reviews.latest(mapping['extraction_id']), new)

    def test_review_event_failure_does_not_create_eligibility(self):
        entry, _, mapping, reviews, report = self.ready()
        with patch.object(self.registry, 'event', side_effect=OSError('synthetic disk fault')), self.assertRaises(OSError):
            reviews.record(entry, mapping['extraction_id'], report)
        self.assertIsNone(reviews.latest(mapping['extraction_id']))
        with self.assertRaises(ContractError):
            self.conversion.publication_extraction(mapping['extraction_id'])

    def test_missing_evidence_references_and_escaping_paths_rejected(self):
        entry, _, mapping, reviews, report = self.ready()
        for change in ('references', 'path'):
            changed = deepcopy(report)
            if change == 'references':
                changed['checks'][0]['evidence_refs'] = ['missing']
            else:
                changed['evidence'][0]['relative_path'] = '../outside.txt'
            with self.assertRaises(ContractError):
                reviews.record(entry, mapping['extraction_id'], changed)


class MaterialDecisionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = publication_fixture.PublicationTests()
        self.fixture.setUp()
        self.registry, self.runtime = self.fixture.registry, self.fixture.runtime
        self.reviews = MaterialReviews(self.registry)
        self.report = report_for(self.runtime, self.fixture.mapping, limitation=True)
        self.review = self.reviews.record(self.fixture.entry, self.fixture.mapping['extraction_id'], self.report)
        self.scope = PilotScope('synthetic-app', 'synthetic-chat', 'synthetic-user')
        self.pilot = PilotPreviews(self.registry, self.scope)
        self.preview = self.fixture.prepare()
        self.pilot.stage(self.preview['publication_id'])
        self.pilot.bind_delivery(self.preview['publication_id'], app_id=self.scope.app_id,chat_id=self.scope.chat_id,
            message_id='synthetic-preview',pdf_sha256=self.preview['preview_sha256'],receipt_ref='synthetic-receipt',outcome='sent')

    def decide(self, text='确认当前版本入库', **extra):
        args = dict(app_id=self.scope.app_id,chat_id=self.scope.chat_id,user_id=self.scope.user_id,
                    message_id='synthetic-owner-choice',reply_to='synthetic-preview',text=text)
        return self.pilot.decide(**(args | extra))

    def test_limitations_appear_in_exact_pdf_and_one_reply_records_both_choices(self):
        data = Path(self.preview['preview_path']).read_bytes()
        text = '\n'.join(p.extract_text() for p in PdfReader(self.preview['preview_path']).pages)
        self.assertIn('当前资料为节选', text)
        self.assertIn('确认当前版本入库', text)
        result = self.decide()
        self.assertEqual((Path(result['path'])/'发布记录/已确认入库预览.pdf').read_bytes(),data)
        self.assertEqual(self.decide(),result)
        events = self.registry.rows("SELECT * FROM custody_events WHERE event_type='material_limitations_accepted'")
        self.assertEqual(len(events),1)
        self.assertEqual(json.loads(events[0]['payload_json'])['review_id'],self.review['review_id'])
        self.assertEqual(len(self.registry.rows('SELECT * FROM publication_confirmations')),1)

    def test_plain_confirmation_and_direct_publisher_cannot_skip_limitations(self):
        with self.assertRaisesRegex(ContractError,'limitations'):
            self.decide('确认入库')
        with self.assertRaisesRegex(ContractError,'limitations'):
            self.fixture.publisher.confirm(self.preview['publication_id'],self.preview['preview_sha256'],
                                            'synthetic-user','synthetic-direct')
        self.assertFalse(self.registry.rows('SELECT * FROM publication_confirmations'))

    def test_wrong_user_and_reply_cannot_accept_material_limitations(self):
        for extra in ({'user_id':'synthetic-other'}, {'reply_to':'synthetic-old'}):
            with self.assertRaises(ContractError):
                self.decide(**extra)
        self.assertFalse(self.registry.rows("SELECT * FROM custody_events WHERE event_type='material_limitations_accepted'"))

    def test_new_review_requires_new_preview_even_if_qualified(self):
        changed = deepcopy(self.report)
        changed['issues'][-1]['note'] = '节选；增加了明确说明。'
        self.reviews.record(self.fixture.entry,self.fixture.mapping['extraction_id'],changed,
                            previous_review_id=self.review['review_id'])
        with self.assertRaisesRegex(ContractError,'new preview'):
            self.decide()

    def test_atomic_owner_choice_rolls_back_with_confirmation_failure(self):
        with self.registry.transaction() as db:
            db.execute("CREATE TRIGGER synthetic_decision_failure BEFORE INSERT ON pilot_preview_decisions BEGIN SELECT RAISE(ABORT,'synthetic fault'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.decide()
        self.assertFalse(self.registry.rows('SELECT * FROM publication_confirmations'))
        self.assertFalse(self.registry.rows("SELECT * FROM custody_events WHERE event_type='material_limitations_accepted'"))

    def test_new_blocker_after_confirmation_prevents_commit_without_erasing_choice(self):
        with patch.object(self.pilot.publisher,'publish',side_effect=RuntimeError('synthetic interruption')):
            with self.assertRaises(RuntimeError):
                self.decide()
        changed = deepcopy(self.report)
        changed['checks'][0]['status'] = 'failed'
        self.reviews.record(self.fixture.entry,self.fixture.mapping['extraction_id'],changed,
                            previous_review_id=self.review['review_id'])
        with self.assertRaises(ContractError):
            self.pilot.publisher.publish(self.preview['publication_id'])
        self.assertEqual(len(self.registry.rows('SELECT * FROM publication_confirmations')),1)
        self.assertFalse(self.registry.rows('SELECT * FROM active_publications_v21'))

    def test_review_change_after_filesystem_rename_stays_unpublished_on_restart(self):
        from kb_custody_v21 import safe_publish
        rename = safe_publish.rename_exclusive
        def change_after_rename(*args):
            rename(*args)
            changed = deepcopy(self.report)
            changed['checks'][0]['status'] = 'pending'
            self.reviews.record(self.fixture.entry,self.fixture.mapping['extraction_id'],changed,
                                previous_review_id=self.review['review_id'])
        with patch.object(safe_publish,'rename_exclusive',side_effect=change_after_rename), self.assertRaises(ContractError):
            self.decide()
        row = self.fixture.publisher._row(self.preview['publication_id'])
        self.assertEqual(row['state'],'publishing')
        self.assertTrue((self.fixture.publisher.root/self.preview['publication_id']).exists())
        with self.assertRaises(ContractError):
            self.fixture.publisher.publish(self.preview['publication_id'])
        self.assertFalse(self.registry.rows('SELECT * FROM active_publications_v21'))
        self.assertEqual(len(self.registry.rows('SELECT * FROM publication_confirmations')),1)
