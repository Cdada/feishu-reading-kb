from copy import deepcopy
import unittest

from kb_custody_v21.content import ReadingGuideAdapter, empty_record
from kb_custody_v21.contracts import ContractError
from kb_custody_v21.conversion import sha
from kb_custody_v21.pilot_text import PilotTextSession
from kb_custody_v21.pilot_text_grounding import ground_response


class QuoteGroundingTests(unittest.TestCase):
    def setUp(self):
        self.text = '🙂前言。\n树荫让步行更舒适。\n后记。'
        self.body = self.text.encode()
        self.record = empty_record('合成材料','synthetic-title')
        self.mapping = dict(extraction_id='synthetic-extraction',output_asset_id='synthetic-asset',output_sha256=sha(self.body))
        self.request = dict(source_record=deepcopy(self.record),authorized_sources=[dict(
            asset_id=self.mapping['output_asset_id'],extraction_id=self.mapping['extraction_id'],sha256=sha(self.body),
            char_start=5,char_end=14,text=self.text[5:14])],skill_version=dict(revision='synthetic',package_sha256='0'*64))
        self.response = dict(guide_markdown='材料认为树荫改善步行体验。',coverage=[[5,14]],coverage_statement='仅阅读授权片段。',
            evidence=[dict(evidence_id='e1',source_index=0,quote='树荫让步行更舒适。',relation_to_claim='supports')],
            claims=[dict(claim_id='c1',text='树荫改善体验',basis='source_statement',evidence_refs=['e1'],conditions=[])],
            evidence_links=[dict(paragraph_index=0,evidence_refs=['e1'])],unresolved_issues=[])

    def ground(self):
        return ground_response(self.record,self.body,self.mapping,self.request,self.response)

    def test_nonzero_slice_and_unicode_positions_are_computed_from_exact_bytes(self):
        original=deepcopy(self.response)
        value,audit=self.ground()
        evidence=value['evidence'][0]
        self.assertEqual(evidence['locator'],dict(char_start=5,char_end=14))
        self.assertEqual(self.text[5:14],evidence['quote'])
        self.assertEqual(evidence['source_id'],self.record['source_id'])
        self.assertEqual(audit['evidence'][0]['quote_sha256'],sha(evidence['quote'].encode()))
        self.assertEqual(self.response,original)
        ReadingGuideAdapter(PilotTextSession).validate_response(self.record,self.body,self.mapping,
            dict(ranges=[[5,14]],event_id='synthetic-consent'),self.request,value)

    def test_repeated_and_overlapping_quotations_are_rejected(self):
        for text,quote in [('abc abc','abc'),('aaa','aa')]:
            with self.subTest(text=text):
                self.text=text;self.body=text.encode();self.mapping['output_sha256']=sha(self.body)
                self.request['authorized_sources'][0].update(text=text,char_start=0,char_end=len(text),sha256=sha(self.body))
                self.response['evidence'][0]['quote']=quote
                with self.assertRaisesRegex(ContractError,'ambiguous'):self.ground()

    def test_quote_elsewhere_in_original_is_not_selected_slice_evidence(self):
        self.response['evidence'][0]['quote']='后记。'
        with self.assertRaisesRegex(ContractError,'absent'):self.ground()

    def test_typography_whitespace_and_linebreaks_are_not_normalized(self):
        for quote in ['树荫 让步行更舒适。','树荫让步行更舒适.', '树荫\n让步行更舒适。']:
            with self.subTest(quote=quote):
                self.response['evidence'][0]['quote']=quote
                with self.assertRaisesRegex(ContractError,'absent'):self.ground()

    def test_invalid_index_and_blank_quote_fail_closed(self):
        for index in [True,-1,1,'0',None]:
            with self.subTest(index=index):
                self.response['evidence'][0]['source_index']=index
                with self.assertRaises(ContractError):self.ground()
        self.response['evidence'][0]['source_index']=0
        for quote in ['', ' ', None]:
            self.response['evidence'][0]['quote']=quote
            with self.assertRaises(ContractError):self.ground()

    def test_tampered_identity_source_slice_or_body_is_rejected(self):
        saved=deepcopy(self.request)
        for field,value in [('sha256','0'*64),('asset_id','another-asset'),('extraction_id','another-extraction'),('text','changed')]:
            self.request=deepcopy(saved);self.request['authorized_sources'][0][field]=value
            with self.assertRaisesRegex(ContractError,'differs'):self.ground()
        self.request=saved;self.body+=b'changed'
        with self.assertRaisesRegex(ContractError,'input changed'):self.ground()

    def test_legacy_response_metadata_is_never_silently_repaired(self):
        self.response['evidence'][0].update(source_id='wrong-source',locator=dict(char_start=0,char_end=3))
        with self.assertRaisesRegex(ContractError,'contract fields'):self.ground()

    def test_final_validator_still_rejects_unread_coverage_and_broken_links(self):
        value,_=self.ground()
        value['coverage']=[[0,len(self.text)]]
        validator=ReadingGuideAdapter(PilotTextSession)
        consent=dict(ranges=[[5,14]],event_id='synthetic-consent')
        with self.assertRaisesRegex(ContractError,'beyond authorization'):
            validator.validate_response(self.record,self.body,self.mapping,consent,self.request,value)
        value,_=self.ground();value['evidence_links'][0]['evidence_refs']=['missing']
        with self.assertRaisesRegex(ContractError,'evidence no longer matches'):
            validator.validate_response(self.record,self.body,self.mapping,consent,self.request,value)
