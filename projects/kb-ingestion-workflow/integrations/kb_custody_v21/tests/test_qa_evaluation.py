"""Evaluation reports retain failures and keep retrieval separate from answers."""
import unittest
from kb_custody_v21.qa_evaluation import evaluate
from kb_custody_v21.contracts import ContractError
import test_qa_index as fixture


class EvaluationTests(unittest.TestCase):
    def test_metrics_do_not_hide_false_positives_or_missing_sources(self):
        f=fixture.QAIndexTests();f.setUp();self.addCleanup(f.doCleanups)
        f.index.sync(fixture.Provider(fixture.source(),fixture.source('two')))
        cases=[dict(id='hit',question='查文献 城市演变',expected_mode='search',expected_materials=['one']),
               dict(id='missing',question='查文献 城市演变',expected_mode='search',expected_materials=['missing']),
               dict(id='false-positive',question='查文献 城市演变',expected_mode='search',expected_materials=[]),
               dict(id='empty',question='查文献 天体轨道',expected_mode='search',expected_materials=[]),
               dict(id='time',question='查文献 城市演变 2020年',expected_mode='clarify',expected_materials=[])]
        result=evaluate(f.index,cases,now=1800000000)
        self.assertEqual(result['cases'],5);self.assertEqual(result['passed'],3)
        self.assertEqual(result['positive_hit_at_5'],0.5)
        self.assertEqual(result['negative_correct'],1)
        self.assertIsNone(result['semantic_answer_accuracy'])
        self.assertEqual(result['model_calls'],0)
        with self.assertRaises(ContractError):evaluate(f.index,[cases[0],cases[0]],now=1800000000)
