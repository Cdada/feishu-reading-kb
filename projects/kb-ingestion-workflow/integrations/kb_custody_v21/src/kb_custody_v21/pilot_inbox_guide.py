"""Derive one exact guide from a prospectively authorized new text original.

The parent grants a bounded policy, not an invented future body hash. Only after
custody, V1 metadata lookup and qualified conversion does the child bind bytes.
P21 remains text-only. P27 preserves packaged images with explicit unread
coverage; neither profile bypasses technical review or authorizes image calls.
"""
import json
from pathlib import Path
import re

from .content import ReadingGuideAdapter, empty_record
from .contracts import ContractError, stable_key
from .conversion import sha
from .f05 import file_sha
from .pilot_existing import lookup_existing, validate_catalog
from .pilot_feishu import read_json
from .pilot_guide_preparation import prepare_delegated_guide
from .pilot_images import checked_image_facts
from .pilot_inbox_images import prepare_retained_images, validate_preservation_policy
from .pilot_inbox import inbox_policy
from .pilot_notices import PilotExecutionSession
from .pilot_text import MODEL, PilotTextSession
from .registry import encoded
from .runtime import no_symlinks


class PilotInboxGuide:
    def __init__(self, session, plan_path, *, wire):
        if not isinstance(session,PilotExecutionSession):
            raise ContractError('approved parent inbox session required')
        self.session,self.wire=session,wire
        self.path=no_symlinks(Path(plan_path).absolute())
        self.digest=file_sha(self.path)
        self.profile=p=read_json(self.path,65536)
        fields={'reference','legacy_catalog','formats','max_body_bytes','max_characters',
                'metadata_policy','policy','font_path','account_sha256','wait_seconds',
                'minimum_remaining_percent'}
        self.preserves_images = p.get('reference') in {'P27-three-format-guide-v1', 'P33-pdf-guide-v1'}
        formats = ['.md','.webarchive','.pdf'] if p.get('reference') == 'P33-pdf-guide-v1' else ['.md','.zip','.webarchive'] if self.preserves_images else ['.md','.txt']
        if (set(p) != fields or p['reference'] not in {'P21-new-material-guide-v1','P27-three-format-guide-v1','P33-pdf-guide-v1'}
                or p['formats'] != formats or p['metadata_policy'] != 'filename-provisional-v1'
                or type(p['max_body_bytes']) is not int or not 1 <= p['max_body_bytes'] <= 1024*1024
                or type(p['max_characters']) is not int or not 1 <= p['max_characters'] <= 200000
                or type(p['wait_seconds']) is not int or not 1 <= p['wait_seconds'] <= 3300
                or type(p['minimum_remaining_percent']) is not int or not 0 <= p['minimum_remaining_percent'] < 100
                or not isinstance(p['account_sha256'],str) or not re.fullmatch('[a-f0-9]{64}',p['account_sha256'])
                or not isinstance(p['policy'],dict)):
            raise ContractError('explicit bounded text-only derivation policy required')
        if self.preserves_images: validate_preservation_policy(p['policy'])
        if p['reference'] != 'P33-pdf-guide-v1' and p['policy'].get('reference') == 'P33-preserve-originals-v1':
            raise ContractError('larger PDF image preservation requires the explicit PDF guide profile')
        validate_catalog(p['legacy_catalog'])
        self.font=no_symlinks(Path(p['font_path']).absolute())
        if str(self.font) != p['font_path']: raise ContractError('absolute approved font required')
        self.value_digest=sha(encoded(p).encode())
        self.check()

    def check(self):
        s=self.session;s.check();p=inbox_policy(s)
        frozen=s.binding()
        service = s.approval.get('operation') == 'inbox-service'
        if (s.approval.get('new_material_guide') != dict(
                reference='P23-service-guide-v1' if service else 'P21-guide-delegation-v1',
                plan_sha256=self.digest,max_business_turns=p['max_files'] if service else 1)
                or not service and p['max_files'] != 1
                or sha(encoded(self.profile).encode()) != self.value_digest
                or frozen.get(str(self.path)) != self.digest or file_sha(self.path) != self.digest
                or frozen.get(str(self.font)) != file_sha(self.font)):
            raise ContractError('parent must explicitly delegate this exact one-material guide policy')
        return frozen

    def _material(self, pipeline, entry_id, *, authorization_time=None):
        frozen=self.check();s=self.session;p=self.profile;intake=pipeline.intake
        if (intake.scope.app_id,intake.scope.chat_id,intake.scope.user_id) != (
                s.approval['app_id'],s.approval['chat_id'],s.approval['account_id']):
            raise ContractError('guide intake differs from approved identity')
        row=intake.registry.entry(entry_id)
        events=intake.registry.rows('SELECT payload_json FROM custody_events WHERE dedupe_key=?',
                                   ('pilot-inbox-custody:'+entry_id,))
        if not events: raise ContractError('new guide requires original inbox custody evidence')
        custody=json.loads(events[0]['payload_json'])
        claim={k:custody[k] for k in ('entry_id','message_id','file_key','filename')}
        accepted=dict(claim,sha256=row['sha256'],size_bytes=row['size_bytes'])
        resumed=s.approval.get('operation')=='inbox-service' and s.value('service-resume:'+entry_id)==dict(
            custody,sha256=row['sha256'],size_bytes=row['size_bytes'])
        if resumed:
            run_path=no_symlinks(Path(s.approval['run_plan']).absolute())
            run=read_json(run_path,65536)
            if (frozen.get(str(run_path))!=file_sha(run_path) or run.get('reference') not in {'P25-service-run-v1', 'P30-image-choice-service-v1'}
                    or s.value('service-resume:'+entry_id) not in run.get('previous_materials',[])):
                raise ContractError('continued original must be selected by the frozen parent plan')
        if (not resumed and (custody['parent_approval_sha256'] != s.approval_sha
                or s.value('inbox-accepted:'+stable_key(claim['message_id'],claim['file_key'])) != accepted)
                or row['status'] != 'accepted' or row['custody_health'] != 'healthy'
                or Path(claim['filename']).suffix.lower() not in p['formats']):
            raise ContractError('only this parent new accepted text original may derive a guide')
        # Read exact source bytes again; previously accepted or cached metadata
        # cannot stand in for a healthy original at model submission time.
        path=no_symlinks(intake.runtime.path(row['relative_path']))
        if path.stat().st_size != row['size_bytes'] or file_sha(path) != row['sha256']:
            raise ContractError('accepted original bytes changed')
        frozen[str(path)]=row['sha256']
        old=lookup_existing(claim['filename'],row['sha256'],p['legacy_catalog'])
        if old['requires_review'] or not old['complete']:
            raise ContractError('existing V1 article or incomplete lookup requires review before new guide')
        job=intake._job(entry_id)
        if not job['task_id'] or job['stop_reason']:
            raise ContractError('qualified new conversion required')
        task=intake.conversion.service.ledger.get_task(job['task_id'])
        if task['status'] != 'ready_candidate':
            raise ContractError('new guide does not bypass conversion review')
        extraction_id=json.loads(task['quality_json'])['extraction']['extraction_id']
        _,mapping,root=intake.conversion.publication_extraction(extraction_id,expected_task_id=job['task_id'])
        with (root/'source.md').open('rb') as stream:body=stream.read(p['max_body_bytes']+1)
        if not body or len(body)>p['max_body_bytes']:
            raise ContractError('complete body exceeds approved byte scope; no silent truncation')
        try: text=body.decode('utf-8')
        except UnicodeError as error: raise ContractError('qualified UTF-8 body required') from error
        if not text.strip() or len(text)>p['max_characters']:
            raise ContractError('complete body exceeds approved character scope; no silent truncation')
        if (mapping['source_asset_id'] != row['asset_id'] or mapping['source_sha256'] != row['sha256']
                or mapping['output_sha256'] != sha(body)):
            raise ContractError('converted body no longer belongs to the accepted original')
        image_material = None
        if self.preserves_images:
            image_material, image_files = prepare_retained_images(pipeline, entry_id, extraction_id,
                policy=p['policy'], parent_sha=s.approval_sha, plan_sha=self.digest)
            extraction_id = image_material['extraction_id']
            _,mapping,root=intake.conversion.publication_extraction(extraction_id,expected_task_id=job['task_id'])
            body=(root/'source.md').read_bytes();text=body.decode('utf-8')
            if len(body)>p['max_body_bytes'] or len(text)>p['max_characters']:
                raise ContractError('complete image expression exceeds approved body scope; no silent truncation')
            frozen.update(image_files)
        else:
            facts=checked_image_facts(intake.conversion,extraction_id,[],p['policy'])
            if facts['observations'] or p['policy']['image_count'] != 0 or p['policy']['per_asset']:
                raise ContractError('new text-only route cannot omit images or grant image calls')
        for name in ('source.md','extraction-map.json','image-inventory.json'):
            frozen[str(root/name)]=file_sha(root/name)
        record=empty_record(claim['filename'],'owner-filename:'+claim['message_id'])
        record.update(source_id='source_'+stable_key(row['asset_id']),
                      canonical_work_id='work_'+stable_key(row['asset_id']))
        record['evidence_scope']['explicit_constraints']=[
            dict(code='filename_title_unverified',description='标题来自本人投递的文件名，尚未核验正式篇名和出版信息。',
                 basis_refs=['owner-filename:'+claim['message_id']]),
            dict(code='registered_catalog_only',description='旧库查询仅覆盖 V1 登记元数据，未检索未登记的散落文件。',
                 basis_refs=['P20-v1-registered-metadata'])]
        if mapping['extractor'] == 'mineru-pipeline':
            record['evidence_scope']['explicit_constraints'].append(dict(code='pdf_ocr_not_proofread',
                description='正文来自本机 MinerU 识别，OCR、公式和表格未人工校对；页码为 PDF 物理页序。跨页表格保留分段，不推断合并。',
                basis_refs=[mapping['extraction_id']]))
        if image_material and image_material['images']:
            record['evidence_scope']['explicit_constraints'].append(dict(code='figures_not_read',
                description=f"本次导读仅依据正文；已保存 {len(image_material['images'])} 幅原图，未识读 {len(image_material['images'])} 幅。图片位置已保留，不把图注当作图像已读证据。",
                basis_refs=['P27-preserve-originals-v1']))
        from .image_availability import require_image_choice
        image_check = require_image_choice(intake.conversion, extraction_id)
        if image_check:
            missing_count = image_check['report']['missing_count']
            record['evidence_scope']['explicit_constraints'].append(dict(code='missing_images_accepted',
                description=f'本材料有 {missing_count} 处图片缺失或无法读取。本人已选择按缺图版本继续；导读仅依据已提供的正文，缺图位置已标记，不能据此判断缺失图像内容。',
                basis_refs=[image_check['binding']['report_sha256']]))
            frozen.update(image_check['decision']['evidence']['receipts'])
        consent=dict(event_id='parent-'+stable_key(s.approval_sha,self.digest,entry_id),
            asset_id=mapping['output_asset_id'],sha256=sha(body),extraction_id=extraction_id,
            ranges=[[0,len(text)]],model=MODEL,expires_at=s.approval['expires_at'])
        args=dict(curation_data={},reader_context=None)
        request=ReadingGuideAdapter(PilotTextSession).prepare_request(record,body,mapping,consent,
            authorization_time=authorization_time,**args)
        derived=dict(reference='P21-derived-new-material-v1',parent_approval_sha256=s.approval_sha,
            guide_plan_sha256=self.digest,entry_id=entry_id,task_id=job['task_id'],extraction_id=extraction_id,
            output_asset_id=mapping['output_asset_id'],original_sha256=row['sha256'],
            body_sha256=sha(body),request_sha256=sha(encoded(request).encode()),
            catalog_scope=old['scope'],catalog_complete=True,catalog_match_count=0)
        if image_material is not None:
            derived['image_material'] = image_material
        if image_check:
            derived['image_choice'] = image_check['decision']
        return frozen,derived,consent,args,record

    def __call__(self, pipeline, entry_id):
        frozen,derived,consent,args,record=self._material(pipeline,entry_id)
        capacity=pipeline.resources.check()
        if capacity['state'] != 'resources_ready': return capacity
        claim=dict(parent_approval_sha256=self.session.approval_sha,guide_plan_sha256=self.digest,
                   entry_id=entry_id,original_sha256=derived['original_sha256'])
        # Stable per-original custody event persists beyond a window or renamed
        # reupload. A new parent cannot reset an already reserved model attempt.
        service = self.session.approval.get('operation') == 'inbox-service'
        parent_key='pilot-inbox-guide-parent:'+self.session.approval_sha
        if service: parent_key+=':'+entry_id
        with pipeline.registry.transaction() as db:
            reserved=[json.loads(r['payload_json']) for r in db.execute(
                "SELECT payload_json FROM custody_events WHERE event_type='pilot_inbox_guide_reserved'")]
            entries={r['entry_id'] for r in reserved if r.get('parent_approval_sha256')==self.session.approval_sha}
            if entry_id not in entries and len(entries)>=inbox_policy(self.session)['max_files']:
                raise ContractError('parent guide material slots exhausted')
            for key in ('pilot-inbox-guide-source:'+derived['original_sha256'],parent_key):
                saved=db.execute('SELECT payload_json FROM custody_events WHERE dedupe_key=?',(key,)).fetchone()
                if saved:
                    if json.loads(saved['payload_json']) != claim:
                        raise ContractError('new material guide slot already reserved; inspect without new budget')
                else:
                    pipeline.registry.event(db,'pilot_inbox_guide_reserved',entry_id,key,claim)
        plan=dict(self.profile,record=record)
        if self.preserves_images:
            plan.update(policy=derived['image_material']['policy'],images=derived['image_material']['images'])
        def inputs(current_pipeline,current_entry):
            current,binding,current_consent,current_args,current_record=self._material(current_pipeline,current_entry)
            if current_record != record: raise ContractError('provisional source metadata changed')
            return current,binding,current_consent,current_args
        return prepare_delegated_guide(pipeline,entry_id,session=self.session,plan=plan,
            plan_digest=self.digest,font=self.font,wire=self.wire,check=self.check,inputs=inputs)
