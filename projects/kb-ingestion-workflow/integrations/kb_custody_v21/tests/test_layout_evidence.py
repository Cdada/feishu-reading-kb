"""Synthetic layout labels, source spans and claim guards; no real documents."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from kb_custody_v21.content import ReadingGuideAdapter, empty_record, validate_evidence
from kb_custody_v21.contracts import ContractError
from kb_custody_v21.layout_evidence import checked_blocks, checked_policy, digest, evidence_policy, require_citable
from kb_custody_v21.mineru_chunks import render_chunk
from kb_custody_v21.image_links import rewrite_images
from kb_custody_v21.reading_contract import quote, request, validate_draft
from test_reading_content import sample


def fixture():
    pages = ['示例期刊 2026\n\n正文甲。\n\n① 补充说明甲。\n\n下载平台\n\n1',
             '示例期刊 2026\n\n正文乙。\n\n① 补充说明乙。\n\n下载平台\n\n2']
    text = '\n\n'.join(pages)
    mapping = dict(extractor='mineru-pipeline', locator_precision='page', page_mappings=[], layout_blocks=[],
                   extraction_id='extract-synthetic', output_asset_id='asset-synthetic', output_sha256=digest(text),
                   image_mappings=[], transformations=[])
    offset = 0
    for index, page in enumerate(pages):
        mapping['page_mappings'].append(dict(page_index=index,text_ranges=[dict(char_start=offset,char_end=offset+len(page))],text_sha256=digest(page)))
        for value, kind, box in [('示例期刊 2026','header',[0,30,500,60]),
                                 ('① 补充说明'+('甲。' if index == 0 else '乙。'),'page_footnote',[0,900,500,950]),
                                 ('下载平台','footer',[0,960,500,980]),(str(index+1),'page_number',[800,960,850,980])]:
            start = offset + (page.rindex(value) if kind == 'page_number' else page.index(value))
            mapping['layout_blocks'].append(dict(kind=kind,page_index=index,char_start=start,char_end=start+len(value),bbox=box,sha256=digest(value)))
        offset += len(page)+2
    return text, mapping


class LayoutEvidenceTests(unittest.TestCase):
    def test_repeated_furniture_excluded_without_changing_source(self):
        text,mapping=fixture();before=deepcopy(mapping)
        p=evidence_policy(mapping,text)
        self.assertEqual(len(p['excluded_ranges']),6)
        self.assertEqual(mapping,before)
        for value in ['正文甲。','① 补充说明甲。','正文乙。','① 补充说明乙。']:
            start=text.index(value);require_citable(p,text,start,start+len(value))
        with self.assertRaisesRegex(ContractError,'header/footer'):
            require_citable(p,text,0,8)

    def test_body_at_page_edge_and_repeated_prose_are_not_removed(self):
        text,mapping=fixture()
        for b in mapping['layout_blocks']:
            if b['kind']=='header': b['bbox']=[0,300,500,330]
        p=evidence_policy(mapping,text)
        self.assertFalse(any(r['kind']=='header' for r in p['excluded_ranges']))

    def test_uncertain_positions_and_single_headers_are_retained(self):
        text,mapping=fixture()
        mapping['layout_blocks'][0]['bbox']=None
        p=evidence_policy(mapping,text)
        self.assertFalse(any(r['kind']=='header' for r in p['excluded_ranges']))

    def test_note_mislabeled_as_footer_remains(self):
        text,mapping=fixture()
        for b in mapping['layout_blocks']:
            if b['kind']=='page_footnote': b['kind']='footer'
        p=evidence_policy(mapping,text)
        for b in mapping['layout_blocks']:
            if text[b['char_start']:b['char_end']].startswith('①'):
                require_citable(p,text,b['char_start'],b['char_end'])

    def test_source_hash_position_and_page_tampering_rejected(self):
        text,mapping=fixture()
        for key,value in [('sha256','0'*64),('char_start',2),('page_index',9),('bbox',[0,0,1,1001])]:
            m=deepcopy(mapping);m['layout_blocks'][0][key]=value
            with self.subTest(key=key), self.assertRaises(ContractError):checked_blocks(m,text)
        p=evidence_policy(mapping,text)
        with self.assertRaises(ContractError):checked_policy(p,text+'changed')

    def test_legacy_mapping_does_not_invent_classification(self):
        self.assertIsNone(evidence_policy({},'正文。'))
        require_citable(None,'正文。',0,3)

    def test_partial_consent_does_not_expose_other_layout_ranges(self):
        text,mapping=fixture();a=text.index('正文甲。');end=a+4
        consent=dict(event_id='synthetic',asset_id=mapping['output_asset_id'],extraction_id=mapping['extraction_id'],
                     sha256=digest(text),ranges=[[a,end]],model='test',expires_at=(datetime.now(timezone.utc)+timedelta(minutes=1)).isoformat())
        adapter=ReadingGuideAdapter(SimpleNamespace(model='test',version='test',is_mock=True))
        req=adapter.prepare_request(empty_record('测试','synthetic'),text.encode(),mapping,consent)
        self.assertEqual(req['authorized_sources'][0]['text'],text[a:end])
        self.assertEqual(req['evidence_policy']['excluded_ranges'],[])

    def test_guide_validator_blocks_furniture_even_with_exact_locator(self):
        text,mapping=fixture()
        e=dict(evidence_id='e1',source_id='s',extraction_id=mapping['extraction_id'],asset_id=mapping['output_asset_id'],
               sha256=digest(text),locator=dict(char_start=0,char_end=9),quote=text[:9],relation_to_claim='supports')
        with self.assertRaisesRegex(ContractError,'header/footer'):
            validate_evidence(e,'s',mapping,text,[[0,len(text)]])

    def test_reading_uses_same_policy_and_preserves_footnote(self):
        text,mapping=fixture();source,_=sample();source['original']=text
        source['provenance']['evidence_policy']=evidence_policy(mapping,text)
        q='① 补充说明甲。'
        self.assertEqual(quote(dict(source='original',quote=q),source)['start'],text.index(q))
        # A unique range combining furniture with adjacent prose is still invalid.
        with self.assertRaisesRegex(ContractError,'header/footer'):
            quote(dict(source='original',quote='示例期刊 2026\n\n正文甲。'),source)
        r=request('organize',source,{k:'synthetic' for k in ('archive_rules','organize_prompt','nlp_skill')})
        self.assertIn('脚注',r['instruction'])

    def test_mineru_retains_types_offsets_and_all_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();raw=root/'raw';raw.mkdir()
            items=[dict(type='header',text='示例期刊',bbox=[0,20,500,50],page_idx=0),
                   dict(type='text',text='正文。',page_idx=0),
                   dict(type='page_footnote',text='① 注释。',bbox=[0,900,500,950],page_idx=0),
                   dict(type='footer',text='平台',bbox=[0,960,500,980],page_idx=0)]
            (raw/'synthetic_content_list.json').write_text(json.dumps(items))
            (raw/'synthetic_middle.json').write_text(json.dumps(dict(_version_name='3.1.15',pdf_info=[dict(page_idx=0)])))
            result=render_chunk(raw,root/'normalized',first_page=3,page_count=1,stem='test',version='3.1.15')
            self.assertEqual(result['pages'],['示例期刊\n\n正文。\n\n① 注释。\n\n平台'])
            for b in result['layout_pages'][0]:
                self.assertEqual(b['page_index'],2)
                self.assertEqual(digest(result['pages'][0][b['char_start']:b['char_end']]),b['sha256'])
            self.assertIn('page_footnote',{b['kind'] for b in result['layout_pages'][0]})

    def test_zero_padded_claim_ids_keep_exact_links(self):
        source,draft=sample()
        draft['claims'][0]['id']='c01'
        for section in draft['objective']+draft['synthesis']+draft['cards']:
            section['claim_ids']=['c01']
        validate_draft(draft,source)
        draft['objective'][0]['claim_ids']=['c1']
        with self.assertRaisesRegex(ContractError,'unknown claim'):validate_draft(draft,source)

    def test_invalid_claim_id_still_rejected(self):
        for value in ['c0','c00000','c123456','../../x','c-1']:
            source,draft=sample();draft['claims'][0]['id']=value
            with self.subTest(value=value), self.assertRaisesRegex(ContractError,'claim identity'):validate_draft(draft,source)

    def test_layout_offsets_follow_image_link_rewrite(self):
        text,mapping=fixture();prefix='![](images/a.png)\n\n';n=len(prefix)
        for index,p in enumerate(mapping['page_mappings']):
            r=p['text_ranges'][0]
            r['char_start']+=n if index else 0;r['char_end']+=n
        for b in mapping['layout_blocks']:
            b['char_start']+=n;b['char_end']+=n
        text=prefix+text;mapping['output_sha256']=digest(text)
        mapping['image_mappings']=[dict(char_start=0,char_end=n-2,source_page_index=0)]
        body,revised=rewrite_images(text.encode(),mapping,{'images/a.png':dict(asset_id='asset-img',local_path='images/longer-image-name.png',representation_asset_id='asset-img')})
        checked_blocks(revised,body.decode())
        self.assertEqual(len(evidence_policy(revised,body.decode())['excluded_ranges']),6)
        self.assertNotEqual(revised['layout_blocks'][0]['char_start'],mapping['layout_blocks'][0]['char_start'])
