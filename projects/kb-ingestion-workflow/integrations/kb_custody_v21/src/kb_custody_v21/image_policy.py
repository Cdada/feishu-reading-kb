"""Versioned image preferences, bounded execution and explicit E1–E8 decisions."""
from copy import deepcopy
import io
from pathlib import Path

from PIL import Image, ImageOps, ImageDraw

from .contracts import ContractError, new_id
from .conversion import sha, durable_new
from .registry import now, encoded

BUDGETS = dict(public_account_or_web=5_000_000, academic_paper=10_000_000,
               research_report=20_000_000, visual_collection=30_000_000, unknown=10_000_000)
STRATEGIES = {
    1: ('all', 'none', 'none', 'none'), 2: ('none', 'all', 'none', 'none'),
    3: ('none', 'all', 'none', 'all'), 4: ('none', 'all', 'none', 'selected'),
    5: ('selected', 'nonlocal', 'by_visual_type', 'none'),
    6: ('none', 'all', 'by_visual_type', 'none'),
}
EXCLUDED_ROLES = {'raw_upload', 'ocr_page', 'model_intermediate', 'qa_screenshot', 'montage', 'diagnostic'}


class ImagePolicies:
    def __init__(self, registry):
        self.registry, self.runtime = registry, registry.runtime

    def save_default(self, material_type, strategy, confirmation_ref):
        if material_type not in BUDGETS or strategy not in STRATEGIES or not confirmation_ref:
            raise ContractError('explicit type, strategy and user confirmation reference required')
        version = new_id('preference')
        with self.registry.transaction() as db:
            db.execute('INSERT INTO image_preferences VALUES (?,?,?,?,?)',
                       (version, material_type, strategy, confirmation_ref, now()))
            db.execute('INSERT INTO active_image_preferences VALUES (?,?) ON CONFLICT(material_type) DO UPDATE SET preference_version=excluded.preference_version',
                       (material_type, version))
        return version

    def decide(self, images, assessment, *, current=None, selected=None, dense_choices=None,
               consent_assets=(), capabilities=None, accept_over_budget=False,
               budget_bytes=None, max_count=500, max_single_bytes=50_000_000, max_package_bytes=100_000_000):
        material = assessment['material_type']
        if material not in BUDGETS:
            raise ContractError('unknown material type')
        budget = BUDGETS[material] if budget_bytes is None else budget_bytes
        if type(budget) is not int or budget <= 0:
            raise ContractError('positive decimal-byte budget required')
        selected, dense_choices = selected or {}, dense_choices or {}
        capabilities = capabilities or {'archive': 'unknown', 'preview': True, 'reading': False}
        originals = [i for i in images if i['role'] == 'extracted_original']
        groups = {}
        for item in originals:
            groups.setdefault(item['sha256'], []).append(item)
        unique = {digest: items[0] for digest, items in groups.items()}
        ids = {i['asset_id'] for i in originals}
        total = sum(i['size_bytes'] for i in unique.values())
        over = total > budget
        exceptions = set()
        if any(i['role'] not in EXCLUDED_ROLES | {'extracted_original'} for i in images):
            exceptions.add('E7')
        preference = None
        if current:
            if set(current) != {'strategy', 'confirmation_ref'} or not current['confirmation_ref']:
                raise ContractError('current choice requires explicit confirmation')
            strategy, mode, origin = current['strategy'], 'user_current', 'user_current'
        elif not over:
            strategy, mode, origin = 1, 'below_threshold', 'below_threshold_default'
        else:
            prefs = self.registry.rows('''SELECT p.* FROM active_image_preferences a
                JOIN image_preferences p ON p.preference_version=a.preference_version WHERE a.material_type=?''', (material,))
            preference = prefs[0] if prefs else None
            strategy = preference['strategy'] if preference else None
            mode, origin = 'automatic_type_default', 'user_type_preference_confirmed'
            if not assessment.get('reliable') or not assessment.get('basis_refs'):
                exceptions.add('E1')
        if strategy is not None and strategy not in STRATEGIES:
            raise ContractError('strategy must be one of six confirmed options')
        local, cloud, preview, reading = STRATEGIES.get(strategy, ('none', 'none', 'none', 'none'))
        if len(unique) > max_count or any(i['size_bytes'] > max_single_bytes for i in unique.values()):
            exceptions.add('E4')
        local_ids = ids if local == 'all' else set(selected.get('local_original', ())) if local == 'selected' else set()
        read_ids = ids if reading == 'all' else set(selected.get('reading', ())) if reading == 'selected' else set()
        if (local == 'selected' and 'local_original' not in selected) or (reading == 'selected' and 'reading' not in selected):
            exceptions.add('E7')
        if (local_ids | read_ids | set(dense_choices)) - ids:
            exceptions.add('E7')
        if read_ids - set(consent_assets):
            exceptions.add('E5')
        if (cloud != 'none' and capabilities.get('archive') not in ('available', 'configured_pending_window')) or (
                preview != 'none' and not capabilities.get('preview')) or (read_ids and not capabilities.get('reading')):
            exceptions.add('E6')
        per_asset = []
        for aliases in groups.values():
            alias_ids = {i['asset_id'] for i in aliases}
            if len({i['size_bytes'] for i in aliases}) != 1 or len({
                    i.get('visual_type', 'unknown') for i in aliases}) != 1:
                exceptions.add('E7')
            choices = {dense_choices[aid] for aid in alias_ids if aid in dense_choices}
            if len(choices) > 1:
                exceptions.add('E7')
            choice = next(iter(choices)) if len(choices) == 1 else None
            explicit_ids = alias_ids & local_ids
            # Preserve the explicitly selected ID, independently of input order.
            # Budget/physical bytes are deduplicated; source identities are not.
            preferred = explicit_ids or alias_ids
            aid = min(preferred)
            item = next(i for i in aliases if i['asset_id'] == aid)
            dense = any(i.get('visual_type') != 'photo' or not i.get('basis_refs') for i in aliases)
            if dense and not explicit_ids:
                if choice not in {'local_original', 'local_placeholder', 'text_only'}:
                    exceptions.add('E3')
                elif choice == 'local_original':
                    local_ids = local_ids | {i for i in alias_ids if dense_choices.get(i) == choice}
                    aid = min(local_ids & alias_ids)
                    item = next(i for i in aliases if i['asset_id'] == aid)
                elif choice == 'local_placeholder' and preview == 'none':
                    exceptions.add('E7')
            mode_asset = 'local_original' if alias_ids & local_ids else (
                choice or 'local_placeholder' if dense and preview != 'none' else
                'compressed_preview' if preview != 'none' else 'text_only')
            per_asset.append(dict(original_asset_id=aid, alias_asset_ids=sorted(alias_ids),
                explicit_local_asset_ids=sorted(alias_ids & local_ids),
                visual_type=item.get('visual_type', 'unknown'),
                basis_refs=item.get('basis_refs', []), local_mode=mode_asset,
                representation_asset_id=aid if mode_asset == 'local_original' else None,
                reading_status='not_read', cloud_required=cloud == 'all' or (cloud == 'nonlocal' and mode_asset != 'local_original')))
        by_id = {i['asset_id']: i for i in originals}
        projected = sum(by_id[i['original_asset_id']]['size_bytes'] if i['local_mode'] == 'local_original' else
                        min(by_id[i['original_asset_id']]['size_bytes'], 350_000 if i['visual_type'] == 'photo' else 150_000)
                        if i['local_mode'] != 'text_only' else 0 for i in per_asset)
        # A saved preference never carries a prior source's over-budget waiver.
        waived = bool(current and accept_over_budget)
        if projected > max_package_bytes or (projected > budget and not waived):
            exceptions.add('E2')
        snapshot = dict(strategy=strategy, extracted_originals={'local': local, 'cloud': cloud},
            local_original_asset_ids=sorted(local_ids), local_preview={'mode': preview,
                'photos': {'max_width_px': 900, 'target_bytes_min': 150_000, 'target_bytes_max': 350_000, 'quality': 80, 'format': 'JPEG', 'upscale': False},
                'dense_visuals': {'mode': 'placeholder', 'width_range': [600, 800], 'target_max_bytes': 150_000, 'detail_readable': False}},
            ai_image_reading={'mode': reading, 'selected_asset_ids': sorted(read_ids)},
            over_budget_waiver=waived, max_package_bytes=max_package_bytes,
            confirmation_ref=current['confirmation_ref'] if current else None)
        return dict(policy_version='image-policy/v2.1', material_type=material, local_budget_mb=budget / 1_000_000,
            budget_basis='unique_extracted_original_sha256_bytes', image_count=len(originals), unique_image_count=len(unique),
            total_unique_bytes=total, threshold_exceeded=over, user_policy_source=origin,
            policy_applies_to='material_type_default' if preference else 'current_source',
            preference_version=preference['preference_version'] if preference else None,
            applied_policy_snapshot=deepcopy(snapshot), **{k:deepcopy(snapshot[k]) for k in ('extracted_originals','local_original_asset_ids','local_preview','ai_image_reading')},
            application_mode=mode, material_type_assessment=deepcopy(assessment),
            exception_check={'performed': True, 'check_version': 'exceptions/v2.1', 'checked_at': now(),
                'status': 'triggered' if exceptions or strategy is None else 'passed',
                'triggered_codes': sorted(exceptions), 'basis_refs': assessment.get('basis_refs', [])},
            local_result_budget={'projected_bytes': projected, 'actual_bytes': None, 'budget_bytes': budget,
                'basis': 'unique_retained_image_asset_bytes'},
            execution_result={'status': 'needs_user_decision' if exceptions or strategy is None else 'planned',
                'per_asset_results': [], 'error_refs': []}, per_asset=per_asset)

    def execute(self, decision, images, destination, *, reader=None):
        result = deepcopy(decision)
        if result['exception_check']['status'] != 'passed':
            raise ContractError('image policy needs a user decision')
        destination = self.runtime.checked(destination)
        destination.mkdir(parents=True, exist_ok=False)
        by_id = {i['asset_id']: i for i in images}
        outputs = []
        try:
            for item in result['per_asset']:
                original = by_id[item['original_asset_id']]
                path = self.runtime.checked(original['path'])
                data = path.read_bytes()
                if len(data) != original['size_bytes'] or sha(data) != original['sha256']:
                    raise ContractError('E7: extracted original byte identity changed')
                for aid in item['alias_asset_ids']:
                    alias = by_id[aid]
                    if (alias['sha256'], alias['size_bytes']) != (original['sha256'], original['size_bytes']) or (
                            self.runtime.checked(alias['path']).read_bytes() != data):
                        raise ContractError('E7: same-SHA alias bytes changed')
                mode = item['local_mode']
                if mode == 'text_only':
                    continue
                if mode == 'local_original':
                    aid, output, suffix = item['original_asset_id'], data, path.suffix.lower()
                else:
                    aid, suffix = new_id('asset'), '.jpg'
                    with Image.open(io.BytesIO(data)) as image:
                        image = ImageOps.exif_transpose(image).convert('RGB')
                        image.thumbnail((900, 900) if mode == 'compressed_preview' else (700, 700))
                        if mode == 'local_placeholder':
                            ImageDraw.Draw(image).rectangle((0, 0, image.width, min(30, image.height)), fill='white')
                            ImageDraw.Draw(image).text((5, 5), 'PLACEHOLDER - use original for detail', fill='black')
                        stream = io.BytesIO()
                        image.save(stream, format='JPEG', quality=80, optimize=True)
                        output = stream.getvalue()
                    with Image.open(io.BytesIO(output)) as verified:
                        verified.verify()
                target = destination / (aid + suffix)
                durable_new(target, output)
                item['representation_asset_id'] = aid
                outputs.append(dict(asset_id=aid, derived_from=item['alias_asset_ids'], sha256=sha(output),
                    size_bytes=len(output), path=str(target), original_asset_id=original['asset_id']))
            actual = sum(v['size_bytes'] for v in {v['sha256']: v for v in outputs}.values())
            result['local_result_budget']['actual_bytes'] = actual
            snapshot = result['applied_policy_snapshot']
            if actual > snapshot['max_package_bytes'] or (actual > result['local_result_budget']['budget_bytes'] and not snapshot['over_budget_waiver']):
                raise ContractError('E2: actual retained images exceed budget')
            read_ids = result['ai_image_reading']['selected_asset_ids']
            observations = []
            if read_ids:
                if reader is None:
                    raise ContractError('E6: reading result adapter has not supplied actual observations')
                for aid in read_ids:
                    observation = reader.read(by_id[aid])
                    observations.append(observation)
                    if observation['reading_status'] == 'failed': raise ContractError('E6: actual image reading failed')
                    for item in result['per_asset']:
                        if aid in item['alias_asset_ids']: item['reading_status'] = observation['reading_status']
            result['execution_result'].update(status='local_completed_archive_pending' if any(i['cloud_required'] for i in result['per_asset']) else 'completed',
                                               per_asset_results=outputs,visual_observations=observations)
        except (OSError, ValueError) as exc:
            # Preserve originals and generated candidates; no automatic fallback.
            result['execution_result'].update(status='failed', error_refs=[str(exc)])
        return result
