"""Retain original images for a prospectively approved, text-led guide.

Local preparation is reusable; no image model is invoked. A completed snapshot
binds the original extraction, each image and the rewritten expression. Partial
local attempts can be replaced without changing any original or model budget.
"""
import json
from pathlib import Path

from .contracts import ContractError, stable_key, new_id
from .conversion import durable_new, sha
from .f05 import file_sha
from .image_policy import ImagePolicies
from .pilot_images import checked_image_facts
from .registry import encoded


def validate_preservation_policy(value):
    pdf = isinstance(value, dict) and value.get('reference') == 'P33-preserve-originals-v1'
    if (not isinstance(value, dict) or set(value) != {'reference', 'reading', 'max_images', 'max_total_bytes'}
            or value['reference'] not in {'P27-preserve-originals-v1','P33-preserve-originals-v1'} or value['reading'] != 'not_requested'
            or type(value['max_images']) is not int or not 1 <= value['max_images'] <= (500 if pdf else 200)
            or type(value['max_total_bytes']) is not int or not 1 <= value['max_total_bytes'] <= (100_000_000 if pdf else 10_000_000)):
        raise ContractError('bounded original-image preservation without model reading required')


def prepare_retained_images(pipeline, entry_id, extraction_id, *, policy, parent_sha, plan_sha):
    validate_preservation_policy(policy)
    conversion, registry = pipeline.intake.conversion, pipeline.registry
    images = conversion.registered_images(extraction_id)
    if len(images) > policy['max_images']:
        raise ContractError('original images exceed approved retention limits; review without dropping images')
    _, original_map, _ = conversion.extraction(extraction_id)
    if any(i['role'] != 'extracted_original' for i in images):
        raise ContractError('unclassified image requires review; cannot silently omit it')
    binding = dict(entry_id=entry_id, parent_approval_sha256=parent_sha, guide_plan_sha256=plan_sha,
                   extraction_id=extraction_id, map_sha256=sha(encoded(original_map).encode()),
                   images_sha256=sha(encoded(images).encode()), policy=policy)
    key = 'pilot-inbox-images:'+stable_key(parent_sha, plan_sha, entry_id)
    cached = registry.rows('SELECT payload_json FROM custody_events WHERE dedupe_key=?', (key,))
    if cached:
        saved = json.loads(cached[0]['payload_json'])
        if saved['binding'] != binding:
            raise ContractError('saved image preparation belongs to different source bytes or policy')
        result_path = conversion.runtime.checked(conversion.runtime.path(saved['relative_path']))
        if file_sha(result_path) != saved['sha256']:
            raise ContractError('saved image preparation changed')
        result = json.loads(result_path.read_bytes())
    else:
        engine = ImagePolicies(registry)
        decision = engine.decide(images, dict(material_type='unknown', reliable=False, basis_refs=[]),
            current=dict(strategy=1, confirmation_ref='parent:'+parent_sha),
            budget_bytes=policy['max_total_bytes'], max_count=policy['max_images'])
        if decision['exception_check']['status'] != 'passed':
            raise ContractError('original images exceed approved retention limits; review without dropping images')
        attempt = conversion.runtime.checked(conversion.runtime.root/'conversion-work'/new_id('images'))
        attempt.mkdir(exist_ok=False)
        executed = engine.execute(decision, images, attempt/'retained')
        if executed['execution_result']['status'] != 'completed':
            raise ContractError('local image preservation did not complete; original images retained')
        replacements = {}
        for item in images:
            output = next(o for o in executed['execution_result']['per_asset_results'] if item['asset_id'] in o['derived_from'])
            replacements[item['original_target']] = dict(asset_id=item['asset_id'],
                local_path='images/'+Path(output['path']).name, representation_asset_id=output['asset_id'])
        _, revised = conversion.reexpress(extraction_id, replacements)
        result = dict(extraction_id=revised['extraction_id'], images=images, policy=executed)
        result_path = attempt/'prepared.json'
        durable_new(result_path, encoded(result).encode())
        with registry.transaction() as db:
            registry.event(db, 'pilot_inbox_images_prepared', entry_id, key,
                dict(binding=binding, relative_path=result_path.relative_to(conversion.runtime.root).as_posix(),
                     sha256=file_sha(result_path)))
    if result['images'] != images:
        raise ContractError('saved image inventory differs from the accepted source')
    facts = checked_image_facts(conversion, result['extraction_id'], images, result['policy'])
    if facts['observations'] or result['policy']['ai_image_reading']['selected_asset_ids']:
        raise ContractError('text-led first version cannot claim an image-model reading')
    frozen = {str(result_path):file_sha(result_path)}
    for _, _, root in conversion.extraction_ancestry(result['extraction_id']):
        for name in ('source.md', 'extraction-map.json', 'image-inventory.json'):
            frozen[str(root/name)] = file_sha(root/name)
    for image in images:
        frozen[image['path']] = image['sha256']
    for output in result['policy']['execution_result']['per_asset_results']:
        frozen[output['path']] = output['sha256']
    return result, frozen
