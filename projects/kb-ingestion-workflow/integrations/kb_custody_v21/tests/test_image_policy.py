from copy import deepcopy
import io
from pathlib import Path
import tempfile
import unittest
from datetime import datetime,timedelta,timezone

from PIL import Image
from kb_custody_v21.contracts import new_id, ContractError
from kb_custody_v21.conversion import sha
from kb_custody_v21.image_policy import ImagePolicies
from kb_custody_v21.registry import Registry
from kb_custody_v21.runtime import Runtime, DEFAULTS
from test_custody import WORKTREE, BASE


class ImagePolicyTests(unittest.TestCase):
    def setUp(self):
        self.runtime = Runtime(WORKTREE, DEFAULTS, Path(tempfile.mkdtemp(prefix='images-', dir=BASE / 'test-output')))
        self.registry = Registry(self.runtime)
        self.engine = ImagePolicies(self.registry)
        stream = io.BytesIO()
        Image.new('RGB', (80, 60), 'navy').save(stream, 'PNG')
        data = stream.getvalue()
        path = self.runtime.root / 'fixtures/synthetic.png'
        path.write_bytes(data)
        self.image = dict(asset_id=new_id('asset'), path=str(path), role='extracted_original',
                          sha256=sha(data), size_bytes=len(data), visual_type='photo', basis_refs=['synthetic-fixture'])
        self.assessment = dict(material_type='public_account_or_web', reliable=True, basis_refs=['synthetic-fixture'])
        self.caps = {'archive': 'configured_pending_window', 'preview': True, 'reading': True}

    def decide(self, **kwargs):
        return self.engine.decide([self.image], self.assessment, capabilities=self.caps, **kwargs)

    def test_IMG_A_below_threshold_no_questionnaire_and_dedup_roles(self):
        excluded = dict(self.image, role='ocr_page', sha256='b'*64, size_bytes=10_000_000)
        plan = self.engine.decide([self.image, self.image, excluded], self.assessment)
        self.assertEqual(plan['image_count'], 2)
        self.assertEqual(plan['unique_image_count'], 1)
        self.assertEqual(plan['total_unique_bytes'], self.image['size_bytes'])
        self.assertEqual(plan['application_mode'], 'below_threshold')
        self.assertEqual(plan['execution_result']['status'], 'planned')

    def test_IMG_B_no_default_asks_and_never_saves_inferred_preference(self):
        plan = self.decide(budget_bytes=1)
        self.assertEqual(plan['execution_result']['status'], 'needs_user_decision')
        self.assertFalse(self.registry.rows('SELECT * FROM image_preferences'))

    def test_IMG_C_confirmed_default_automatic_snapshot_stable(self):
        version = self.engine.save_default('public_account_or_web', 2, 'synthetic-confirmation')
        plan = self.decide(budget_bytes=1)
        self.assertEqual(plan['exception_check']['status'], 'passed')
        self.assertEqual(plan['preference_version'], version)
        self.engine.save_default('public_account_or_web', 6, 'synthetic-confirmation-2')
        self.assertEqual(plan['applied_policy_snapshot']['strategy'], 2)
        self.assertEqual(len(self.registry.rows('SELECT * FROM image_preferences')), 2)

    def test_IMG_D_current_choice_does_not_mutate_default(self):
        version = self.engine.save_default('public_account_or_web', 6, 'synthetic-default')
        result = self.decide(current={'strategy': 2, 'confirmation_ref': 'synthetic-current'})
        self.assertEqual(result['application_mode'], 'user_current')
        self.assertEqual(self.registry.rows('SELECT * FROM active_image_preferences')[0]['preference_version'], version)

    def test_IMG_E_unknown_type_blocks_auto_and_defaults_are_scoped(self):
        self.engine.save_default('public_account_or_web', 2, 'synthetic-default')
        self.assessment.update(material_type='unknown', reliable=False, basis_refs=[])
        plan = self.decide(budget_bytes=1)
        self.assertIn('E1', plan['exception_check']['triggered_codes'])
        self.assertIsNone(plan['preference_version'])

    def test_IMG_F_saved_all_local_is_not_permanent_budget_waiver(self):
        self.engine.save_default('public_account_or_web', 1, 'synthetic-default')
        self.assertIn('E2', self.decide(budget_bytes=1)['exception_check']['triggered_codes'])
        self.assertNotIn('E2', self.decide(budget_bytes=1, current={'strategy':1,'confirmation_ref':'synthetic-current'},
                                        accept_over_budget=True)['exception_check']['triggered_codes'])

    def test_IMG_G_dense_selection_not_bypassed_by_text_only(self):
        self.image['visual_type'] = 'dense'
        for strategy in (2, 3, 4, 5, 6):
            plan = self.decide(current={'strategy':strategy,'confirmation_ref':'synthetic-current'})
            self.assertIn('E3', plan['exception_check']['triggered_codes'])
        plan = self.decide(current={'strategy':2,'confirmation_ref':'synthetic-current'},
                           dense_choices={self.image['asset_id']:'text_only'})
        result = self.engine.execute(plan, [self.image], self.runtime.root / 'conversion-work/text-only')
        self.assertEqual(result['execution_result']['status'], 'local_completed_archive_pending')
        self.assertIsNone(result['per_asset'][0]['representation_asset_id'])
        self.assertEqual(result['execution_result']['per_asset_results'], [])

    def test_IMG_H_anomaly_and_conflicting_list(self):
        plan = self.decide(max_single_bytes=1)
        self.assertIn('E4', plan['exception_check']['triggered_codes'])
        plan = self.decide(current={'strategy':4,'confirmation_ref':'synthetic-current'}, selected={'reading':['other-source']})
        self.assertIn('E7', plan['exception_check']['triggered_codes'])

    def test_IMG_I_reading_requires_current_asset_consent_and_actual_result(self):
        plan = self.decide(current={'strategy':3,'confirmation_ref':'synthetic-current'})
        self.assertIn('E5', plan['exception_check']['triggered_codes'])
        plan = self.decide(current={'strategy':3,'confirmation_ref':'synthetic-current'}, consent_assets=[self.image['asset_id']])
        result = self.engine.execute(plan, [self.image], self.runtime.root / 'conversion-work/read-test')
        self.assertEqual(result['execution_result']['status'], 'failed')
        self.assertEqual(result['per_asset'][0]['reading_status'], 'not_read')

    def test_IMG_J_known_capability_failure_vs_pending_window(self):
        self.engine.save_default('public_account_or_web', 2, 'synthetic-default')
        self.assertEqual(self.decide(budget_bytes=1)['exception_check']['status'], 'passed')
        self.caps['archive'] = 'failed'
        self.assertIn('E6', self.decide(budget_bytes=1)['exception_check']['triggered_codes'])

    def test_IMG_K_actual_generated_bytes_rechecked_and_original_preserved(self):
        plan = self.decide(current={'strategy':6,'confirmation_ref':'synthetic-current'}, budget_bytes=200)
        # A small PNG can produce a larger JPEG; estimate cannot certify result.
        if plan['exception_check']['status'] != 'passed':
            self.fail(plan)
        result = self.engine.execute(plan, [self.image], self.runtime.root / 'conversion-work/result')
        self.assertEqual(result['execution_result']['status'], 'failed')
        self.assertIn('E2', result['execution_result']['error_refs'][0])
        self.assertEqual(sha(Path(self.image['path']).read_bytes()), self.image['sha256'])

    def test_IMG_L_preview_has_new_identity_and_does_not_upscale(self):
        plan = self.decide(current={'strategy':6,'confirmation_ref':'synthetic-current'})
        result = self.engine.execute(plan, [self.image], self.runtime.root / 'conversion-work/preview')
        output = result['execution_result']['per_asset_results'][0]
        self.assertNotEqual(output['asset_id'], self.image['asset_id'])
        self.assertNotEqual(output['sha256'], self.image['sha256'])
        with Image.open(output['path']) as image:
            self.assertEqual(image.size, (80, 60))

    def test_IMG_strategy5_same_sha_alias_preserves_explicit_original_in_both_orders(self):
        alias = dict(self.image, asset_id=new_id('asset'))
        for index, images in enumerate(([self.image, alias], [alias, self.image])):
            with self.subTest(order=index):
                plan = self.engine.decide(images, self.assessment, capabilities=self.caps,
                    current={'strategy': 5, 'confirmation_ref': 'synthetic-current'},
                    selected={'local_original': [self.image['asset_id']]})
                self.assertEqual(plan['exception_check']['status'], 'passed')
                result = self.engine.execute(plan, images, self.runtime.root / f'conversion-work/alias-{index}')
                outputs = result['execution_result']['per_asset_results']
                self.assertEqual(len(outputs), 1)
                self.assertEqual(outputs[0]['asset_id'], self.image['asset_id'])
                self.assertEqual(Path(outputs[0]['path']).read_bytes(), Path(self.image['path']).read_bytes())
                self.assertEqual(outputs[0]['sha256'], self.image['sha256'])
                self.assertEqual(set(result['per_asset'][0]['alias_asset_ids']),
                                 {self.image['asset_id'], alias['asset_id']})
                self.assertEqual(set(outputs[0]['derived_from']), {self.image['asset_id'], alias['asset_id']})
                self.assertEqual(result['unique_image_count'], 1)
                self.assertEqual(result['local_result_budget']['actual_bytes'], self.image['size_bytes'])
                self.assertFalse(result['per_asset'][0]['cloud_required'])

    def test_IMG_M_bytes_conflict_fails_without_fallback(self):
        plan = self.decide()
        Path(self.image['path']).write_bytes(b'changed')
        result = self.engine.execute(plan, [self.image], self.runtime.root / 'conversion-work/fail')
        self.assertEqual(result['execution_result']['status'], 'failed')
        self.assertIn('E7', result['execution_result']['error_refs'][0])

    def test_mock_visual_reader_records_exact_bytes_and_creates_notes_only_after_read(self):
        from kb_custody_v21.visual_reading import VisualReader,render_image_notes
        fixture = self.image
        class Reader:
            is_mock,model = True,'synthetic-vision'
            def read(self,data,**binding):
                if sha(data)!=fixture['sha256']: raise AssertionError('wrong image')
                return dict(transcribed_text='',interpretation='合成深蓝色图片。',reading_status='read',unresolved=[])
        consent = dict(event_id='synthetic-image-consent',asset_id=fixture['asset_id'],sha256=fixture['sha256'],
                       model='synthetic-vision',expires_at=(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat())
        reader = VisualReader(self.runtime,Reader(),{fixture['asset_id']:consent})
        plan = self.decide(current={'strategy':3,'confirmation_ref':'synthetic-current'},consent_assets=[fixture['asset_id']])
        result = self.engine.execute(plan,[fixture],self.runtime.root/'conversion-work/vision',reader=reader)
        self.assertEqual(result['execution_result']['status'],'local_completed_archive_pending')
        observations = result['execution_result']['visual_observations']
        self.assertEqual(observations[0]['read_resolution'],[80,60])
        self.assertEqual(observations[0]['validation_mode'],'offline_mock')
        self.assertIn('合成深蓝色',render_image_notes(observations).decode())
        self.assertIsNone(render_image_notes([]))
        consent['sha256'] = '0'*64
        with self.assertRaises(ContractError): reader.read(fixture)
