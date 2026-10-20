"""Sanitized real-platform shape; no real IDs, titles or receipts leave DEV."""
import copy
import json
from pathlib import Path
import unittest

from kb_custody_v21.daily_cards import render_card
from kb_custody_v21.daily_card_readback import card_matches


def platform_view(definition):
    # Shape is a literal sanitized response, independent of production matcher.
    result = json.loads((Path(__file__).parent/'fixtures/p40_platform_card.json').read_text())
    elements = result['body']['elements']
    requested = definition['body']['elements']
    elements[0]['text']['content'] = requested[0]['text']['content']
    elements[1]['text']['content'] = requested[1]['text']['content']
    if len(requested) == 3 and requested[-1]['tag'] == 'div':
        result['body']['elements'] = elements[:2] + [dict(element_id='_6', tag='div', width='fill',
            text=dict(tag='plain_text', text_align='left', content=requested[-1]['text']['content']))]
    return result


class ReadbackTests(unittest.TestCase):
    def setUp(self):
        self.card = dict(card_id='c'*64, title='合成资料', binding=dict(purpose='preview',
            document_id='om_original', source_sha256='a'*64, target_message_id='om_pdf',
            publication_id='publication_synthetic', pdf_sha256='b'*64, preview_version=1))
        self.expected = render_card(self.card)
        self.actual = json.loads((Path(__file__).parent/'fixtures/p40_platform_card.json').read_text())

    def test_observed_normalization_and_exact_definition_are_accepted(self):
        self.assertTrue(card_matches(self.actual, self.expected))
        self.assertTrue(card_matches(self.expected, self.expected))
        before = copy.deepcopy(self.actual)
        card_matches(self.actual, self.expected)
        self.assertEqual(self.actual, before)

    def test_result_requires_result_text_and_no_remaining_buttons(self):
        done = render_card(self.card, result='已确认入库。')
        self.assertTrue(card_matches(platform_view(done), done))
        self.assertFalse(card_matches(self.actual, done))
        self.assertFalse(card_matches(platform_view(done), self.expected))

    def test_semantic_changes_extra_fields_and_ambiguous_ids_fail_closed(self):
        mutations = [
            lambda c:c['body']['elements'][0]['text'].update(content='其他资料'),
            lambda c:c['body']['elements'][1]['text'].update(content='导读第 2 版'),
            lambda c:c['body']['elements'][2]['text'].update(content='其他命令'),
            lambda c:c['body']['elements'][2].update(behaviors=[dict(type='callback',value='changed')]),
            lambda c:c['body']['elements'].append(copy.deepcopy(c['body']['elements'][2])),
            lambda c:c['body']['elements'][1].update(element_id='_2'),
            lambda c:c['body']['elements'][1].update(element_id='../file'),
            lambda c:c['config'].update(enable_forward_interaction=True),
            lambda c:c['header'].update(extra='unexpected'),
            lambda c:c.update(schema='1.0'),
            lambda c:c['body']['elements'][0]['text'].update(extra='unexpected'),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                value=copy.deepcopy(self.actual);mutate(value)
                self.assertFalse(card_matches(value,self.expected))
        for value in (None, [], {}, dict(body=[]), dict(body=dict(elements=[None]))):
            self.assertFalse(card_matches(value,self.expected))


if __name__ == '__main__':unittest.main()
