"""P32 composition over the existing custody, conversion and publication code."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import threading
import uuid

from .contracts import ContractError, stable_key
from .daily_collector import DailyCollector
from .daily_original import restore_original
from .daily_guide import generate_daily_guide, DailyTextWire
from .daily_scheduler import MaterialProblem
from .daily_store import Deferred
from .daily_subscription import require_subscription
from .daily_transport import DailyTransport
from .f01 import PREFIX, validated_message_content
from .f05 import file_sha
from .image_availability import availability, warning_text, COMMANDS as IMAGE_COMMANDS
from .locking import open_lock
from .storage_layout import StorageUnavailable
from .pilot_decision import observed_reply
from .pilot_existing import lookup_existing
from .pilot_intake import PilotIntake
from .pilot_pipeline import PilotPipeline
from .pilot_preview import PilotScope
from .preview_name import preview_filename
from .registry import Registry, encoded


class DailyBackend:
    def __init__(self, authority, store, *, transport=None, registry=None, resources=None, wire=None, pdf_converter=None):
        self.authority, self.store, self.wire = authority, store, wire
        self.transport = transport if transport is not None else DailyTransport(authority,store)
        self.collector = DailyCollector(authority,store,self.transport)
        self.transport.before_write=lambda **kw:self.collector.verify_isolation(force=True,**kw)
        self.registry = registry if registry is not None else Registry(authority.runtime)
        p=authority.policy
        self.intake=PilotIntake(self.registry,PilotScope(p.app_id,p.chat_id,p.account_id),
                                max_attachments=1,max_pending=p.max_pending)
        self.pipeline=PilotPipeline(self.intake,resources=resources)
        self.stop=threading.Event()
        self.card_flow = None
        self.card_listener = None
        if 'cards' in authority.config:
            from .daily_card_flow import CardFlow
            from .daily_card_events import CardListener
            self.card_flow = CardFlow(self)
            self.card_listener = CardListener(self)
        self.archive_mentions = None
        if 'archive_mentions' in authority.config:
            from .daily_archive import ArchiveMentions
            self.archive_mentions = ArchiveMentions(self)
        self.reading_flow = None
        if getattr(authority, 'reading', None) is not None:
            from .reading_flow import ReadingFlow
            self.reading_flow = ReadingFlow(self)
        self.relations = None
        if 'relations' in authority.config:
            from .relation_flow import RelationFlow
            self.relations = RelationFlow(self)
        self.qa = None
        if getattr(authority, 'qa', None) is not None:
            from .qa_flow import QAFlow
            self.qa = QAFlow(self)
        self.pdf_converter = None
        self.pdf_chunks = None
        self.pdf_progress = None
        if 'pdf' in authority.config:
            from .mineru_chunks import PdfChunks, PdfOptions
            from .mineru_service import MineruService
            config = authority.config['pdf']
            if config.get('progress') is True:
                from .daily_pdf_progress import PdfProgress
                self.pdf_progress = PdfProgress(self)
            self.pdf_converter = pdf_converter if pdf_converter is not None else MineruService(
                authority.root/'mineru', config, check=self.check_authorization, stop=self.stop,
                resources=self.pipeline.resources.check, emit=self._mineru_event)
            self.pdf_chunks = PdfChunks(authority.runtime.path('conversion-work/pdf-chunks'),
                PdfOptions(**config['options']), self.pdf_converter, check=self._pdf_admission)
            self.intake.conversion.service.custody_pdf_route = 'pdf_mineru_local'
        if self.wire is None:
            c=authority.config
            self.wire=DailyTextWire(c['text_node'],c['text_script'],c['codex_binary'],
                                    authority.runtime.worktree,stop=self.stop,authority=authority)

    def check_authorization(self):
        return self.authority.check()

    def _storage_pause(self, error):
        previous=self.store.get('storage_wait')
        if not previous or previous['reason']!=error.reason:
            self.store.put('storage_wait', dict(reason=error.reason, message=str(error), at=self.store.now()))

    def _storage_ready(self):
        try:
            self.authority.runtime.check_storage()
        except StorageUnavailable as error:
            self._storage_pause(error)
            raise Deferred('storage_unavailable') from error
        if self.store.get('storage_wait') is not None:
            self.store.put('storage_wait', None)

    def request_stop(self):
        self.stop.set()

    def _mineru_event(self, event):
        self.store.put('mineru-notice:'+event['id'], event)
        self.store.put('mineru_status', event)

    def _pdf_admission(self):
        self.check_authorization()
        if self.pdf_converter is not None:
            self.pdf_converter.check_previous()
        if self.stop.is_set():
            raise Deferred('stopping')
        if self.pipeline.resources.check()['state'] != 'resources_ready':
            raise Deferred('paused_resources')

    def _mineru_notices(self):
        rows = self.store.rows("SELECT key,value FROM daily_meta WHERE key LIKE 'mineru-notice:%' ORDER BY rowid")
        for row in rows:
            event = json.loads(row['value'])
            if event is not None:
                self.notice(row['key'], event['text'], purpose='service', confirmation=True)
                self.store.put(row['key'], None)

    def close(self):
        if self.card_listener is not None:
            self.card_listener.close()
        if self.pdf_converter is not None:
            self.pdf_converter.close()
            try:
                self._mineru_notices()
            except (Deferred, MaterialProblem) as error:
                # Durable notices are retained and sent after the next start.
                self.store.put('mineru_notice_wait', dict(reason=str(error)))

    def recover(self):
        self.check_authorization()
        # Stage methods re-read authoritative material records. Saved scheduler
        # phases are hints for selection, never proof of generation/publication.
        self.store.put('stop_requested',False)
        previous=self.registry.rows("""SELECT e.received_filename FROM publications_v21 p
            JOIN publication_confirmations c ON c.publication_id=p.publication_id
            JOIN intake_entries e ON e.entry_id=p.entry_id WHERE p.state='published'
            ORDER BY c.confirmed_at DESC LIMIT 1""")
        self.store.put('previous_last_published',previous[0]['received_filename'] if previous else None)
        if not self.store.get('recovery_report_pending'):
            self.store.put('recovery_report_pending',uuid.uuid4().hex)


    def report_status(self):
        try:
            self._storage_ready()
        except Deferred as error:
            if error.reason!='storage_unavailable':raise
            pause=self.store.get('storage_wait')
            key='storage-pause:'+stable_key(pause['reason'],str(pause['at']))
            if not self.store.get('reported:'+key):
                self.notice(key,'资料盘暂不可用，资料处理已暂停。已登记的材料和确认记录保留；请连接原资料盘并检查可用空间。',purpose='service',confirmation=True)
                self.store.put('reported:'+key,True)
            raise
        if self.reading_flow is not None:
            self.reading_flow.report()
        if self.card_flow is not None:
            self.card_flow.report()
        if self.relations is not None:
            self.relations.report()
        if self.pdf_converter is not None:
            queued = any(Path(r['filename']).suffix.lower() == '.pdf' for r in self.store.rows(
                "SELECT filename FROM daily_documents WHERE phase IN ('registered','original_saved')"))
            self.pdf_converter.maintain(queued_pdf=queued)
            self._mineru_notices()
        if self.pdf_progress is not None:
            self.pdf_progress.report()
        pending=self.store.get('recovery_report_pending')
        if pending:
            from .daily_status import local_status,user_summary
            # A transient scope/network check can defer after the transport
            # persists a notice but before it sends it. Reuse that exact text:
            # live status may change before the same operation is retried.
            saved=self.store.rows('SELECT payload FROM daily_operations WHERE op_key=?',
                                  ('notice:recovery:'+pending,))
            if saved:
                text=json.loads(saved[0]['payload'])['notice_plan']['payload']
            else:
                value=local_status(self.authority.root)
                value['running']=True
                text=user_summary(value,max_items=6)
            self.notice('recovery:'+pending,text,purpose='service',confirmation=True)
            self.store.put('recovery_report_pending',None)
            return
        for row in self.store.rows("SELECT * FROM daily_documents WHERE phase IN ('held','published') ORDER BY updated"):
            deferred=self.store.get('defer:'+row['message_id']) or {}
            reason=row['reason'] or deferred.get('reason')
            if not reason:
                continue
            key='problem:'+stable_key(row['message_id'],reason)
            if not self.store.get('reported:'+key):
                text='“'+row['filename']+'”'+('已入库，完成通知尚待处理。' if row['phase']=='published' else '需要后续处理：'+reason)
                self.notice(key,text,purpose='service',confirmation=True)
                self.store.put('reported:'+key,True)
                return
        registered=self.store.rows("SELECT message_id,filename FROM daily_documents WHERE phase='registered' ORDER BY created,message_id")
        count=0
        for row in registered:
            key='registered:'+row['message_id']
            if self.store.get('reported:'+key):
                continue
            self.notice(key,'已登记“'+row['filename']+'”，正在等待保存原件。')
            self.store.put('reported:'+key,True)
            count+=1
            if count>=2:
                break
        limits=self.store.rows("SELECT message_id FROM daily_documents WHERE phase IN ('converted','generating','model_unknown')")
        subscription_wait = self.store.get('subscription-admission')
        if subscription_wait:
            self.notice('subscription-wait:'+self.authority.digest, '订阅余量不足或状态暂不可核验，未发送的任务已排队；恢复后继续，无需追加项目次数。', purpose='service')
        if any((self.store.get('defer:'+r['message_id']) or {}).get('reason')=='model_limit' for r in limits):
            key='model-budget:'+self.authority.policy.digest
            if not self.store.get('reported:'+key):
                self.notice(key,'正文累计额度已用完，已保存的材料会继续保留。已发 PDF 仍可确认入库；请在 Codex 集中补充正文额度。',purpose='service',confirmation=True)
                self.store.put('reported:'+key,True)
        if self.qa is not None:
            self.qa.report()

    def process_events(self):
        if self.card_listener is not None:
            try:
                try:
                    self._storage_ready()
                except Deferred:
                    self.card_listener.tick(process_events=False)
                    raise
                self.card_listener.tick()
            except StorageUnavailable as error:
                self._storage_pause(error)
                self.store.put('card_listener_wait', dict(reason='storage_unavailable'))
            except Deferred as error:
                self.store.put('card_listener_wait', dict(reason=error.reason, until=error.until))
            else:
                self.store.put('card_listener_wait', None)
        if self.archive_mentions is not None:
            self.archive_mentions.drain()

    def collect(self):
        # Direct collector callers retain callback upkeep as well.
        self.process_events()
        result = self.collector.collect()
        if self.archive_mentions is not None:
            self.archive_mentions.collect()
            self.archive_mentions.drain()
        if self.qa is not None:
            self.qa.collect()
        return result

    def _check_material(self, document):
        self.check_authorization()
        row=self.registry.entry(document['entry_id'])
        original=self.registry.runtime.checked(self.registry.runtime.path(row['relative_path']))
        if (row['status']!='accepted' or row['custody_health']!='healthy'
                or not original.exists() or original.stat().st_size!=row['size_bytes'] or file_sha(original)!=row['sha256']):
            raise MaterialProblem('已保存原件的完整性检查未通过，需要核查或补传。','resend_file')
        return row,original

    def _ensure_original(self, document):
        try:
            return self._check_material(document)
        except MaterialProblem:
            row=self.registry.entry(document['entry_id'])
            if row['status']!='accepted':
                raise
            item,detail,listed=self.collector.message(document['message_id'])
            resource=document['data']['resource']
            metadata=json.loads(item['body']['content'])
            if item.get('deleted') is not False or metadata.get('file_key')!=resource['file_key']:
                raise MaterialProblem('原件当前不可下载，请补传该文件。','resend_file')
            download=dict(reference='P19-inbox-download-v1',message_id=document['message_id'],
                file_key=resource['file_key'],filename=document['filename'],max_asset_bytes=self.authority.policy.max_asset_bytes)
            response=self.transport.request('restore-original:'+document['message_id'],'download',
                PREFIX+'/im/v1/messages/'+document['message_id']+'/resources/'+resource['file_key']+'?type=file',
                download=download,evidence=dict(listed['evidence'],**detail['evidence']),confirmation=True)
            self.transport._checked_result(response)
            restore_original(self.intake.staging,document['entry_id'],Path(response['value']['path']))
            return self._check_material(document)

    def notice(self, key, text, *, purpose='collection', confirmation=False):
        p=self.authority.policy
        nid=stable_key('daily-notice',key)
        plan=dict(reference='P11',purpose=purpose,app_id=p.app_id,account_id=p.account_id,
            target_chat_id=p.chat_id,notification_id=nid,provider_request_key='daily-'+nid[:44],payload=text)
        body=encoded(dict(receive_id=p.chat_id,msg_type='text',uuid=plan['provider_request_key'],content=encoded(dict(text=text))))
        receipt=self.transport.request('notice:'+key,'send',PREFIX+'/im/v1/messages?receive_id_type=chat_id',
            mode='send',body=body,notice_plan=plan,confirmation=confirmation)
        item=receipt['value'].get('data')
        content=validated_message_content(item,app_id=p.app_id,bot_id=p.bot_id,chat_id=p.chat_id,kind='text',name='',text=text)
        if content.get('text')!=text:
            raise ContractError('sent notice text differs from the persisted plan')
        return dict(message_id=item['message_id'],receipt=receipt,plan=plan)

    def advance_heavy(self, document):
        if self.stop.is_set():
            raise Deferred('stopping')
        self.check_authorization()
        self._storage_ready()
        self.collector.verify_isolation()
        phase=document['phase']
        if self.pipeline.resources.check()['state']!='resources_ready':
            raise Deferred('paused_resources')
        if phase=='reading':
            if self.reading_flow is None:
                raise ContractError('reading profile required to resume reading work')
            return self.reading_flow.step(document)
        if phase=='registered':
            return self._receive(document)
        self._ensure_original(document)
        if phase=='original_saved':
            return self._convert(document)
        if phase in ('converted','generating','model_unknown'):
            return self._guide(document)
        if phase=='guide_ready':
            self.store.advance(document['message_id'],'guide_ready','preview_ready')
            return
        if phase in ('preview_ready','send_unknown'):
            return self._preview(document)
        raise ContractError('unexpected heavy material phase')

    def _receive(self, document):
        mid=document['message_id']
        name=document['filename']
        supported = {'.md','.webarchive'} | ({'.pdf'} if self.pdf_converter is not None else set())
        if Path(name).suffix.lower() not in supported:
            raise MaterialProblem('当前支持 Markdown、Safari 网页归档'+('和论文 PDF。' if self.pdf_converter is not None else '；PDF 接入尚未启用。'),'resend_supported_file')
        self.notice('registered:'+mid,'已登记“'+name+'”，正在等待保存原件。')
        item,detail,listed=self.collector.message(mid)
        if item.get('msg_type')!='file' or item.get('deleted') is not False:
            raise MaterialProblem('原文件消息已不可读取，请补传。','resend_file')
        metadata=json.loads(item['body']['content'])
        resource=document['data']['resource']
        if metadata.get('file_name')!=name or metadata.get('file_key')!=resource['file_key']:
            raise ContractError('file resource changed after registration')
        p=self.authority.policy
        envelope=dict(appId=p.app_id,chatId=p.chat_id,senderId=p.account_id,chatType='group',messageId=mid,
            resources=[dict(type='file',fileKey=resource['file_key'],fileName=name)])
        entry_id=self.intake.prepare(envelope)[0]['entry_id']
        custody=dict(parent_approval_sha256=self.authority.digest,entry_id=entry_id,message_id=mid,
                     file_key=resource['file_key'],filename=name)
        with self.registry.transaction() as db:
            key='pilot-inbox-custody:'+entry_id
            if not db.execute('SELECT 1 FROM custody_events WHERE dedupe_key=?',(key,)).fetchone():
                self.registry.event(db,'pilot_inbox_custody',entry_id,key,custody)
        @contextmanager
        def source():
            download=dict(reference='P19-inbox-download-v1',message_id=mid,file_key=resource['file_key'],
                          filename=name,max_asset_bytes=p.max_asset_bytes)
            response=self.transport.request('original:'+mid,'download',
                PREFIX+'/im/v1/messages/'+mid+'/resources/'+resource['file_key']+'?type=file',download=download,
                evidence=dict(listed['evidence'],**detail['evidence']))
            raw=response['value']
            with Path(raw['path']).open('rb') as stream:
                yield stream
            self.transport._checked_result(response)
        self.intake.staging.ingest(entry_id,source)
        row=self.registry.entry(entry_id)
        if row['status']!='accepted' or row['custody_health']!='healthy':
            raise ContractError('original custody did not complete')
        self.store.advance(mid,'registered','original_saved',entry_id=entry_id)
        canonical=self.store.bind_source(mid,row['sha256'])
        if canonical:
            self.store.advance(mid,'original_saved','duplicate',data=dict(existing_message_id=canonical))
            self.notice('duplicate:'+mid,'“'+name+'”与已登记原件相同，沿用已有处理记录。')
            return
        self.notice('custody:'+mid,'“'+name+'”的原件已完整保存，将按队列制作导读。',purpose='custody')

    def _convert(self, document):
        mid=document['message_id']
        # Reconcile a crash between saving the original and its content key.
        if not document['source_sha']:
            row,_=self._check_material(document)
            canonical=self.store.bind_source(mid,row['sha256'])
            if canonical:
                self.store.advance(mid,'original_saved','duplicate',data=dict(existing_message_id=canonical))
                return
        self.notice('custody:'+mid,'“'+document['filename']+'”的原件已完整保存，将按队列制作导读。',purpose='custody')
        previous=self.registry.rows("""SELECT p.publication_id,p.state FROM publications_v21 p
            JOIN assets a ON a.asset_id=p.source_asset_id WHERE a.sha256=?""",
            (document['source_sha'] or self.registry.entry(document['entry_id'])['sha256'],))
        if previous:
            complete=[r for r in previous if r['state']=='published']
            if not complete:
                raise MaterialProblem('同一原件已有处理记录，请由 Codex 接续原记录，避免重复生成。')
            self.notice('duplicate:'+mid,'“'+document['filename']+'”的同一原件已经入库，沿用已有成果。')
            self.store.advance(mid,'original_saved','duplicate',data=dict(existing_publication=complete[-1]['publication_id']))
            return
        profile=json.loads(Path(self.authority.config['guide_plan']).read_bytes())
        row=self.registry.entry(document['entry_id'])
        prior=lookup_existing(document['filename'],row['sha256'],profile['legacy_catalog'])
        if prior['requires_review'] or not prior['complete']:
            raise MaterialProblem('旧库有相关记录或目录尚未完整核对，原件已保存。')
        pending=self.store.get('conversion-restart:'+mid)
        if pending:
            return self._recover_conversion(document,pending['task_id'])
        older=self.registry.rows('''SELECT e.entry_id FROM intake_entries e JOIN assets a ON a.asset_id=e.asset_id
            JOIN custody_events c ON c.aggregate_id=e.entry_id WHERE a.sha256=? AND c.event_type LIKE 'pilot_pipeline_%text_started' ''',
            (row['sha256'],))
        if older:
            raise MaterialProblem('同一原件存在早期正文调用记录，请由 Codex 核查后接续，避免重新消耗额度。')
        if Path(document['filename']).suffix.lower() == '.pdf':
            from .mineru_chunks import PdfHeld
            if self.pdf_chunks is None:
                raise MaterialProblem('PDF 转换尚未获得日常规则授权。')
            try:
                with self.intake.staging.input_binding(document['entry_id'], 'daily_mineru_conversion') as binding:
                    kwargs = dict(progress=lambda event:self.pdf_progress.record(mid,event)) if self.pdf_progress else {}
                    complete = self.pdf_chunks.convert(Path(binding['path']), row['sha256'], **kwargs)
            except PdfHeld as error:
                raise MaterialProblem(str(error)) from error
            self.intake.conversion.pdf_result = lambda accepted: complete if accepted['sha256'] == row['sha256'] else None
        result=self.pipeline.convert_once(entry_id=document['entry_id'])
        if result.get('state') in ('busy','paused_resources','paused_capacity'):
            raise Deferred(result['state'])
        state=self.intake.status(document['entry_id'])
        if state['processing_state']=='needs_recovery':
            return self._recover_conversion(document,state['task_id'])
        if state['processing_state']!='ready_candidate':
            raise MaterialProblem('转换结果需要检查，原件已保存。')
        task=self.intake.conversion.service.ledger.get_task(state['task_id'])
        extraction=json.loads(task['quality_json'])['extraction']['extraction_id']
        self.store.advance(mid,'original_saved','converted',data=dict(extraction_id=extraction))

    def _guide(self, document):
        mid=document['message_id']
        source=document['source_sha']
        if document['phase']=='model_unknown':
            return self._recover_model(document)
        if document['phase']=='converted':
            if Path(document['filename']).suffix.lower() == '.pdf':
                self._pdf_guide_bounds(document)
            images=availability(self.intake.conversion,document['data']['extraction_id'])
            if images and not images['decision']:
                notice=self.notice('images:'+mid,warning_text(document['filename'],images))
                self.store.advance(mid,'converted','awaiting_images',user_action='reply_to_image_warning',
                    data=dict(image_check=images,image_notice=notice))
                return
            if images and images['decision']['choice']!='continue_without_missing':
                raise MaterialProblem('已选择重新发送含图原件，等待补传。','resend_file')
            rows=self.store.rows('SELECT * FROM daily_models WHERE source_sha=? ORDER BY attempt',(source,))
            if not rows:
                require_subscription(self.authority, self.store)
            attempt=rows[-1]['attempt'] if rows else self.store.reserve_model(source)
            self.store.advance(mid,'converted','generating',data=dict(model_attempt=attempt),user_action=None)
            document=self.store.document(mid)
        attempt=document['data']['model_attempt']
        try:
            result=generate_daily_guide(self.authority,self.store,self.pipeline,document['entry_id'],attempt,wire=self.wire)
        except ContractError as error:
            # Inspect actual child receipts before deciding whether any model
            # recovery is permitted. The adapter never treats every error as a
            # temporary network failure or starts a new body blindly.
            rows=self.store.rows('SELECT * FROM daily_models WHERE source_sha=? AND attempt=?',(source,attempt))
            if rows and rows[0]['state']=='started':
                self.store.model_result(source,attempt,'unknown',dict(error_type=type(error).__name__))
            if document['phase']=='generating':
                self.store.advance(mid,'generating','model_unknown',reason=str(error))
            raise Deferred('model_receipt_needs_reconciliation') from error
        if result.get('state') in ('busy','paused_resources','paused_capacity'):
            raise Deferred(result['state'])
        rows=self.store.rows('SELECT state FROM daily_models WHERE source_sha=? AND attempt=?',(source,attempt))
        if rows[0]['state']!='validated':
            self.store.model_result(source,attempt,'validated',dict(publication_id=result['publication_id']))
        self.store.advance(mid,document['phase'],'guide_ready',publication_id=result['publication_id'],data=dict(guide_result=result),reason=None)

    def _pdf_guide_bounds(self, document):
        # Reject a complete but oversized paper before reserving a model turn.
        # Every page and original figure stays local; never truncate to fit.
        profile = json.loads(Path(self.authority.config['guide_plan']).read_bytes())
        extraction = document['data']['extraction_id']
        _, _, root = self.intake.conversion.publication_extraction(extraction)
        body = (root/'source.md').read_bytes()
        text = body.decode('utf-8')
        images = self.intake.conversion.registered_images(extraction)
        policy = profile['policy']
        if (len(images) > policy['max_images']
                or sum(i['size_bytes'] for i in images) > policy['max_total_bytes']):
            raise MaterialProblem('论文图片超过本次完整保留范围；原件和转换结果已保存，请交 Codex 接续。')
        # Image-policy output names are bounded; reserve space for rewriting
        # references so a near-limit body cannot consume a turn then fail.
        margin = len(images) * 160
        if (not text.strip() or len(body)+margin > profile['max_body_bytes']
                or len(text)+margin > profile['max_characters']):
            raise MaterialProblem('论文正文超过本次完整导读范围；所有页面已保存，请交 Codex 接续。')

    def _model_child(self,document):
        attempt=document['data']['model_attempt']
        grant=self.authority.root/'model-grants'/document['entry_id']/str(attempt)/'p11-requests/approval.local.json'
        if not grant.exists():
            raise ContractError('missing finite model grant; inspect configuration')
        digest=stable_key(file_sha(grant),file_sha(Path(self.authority.config['guide_plan'])),document['entry_id'])
        return self.registry.runtime.root/'conversion-work'/('pilot-text-'+digest)

    def _recover_model(self,document):
        root=self._model_child(document)
        journal=root/'journal.local.jsonl'
        root.mkdir(parents=True,exist_ok=True)
        grant=self.authority.root/'model-grants'/document['entry_id']/str(document['data']['model_attempt'])/'p11-requests'
        parent_fd=open_lock(grant/'execution.lock')
        try:
            fcntl.flock(parent_fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(parent_fd)
            raise Deferred('original_model_parent_still_running') from error
        fd=open_lock(root/'execution.lock')
        try:
            try:
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise Deferred('original_model_attempt_still_running') from error
            events=[json.loads(line) for line in journal.read_text().splitlines()] if journal.exists() else []
            states=[event.get('state') for event in events if event.get('state')]
            attempt=document['data']['model_attempt']
            latest=self.store.rows('SELECT * FROM daily_models WHERE source_sha=? ORDER BY attempt', (document['source_sha'],))
            if latest and latest[-1]['attempt']>attempt:
                self.store.advance(document['message_id'],'model_unknown','generating',
                    data=dict(model_attempt=latest[-1]['attempt']),reason=None)
                return
            if states and states[-1]=='validated':
                self.store.advance(document['message_id'],'model_unknown','generating',reason=None)
                return
            if states and states[-1]=='received_rejected':
                raise MaterialProblem('原件和提取正文已保存；AI 生成结果的格式或引用未通过校验，需 Codex 核对，不必重传原件。')
            if attempt>=2:
                raise MaterialProblem('正文结果仍未找到，已达到一次补生成上限，请交 Codex 核查。')
            if any(event.get('state')=='received' for event in events):
                raise MaterialProblem('已保存模型返回，需先核查返回内容，暂不重复生成。')
            receipts={str(grant/'approval.local.json'):file_sha(grant/'approval.local.json')}
            for path in (journal,root/'approval.local.json'):
                if path.exists():
                    receipts[str(path)]=file_sha(path)
            evidence=dict(checked_saved_results=True,old_attempt_fenced=True,receipts=receipts,
                no_child_journal=not journal.exists())
            if latest[-1]['state']!='retry_authorized':
                self.store.allow_model_recovery(document['source_sha'],evidence)
            replacement=self.store.reserve_model(document['source_sha'])
            self.store.advance(document['message_id'],'model_unknown','generating',
                data=dict(model_attempt=replacement),reason=None)
        finally:
            os.close(fd)
            os.close(parent_fd)

    def _recover_conversion(self,document,task_id):
        reference='P32:'+self.authority.digest
        key='conversion-restart:'+document['message_id']
        self.store.put(key,dict(task_id=task_id))
        job=self.intake._job(document['entry_id'])
        if job['task_id'] is None:
            # Previous recovery committed its pointer change but stopped before
            # clearing the scheduling hint. Do not create a second replacement.
            self.store.put(key,None)
            return
        try:
            self.intake.conversion.recovery.recover(task_id,reference)
        except ContractError as error:
            if isinstance(error.__cause__,BlockingIOError):
                raise Deferred('original_conversion_still_running') from error
            raise
        # The old task and all its artifacts remain in the conversion ledger.
        # Only this job's scheduling pointer moves to a new explicit attempt.
        with self.registry.transaction() as db:
            job=db.execute('SELECT * FROM pilot_conversion_jobs WHERE entry_id=?',(document['entry_id'],)).fetchone()
            if job['task_id']!=task_id:
                raise ContractError('conversion job changed during recovery')
            key='daily-conversion-restart:'+task_id
            old=db.execute('SELECT 1 FROM custody_events WHERE dedupe_key=?',(key,)).fetchone()
            if not old:
                self.registry.event(db,'daily_conversion_restart',document['entry_id'],key,
                    dict(previous_task_id=task_id,previous_request_key=job['request_key'],authorization_ref=reference))
            db.execute('UPDATE pilot_conversion_jobs SET task_id=NULL,inspection_id=NULL,request_key=? WHERE entry_id=?',
                       ('pilot-'+stable_key(job['request_key'],task_id),document['entry_id']))
        self.store.put('conversion-restart:'+document['message_id'],None)

    def _preview(self, document):
        previews=self.pipeline.previews
        pid=document['publication_id']
        row=previews.publisher._row(pid)
        staged=previews.stage(pid)
        tree=previews.publisher._tree(row,pid+'.candidate')
        pdf=tree/'发布记录/已确认入库预览.pdf'
        if file_sha(pdf)!=row['preview_sha256']:
            raise ContractError('preview bytes changed')
        title=json.loads((tree/'metadata.json').read_text())['source_record']['work']['title']
        plan=dict(publication_id=pid,pdf_path=str(pdf),sha256=row['preview_sha256'],
                  size_bytes=pdf.stat().st_size,filename=preview_filename(title,staged['version']))
        # An interrupted upload must keep its original name and request identity,
        # including previews started before readable filenames were introduced.
        saved=self.store.rows('SELECT payload FROM daily_operations WHERE op_key=?',('preview:'+pid+':upload',))
        if saved:
            previous=json.loads(saved[0]['payload'])['preview']
            plan['filename']=previous['filename']
            if previous!=plan:
                raise ContractError('saved preview upload no longer matches the candidate')
        evidence={str(pdf):plan['sha256']}
        upload=self.transport.request('preview:'+pid+':upload','upload',PREFIX+'/im/v1/files',mode='upload',preview=plan,evidence=evidence)
        plan=dict(plan,upload_receipt=upload['receipt'])
        p=self.authority.policy
        body=encoded(dict(receive_id=p.chat_id,msg_type='file',content=encoded(dict(file_key=upload['value']['data']['file_key'])),
                          uuid='preview-'+hashlib.sha256((p.app_id+':'+p.chat_id+':'+pid).encode()).hexdigest()[:40]))
        evidence.update(upload['evidence'])
        sent=self.transport.request('preview:'+pid+':send','send',PREFIX+'/im/v1/messages?receive_id_type=chat_id',
            mode='send',body=body,preview=plan,evidence=evidence)
        item=sent['value']['data']
        content=validated_message_content(item,app_id=p.app_id,bot_id=p.bot_id,chat_id=p.chat_id,kind='file',name=plan['filename'])
        mid=item['message_id']
        plan=dict(plan,send_receipt=sent['receipt'])
        evidence.update(sent['evidence'])
        download=dict(message_id=mid,file_key=content['file_key'],filename=plan['filename'],sample_path=str(pdf),
                      sha256=plan['sha256'],size_bytes=plan['size_bytes'])
        checked=self.transport.request('preview:'+pid+':download','download',
            PREFIX+'/im/v1/messages/'+mid+'/resources/'+content['file_key']+'?type=file',
            download=download,preview=plan,evidence=evidence,confirmation=True)
        def access():
            self._check_material(document)
            for receipt in (upload,sent,checked):
                self.transport._checked_result(receipt)
        access()
        previews.bind_delivery(pid,app_id=p.app_id,chat_id=p.chat_id,message_id=mid,
            pdf_sha256=plan['sha256'],receipt_ref=checked['value']['sha256'],outcome='sent',checked_access=access)
        self.store.advance(document['message_id'],document['phase'],'awaiting_confirmation',preview_message_id=mid,
            user_action='reply_to_original_pdf',data=dict(preview=dict(upload=upload,sent=sent,checked=checked)))

    def _replies(self, parent):
        # A later edited version supersedes its own old observation; two
        # different reply messages remain a conflict requiring clarification.
        latest={}
        for row in self.store.rows('SELECT * FROM daily_messages ORDER BY observed,rowid'):
            item=json.loads(row['payload'])
            latest[item['message_id']]=item
        return [item for item in latest.values() if item.get('parent_id')==parent and item.get('deleted') is False]

    def _verified_reply(self, observed):
        item,detail,listed=self.collector.message(observed['message_id'])
        parsed=observed_reply(item,self.authority.policy.bot_id)
        if parsed[2]!=observed_reply(observed,self.authority.policy.bot_id)[2]:
            raise Deferred('reply_changed_wait_for_fresh_observation')
        return parsed,detail,listed

    def advance_light(self, document):
        self.check_authorization()
        self._storage_ready()
        self._ensure_original(document)
        phase=document['phase']
        if phase=='reading_wait':
            if self.reading_flow is None:
                raise ContractError('reading profile required to answer reading question')
            return self.reading_flow.question_reply(document)
        if self.card_flow is not None and phase in ('awaiting_images','awaiting_confirmation'):
            self.card_flow.advance(document)
            document=self.store.document(document['message_id'])
            phase=document['phase']
            if phase!='confirmed':
                return
        if phase=='awaiting_images':
            return self._image_choice(document)
        if phase=='awaiting_confirmation':
            choices=[]
            for observed in self._replies(document['preview_message_id']):
                try:
                    command=observed_reply(observed,self.authority.policy.bot_id)[1]
                except ContractError:
                    command=None
                if command in self.pipeline.previews.COMMANDS:
                    choices.append(observed)
                else:
                    version=hashlib.sha256(encoded(observed).encode()).hexdigest()
                    self.notice('correct:'+version,'“'+document['filename']+'”的回复尚未识别。请编辑对原 PDF 的同一条回复为“确认入库”或“暂不入库”，保留飞书自动提及即可。',confirmation=True)
            if len({observed_reply(item,self.authority.policy.bot_id)[1] for item in choices})>1:
                raise MaterialProblem('收到多条不同确认回复，请核对这一份 PDF。')
            if not choices:
                return
            parsed,detail,listed=self._verified_reply(choices[0])
            for result in document['data']['preview'].values():
                self.transport._checked_result(result)
            evidence=dict(reference='P32-owner-reply-v1',rules_sha256=self.authority.digest,
                message_id=choices[0]['message_id'],reply_to=parsed[0],command=parsed[1],
                receipts=dict(detail['evidence'],**listed['evidence']))
            # Persist every verified choice as pending work. A crash must not
            # turn a saved defer/supplement choice into an unrecorded terminal.
            self.store.advance(document['message_id'],'awaiting_confirmation','confirmed',
                user_action=None,data=dict(decision=evidence))
            document=self.store.document(document['message_id'])
            phase='confirmed'
        if phase=='confirmed':
            result=self._decide(document)
            if result['status']!='published':
                self.store.advance(document['message_id'],'confirmed','rejected',data=dict(publication=result))
                return
            if self.reading_flow is not None:
                self.reading_flow.start(document)
                return
            self.store.advance(document['message_id'],'confirmed','published',data=dict(publication=result))
            document=self.store.document(document['message_id'])
            phase='published'
        if phase=='published':
            row=self.pipeline.publisher._row(document['publication_id'])
            if row['state']!='published':
                raise ContractError('completion notice requires a published material')
            if document['data'].get('reading_material_id') and not document['data'].get('reading_completed'):
                raise ContractError('reading completion must precede the completion notice')
            outcome='已完成独立测试库的完整整理。' if document['data'].get('reading_completed') else '已确认入库。'
            receipt=self.notice('published:'+row['publication_id'],'“'+document['filename']+'”'+outcome,
                                purpose='publication',confirmation=True)
            if self.relations is not None and document['data'].get('reading_completed'):
                self.relations.start(document)
            self.store.advance(document['message_id'],'published','notified',data=dict(completion_notice=receipt))

    def _decide(self, document):
        evidence=document['data']['decision']
        def access():
            self._check_material(document)
            if any(file_sha(Path(path))!=digest for path,digest in evidence['receipts'].items()):
                raise ContractError('verified owner decision receipt changed')
            for result in document['data']['preview'].values():
                self.transport._checked_result(result)
        access()
        p=self.authority.policy
        return self.pipeline.previews.decide(app_id=p.app_id,chat_id=p.chat_id,user_id=p.account_id,
            message_id=evidence['message_id'],reply_to=evidence['reply_to'],text=evidence['command'],
            checked_access=access,evidence=evidence)

    def _image_choice(self, document):
        notice=document['data']['image_notice']
        choices=[]
        for item in self._replies(notice['message_id']):
            try:
                command=observed_reply(item,self.authority.policy.bot_id)[1]
            except ContractError:
                command=None
            if command in IMAGE_COMMANDS:
                choices.append(item)
            else:
                version=hashlib.sha256(encoded(item).encode()).hexdigest()
                self.notice('image-correct:'+version,'“'+document['filename']+'”的缺图选择尚未识别。请编辑对原缺图提示的同一条回复为“按缺图版本继续”或“重新发文件”，保留飞书自动提及即可。',confirmation=True)
        if len({observed_reply(item,self.authority.policy.bot_id)[1] for item in choices})>1:
            raise MaterialProblem('收到多条不同缺图处理决定，请核对该文件。')
        if not choices:
            return
        parsed,detail,listed=self._verified_reply(choices[0])
        self.transport._checked_result(notice['receipt'])
        current=availability(self.intake.conversion,document['data']['extraction_id'])
        if current is None or current['binding']!=document['data']['image_check']['binding']:
            raise ContractError('image availability changed')
        decision=dict(binding=current['binding'],choice=IMAGE_COMMANDS[parsed[1]],evidence=dict(
            reference='P32-image-owner-choice-v1',rules_sha256=self.authority.digest,
            message_id=choices[0]['message_id'],reply_to=notice['message_id'],
            receipts=dict(detail['evidence'],**listed['evidence'],**notice['receipt']['evidence'])))
        with self.registry.transaction() as db:
            old=db.execute('SELECT payload_json FROM custody_events WHERE dedupe_key=?',(current['key'],)).fetchone()
            if old and json.loads(old[0])!=decision:
                raise ContractError('image decision cannot be replaced')
            if not old:
                self.registry.event(db,'owner_image_availability_choice',current['binding']['source_asset_id'],current['key'],decision)
        target='converted' if decision['choice']=='continue_without_missing' else 'needs_upload'
        self.store.advance(document['message_id'],'awaiting_images',target,user_action=None,
                           data=dict(image_decision=decision))
