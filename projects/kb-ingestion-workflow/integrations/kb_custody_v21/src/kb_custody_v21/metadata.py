"""Formal v2.1 package metadata: immutable identities, no live cloud locators."""
from copy import deepcopy
from pathlib import PurePosixPath
import re
from .content import exact, validate_record
from .contracts import ContractError


def relative_path(value):
    if value is None:
        return
    if not isinstance(value, str) or not value or '\\' in value or ':' in value or any(p in {'', '.', '..'} for p in value.split('/')) or PurePosixPath(value).is_absolute():
        raise ContractError('package paths must be relative POSIX paths')


def asset(asset_id, kind, filename, data, *, local_path=None, derived_from=(), extraction_id=None):
    import hashlib
    return dict(asset_id=asset_id,kind=kind,media_type='text/markdown' if kind=='extracted_text' else 'application/octet-stream',
        captured_filename=filename,uploaded_filename=None,size_bytes=len(data),sha256=hashlib.sha256(data).hexdigest(),
        md5=hashlib.md5(data).hexdigest(),local_path=local_path,derived_from=list(derived_from),
        extraction_id=extraction_id,immutable=True)


def validate_metadata(value):
    exact(value, ('schema_version','source_record','raw_asset_id','assets','extraction','derived_asset_policy',
                 'classification','privacy','publication','registry_namespace'), ('archive_snapshot','lineage'))
    if value['schema_version'] != 'kb-source-group/v2.1':
        raise ContractError('explicit metadata v2.1 required; no V1 reinterpretation')
    validate_record(value['source_record'])
    exact(value['classification'], ('source_family','content_nature'))
    exact(value['privacy'], ('sensitivity','brief_visibility'))
    exact(value['publication'], ('publication_id','state','guide_path','curation_path','preview_path','confirmation_event_id','generated_at'), ('revision_of',))
    for key in ('guide_path','curation_path','preview_path'): relative_path(value['publication'][key])
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', value['registry_namespace']):
        raise ContractError('registry namespace cannot be a machine path')
    by_id = {}
    for item in value['assets']:
        exact(item, ('asset_id','kind','media_type','captured_filename','uploaded_filename','size_bytes','sha256','md5','local_path','derived_from','extraction_id','immutable'))
        if item['asset_id'] in by_id or item['immutable'] is not True or type(item['size_bytes']) is not int or item['size_bytes'] < 0:
            raise ContractError('invalid immutable asset identity')
        if not re.fullmatch('[0-9a-f]{64}', item['sha256']): raise ContractError('invalid asset SHA-256')
        relative_path(item['local_path'])
        by_id[item['asset_id']] = item
    raw = by_id.get(value['raw_asset_id'])
    if not raw or raw['kind'] != 'raw_upload' or raw['local_path'] is not None or not re.fullmatch('[0-9a-f]{32}', raw['md5']):
        raise ContractError('raw asset must retain identity without embedding the large original')
    for item in by_id.values():
        if set(item['derived_from']) - set(by_id) or item['asset_id'] in item['derived_from']:
            raise ContractError('invalid asset derivation')
    summary = value['extraction']
    exact(summary, ('extraction_id','source_asset_id','output_asset_id','extractor','extractor_version','mode','extracted_at','map_path',
                    'text_extraction_status','ocr_status','figure_status','table_status'))
    relative_path(summary['map_path'])
    if summary['source_asset_id'] != raw['asset_id'] or summary['output_asset_id'] not in by_id:
        raise ContractError('extraction asset binding mismatch')
    exact(summary['ocr_status'], ('execution','quality','issues'))
    exact(summary['figure_status'], ('inventory','extracted_count'))
    exact(summary['table_status'], ('inventory','extraction','reading'))
    policy = value['derived_asset_policy']
    if policy['policy_version'] != 'image-policy/v2.1' or policy['execution_result']['status'] not in {'completed','local_completed_archive_pending'}:
        raise ContractError('image policy is incomplete')
    for key in ('extracted_originals','local_original_asset_ids','local_preview','ai_image_reading'):
        if policy[key] != policy['applied_policy_snapshot'][key]:
            raise ContractError('image policy expanded fields drifted from snapshot')
    return value


def render_metadata(record, mapping, policy, assets, publication, *, classification, privacy):
    summary = {key: mapping[key] for key in ('extraction_id','source_asset_id','output_asset_id','extractor','extractor_version','mode','extracted_at')}
    summary.update(map_path='提取/extraction-map.json',text_extraction_status='completed',
                   ocr_status=dict(execution='not_used' if mapping['mode'] in {'text_direct','webarchive_offline','markdown_bundle_offline','custody_office_document_v1'} else 'unknown',quality='not_assessed',issues=[]),
                   figure_status=dict(inventory='unknown',extracted_count=policy['unique_image_count']),
                   table_status=dict(inventory='unknown',extraction='unknown',reading='not_read'))
    value = dict(schema_version='kb-source-group/v2.1',source_record=deepcopy(record),raw_asset_id=mapping['source_asset_id'],
                 assets=deepcopy(assets),extraction=summary,derived_asset_policy=deepcopy(policy),classification=deepcopy(classification),
                 privacy=deepcopy(privacy),publication=deepcopy(publication),registry_namespace='kb-v21-dev')
    return validate_metadata(value)
