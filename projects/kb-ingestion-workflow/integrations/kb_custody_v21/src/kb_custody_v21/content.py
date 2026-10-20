"""Source-first ReadingGuide boundary. Offline fixtures never certify model quality."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from .contracts import ContractError, new_id
from .conversion import sha
from .registry import now
from .layout_evidence import evidence_policy, require_citable


def exact(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= set(value) or set(value) - set(required) - set(optional):
        raise ContractError('unknown or missing contract fields')


def text_list(value, field):
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ContractError(field + ' must be an array of nonempty strings')


def nullable_text(value, field):
    if value is not None and not isinstance(value, str):
        raise ContractError(field + ' must be text or null')


def ranges(value, length):
    previous = 0
    if not isinstance(value, list) or not value:
        raise ContractError('explicit nonempty reading ranges required')
    for pair in value:
        if not isinstance(pair, list) or len(pair) != 2 or any(type(n) is not int for n in pair):
            raise ContractError('invalid code-point range')
        a, b = pair
        if not 0 <= previous <= a < b <= length:
            raise ContractError('reading ranges must be ordered, nonoverlapping and inside stored text')
        previous = b
    return value


def within(pair, coverage):
    return any(a <= pair[0] < pair[1] <= b for a, b in coverage)


def validate_evidence(item, source_id, mapping, text, coverage):
    exact(item, ('evidence_id','source_id','extraction_id','asset_id','sha256','locator','relation_to_claim'), ('quote',))
    if (item['source_id'], item['extraction_id'], item['asset_id'], item['sha256']) != (
            source_id, mapping['extraction_id'], mapping['output_asset_id'], mapping['output_sha256']):
        raise ContractError('evidence identity does not match current extraction')
    locator = item['locator']
    exact(locator, ('char_start','char_end'))
    pair = [locator['char_start'], locator['char_end']]
    ranges([pair], len(text))
    if not within(pair, coverage):
        raise ContractError('evidence outside actual authorized reading coverage')
    if 'quote' in item and item['quote'] != text[pair[0]:pair[1]]:
        raise ContractError('quotation does not match stored source text')
    require_citable(evidence_policy(mapping, text), text, *pair)


def empty_record(title, title_evidence_ref):
    """Unknown facts stay unknown; supplied title still requires provenance."""
    return dict(source_id=new_id('source'), canonical_work_id=new_id('work'), canonical_status='provisional',
        work=dict(title=title, authors=[], translators=[], publication=None, publication_year=None,
                  issue=None, volume=None, pages=None, doi=None, canonical_url=None, article_url=None, publisher=None),
        document_role='unknown', additional_roles=[], version_label=None, language=None,
        release_context=dict(channel_type='unknown', source_name=None, publisher=None, account=None, website=None,
                             source_url=None, released_at=None, declared_in_source=None, issuer_verification_status='unknown'),
        completeness=dict(relative_to_work='uncertain', acquired_version_integrity='uncertain', raw_page_count=None,
                          expected_raw_page_count=None, missing_components=[], basis_refs=[]), relations=[],
        field_provenance=[dict(fields=['/work/title'],basis='user_supplied',evidence_refs=[title_evidence_ref])],
        source_reading={k:dict(status='not_read',asset_ids=[],extraction_id=None,ranges=[],read_at=None,coverage_note='') for k in ('text','figures','tables')},
        bibliographic_verification=dict(status='unknown',fields_checked=[],basis_refs=[],checked_at=None),
        content_fact_check=dict(status='not_requested',scope=[],result_refs=[]),
        evidence_scope=dict(applies_to='captured_version',supports=[],explicit_constraints=[]))


def validate_record(record):
    template = empty_record('schema', 'schema')
    exact(record, template)
    for key in ('work','release_context','completeness','source_reading','bibliographic_verification','content_fact_check','evidence_scope'):
        exact(record[key], template[key])
    for key in ('source_id', 'canonical_work_id'):
        if not isinstance(record[key], str) or not record[key].strip():
            raise ContractError(key + ' must be nonempty text')
    if not isinstance(record['work']['title'], str) or not record['work']['title'].strip():
        raise ContractError('work.title must be nonempty text')
    for key in ('authors', 'translators'):
        if not isinstance(record['work'][key], list):
            raise ContractError('work.' + key + ' must be an array')
    for key in ('relations', 'field_provenance'):
        if not isinstance(record[key], list):
            raise ContractError(key + ' must be an array')
    text_list(record['additional_roles'], 'additional_roles')
    if len(set(record['additional_roles'])) != len(record['additional_roles']):
        raise ContractError('additional roles must not repeat')
    for key in ('version_label', 'language'):
        nullable_text(record[key], key)
    for key in ('issue', 'volume', 'doi', 'canonical_url', 'article_url', 'publisher'):
        nullable_text(record['work'][key], 'work.' + key)
    year = record['work']['publication_year']
    if year is not None and type(year) is not int:
        raise ContractError('publication_year must be integer or null')
    release = record['release_context']
    if release['channel_type'] not in {'journal','book','public_account','website','repository','personal','other','unknown'}:
        raise ContractError('invalid independent release channel')
    if release['issuer_verification_status'] not in {'source_declared','user_supplied','external_verified','unknown','conflicting'}:
        raise ContractError('invalid issuer verification status')
    for key in ('source_name','publisher','account','website','source_url','released_at','declared_in_source'):
        nullable_text(release[key], 'release_context.' + key)
    for key in ('raw_page_count', 'expected_raw_page_count'):
        value = record['completeness'][key]
        if value is not None and (type(value) is not int or value < 0):
            raise ContractError(key + ' must be a nonnegative integer or null')
    for value, field in ((record['completeness']['basis_refs'], 'completeness.basis_refs'),
                         (record['bibliographic_verification']['fields_checked'], 'fields_checked'),
                         (record['bibliographic_verification']['basis_refs'], 'bibliography.basis_refs'),
                         (record['content_fact_check']['scope'], 'fact_check.scope'),
                         (record['content_fact_check']['result_refs'], 'fact_check.result_refs'),
                         (record['evidence_scope']['supports'], 'evidence_scope.supports')):
        text_list(value, field)
    if record['bibliographic_verification']['status'] not in {'internal_only','external_verified','partial','conflicting','unknown'}:
        raise ContractError('invalid bibliographic verification status')
    if record['content_fact_check']['status'] not in {'not_requested','requested','completed','inconclusive'}:
        raise ContractError('invalid fact-check status')
    if record['canonical_status'] not in ('provisional','matched','disputed'):
        raise ContractError('invalid canonical status')
    roles = {'canonical_fulltext','abridged_version','abstract','user_excerpt','repost','secondary_summary','translation','presentation','notes','unknown'}
    if record['document_role'] not in roles or not set(record['additional_roles']) <= roles or record['document_role'] in record['additional_roles']:
        raise ContractError('invalid independent document roles')
    if record['completeness']['relative_to_work'] not in {'full','excerpt','abstract_only','partial','uncertain'} or record['completeness']['acquired_version_integrity'] not in {'complete','incomplete','uncertain'}:
        raise ContractError('invalid completeness')
    for people in (record['work']['authors'], record['work']['translators']):
        for person in people:
            exact(person, ('name','roles'))
            if not isinstance(person['name'], str) or not person['name'].strip():
                raise ContractError('person name must be nonempty text')
            text_list(person['roles'], 'person.roles')
    publication = record['work']['publication']
    if publication is not None:
        exact(publication, ('name','kind'))
        if publication['kind'] not in {'journal','book','site','other','unknown'}: raise ContractError('invalid publication kind')
    if record['work']['pages'] is not None: exact(record['work']['pages'], ('label','start','end'))
    provided = set()
    for provenance in record['field_provenance']:
        exact(provenance, ('fields','basis','evidence_refs'), ('note',))
        text_list(provenance['fields'], 'provenance.fields')
        text_list(provenance['evidence_refs'], 'provenance.evidence_refs')
        if provenance['basis'] not in {'in_document','source_declared','user_supplied','external_verified','inferred'} or not provenance['evidence_refs']:
            raise ContractError('identity provenance lacks a valid basis')
        for pointer in provenance['fields']:
            if not isinstance(pointer,str) or not pointer.startswith('/') or pointer == '/':
                raise ContractError('provenance must use actual JSON Pointers')
            value = record
            try:
                for part in pointer[1:].split('/'):
                    key = part.replace('~1','/').replace('~0','~')
                    value = value[int(key)] if isinstance(value,list) else value[key]
            except (KeyError,IndexError,ValueError,TypeError) as exc:
                raise ContractError('provenance pointer does not resolve') from exc
        provided.update(provenance['fields'])
    def leaves(value,path):
        if isinstance(value,dict):
            return [p for k,v in value.items() for p in leaves(v,path+'/'+k)]
        if isinstance(value,list):
            return [p for i,v in enumerate(value) for p in leaves(v,path+'/'+str(i))]
        return [] if value in (None,'','unknown') else [path]
    judgments = [p for k in ('work','document_role','additional_roles','version_label','language','release_context') for p in leaves(record[k],'/'+k)]
    for pointer in judgments:
        if not any(pointer == p or pointer.startswith(p + '/') for p in provided):
            raise ContractError('nonempty identity field lacks provenance: ' + pointer)
    for item in record['relations']:
        exact(item, ('relation_id','type','target_source_id','basis_refs','status'))
        text_list(item['basis_refs'], 'relation.basis_refs')
        if item['status'] not in {'supported','user_confirmed','disputed'}:
            raise ContractError('invalid source relation status')
        if item['type'] not in {'abridged_from','translated_from','excerpted_from','reposted_from','summarized_from','revised_from'} or not item['basis_refs']:
            raise ContractError('unsupported source relation')
    for item in record['completeness']['missing_components']: exact(item, ('position','kind','basis_refs'))
    for item in record['source_reading'].values(): exact(item, ('status','asset_ids','extraction_id','ranges','read_at','coverage_note'))
    for item in record['evidence_scope']['explicit_constraints']: exact(item, ('code','description','basis_refs'))
    if record['evidence_scope']['applies_to'] != 'captured_version': raise ContractError('evidence scope must describe the captured version')
    return record


class ReadingGuideAdapter:
    def __init__(self, provider=None):
        self.provider = provider

    def generate(self, record, content, mapping, consent, *, curation_data=None, reader_context=None):
        if self.provider is None:
            raise ContractError('private-reading-guide provider/version not configured; real content validation blocked')
        if not self.provider.is_mock:
            raise ContractError('DEV has no real model authorization composition; real content validation blocked')
        request = self.prepare_request(record, content, mapping, consent,
            curation_data=curation_data, reader_context=reader_context)
        return self.validate_response(record, content, mapping, consent, request, self.provider.generate(request))

    def prepare_request(self, record, content, mapping, consent, *, curation_data=None, reader_context=None,
                        authorization_time=None):
        """Pure preparation; a saved receipt can be checked at its original time.

        authorization_time is only for local receipt validation. Live Pilot
        access independently checks the current window at every boundary.
        """
        validate_record(record)
        skill_root = Path(__file__).resolve().parents[2] / 'skills/private-reading-guide'
        skill_version = json.loads((skill_root / 'version.json').read_text())
        skill_text = {}
        for relative, digest in skill_version['files'].items():
            data = (skill_root / relative).read_bytes()
            if sha(data) != digest:
                raise ContractError('ReadingGuide Skill version hash mismatch')
            skill_text[relative] = data.decode('utf-8')
        exact(consent, ('event_id','asset_id','sha256','extraction_id','ranges','model','expires_at'))
        text = content.decode('utf-8')
        if sha(content) != mapping['output_sha256'] or (consent['asset_id'],consent['sha256'],consent['extraction_id']) != (
                mapping['output_asset_id'],mapping['output_sha256'],mapping['extraction_id']):
            raise ContractError('authorization does not bind current source bytes')
        expiry = datetime.fromisoformat(consent['expires_at'])
        at = authorization_time if authorization_time is not None else datetime.now(timezone.utc)
        if at.tzinfo is None or expiry.tzinfo is None or expiry <= at or not consent['event_id']:
            raise ContractError('expired or ambiguous content authorization')
        authorized = ranges(consent['ranges'], len(text))
        if consent['model'] != self.provider.model or not self.provider.version:
            raise ContractError('model and adapter version must match authorization')
        # The provider receives exactly the allowed source slices, not old
        # summaries in place of source text. No image bytes enter this call.
        request = dict(source_record=deepcopy(record), authorized_sources=[dict(
            asset_id=mapping['output_asset_id'],extraction_id=mapping['extraction_id'],sha256=mapping['output_sha256'],
            char_start=a,char_end=b,text=text[a:b]) for a,b in authorized],
            curation_data=deepcopy(curation_data or {}), reader_context=reader_context,
            reading_coverage=deepcopy(record['source_reading']),
            style_rules=skill_text, skill_version=skill_version,
            style_rules_version='reading-guide/v2.1', consent_event_id=consent['event_id'])
        layout = evidence_policy(mapping, text, authorized)
        if layout is not None:
            request['evidence_policy'] = layout
        return request

    def validate_response(self, record, content, mapping, consent, request, response):
        text = content.decode('utf-8')
        authorized = consent['ranges']
        skill_version = request['skill_version']
        exact(response, ('guide_markdown','evidence','claims','evidence_links','unresolved_issues','coverage','coverage_statement'))
        actual = ranges(response['coverage'], len(text))
        if not all(within(pair, authorized) for pair in actual):
            raise ContractError('provider reported reading beyond authorization')
        evidence_ids = set()
        for evidence in response['evidence']:
            validate_evidence(evidence, record['source_id'], mapping, text, actual)
            if evidence['evidence_id'] in evidence_ids: raise ContractError('duplicate evidence ID')
            evidence_ids.add(evidence['evidence_id'])
        for claim in response['claims']:
            exact(claim, ('claim_id','text','basis','evidence_refs','conditions'))
            if claim['basis'] not in {'source_statement','interpretation','fictional_setting'} or not claim['evidence_refs'] or not set(claim['evidence_refs']) <= evidence_ids:
                raise ContractError('claim lacks current source evidence')
        paragraphs = [p for p in response['guide_markdown'].split('\n\n') if p.strip()]
        linked = set()
        for link in response['evidence_links']:
            exact(link, ('paragraph_index','evidence_refs'))
            if type(link['paragraph_index']) is not int or not 0 <= link['paragraph_index'] < len(paragraphs) or not link['evidence_refs'] or not set(link['evidence_refs']) <= evidence_ids:
                raise ContractError('guide paragraph evidence no longer matches')
            linked.add(link['paragraph_index'])
        if not paragraphs or linked != set(range(len(paragraphs))):
            raise ContractError('guide needs current evidence links for every paragraph')
        record = deepcopy(record)
        record['source_reading']['text'] = dict(status='read' if actual == [[0,len(text)]] else 'partial',
            asset_ids=[mapping['output_asset_id']], extraction_id=mapping['extraction_id'],ranges=actual,
            read_at=now(),coverage_note=response['coverage_statement'])
        guide_id = new_id('guide')
        curation = dict(schema_version='kb-curation/v2.1',source_id=record['source_id'],extraction_id=mapping['extraction_id'],
            claims=response['claims'],evidence=response['evidence'],keywords=[],tags=[],entities=[],
            value_assessment={'reasons':[]},gaps=[],review_summary=dict(status='structure_checked',reviewed_at=now(),
            scope='contract_and_evidence_only',issue_refs=response['unresolved_issues'],review_event_id=None),
            guide_generation=dict(guide_id=guide_id,skill='private-reading-guide',version=skill_version['revision'],
                skill_package_sha256=skill_version['package_sha256'],adapter_version=self.provider.version,
                model=self.provider.model,extraction_id=mapping['extraction_id'],coverage=actual,
                evidence_links=response['evidence_links'],unresolved_issues=response['unresolved_issues'],
                coverage_statement=response['coverage_statement'],input_sha256=sha(content),
                guide_sha256=sha(response['guide_markdown'].encode()),consent_event_id=consent['event_id'],
                validation_mode='offline_mock' if self.provider.is_mock else 'real_content_pending_review'), visual_observations=[])
        layout = evidence_policy(mapping, text)
        if layout is not None:
            curation['guide_generation']['evidence_policy'] = layout
        return record, curation, response['guide_markdown']
