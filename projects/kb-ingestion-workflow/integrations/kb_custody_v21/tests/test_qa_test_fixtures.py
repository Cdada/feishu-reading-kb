import copy
import hashlib
import unittest

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.qa_test_fixtures import compile_fixture


class FixtureTests(unittest.TestCase):
    def setUp(self):
        self.sources = {'a': dict(title='Seed log', role='record', text='Day 1: planned.\nDay 2: started.')}
        body = self.sources['a']['text']
        self.case = dict(id='case-a', question='Was the work completed?',
            selectors=[dict(material_id='a', body_sha256=hashlib.sha256(body.encode()).hexdigest(), start=0, end=len(body))],
            constraints=['Use only supplied evidence.'], conversation=[],
            expected=dict(answerability='partial', required_claims=['Completion is not evidenced.'], forbidden_claims=['Completed.']))

    def test_reference_stays_out_of_input(self):
        result = compile_fixture(self.case, self.sources)
        self.assertNotIn('expected', result['model_input'])
        self.assertNotIn('Completion is not evidenced.', str(result['model_input']))
        self.assertEqual(result['corpus_answerability'], 'not_assessed')

    def test_source_change_requires_review(self):
        self.sources['a']['text'] += '\nDay 3: complete.'
        with self.assertRaisesRegex(ContractError, 'source changed'):
            compile_fixture(self.case, self.sources)

    def test_rubric_change_does_not_change_input_hash(self):
        before = compile_fixture(self.case, self.sources)
        self.case['expected']['required_claims'].append('State the timeline.')
        after = compile_fixture(self.case, self.sources)
        self.assertEqual(before['input_sha256'], after['input_sha256'])
        self.assertNotEqual(before['fixture_sha256'], after['fixture_sha256'])

    def test_changed_scope_changes_input_hash(self):
        before = compile_fixture(self.case, self.sources)
        self.case['selectors'][0]['end'] = 15
        after = compile_fixture(self.case, self.sources)
        self.assertNotEqual(before['input_sha256'], after['input_sha256'])
        self.assertEqual(after['model_input']['documents'][0]['text'], 'Day 1: planned.')

    def test_missing_authorized_source_cannot_be_inserted(self):
        self.case['selectors'][0]['material_id'] = 'forbidden-b'
        with self.assertRaisesRegex(ContractError, 'authorized source'):
            compile_fixture(self.case, self.sources)

    def test_empty_context_is_explicit(self):
        self.case['selectors'] = []
        self.case['expected']['answerability'] = 'missing_in_fixture'
        self.assertEqual(compile_fixture(self.case, {})['context_characters'], 0)
        self.case['expected']['answerability'] = 'supported'
        with self.assertRaises(ContractError):
            compile_fixture(self.case, {})

    def test_offsets_and_duplicate_spans_rejected(self):
        for offsets in [(-1, 2), (0, 999), (2, 2), (True, 2)]:
            case = copy.deepcopy(self.case)
            case['selectors'][0].update(start=offsets[0], end=offsets[1])
            with self.assertRaises(ContractError): compile_fixture(case, self.sources)
        self.case['selectors'] *= 2
        with self.assertRaisesRegex(ContractError, 'duplicate'):
            compile_fixture(self.case, self.sources)

    def test_private_unknown_fields_rejected(self):
        self.case['answer'] = 'Private reference answer'
        with self.assertRaises(ContractError): compile_fixture(self.case, self.sources)

    def test_snapshots_do_not_alias_caller_values(self):
        result = compile_fixture(self.case, self.sources)
        self.case['constraints'][0] = 'changed'
        self.case['expected']['required_claims'][0] = 'changed'
        self.assertEqual(result['model_input']['constraints'], ['Use only supplied evidence.'])
        self.assertEqual(result['expected']['required_claims'], ['Completion is not evidenced.'])


if __name__ == '__main__':
    unittest.main()
