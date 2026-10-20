from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from kb_custody_v21.content import ReadingGuideAdapter, empty_record, validate_record
from kb_custody_v21.contracts import ContractError
from kb_custody_v21.conversion import sha
from fixtures_guide import SyntheticGuide


class ContentTests(unittest.TestCase):
    def setUp(self):
        self.body = '街道树荫影响步行路线。\n仅供离线测试。'.encode()
        self.mapping = dict(extraction_id='extract-test',output_asset_id='asset-test',output_sha256=sha(self.body))
        self.record = empty_record('合成街道研究', 'synthetic-title-confirmation')
        self.consent = dict(event_id='synthetic-consent',asset_id='asset-test',sha256=sha(self.body),
            extraction_id='extract-test',ranges=[[0,len(self.body.decode())]],model='synthetic-reader',
            expires_at=(datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat())
        self.provider = SyntheticGuide()
        self.adapter = ReadingGuideAdapter(self.provider)

    def generate(self):
        return self.adapter.generate(self.record,self.body,self.mapping,self.consent,curation_data={'claims':['old-summary-not-evidence']})

    def test_source_first_skill_version_and_real_coverage_binding(self):
        record, curated, guide = self.generate()
        request = self.provider.request
        self.assertEqual(request['authorized_sources'][0]['text'], self.body.decode())
        self.assertIn('SKILL.md', request['style_rules'])
        self.assertEqual(curated['guide_generation']['skill'], 'private-reading-guide')
        self.assertEqual(curated['guide_generation']['validation_mode'], 'offline_mock')
        self.assertEqual(record['source_reading']['figures']['status'], 'not_read')
        self.assertEqual(curated['guide_generation']['guide_sha256'], sha(guide.encode()))

    def test_partial_authorization_only_supplies_selected_codepoints(self):
        self.consent['ranges'] = [[0, 6]]
        record, _, _ = self.generate()
        self.assertEqual(self.provider.request['authorized_sources'][0]['text'], self.body.decode()[:6])
        self.assertEqual(record['source_reading']['text']['status'], 'partial')

    def test_changed_bytes_expired_consent_and_wrong_model_rejected_before_call(self):
        for field,value in [('sha256','0'*64),('expires_at','2000-01-01T00:00:00+00:00'),('model','unapproved-model')]:
            with self.subTest(field=field), patch.object(self.provider,'generate',side_effect=AssertionError('must not call')):
                old = self.consent[field]
                self.consent[field] = value
                with self.assertRaises(ContractError): self.generate()
                self.consent[field] = old

    def test_fabricated_quote_or_unread_range_rejected(self):
        original = self.provider.generate
        for mutation in ('quote','coverage','paragraph'):
            def changed(request):
                response = original(request)
                if mutation == 'quote': response['evidence'][0]['quote'] = 'fabricated'
                if mutation == 'coverage': response['coverage'] = [[0,999]]
                if mutation == 'paragraph': response['guide_markdown'] += '\n\n未经重新绑定的段落。'
                return response
            with self.subTest(mutation=mutation), patch.object(self.provider,'generate',side_effect=changed):
                with self.assertRaises(ContractError): self.generate()

    def test_identity_judgments_need_provenance_and_unknown_fields_fail(self):
        self.record['work']['publication_year'] = 2026
        with self.assertRaisesRegex(ContractError,'provenance'): validate_record(self.record)
        self.record['field_provenance'].append(dict(fields=['/work/publication_year'],basis='source_declared',evidence_refs=['synthetic']))
        validate_record(self.record)
        self.record['work']['citation_ready'] = True
        with self.assertRaises(ContractError): validate_record(self.record)

    def test_real_provider_not_enabled_by_mock_consent(self):
        self.provider.is_mock = False
        with self.assertRaisesRegex(ContractError,'no real model authorization'): self.generate()

    def test_baseline_field_types_and_independent_verification_enums(self):
        mutations = [('release_context','channel_type','canonical_fulltext'),
                     ('release_context','issuer_verification_status','complete'),
                     ('bibliographic_verification','status','read'),
                     ('content_fact_check','status','external_verified'),
                     ('completeness','raw_page_count',-1),
                     ('completeness','expected_raw_page_count',True),
                     ('work','authors','someone'),
                     ('work','publication_year','2026'),
                     ('work','title',None),
                     ('bibliographic_verification','basis_refs','synthetic-ref')]
        for section, key, value in mutations:
            with self.subTest(field=section+'.'+key):
                record = deepcopy(self.record)
                record[section][key] = value
                with self.assertRaises(ContractError):
                    validate_record(record)

    def test_relation_requires_baseline_status_and_array_evidence(self):
        relation = dict(relation_id='synthetic-relation',type='excerpted_from',target_source_id='synthetic-source',
                        basis_refs=['synthetic-ref'],status='supported')
        self.record['relations'] = [relation]
        validate_record(self.record)
        for key,value in [('status','automatically_merged'),('basis_refs','synthetic-ref')]:
            record = deepcopy(self.record)
            record['relations'][0][key] = value
            with self.subTest(field=key), self.assertRaises(ContractError):
                validate_record(record)
