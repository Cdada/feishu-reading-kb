"""Resolve exact model-selected quotations against the frozen authorized text.

The model selects a source slice and quote. Identity and code-point positions
come from the host's checked input, never from guessed model metadata. No fuzzy
matching, whitespace normalization, or repair of legacy rejected responses.
"""
from copy import deepcopy

from .content import exact, ranges
from .contracts import ContractError
from .conversion import sha

EVIDENCE_PROTOCOL = 'authorized-source-index-exact-quote/v1'


def ground_response(record, content, mapping, request, response):
    text = content.decode('utf-8')
    if sha(content) != mapping['output_sha256'] or request['source_record'] != record:
        raise ContractError('quotation grounding input changed')
    exact(response, ('guide_markdown','evidence','claims','evidence_links','unresolved_issues','coverage','coverage_statement'))
    sources = request['authorized_sources']
    if not isinstance(sources, list) or not sources or not isinstance(response['evidence'], list):
        raise ContractError('explicit authorized sources and evidence required')
    result, evidence, audit = deepcopy(response), [], []
    for item in response['evidence']:
        exact(item, ('evidence_id','source_index','quote','relation_to_claim'))
        index, quote = item['source_index'], item['quote']
        if (type(index) is not int or not 0 <= index < len(sources)
                or not isinstance(quote, str) or not quote.strip()
                or not isinstance(item['evidence_id'], str) or not item['evidence_id'].strip()):
            raise ContractError('explicit source index and nonempty exact quotation required')
        source = sources[index]
        exact(source, ('asset_id','extraction_id','sha256','char_start','char_end','text'))
        start, end = source['char_start'], source['char_end']
        ranges([[start,end]], len(text))
        if ((source['asset_id'],source['extraction_id'],source['sha256']) !=
                (mapping['output_asset_id'],mapping['extraction_id'],mapping['output_sha256'])
                or source['text'] != text[start:end]):
            raise ContractError('selected source differs from the authorized extraction')
        position = source['text'].find(quote)
        if position < 0:
            raise ContractError('exact quotation absent from the selected authorized source')
        if source['text'].find(quote, position+1) >= 0:
            raise ContractError('quotation is ambiguous in the selected authorized source')
        locator = dict(char_start=start+position, char_end=start+position+len(quote))
        evidence.append(dict(evidence_id=item['evidence_id'],source_id=record['source_id'],
            extraction_id=mapping['extraction_id'],asset_id=mapping['output_asset_id'],
            sha256=mapping['output_sha256'],quote=quote,locator=locator,
            relation_to_claim=item['relation_to_claim']))
        audit.append(dict(evidence_id=item['evidence_id'],source_index=index,
            quote_sha256=sha(quote.encode()),locator=locator))
    result['evidence'] = evidence
    return result, dict(protocol=EVIDENCE_PROTOCOL, source_sha256=sha(content), evidence=audit)
