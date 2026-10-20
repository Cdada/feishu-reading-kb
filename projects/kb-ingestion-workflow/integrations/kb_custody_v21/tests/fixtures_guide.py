"""Fixed synthetic provider for interface regression; never content quality evidence."""
from kb_custody_v21.conversion import sha


class SyntheticGuide:
    is_mock, model, version = True, 'synthetic-reader', 'fixture/v1'

    def generate(self, request):
        self.request = request
        source = request['authorized_sources'][0]
        self.response = dict(guide_markdown='这份合成材料讨论街道树荫与步行路线。保留它可以帮助核对两个概念在原文中的关系。',
            evidence=[dict(evidence_id='e1', source_id=request['source_record']['source_id'],
                extraction_id=source['extraction_id'],asset_id=source['asset_id'],sha256=source['sha256'],
                locator=dict(char_start=source['char_start'],char_end=source['char_end']),quote=source['text'],relation_to_claim='supports')],
            claims=[dict(claim_id='c1',text='材料讨论街道树荫与步行路线。',basis='source_statement',evidence_refs=['e1'],conditions=[])],
            evidence_links=[dict(paragraph_index=0,evidence_refs=['e1'])],unresolved_issues=[],
            coverage=[[source['char_start'],source['char_end']]],coverage_statement='已读当前版本的提取文字；未进行图片识读。')
        return self.response


class SyntheticQuoteGuide(SyntheticGuide):
    """Same synthetic material, using the explicitly selected quote protocol."""
    def generate(self, request):
        response = super().generate(request)
        response['evidence'] = [dict(evidence_id=e['evidence_id'], source_index=0,
            quote=e['quote'], relation_to_claim=e['relation_to_claim']) for e in response['evidence']]
        return response
