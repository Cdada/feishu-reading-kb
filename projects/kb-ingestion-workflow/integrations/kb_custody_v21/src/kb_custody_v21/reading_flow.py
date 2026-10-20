"""Full reading organization after a fresh verified Feishu intake decision."""
from datetime import datetime, timezone
import hashlib
import json
import os
import re
from pathlib import Path

from .contracts import ContractError
from .daily_guide import DailyTextWire
from .daily_scheduler import MaterialProblem
from .daily_store import Deferred
from .daily_transport import durable_file
from .f05 import file_sha
from .pilot_decision import observed_reply
from .reading_bundle import build_bundle, READING, CARDS, TOPICS
from .reading_contract import require, sha, validate_input, MAX_INPUT_BYTES
from .reading_model import ReadingModel
from .reading_publish import prepare_plan, publish_plan, read_at, snapshot_library, MAX_LIBRARY_BYTES
from .reading_store import ReadingStore
from .layout_evidence import evidence_policy, checked_policy
from .registry import encoded
from .runtime import no_symlinks
from .safe_publish import open_root, root_identity, inspect_tree


class ReadingFlow:
    def __init__(self, backend, *, wire=None):
        self.backend=backend
        self.authority=backend.authority
        self.profile=self.authority.reading
        self.store=ReadingStore(backend.store)
        config=self.authority.config
        self.wire=wire if wire is not None else DailyTextWire(config['text_node'],self.profile.script,
            config['codex_binary'],self.authority.runtime.worktree,stop=backend.stop)
        self.model=ReadingModel(self.authority,self.store,self.wire,checked_source=self.checked_source)

    def check(self):
        self.backend.check_authorization(); self.profile.check()
        if self.backend.stop.is_set():
            raise Deferred('stopping')

    def binding(self,document):
        self.check()
        entry,_=self.backend._check_material(document)
        row=self.backend.pipeline.publisher._row(document['publication_id'])
        require(row['state']=='published' and row['entry_id']==document['entry_id'], 'reading requires approved published evidence')
        versions=self.backend.registry.rows('SELECT version FROM pilot_candidates WHERE publication_id=?',(document['publication_id'],))
        require(len(versions)==1,'one approved preview version required')
        decision=document['data']['decision']
        require(decision['command'] in ('确认入库','确认当前版本入库'),'only intake approval can start reading')
        for path,digest in decision['receipts'].items():
            require(file_sha(no_symlinks(Path(path)))==digest,'approved reading decision changed')
        return dict(document_id=document['message_id'],source_id=row['source_id'],extraction_id=document['data']['extraction_id'],
            source_sha256=entry['sha256'],publication_id=row['publication_id'],preview_sha256=row['preview_sha256'],
            preview_version=versions[0]['version'],decision_sha256=sha(decision),rules_sha256=self.profile.rules_sha256)

    def start(self,document):
        material=self.store.approve(self.binding(document),checked_access=self.check)
        self.backend.store.advance(document['message_id'],'confirmed','reading',
            user_action=None,data=dict(reading_material_id=material['material_id']))

    def _job(self,material):
        return no_symlinks(self.authority.root/'reading-jobs'/material['material_id'])

    def assets(self,document):
        entry,original=self.backend._check_material(document)
        publisher=self.backend.pipeline.publisher
        row=publisher._row(document['publication_id'])
        path=publisher._tree(row,row['publication_id'])
        expected=json.loads(row['files_json'])
        if sum(v['size_bytes'] for v in expected.values())+entry['size_bytes']>MAX_LIBRARY_BYTES//2:
            raise MaterialProblem('本份资料超出受控写入体积，需要 Codex 分批整理；已保存的原件保留，其他材料可继续处理。')
        root_fd=open_root(path,root_identity(path))
        try:
            files={name:read_at(root_fd,name) for name in expected}
            require(inspect_tree(root_fd,max_bytes=MAX_LIBRARY_BYTES,expected_files=expected)==expected,'approved source changed during read')
        finally:
            os.close(root_fd)
        with os.fdopen(self.authority.runtime.read_fd(original),'rb') as stream:
            raw=stream.read(entry['size_bytes']+1)
        require(len(raw)==entry['size_bytes'] and hashlib.sha256(raw).hexdigest()==entry['sha256'],'original bytes changed')
        # The exact received filename is metadata. Never treat it as a path.
        files['原件/source'+Path(document['filename']).suffix.lower()]=raw
        evidence={str(path/name):value['sha256'] for name,value in expected.items()}
        evidence[str(original)]=entry['sha256']
        return files,evidence

    def checked_source(self,source):
        document=self.backend.store.document(source['binding']['document_id'])
        require(self.binding(document)==source['binding'],'source, preview or decision no longer matches reading input')
        _,evidence=self.assets(document)
        for key in ('existing_cards','existing_topics'):
            for path,entry in source[key].items():
                target=self.profile.target(path)
                require(file_sha(target)==entry['sha256'],'test-library context changed during reading')
                evidence[str(target)]=entry['sha256']
        material=self.store.get(source['material_id'])
        for answer in material['detail'].get('answers',[]):
            for path,digest in answer['receipts'].items():
                require(file_sha(no_symlinks(Path(path)))==digest,'reading clarification receipt changed')
                evidence[path]=digest
        return evidence

    def _prepare_input(self,document,material):
        files,_=self.assets(document)
        metadata=json.loads(files['metadata.json'])
        body=files['提取/source.md'].decode('utf-8')
        record=metadata['source_record']
        snapshot=snapshot_library(self.profile)
        image_boundary=''
        if any('/images/' in p or 'image-notes' in p or '图片完整性' in p for p in files) or '【缺图' in body:
            image_boundary='本轮没有重新阅读图片。既有图像记录和缺图决定保留在入库依据中；本轮以文本和既有记录为限。'
        collections={}
        for path in snapshot['available_paths']:
            segments=path.split('/')
            if len(segments)==5 and segments[0]==READING and segments[-1]=='metadata.json':
                collections.setdefault(segments[1],set()).add(segments[2])
        excerpt_only=bool(re.search(r'[-_—]摘录(?:稿)?$',Path(document['filename']).stem))
        provenance=dict(source_record=record,existing_collections={k:sorted(v) for k,v in collections.items()},
            input_role='excerpt_only' if excerpt_only else 'supplied_material')
        if '提取/extraction-map.json' in files:
            layout = evidence_policy(json.loads(files['提取/extraction-map.json']), body)
            if layout is not None:
                provenance['evidence_policy'] = layout
        if 'curation.json' in files:
            retained_policy = json.loads(files['curation.json'])['guide_generation'].get('evidence_policy')
            if retained_policy is not None:
                checked_policy(retained_policy, body)
                require('evidence_policy' not in provenance or provenance['evidence_policy'] == retained_policy, 'published layout policies disagree')
                provenance['evidence_policy'] = retained_policy
        if '提取/image-notes.md' in files:
            provenance['prior_image_observations']=files['提取/image-notes.md'].decode()
        if '发布记录/材料复核.json' in files:
            provenance['prior_material_review']=json.loads(files['发布记录/材料复核.json'])
        source=dict(material_id=material['material_id'],binding=material['binding'],filename=document['filename'],
            source_type=Path(document['filename']).suffix.lower().lstrip('.'),original=body,excerpts=body if excerpt_only else '',
            provenance=provenance,image_boundary=image_boundary,
            user_answers=[dict(text=a['text'],receipt_sha256=sha(a)) for a in material['detail'].get('answers',[])],
            existing_cards={},existing_topics={})
        for prefix,key in ((CARDS,'existing_cards'),(TOPICS,'existing_topics')):
            source[key]={p:dict(sha256=hashlib.sha256(raw).hexdigest(),text=raw.decode()) for p,raw in snapshot['existing_files'].items() if p.startswith(prefix+'/')}
        if len(encoded(source).encode())>MAX_INPUT_BYTES:
            raise MaterialProblem('材料或已有卡片超出单轮整理范围，需要 Codex 分章节处理；完整原文已保留，未截断或生成完成标记。')
        validate_input(source)
        root=self._job(material);root.mkdir(parents=True,exist_ok=True,mode=0o700)
        revision=material['detail'].get('revision',0)
        input_path=root/f'input-{revision}.local.json'
        saved=dict(source=source,existing_files={p:raw.decode() for p,raw in snapshot['existing_files'].items()},
            available_paths=snapshot['available_paths'],date=datetime.fromtimestamp(material['created'],timezone.utc).astimezone().date().isoformat())
        durable_file(input_path,saved)
        return input_path,file_sha(input_path)

    def step(self,document):
        self.check()
        material=self.store.get(document['data']['reading_material_id'])
        require(material['binding']==self.binding(document),'reading binding changed')
        state=material['state'];detail=material['detail'];root=self._job(material)
        if state=='approved':
            path,digest=self._prepare_input(document,material)
            self.store.advance(material['material_id'],'approved','organizing',detail=dict(input_path=str(path),input_file_sha256=digest),checked_access=self.check)
            return
        if state in ('organizing','reviewing'):
            input_path=no_symlinks(Path(detail['input_path']))
            require(input_path.parent==root and file_sha(input_path)==detail['input_file_sha256'],'saved reading input changed')
            saved=json.loads(input_path.read_bytes());source=saved['source']
            self.checked_source(source)
            if state=='organizing':
                draft=self.model.run('organize',source)
                if draft['status']=='needs_user_input':
                    return self._question(document,material,draft['question'])
                path=root/f'draft-{detail.get("revision",0)}.local.json'
                durable_file(path,draft)
                self.store.advance(material['material_id'],'organizing','reviewing',detail=dict(draft_path=str(path),draft_file_sha256=file_sha(path)),checked_access=self.check)
                return
            path=no_symlinks(Path(detail['draft_path']))
            require(path.parent==root and file_sha(path)==detail['draft_file_sha256'],'reading draft changed')
            draft=json.loads(path.read_bytes())
            review=self.model.run('review',source,draft)
            if review['status']=='needs_user_input':
                return self._question(document,material,review['question'])
            if review['status']=='reject':
                self.store.advance(material['material_id'],'reviewing','failed',detail=dict(failure=review['findings']),checked_access=self.check)
                raise MaterialProblem('整理复核未通过，结果已保留，请由 Codex 核查；尚未标记整理完成。')
            files,_=self.assets(document)
            existing={p:b'' for p in saved['available_paths']}
            existing.update({p:t.encode() for p,t in saved['existing_files'].items()})
            bundle=build_bundle(source,draft,review,assets=files,date=saved['date'],existing_files=existing)
            plan_sha=prepare_plan(self.profile,bundle,root/'publication',checked_access=self.check)
            self.store.advance(material['material_id'],'reviewing','ready',detail=dict(bundle_sha256=plan_sha,review_receipt=sha(review),group=bundle['group']),checked_access=self.check)
            return
        if state=='ready':
            self.store.advance(material['material_id'],'ready','publishing',checked_access=self.check)
            return
        if state in ('publishing','completed'):
            receipt=publish_plan(self.profile,root/'publication',detail['bundle_sha256'],checked_access=self.check)
            if state=='publishing':
                self.store.advance(material['material_id'],'publishing','completed',detail=dict(
                    publication_receipt=sha(receipt),quality_receipt=sha(dict(review=detail['review_receipt'],publication=receipt))),checked_access=self.check)
            self.backend.store.advance(document['message_id'],'reading','published',user_action=None,reason=None,
                data=dict(reading_completed=True,reading_group=detail['group']))
            return
        if state=='needs_user_input':
            # Repair interruption between saving the question and its scheduler
            # state; the model is not called again on this route.
            self.backend.store.advance(document['message_id'],'reading','reading_wait',user_action='reply_to_reading_question',reason=detail['question'])
            return
        if state=='failed':
            raise MaterialProblem('整理复核需要 Codex 处理，原件与当前结果已保留。')
        raise ContractError('unexpected reading progress')

    def _question(self,document,material,question):
        self.store.advance(material['material_id'],material['state'],'needs_user_input',detail=dict(question=question),checked_access=self.check)
        self.backend.store.advance(document['message_id'],'reading','reading_wait',user_action='reply_to_reading_question',reason=question)

    def question_reply(self,document):
        material=self.store.get(document['data']['reading_material_id'])
        require(material['binding']==self.binding(document),'reading question source changed')
        # State committed before scheduler transition: repair without accepting
        # the same answer twice or creating another revision.
        if material['state']=='approved':
            self.backend.store.advance(document['message_id'],'reading_wait','reading',user_action=None,reason=None)
            return
        require(material['state']=='needs_user_input','unexpected reading clarification state')
        revision=material['detail'].get('revision',0)
        key=f'reading-question:{material["material_id"]}:{revision}'
        notice=self.backend.notice(key,'“'+document['filename']+'”已确认入库，完整整理还需确认：'+material['detail']['question']+' 请直接回复这条消息。',purpose='service',confirmation=True)
        choices=self.backend._replies(notice['message_id'])
        if not choices:
            return
        require(len(choices)==1,'conflicting reading clarification replies')
        observed=choices[0]
        p=self.authority.policy
        sender=observed.get('sender',{})
        require(all(sender.get(k)==v for k,v in dict(id=p.account_id,id_type='open_id',sender_type='user').items()) and observed.get('chat_id')==p.chat_id,'only approved owner can clarify reading')
        parsed,detail,listed=self.backend._verified_reply(observed)
        require(parsed[0]==notice['message_id'] and bool(parsed[1].strip()) and len(parsed[1])<=2000,'bounded direct answer required')
        receipts=dict(detail['evidence'],**listed['evidence'],**notice['receipt']['evidence'])
        def access():
            self.check()
            for proof in (detail,listed,notice['receipt']):
                self.backend.transport._checked_result(proof)
        self.store.answer(material['material_id'],dict(text=parsed[1],message_id=observed['message_id'],receipts=receipts),checked_access=access)
        self.backend.store.advance(document['message_id'],'reading_wait','reading',user_action=None,reason=None)
        self.backend.store.put('defer:'+document['message_id'],None)

    def report(self):
        rows=self.backend.store.rows("SELECT message_id FROM daily_documents WHERE phase IN ('reading','reading_wait') ORDER BY created")
        for row in rows:
            document=self.backend.store.document(row['message_id'])
            material=self.store.get(document['data']['reading_material_id'])
            deferred=self.backend.store.get('defer:'+row['message_id']) or {}
            reason=deferred.get('reason','')
            key='reading-progress:'+material['material_id']+':'+reason
            if self.backend.store.get(key):
                continue
            text='已记录你的入库决定，正在按读书知识库规范整理到独立测试库。'
            if document['phase']=='reading_wait':
                text='已记录入库决定；完整整理等待补充来源信息，请回复对应的问题消息。'
            if reason=='reading_model_approval_required':
                text='已记录入库决定；整理请求已准备，需在 Codex 确认本轮模型调用范围后继续。'
            elif reason=='model_limit':
                text='已记录入库决定；累计模型额度已用完，完整整理暂停，已有原件与结果保留。'
            elif reason.startswith('reading_model_result_') or reason=='reading_result_requires_codex_review':
                text='已记录入库决定；本轮整理结果需由 Codex 核查，程序不会重复调用模型。'
            flow=self.backend.card_flow
            card_id=document['data'].get('card_id')
            wait_key=key+':wait'
            wait=self.backend.store.get(wait_key) or {}
            if wait.get('until',0)>self.backend.store.now():
                continue
            try:
                if flow is not None and card_id and flow.cards.get(card_id)['receipt']:
                    proof=flow.delivery.update(flow.cards.get(card_id),text)
                else:
                    proof=self.backend.notice(key,text,purpose='service',confirmation=True)
            except Deferred as error:
                if error.reason not in ('card_update_limit','attempts_exhausted'):
                    self.backend.store.put(wait_key,dict(reason=error.reason,until=error.until or self.backend.store.now()+self.backend.store.policy.poll_seconds))
                    continue
                proof=self.backend.notice(key+':fallback',text+' 卡片显示暂未更新，处理记录已保留。',purpose='service',confirmation=True)
            except MaterialProblem:
                proof=self.backend.notice(key+':fallback',text+' 卡片显示暂未更新，处理记录已保留。',purpose='service',confirmation=True)
            self.backend.store.put(key,dict(proof=proof))
            return
