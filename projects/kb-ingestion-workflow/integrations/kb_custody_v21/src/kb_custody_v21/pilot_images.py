"""Bind existing image results to the accepted extraction; no image/model calls."""
from copy import deepcopy

from .contracts import ContractError
from .f05 import file_sha


def checked_image_facts(conversion, extraction_id, images, policy):
    if policy['exception_check']['status'] != 'passed' or policy['execution_result']['status'] not in {
            'completed', 'local_completed_archive_pending'}:
        raise ContractError('Pilot image policy must be completed before guide preparation')
    chain = conversion.extraction_ancestry(extraction_id)
    mapping = chain[-1][1]
    inventory = {i['asset_id']: i for i in conversion.registered_images(extraction_id) if i['role'] == 'extracted_original'}
    by_id = {i['asset_id']: i for i in images}
    if len(by_id) != len(images) or set(by_id) != set(inventory):
        raise ContractError('Pilot images must match the registered original image inventory')
    for aid, stored in inventory.items():
        item = by_id[aid]
        path = conversion.runtime.checked(item['path'])
        if (item['role'], item['sha256'], item['size_bytes'], path) != (
                stored['role'], stored['sha256'], stored['size_bytes'], conversion.runtime.checked(stored['path'])):
            raise ContractError('Pilot image identity or path differs from registered extraction')
        if path.stat().st_size != stored['size_bytes'] or file_sha(path) != stored['sha256']:
            raise ContractError('Pilot original image bytes changed')
    covered = [aid for item in policy['per_asset'] for aid in item['alias_asset_ids']]
    if len(covered) != len(set(covered)) or set(covered) != set(inventory):
        raise ContractError('Pilot policy must cover each registered image identity once')
    for output in policy['execution_result']['per_asset_results']:
        path = conversion.runtime.checked(output['path'])
        if output['original_asset_id'] not in inventory or path.stat().st_size != output['size_bytes'] or file_sha(path) != output['sha256']:
            raise ContractError('Pilot image representation changed')
    selected = policy['ai_image_reading']['selected_asset_ids']
    observations = deepcopy(policy['execution_result'].get('visual_observations', []))
    observed = [o['image_asset_id'] for o in observations]
    if len(selected) != len(set(selected)) or set(selected) - set(inventory) or sorted(observed) != sorted(selected):
        raise ContractError('Pilot selected images and actual observations disagree')
    ancestors = {m['extraction_id'] for _, m, _ in chain}
    for observation in observations:
        stored = inventory[observation['image_asset_id']]
        locator = observation.get('source_locator', {})
        if (locator.get('extraction_id') not in ancestors or
                locator.get('source_sha256', mapping['source_sha256']) != mapping['source_sha256'] or
                observation.get('original_sha256', stored['sha256']) != stored['sha256'] or
                observation.get('cloud_original_asset_id') != stored['asset_id']):
            raise ContractError('Pilot visual observation belongs to another original or extraction')
        if observation.get('reading_status') not in {'read', 'partially_read'} or observation.get('validation_mode') not in {
                'offline_mock', 'real_model_observation_pending_quality_review'}:
            raise ContractError('Pilot visual observation is unfinished or has unsupported provenance')
        if any(not isinstance(observation.get(k), str) or not observation[k] for k in
               ('observation_id', 'reader', 'read_at', 'consent_event_id')):
            raise ContractError('Pilot visual observation lacks reading provenance')
    count, total = len(observations), len(inventory)
    partial = any(o['reading_status'] == 'partially_read' for o in observations)
    note = f'记录 {count}/{total} 幅原图的实际识读结果；未识读 {total-count} 幅。'
    if partial:
        note += '部分图像仅部分可读，未确认项保留在图像记录中。'
    if any(o['validation_mode'] == 'real_model_observation_pending_quality_review' for o in observations):
        note += '真实模型识读质量仍待复核。'
    if any(o['validation_mode'] == 'offline_mock' for o in observations):
        note += '包含离线合成识读结果。'
    owners = {i['extraction_id'] for i in inventory.values()}
    figures = dict(status='not_read' if not count else 'partial' if count < total or partial else 'read',
        asset_ids=sorted(selected), extraction_id=next(iter(owners)) if len(owners)==1 else mapping['extraction_id'] if not owners else None, ranges=[],
        read_at=max((o['read_at'] for o in observations), default=None), coverage_note=note)
    return dict(figures=figures, observations=observations)


def with_image_facts(result, facts):
    record, curation, guide = deepcopy(result)
    record['source_reading']['figures'] = facts['figures']
    # Keep the text guide and its paragraph evidence unchanged. These facts came
    # from the separately bound image reader, not from the body model.
    curation['visual_observations'] = facts['observations']
    return record, curation, guide
