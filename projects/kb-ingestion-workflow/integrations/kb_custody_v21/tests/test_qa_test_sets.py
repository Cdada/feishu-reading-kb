from copy import deepcopy
import unittest
from kb_custody_v21.contracts import ContractError
from kb_custody_v21.qa_test_sets import make_variant, partition_cases


def case(id, family=None, digest=None, **kw):
    return dict(id=id,parent_group=family or id,corpus_id='synthetic',
                evidence_bindings={id:digest or (id*64)[:64]},gold_status='reviewed',**kw)


class TestSetTests(unittest.TestCase):
    def test_shared_source_and_transitive_family_cannot_cross_partitions(self):
        cases=[case('a'),case('b',digest='a'*64),case('c',family='b'),case('d')]
        result=partition_cases(cases,seed='fixed')
        self.assertEqual(len({result['assignments'][x] for x in 'abc'}),1)
        self.assertEqual(len(result['groups']),2)

    def test_identical_text_in_another_corpus_stays_with_exposed_source(self):
        a=case('a',seen_in_development=True);b=case('b',digest='a'*64);b['corpus_id']='different'
        result=partition_cases([a,b,case('c'),case('d'),case('e')],seed='fixed')
        self.assertEqual(result['assignments']['b'],'development')

    def test_many_independent_cases_can_fill_all_three_partitions(self):
        cases=[case(c) for c in 'abcdef0123']
        result=partition_cases(cases,seed='fixed')
        self.assertEqual(result['counts'],dict(development=6,regression=2,heldout=2))
        reverse=partition_cases(list(reversed(cases)),seed='fixed')
        self.assertEqual(result['assignments'],reverse['assignments'])

    def test_single_shared_cluster_does_not_fake_heldout_coverage(self):
        result=partition_cases([case(c,digest='a'*64) for c in 'abc'],seed='fixed')
        self.assertEqual(result['counts']['heldout'],0)
        self.assertEqual(len(result['groups']),1)

    def test_reviewed_lineage_groups_different_text_without_guessing_titles(self):
        a=case('a',seen_in_development=True,source_lineage=['original-work'])
        b=case('b',source_lineage=['original-work'])
        result=partition_cases([a,b,case('c')],seed='fixed')
        self.assertEqual(result['assignments']['b'],'development')
        self.assertEqual(len(result['groups']),2)

    def test_changed_question_never_inherits_gold_claims_or_answer(self):
        base=case('a');base.update(question='原问题',required_claims=['旧结论'],answer='旧答案',answerability='supported',split='heldout')
        changed=make_variant(base,id='b',kind='time_scope',question='新的时间范围？',expected_behavior='核对时间',changed_dimensions=['time'])
        self.assertEqual(changed['parent_group'],'a')
        self.assertEqual(changed['variant_of'],'a')
        self.assertEqual(changed['gold_status'],'unreviewed')
        self.assertIsNone(changed['split']);self.assertIsNone(changed['answerability'])
        self.assertNotIn('answer',changed);self.assertNotIn('required_claims',changed)
        self.assertEqual(partition_cases([base,changed],seed='s')['not_gold_ready'],['b'])

    def test_candidates_missing_evidence_are_reported_not_counted_as_passed(self):
        c=case('a');c['evidence_bindings']={};c['gold_status']='unreviewed'
        r=partition_cases([c],seed='fixed')
        self.assertEqual(r['missing_evidence_cases'],['a'])
        self.assertEqual(r['not_gold_ready'],['a'])
        self.assertEqual(r['evaluated_cases'],0)

    def test_variant_does_not_mutate_base(self):
        base=case('a');before=deepcopy(base)
        changed=make_variant(base,id='b',kind='paraphrase',question='另一种问法',expected_behavior='同一证据',changed_dimensions=['wording'])
        changed['evidence_bindings']['a']='b'*64
        self.assertEqual(base,before)

    def test_invalid_inputs_fail_early(self):
        for cases in ([],[case('a'),case('a')],[dict(case('a'),evidence_bindings={'a':'not-a-digest'})]):
            with self.subTest(cases=cases),self.assertRaises(ContractError):partition_cases(cases,seed='s')
        for p in ((60,20,30),(0,50,50),(True,49,50)):
            with self.subTest(p=p),self.assertRaises(ContractError):partition_cases([case('a')],seed='s',proportions=p)
        with self.assertRaises(ContractError):
            make_variant(case('a'),id='a',kind='paraphrase',question='问法',expected_behavior='核对',changed_dimensions=['wording'])


if __name__=='__main__':unittest.main()
