"""Synthetic cross-corpus designs: no real questions or library metadata."""
from copy import deepcopy
import unittest

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.qa_test_design import BLUEPRINTS, design_candidates


def profile():
    return dict(corpus_id='synthetic-orbit', capabilities=['text'],
        sources=[dict(id='a', title='轨道测量实验', role='original', body_sha256='a'*64),
                 dict(id='b', title='轨道测量评述', role='interpretation', body_sha256='b'*64)],
        subjects=[dict(id='orbit', label='轨道测量', source_ids=['a', 'b'],
                       feature_evidence=dict(definitions=['a'], source_text=['a'],
                                             comparative_material=['a', 'b'], research_findings=['a','b']))])


class TestDesignTests(unittest.TestCase):
    def test_real_feature_requirements_control_question_types(self):
        result = design_candidates(profile())
        self.assertEqual({c['type'] for c in result['candidates']},
                         {'definition', 'inventory', 'provenance', 'comparison', 'synthesis'})
        comparison = next(c for c in result['candidates'] if c['type']=='comparison')
        self.assertIn('轨道测量实验', comparison['question'])
        self.assertIn('轨道测量评述', comparison['question'])
        self.assertEqual(comparison['source_roles']['b'], 'interpretation')
        self.assertEqual(result['model_calls'], 0)

    def test_profile_of_another_domain_uses_its_own_evidence_and_types(self):
        p = profile(); p['corpus_id']='synthetic-seed'
        p['sources']=[dict(id='s',title='种子保管计划',role='record',body_sha256='c'*64)]
        p['subjects']=[dict(id='seed',label='种子保管',source_ids=['s'],
                           feature_evidence=dict(dated_records=['s'],plans=['s'],constraints=['s']))]
        result = design_candidates(p)
        self.assertEqual({c['type'] for c in result['candidates']},
                         {'inventory','timeline','planning','feasibility'})
        self.assertTrue(all(c['evidence_candidates']==['s'] for c in result['candidates']))
        self.assertNotIn('轨道', str(result))

    def test_unknown_feature_is_not_inferred_from_source_title(self):
        p=profile();p['subjects'][0]['feature_evidence']={}
        p['sources'][0]['title']='版本变化与研究方法'
        self.assertEqual([c['type'] for c in design_candidates(p)['candidates']], ['inventory'])

    def test_missing_capability_becomes_explicit_boundary_candidate(self):
        p=profile();p['subjects'][0]['feature_evidence']['quantitative_records']=['a']
        case=next(c for c in design_candidates(p)['candidates'] if c['type']=='calculation')
        self.assertEqual(case['intended_check'],'capability_boundary')
        self.assertTrue(case['capability_gap'])
        p['capabilities'].append('tabular')
        case=next(c for c in design_candidates(p)['candidates'] if c['type']=='calculation')
        self.assertFalse(case['capability_gap'])

    def test_paraphrases_share_family_and_cannot_be_gold_automatically(self):
        result=design_candidates(profile(),paraphrases=True)
        self.assertEqual(result['generated_families'],5)
        self.assertEqual(len(result['candidates']),10)
        for base,variant in zip(result['candidates'][::2],result['candidates'][1::2]):
            self.assertEqual(variant['variant_of'],base['id'])
            self.assertEqual(variant['parent_group'],base['parent_group'])
            self.assertEqual(variant['evidence_bindings'],base['evidence_bindings'])
            self.assertIsNone(variant['answerability'])
            self.assertEqual(variant['gold_status'],'unreviewed')

    def test_limit_preserves_pairs_and_reports_omissions(self):
        result=design_candidates(profile(),limit=3,paraphrases=True)
        self.assertEqual(len(result['candidates']),2)
        self.assertEqual(len(result['omitted_by_limit']),4)
        empty=design_candidates(profile(),limit=1,paraphrases=True)
        self.assertEqual(empty['candidates'],[])
        self.assertEqual(len(empty['omitted_by_limit']),5)

    def test_comparison_requires_two_distinct_supplied_sources(self):
        p=profile();p['subjects'][0]['feature_evidence']['comparative_material']=['a']
        self.assertNotIn('comparison',{c['type'] for c in design_candidates(p)['candidates']})
        p['subjects'][0]['feature_evidence']['comparative_material']=['a','a']
        with self.assertRaises(ContractError): design_candidates(p)

    def test_missing_or_foreign_evidence_is_rejected(self):
        for change in ('missing','foreign','duplicate','unknown_feature'):
            p=profile()
            if change=='missing':p['subjects'][0]['feature_evidence']['definitions']=['absent']
            elif change=='foreign':
                p['subjects'][0]['source_ids']=['a']
            elif change=='duplicate':p['sources'].append(deepcopy(p['sources'][0]))
            else:p['subjects'][0]['feature_evidence']['guess']=['a']
            with self.subTest(change=change),self.assertRaises(ContractError):design_candidates(p)

    def test_identity_changes_when_corpus_changes_and_versions_are_bound(self):
        p=profile();first=design_candidates(p)
        p['sources'][0]['body_sha256']='d'*64
        second=design_candidates(p)
        self.assertNotEqual(first['profile_sha256'],second['profile_sha256'])
        self.assertEqual(first['candidates'][0]['id'],second['candidates'][0]['id'])
        p['corpus_id']='another-snapshot'
        self.assertNotEqual(first['candidates'][0]['id'],design_candidates(p)['candidates'][0]['id'])

    def test_input_is_not_mutated_and_output_is_deterministic(self):
        p=profile();before=deepcopy(p)
        self.assertEqual(design_candidates(p),design_candidates(p))
        self.assertEqual(p,before)
        self.assertEqual(len({b.id for b in BLUEPRINTS}),len(BLUEPRINTS))

    def test_variant_annotations_do_not_modify_base_question(self):
        base, variant = design_candidates(profile(), limit=2, paraphrases=True)['candidates']
        variant['criteria'].append('变体专用检查')
        variant['evidence_bindings']['a']='e'*64
        self.assertNotIn('变体专用检查', base['criteria'])
        self.assertEqual(base['evidence_bindings']['a'],'a'*64)

    def test_invalid_profile_fields_fail_early(self):
        for limit in (0,1001,True,'20'):
            with self.subTest(limit=limit),self.assertRaises(ContractError):design_candidates(profile(),limit=limit)
        p=profile();p['sources'][0]['body_sha256']='unverified'
        with self.assertRaises(ContractError):design_candidates(p)
        for role in ('official_because_ai_said_so', [], {}):
            p=profile();p['sources'][0]['role']=role
            with self.subTest(role=role),self.assertRaises(ContractError):design_candidates(p)


if __name__=='__main__':unittest.main()
