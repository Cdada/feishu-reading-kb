"""Derive one exact text request from an explicitly approved parent material.

The frozen plan supplies metadata, text ranges and image policy. Version 1
binds a zero-image conversion at runtime; version 2 selects an already qualified
expression and completed image handling by their explicit identities.
The child approval records delegated scope; it cannot authorize itself.
"""
from pathlib import Path
import json
import re

from .content import ReadingGuideAdapter, validate_record
from .contracts import ContractError, stable_key, validate_id
from .conversion import durable_new, sha
from .f05 import file_sha
from .pilot_feishu import read_json
from .pilot_images import checked_image_facts
from .pilot_notices import PilotExecutionSession
from .pilot_text import MODEL, PilotTextSession
from .pilot_text_grounding import EVIDENCE_PROTOCOL
from .pilot_revision import digest, failed_receipt
from .pilot_continuation import blocked_receipt
from .registry import encoded
from .runtime import no_symlinks


class PilotGuidePreparation:
    """Parent-bound text generation for the original or an explicitly qualified expression."""
    def __init__(self, session, plan_path, *, wire):
        if not isinstance(session, PilotExecutionSession):
            raise ContractError('independently approved parent Pilot session required')
        self.session, self.wire = session, wire
        self.path = no_symlinks(Path(plan_path).absolute())
        self.digest = file_sha(self.path)
        self.plan = p = read_json(self.path)
        fields = {'reference','run_plan_path','run_plan_sha256','body_sha256','record','ranges',
                  'reader_context','curation_data','policy','font_path','account_sha256','wait_seconds'}
        prepared = p.get('reference') == 'P11-guide-derivation-v2'
        if 'minimum_remaining_percent' in p:
            fields.add('minimum_remaining_percent')
        if type(p.get('minimum_remaining_percent',5)) is not int or not 0 <= p.get('minimum_remaining_percent',5) < 100:
            raise ContractError('explicit subscription reserve must be an integer from 0 to 99')
        if (set(p) != (fields | {'material', 'images'} if prepared else fields)
                or p['reference'] not in {'P11-guide-derivation-v1', 'P11-guide-derivation-v2'}
                or any(not isinstance(p[k], str) or not re.fullmatch('[a-f0-9]{64}', p[k])
                       for k in ('run_plan_sha256','body_sha256','account_sha256'))
                or type(p['wait_seconds']) is not int or not 1 <= p['wait_seconds'] <= 3300):
            raise ContractError('exact frozen guide derivation plan required')
        if prepared:
            material = p['material']
            if not isinstance(material, dict) or set(material) != {'entry_id', 'task_id', 'extraction_id'}:
                raise ContractError('exact prepared material identities required')
            for key, kind in (('entry_id', 'entry'), ('task_id', 'task'), ('extraction_id', 'extraction')):
                validate_id(material[key], kind)
            if not isinstance(p['images'], list):
                raise ContractError('explicit prepared image inventory required')
        validate_record(p['record'])
        self.value_digest = sha(encoded(p).encode())
        self.run_path = no_symlinks(Path(p['run_plan_path']).absolute())
        self.font = no_symlinks(Path(p['font_path']).absolute())
        if str(self.run_path) != p['run_plan_path'] or str(self.font) != p['font_path']:
            raise ContractError('absolute frozen run and font paths required')
        self.prefix = 'pilot-run:'+p['run_plan_sha256']+':'
        self.check()

    def check(self):
        s = self.session
        s.check()
        frozen = s.binding()
        if (sha(encoded(self.plan).encode()) != self.value_digest
                or s.approval.get('guide_plan_sha256') != self.digest
                or frozen.get(str(self.path)) != self.digest or file_sha(self.path) != self.digest
                or frozen.get(str(self.run_path)) != self.plan['run_plan_sha256']
                or file_sha(self.run_path) != self.plan['run_plan_sha256']
                or frozen.get(str(self.font)) != file_sha(self.font)):
            raise ContractError('parent must explicitly authorize this exact guide plan and font')
        return frozen

    def _inputs(self, pipeline, entry_id):
        frozen = self.check()
        intake, p, s = pipeline.intake, self.plan, self.session
        scope = intake.scope
        if (scope.app_id, scope.chat_id, scope.user_id) != (
                s.approval['app_id'], s.approval['chat_id'], s.approval['account_id']):
            raise ContractError('guide intake differs from approved parent identity')
        run_plan = read_json(self.run_path, 65536)
        row = intake.registry.entry(entry_id)
        path = intake.runtime.path(row['relative_path'])
        original = dict(entry_id=entry_id, path=str(path), sha256=row['sha256'], size_bytes=row['size_bytes'])
        if (s.value(self.prefix+'original') != original or row['status'] != 'accepted'
                or row['custody_health'] != 'healthy' or row['sha256'] != run_plan['sample_sha256']
                or frozen.get(str(path)) != row['sha256']):
            raise ContractError('only this run\'s byte-bound accepted material may derive a request')
        job = intake._job(entry_id)
        if not job['task_id'] or job['stop_reason']:
            raise ContractError('guide requires a completed qualified local conversion')
        task = intake.conversion.service.ledger.get_task(job['task_id'])
        material = p.get('material')
        if material is not None:
            if (material != run_plan.get('prepared_material') or material['entry_id'] != entry_id
                    or material['task_id'] != job['task_id']):
                raise ContractError('prepared expression differs from the approved material and task')
            extraction_id = material['extraction_id']
        else:
            if 'prepared_material' in run_plan:
                raise ContractError('prepared material requires a versioned expression guide plan')
            extraction_id = json.loads(task['quality_json'])['extraction']['extraction_id']
        _, mapping, root = intake.conversion.publication_extraction(extraction_id, expected_task_id=job['task_id'])
        body = (root/'source.md').read_bytes()
        if (mapping['source_asset_id'] != row['asset_id'] or sha(body) != p['body_sha256']
                or mapping['output_sha256'] != p['body_sha256']):
            raise ContractError('derived body differs from the approved conversion bytes')
        revision = run_plan.get('revision')
        if revision:
            if revision['entry_id'] != entry_id:
                raise ContractError('revision must reuse the explicitly bound original entry')
            previous = revision['binding']
            receipt = failed_receipt(previous,p['record'],body,mapping,
                curation_data=p['curation_data'],reader_context=p['reader_context'])
            if (digest(receipt) != previous['previous_receipt_sha256']
                    or any(frozen.get(name) != value for name,value in receipt['files'].items())):
                raise ContractError('parent approval must freeze the complete previous failed evidence')
        continuation = run_plan.get('continuation')
        if continuation:
            receipt = blocked_receipt(continuation,p['record'],body,mapping,
                curation_data=p['curation_data'],reader_context=p['reader_context'])
            if (digest(receipt) != continuation['previous_receipt_sha256']
                    or any(frozen.get(name) != value for name,value in receipt['files'].items())):
                raise ContractError('parent approval must freeze the complete previous stopped evidence and source')
        images = p.get('images', ())
        facts = checked_image_facts(intake.conversion, extraction_id, images, p['policy'])
        if material is None:
            if facts['observations'] or p['policy']['image_count'] != 0 or p['policy']['per_asset']:
                raise ContractError('this first guide plan permits no image omission or image calls')
        else:
            # This route reuses completed local image handling. It never creates
            # images, runs a visual model, or infers consent from a saved policy.
            paths = [dict(path=str(root/'source.md'), sha256=p['body_sha256']),
                     dict(path=str(root/'extraction-map.json'), sha256=file_sha(root/'extraction-map.json'))]
            paths += list(images) + p['policy']['execution_result']['per_asset_results']
            for item in paths:
                path = no_symlinks(Path(item['path']).absolute())
                if (str(path) != item['path'] or frozen.get(str(path)) != item['sha256']
                        or file_sha(path) != item['sha256']):
                    raise ContractError('prepared text and image files must be explicitly frozen')
        consent = dict(event_id='parent-'+stable_key(s.approval_sha,self.digest,entry_id),
            asset_id=mapping['output_asset_id'], sha256=p['body_sha256'], extraction_id=extraction_id,
            ranges=p['ranges'], model=MODEL, expires_at=s.approval['expires_at'])
        args = dict(curation_data=p['curation_data'], reader_context=p['reader_context'])
        request = ReadingGuideAdapter(PilotTextSession).prepare_request(p['record'],body,mapping,consent,**args)
        frozen.update({str(root/'source.md'):p['body_sha256'],
                       str(root/'extraction-map.json'):file_sha(root/'extraction-map.json')})
        derived = dict(parent_approval_sha256=s.approval_sha, guide_plan_sha256=self.digest,
            entry_id=entry_id, task_id=job['task_id'], extraction_id=extraction_id,
            output_asset_id=mapping['output_asset_id'], body_sha256=p['body_sha256'],
            request_sha256=sha(encoded(request).encode()))
        if revision:
            derived['revision_sha256'] = digest(revision['binding'])
        if continuation:
            derived['continuation_sha256'] = digest(continuation)
        return frozen, derived, consent, args

    def __call__(self, pipeline, entry_id):
        run_plan = read_json(self.run_path,65536)
        return prepare_delegated_guide(pipeline,entry_id,session=self.session,plan=self.plan,
            plan_digest=self.digest,font=self.font,wire=self.wire,check=self.check,inputs=self._inputs,
            revision=run_plan.get('revision'),continuation=run_plan.get('continuation'))


def prepare_delegated_guide(pipeline, entry_id, *, session, plan, plan_digest, font, wire,
                            check, inputs, revision=None, continuation=None, daily_attempt=None, checked_attempt=None):
    """Shared execution after a version-specific parent grant validates inputs."""
    frozen, derived, consent, args = inputs(pipeline, entry_id)
    s, p = session, plan
    # Old grants retain one child per plan. The service explicitly reserves
    # bounded per-original slots before deriving distinct child roots.
    child_key=stable_key(s.approval_sha,plan_digest)
    if s.approval.get('operation') == 'inbox-service':
        # The inbox guide validates the multi-material parent grant and reserves
        # its durable slot before this shared executor is entered.
        grant=s.approval.get('new_material_guide',{})
        if grant.get('reference') != 'P23-service-guide-v1' or derived.get('entry_id') != entry_id:
            raise ContractError('explicit service guide delegation required')
        reserved=pipeline.registry.rows('SELECT payload_json FROM custody_events WHERE dedupe_key=?',
            ('pilot-inbox-guide-parent:'+s.approval_sha+':'+entry_id,))
        claim=dict(parent_approval_sha256=s.approval_sha,guide_plan_sha256=plan_digest,
                   entry_id=entry_id,original_sha256=derived.get('original_sha256'))
        if len(reserved)!=1 or json.loads(reserved[0]['payload_json'])!=claim:
            raise ContractError('service child requires its reserved original slot')
        child_key=stable_key(s.approval_sha,plan_digest,entry_id)
    root = no_symlinks(pipeline.intake.runtime.root/'conversion-work'/('pilot-text-'+child_key))
    root.mkdir(exist_ok=True)
    approval = dict(approved=True, root=str(root), model=MODEL, effort='medium',
        evidence_protocol=EVIDENCE_PROTOCOL,
        billing='chatgpt_subscription_only', automatic_business_retries=0,
        max_business_turns=1, wait_seconds=p['wait_seconds'],
        starts_at=s.approval['starts_at'], expires_at=s.approval['expires_at'],
        request_sha256=[derived['request_sha256']], account_sha256=p['account_sha256'],
        parent_approval_sha256=s.approval_sha, guide_plan_sha256=plan_digest)
    if 'minimum_remaining_percent' in p:
        approval['minimum_remaining_percent'] = p['minimum_remaining_percent']
    if revision:
        approval['revision_sha256'] = digest(revision['binding'])
    if continuation:
        approval['continuation_sha256'] = digest(continuation)
    for name, value in [('derivation.local.json',derived),('approval.local.json',approval)]:
        path = no_symlinks(root/name); raw = encoded(value).encode()
        check()
        if path.exists():
            if path.read_bytes() != raw:
                raise ContractError('saved child derivation differs; no new budget or overwrite')
        else:
            durable_new(path,raw)
    child_files = {str(root/name):file_sha(root/name) for name in ('derivation.local.json','approval.local.json')}
    def source():
        current, binding, _, _ = inputs(pipeline,entry_id)
        if binding != derived or current != frozen:
            raise ContractError('accepted source or parent derivation changed')
        if any(file_sha(Path(path)) != digest for path,digest in child_files.items()):
            raise ContractError('saved child approval or provenance changed')
        return dict(current, **child_files)
    def generate(*values,**kwargs):
        child = PilotTextSession(root,child_files[str(root/'approval.local.json')],
            checked_source=source, wire=wire, clock=s.clock)
        try:
            return child.generate_guide(*values,**kwargs)
        finally:
            child.close()
    source()
    return pipeline.prepare(entry_id,derived['extraction_id'],p['record'],consent,p['policy'],
        font_path=font, text_root=root, text_approval_sha=child_files[str(root/'approval.local.json')],
        checked_source=source, generate_guide=generate, images=p.get('images', ()),
        revision=revision['binding'] if revision else None,
        continuation=continuation, daily_attempt=daily_attempt, checked_attempt=checked_attempt, **args)
