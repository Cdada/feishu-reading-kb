"""A finite multi-material service with saved pending work and exact decisions.

Polling, original count, model turns and notices share one fixed approval.
Transient resource holds retain local progress. Unknown external effects stop
the run; a new approved run can continue explicitly bound prior previews.
"""
from datetime import timedelta
import json
from pathlib import Path
import time

from .contracts import ContractError, stable_key
from .f05 import file_sha
from .pilot_decision import apply_owner_reply, observed_reply
from .pilot_delivery import deliver_preview
from .pilot_existing import lookup_existing, validate_catalog
from .pilot_feishu import read_json
from .pilot_inbox import inbox_policy, receive_new_file
from .pilot_inbox_guide import PilotInboxGuide
from .pilot_lifecycle import PilotLifecycle
from .pilot_notice_workflow import PilotNoticeWorkflow
from .pilot_notices import PilotNoticeSender
from .pilot_owner_followup import checked_previous
from .pilot_pipeline import PilotPipeline
from .registry import encoded
from .runtime import no_symlinks
from .s6 import instant


def service_limits(files, scans, previous=0, *, image_choices=False):
    if (type(files) is not int or not 1 <= files <= 3
            or type(scans) is not int or not 2 <= scans <= 35
            or type(previous) is not int or not 0 <= previous <= 3
            or files+previous > 3):
        raise ContractError('explicit bounded service material and scan counts required')
    # Ready/pause + one final status; each new file: detail, original download,
    # custody, preview upload/send/download, status, owner detail, final notice.
    # One correction notice per material has its own auth/send allowance.
    limits=dict(auth=6+scans+10*files+3*previous,identity=3,list=scans,
                message=2*files+previous,download=2*files,upload=files,
                send=3+5*files+2*previous)
    if image_choices:
        # Per new material: warning, one exact reply detail, one malformed-reply
        # correction. These reads/writes need a newly frozen P30 service plan.
        limits['auth'] += 3 * files
        limits['message'] += files
        limits['send'] += 2 * files
    if sum(limits.values()) > 100:
        raise ContractError('service exceeds the fixed 100 HTTP request ceiling')
    return limits


def continuation_material(intake,entry_id):
    """Read-only selection of a saved original with no prior model reservation."""
    row=intake.registry.entry(entry_id)
    if row['status']!='accepted' or row['custody_health']!='healthy':
        raise ContractError('continued original must remain accepted and healthy')
    original=intake.runtime.checked(intake.runtime.path(row['relative_path']))
    if file_sha(original)!=row['sha256'] or original.stat().st_size!=row['size_bytes']:
        raise ContractError('continued original bytes changed')
    events=intake.registry.rows('SELECT payload_json FROM custody_events WHERE dedupe_key=?',
                               ('pilot-inbox-custody:'+entry_id,))
    if len(events)!=1:
        raise ContractError('original inbox custody evidence required')
    custody=json.loads(events[0]['payload_json'])
    if intake.registry.rows('SELECT 1 FROM custody_events WHERE dedupe_key=?',
                            ('pilot-inbox-guide-source:'+row['sha256'],)):
        raise ContractError('saved model reservation requires explicit result inspection; no new turn')
    return dict(custody,sha256=row['sha256'],size_bytes=row['size_bytes'])


class PilotServiceRun:
    def __init__(self,session,intake,plan_path,*,wire,checked_ready,resources=None,wait=time.sleep):
        self.session,self.intake=session,intake
        self.path=no_symlinks(Path(plan_path).absolute());self.digest=file_sha(self.path)
        self.plan=p=read_json(self.path,65536);frozen=session.binding()
        fields={'reference','guide_plan_path','legacy_catalog','lookback_seconds',
                'max_scans','poll_interval_seconds','previous_previews','previous_materials'}
        self.image_choices = p.get('reference') == 'P30-image-choice-service-v1'
        if (set(p)!=fields or p['reference'] not in {'P25-service-run-v1', 'P30-image-choice-service-v1'}
                or session.approval.get('operation')!='inbox-service'
                or frozen.get(str(self.path))!=self.digest
                or session.approval.get('run_plan')!=str(self.path)
                or not isinstance(p['previous_previews'],list)
                or not isinstance(p['previous_materials'],list)
                or type(p['lookback_seconds']) is not int or not 1<=p['lookback_seconds']<=86400
                or type(p['poll_interval_seconds']) is not int or not 30<=p['poll_interval_seconds']<=3600
                or not callable(checked_ready) or not callable(wait)):
            raise ContractError('exact frozen finite service plan required')
        self.policy=inbox_policy(session)
        if session.limits!=service_limits(self.policy['max_files'],p['max_scans'],len(p['previous_previews']), image_choices=self.image_choices):
            raise ContractError('service requires its exact category budget')
        validate_catalog(p['legacy_catalog'])
        self.guide=PilotInboxGuide(session,p['guide_plan_path'],wire=wire)
        if (self.guide.profile['legacy_catalog']!=p['legacy_catalog']
                or session.approval.get('guide_plan')!=str(self.guide.path)):
            raise ContractError('same explicitly approved guide and V1 catalog required')
        self.pipeline=PilotPipeline(intake,resources=resources)
        self.lifecycle=PilotLifecycle(session,intake,resources=self.pipeline.resources)
        self.notices=PilotNoticeWorkflow(session,intake)
        self.checked_ready,self.wait=checked_ready,wait
        self.prefix='service:'+session.approval_sha+':'
        self.items={};self.prior={};self.scans=0;self.closed=False
        for binding in p['previous_previews']:
            checked_previous(session,self.pipeline.previews,binding)
            pid=binding['publication_id'];row=self.pipeline.previews.publisher._row(pid)
            if pid in self.prior or row['state']!='prepared' or intake.registry.rows(
                    'SELECT * FROM pilot_preview_decisions WHERE publication_id=?',(pid,)):
                raise ContractError('unique undecided previous preview required')
            original=intake.runtime.path(intake.registry.entry(row['entry_id'])['relative_path'])
            if frozen.get(str(original))!=file_sha(original):
                raise ContractError('previous preview original must be frozen')
            self.prior[pid]=binding
            self.items['previous:'+pid]=dict(entry_id=row['entry_id'],filename=original.name,state='awaiting_owner_decision',
                delivery=binding,publication_id=pid,published=False,previous=True)
        if len(p['previous_materials'])>self.policy['max_files']:
            raise ContractError('continued originals consume service material slots')
        seen=set()
        for selected in p['previous_materials']:
            binding=continuation_material(intake,selected['entry_id'])
            if binding!=selected or binding['entry_id'] in seen or any(
                    item['entry_id']==binding['entry_id'] for item in self.items.values()):
                raise ContractError('exact unique continued original required')
            seen.add(binding['entry_id'])
            original=intake.runtime.path(intake.registry.entry(binding['entry_id'])['relative_path'])
            if frozen.get(str(original))!=binding['sha256']:
                raise ContractError('continued original must be explicitly frozen')
            session.bind('service-resume:'+binding['entry_id'],binding)
            self.items['continued:'+binding['entry_id']]=dict(entry_id=binding['entry_id'],
                filename=binding['filename'],original=binding,state='received',published=False,continued=True)
        self.base_source=session.checked_source;self.binding_callback=self._source
        session.checked_source=self.binding_callback
        try:session.check()
        except BaseException:
            self.close();raise

    def _source(self):
        frozen=dict(self.base_source())
        if file_sha(self.path)!=self.digest or read_json(self.path,65536)!=self.plan:
            raise ContractError('service plan changed')
        accepted=[e['value'] for e in self.session.events if str(e.get('binding','')).startswith('inbox-accepted:')]
        if len(accepted)>self.policy['max_files']:
            raise ContractError('service original limit exceeded')
        for original in accepted+self.plan['previous_materials']:
            row=self.intake.registry.entry(original['entry_id'])
            if (row['status']!='accepted' or row['custody_health']!='healthy'
                    or row['sha256']!=original['sha256'] or row['size_bytes']!=original['size_bytes']):
                raise ContractError('service original no longer healthy')
            self.notices._custody_item(row['entry_id'])
            with self.intake.staging.asset_lock(row,shared=True) as directory:
                evidence=self.intake.staging.file_evidence(directory,row['safe_filename'])
                self.intake.staging.compare(evidence,{k:row[k] for k in ('size_bytes','sha256','md5')})
            frozen[str(self.intake.runtime.path(row['relative_path']))]=row['sha256']
        return frozen

    def close(self):
        if not self.closed:
            if self.session.checked_source is not self.binding_callback:
                raise ContractError('service source checker was replaced')
            self.session.checked_source=self.base_source;self.closed=True

    def _snapshot(self,state):
        return json.loads(encoded(dict(state=state,materials=list(self.items.values()),
            scans_completed=self.scans,service_admission_open=False,waiting_active=False,
            automatically_restarts=False)))

    def _save(self,state):
        value=self._snapshot(state)
        sequence=sum(str(e.get('binding','')).startswith(self.prefix+'checkpoint:') for e in self.session.events)
        self.session.bind(self.prefix+'checkpoint:'+str(sequence),value)
        return value

    def saved_observation(self):
        values=[e['value'] for e in self.session.events if str(e.get('binding','')).startswith(self.prefix+'checkpoint:')]
        return dict(values[-1] if values else self._snapshot('stopped_before_start'),observed_before_stop=True)

    def _report(self,key,text):
        s=self.session;s.check();nid=stable_key(self.prefix,key)
        plan=dict(notification_id=nid,provider_request_key='service-status-'+nid[:34],
                  target_chat_id=s.approval['chat_id'],payload=text)
        s.bind(self.prefix+'notice:'+key,plan)
        def authoritative():
            s.check()
            if s.value(self.prefix+'notice:'+key)!=plan:
                raise ContractError('saved service notice changed')
            return plan
        return PilotNoticeSender(s,'collection',authoritative).send(plan,nid)

    def _hold(self,key,item,reason,next_action):
        item.update(state='needs_review',reason=reason,next_action=next_action,published=False)
        self._save('processing')
        self._report(key, '材料“'+item['filename']+'”的原件已保存。'+reason+
                     ' 后续会从已保存的材料继续处理，你无需重新发送。尚未生成正式入库结果。')

    def _advance(self,key,item):
        s=self.session
        if item['state'] not in {'queued','received','converted','candidate'}:return
        if self.pipeline.resources.check()['state']!='resources_ready':return
        self.lifecycle.admit()
        if item['state']=='queued':
            received=receive_new_file(s,item['scan_id'],item['message_id'],self.intake)
            item.update(entry_id=received['original']['entry_id'],original=received['original'],state='received')
            self._save('processing')
            notice=self.notices.custody(item['entry_id'])
            if notice['delivery_state']!='delivered':raise ContractError('custody notice unknown')
            original=received['original']
            old=lookup_existing(original['filename'],original['sha256'],self.plan['legacy_catalog'])
            item['existing']=old
            if old['requires_review'] or not old['complete']:
                return self._hold(key,item,'旧库存在相关记录或目录查询不完整，需要先核对。','review_existing')
        if item['state']=='received':
            old=lookup_existing(item['filename'],item['original']['sha256'],self.plan['legacy_catalog'])
            if old['requires_review'] or not old['complete']:
                return self._hold(key,item,'旧库存在相关记录或目录查询不完整，需要先核对。','review_existing')
            converted=self.pipeline.convert_once(entry_id=item['entry_id'])
            if converted.get('state') in {'paused_resources','paused_capacity','busy'}:return
            status=self.intake.status(item['entry_id']);item['conversion']=status
            if status['processing_state']!='ready_candidate':
                return self._hold(key,item,'转换需要技术复核，需检查正文顺序、完整性及图表。技术问题解决前不会发布。',
                                  'technical_review_existing_conversion')
            if Path(item['filename']).suffix.lower() not in self.guide.profile['formats']:
                return self._hold(key,item,'此格式已完成本地转换，仍需核对原件与转换结果后才能生成导读。',
                                  'technical_review_existing_conversion')
            item['state']='converted';self._save('processing')
        if item['state']=='converted':
            if self.image_choices and not self._images_ready(key, item):
                return
            try:self.guide._material(self.pipeline,item['entry_id'])
            except ContractError as error:
                item['technical_reason']=str(error)
                s.check()
                return self._hold(key,item,'正文范围、图片或来源关联尚未满足当前处理条件，需要技术复核。',
                                  'review_existing_conversion')
            result=self.guide(self.pipeline,item['entry_id'])
            if result.get('state') in {'paused_resources','paused_capacity','busy'}:return
            item.update(state='candidate',publication_id=result['publication_id']);self._save('processing')
        if item['state']=='candidate':
            delivered=deliver_preview(s,self.pipeline.previews,item['publication_id'])
            item.update(state='awaiting_owner_decision',delivery=delivered,published=False)
            self._save('processing')
            self._report(key,'材料“'+item['filename']+'”的预览已发送。请直接回复对应 PDF 消息“确认入库”或“暂不入库”。'
                         '你可以稍后查看；本次服务窗口结束后会保留预览与原件，未经你确认不会发布。')

    def _images_ready(self, key, item):
        from .image_availability import availability, warning_text, image_choice_notice_id
        conversion = self.intake.conversion
        job = self.intake._job(item['entry_id'])
        task = conversion.service.ledger.get_task(job['task_id'])
        extraction_id = json.loads(task['quality_json'])['extraction']['extraction_id']
        value = availability(conversion, extraction_id)
        if value is None:
            return True
        if value['decision']:
            if value['decision']['choice'] == 'continue_without_missing':
                return True
            item.update(state='awaiting_replacement_file', image_check=value, published=False)
            self._save('processing')
            return False
        item.update(state='awaiting_image_choice', image_check=value,
                    image_notice_id=image_choice_notice_id(self, key), published=False)
        self._save('processing')
        message_id = self._report('image-choice:' + key, warning_text(item['filename'], value))
        item['image_warning_message_id'] = message_id
        self._save('processing')
        return False

    def _image_decisions(self, sid, messages):
        from .image_availability import COMMANDS, apply_image_reply
        for key, item in self.items.items():
            if item['state'] != 'awaiting_image_choice':
                continue
            choices, invalid = [], []
            for message in messages:
                if message.get('parent_id') != item['image_warning_message_id']:
                    continue
                try:
                    command = observed_reply(message, self.session.approval['bot_id'])[1]
                except ContractError:
                    invalid.append(message)
                    continue
                (choices if command in COMMANDS else invalid).append(message)
            if len(choices) > 1:
                raise ContractError('conflicting image choices; no guide generated')
            if not choices:
                if invalid and not item.get('image_correction_notice'):
                    item['image_correction_notice'] = self._report('image-correction:' + key,
                        '材料“' + item['filename'] + '”仍在等待缺图处理决定。'
                        '请直接回复原来的缺图提示：“重新发文件”或“按缺图版本继续”。'
                        '请勿手输 @；当前未生成导读，未入库。')
                    self._save('processing')
                continue
            decision = apply_image_reply(self.session, self.intake.conversion, item['image_check'],
                item['image_notice_id'], sid, choices[0])
            item.update(image_decision=decision, state='converted' if decision['choice'] ==
                        'continue_without_missing' else 'awaiting_replacement_file', published=False)
            self._save('processing')

    def _reply_correction(self,item,invalid):
        """Keep malformed owner input pending; never infer a decision from it."""
        issue=dict(reason='unrecognized_owner_reply',message_ids=[m['message_id'] for m in invalid])
        if item.get('owner_reply_issue')!=issue:
            item['owner_reply_issue']=issue
            self._save('processing')
        if 'correction_notice' not in item:
            notice=self._report('correction:'+item['publication_id'],
                '材料“'+item['filename']+'”的回复暂未识别为有效决定，尚未入库。'
                '请直接回复对应 PDF：“确认入库”“确认当前版本入库”“补充材料后再入库”或“暂不入库”。'
                '保留飞书自动带出的机器人提及即可，请勿在决定文字前再输入 @。'
                '本次运行窗口内收到准确回复后会继续处理，其他材料仍可照常处理。')
            item['correction_notice']=dict(delivery_state='delivered',message_id=notice)
            self._save('processing')

    def _decisions(self,sid,messages):
        s=self.session;previews=self.pipeline.previews
        for item in self.items.values():
            if item['state']!='awaiting_owner_decision':continue
            delivery=item['delivery'];pid=item['publication_id']
            choices=[];invalid=[]
            for message in messages:
                if message.get('parent_id')!=delivery['message_id']:continue
                # Only the input parser's expected rejection is recoverable.
                # Detail/provenance checks and transport failures below still stop.
                try:command=observed_reply(message,s.approval['bot_id'])[1]
                except ContractError:
                    invalid.append(message);continue
                if command in previews.COMMANDS:choices.append(message)
                else:invalid.append(message)
            if len(choices)>1:raise ContractError('conflicting owner replies; service stopped')
            if not choices:
                if invalid:self._reply_correction(item,invalid)
                continue
            result=apply_owner_reply(s,previews,sid,choices[0]['message_id'],
                prior_delivery=self.prior.get(pid),observed=choices[0])
            item.pop('owner_reply_issue',None)
            item.update(state=result['status'],published=result['status']=='published',decision=result)
            self._save('processing')  # Publication remains true if its notice fails.
            if item['published']:
                item['notification']=self.notices.publication(pid)
                self._save('processing')

    def _scan(self,index,start):
        s=self.session;sid='scan_'+stable_key(self.prefix,index)
        end=s.clock();result=s.scan(sid,start=start,end=end,max_pages=1);self.scans+=1
        if result['status']!='COMPLETE_INTERVAL':
            return 'incomplete_scan',start
        messages=[]
        for receipt,digest in result['receipts'].items():
            if file_sha(Path(receipt))!=digest:raise ContractError('service scan receipt changed')
            messages.extend(m for m in read_json(Path(receipt))['data']['items']
                            if m['message_id'] in result['message_ids'])
        self._image_decisions(sid,messages)
        self._decisions(sid,messages)
        new={m['message_id']:m for m in messages if m.get('msg_type')=='file' and m['message_id'] not in self.items}
        # Older already registered originals stay in their existing workflows.
        new={mid:m for mid,m in new.items() if not self.intake.registry.rows(
            'SELECT 1 FROM pilot_intake_messages WHERE message_key=?',
            (stable_key('pilot-message',s.approval['app_id'],s.approval['chat_id'],mid),))}
        if len(new)+sum(not k.startswith('previous:') for k in self.items)>self.policy['max_files']:
            return 'material_limit_reached',start
        for mid,m in new.items():
            try:filename=json.loads(m['body']['content'])['file_name']
            except (KeyError,TypeError,ValueError) as error:raise ContractError('native file metadata required') from error
            self.items[mid]=dict(message_id=mid,scan_id=sid,filename=filename,state='queued',published=False)
        self._save('processing')
        # One-second overlap protects platform timestamp rounding; stable message
        # identities make the overlap harmless without claiming infinite history.
        return None,end-timedelta(seconds=1)

    def run(self):
        if self.closed:raise ContractError('service controller closed')
        s=self.session;s.check()
        if s.value(self.prefix+'started') is not None:
            raise ContractError('service already consumed; inspect saved work before a new approved continuation')
        s.bind(self.prefix+'started',dict(plan_sha256=self.digest))
        if self.pipeline.resources.check()['state']!='resources_ready':return self._save('paused_resources')
        start=s.clock()-timedelta(seconds=self.plan['lookback_seconds'])
        expires=instant(s.approval['expires_at']);margin=timedelta(seconds=60)
        reason='scan_limit_reached'
        with self.lifecycle.running():
            s.verify_isolation()
            if self.lifecycle.start(checked_ready=self.checked_ready)['delivery_state']!='delivered':
                raise ContractError('service readiness notice unknown')
            for index in range(self.plan['max_scans']):
                s.check()
                if s.clock()+margin>=expires:
                    reason='window_ending';break
                # Resource shortage skips new work, preserving the last cursor
                # and queued originals for the next bounded wakeup.
                if self.pipeline.resources.check()['state']=='resources_ready':
                    reason,start=self._scan(index,start)
                    if reason:break
                    for key,item in self.items.items():self._advance(key,item)
                    self._save('processing')
                    reason='scan_limit_reached'
                else:reason='paused_resources'
                if index+1==self.plan['max_scans']:break
                target=s.clock()+timedelta(seconds=self.plan['poll_interval_seconds'])
                if target+margin>=expires:
                    reason='window_ending';break
                remaining=self.plan['poll_interval_seconds']
                while remaining:
                    s.check();seconds=min(30,remaining);self.wait(seconds);remaining-=seconds;s.check()
                if s.clock()<target:raise ContractError('service wait returned before its planned interval')
            self._save(reason)
            self._report('final','本次资料服务已结束，待确认的预览和已接管的原件均保留。'
                         '已发布、待确认、待复核材料分别保留原状态；再次启动需使用新的有效运行窗口。'
                         '本次结束原因：'+{'scan_limit_reached':'查询次数已用完','window_ending':'运行窗口将结束',
                            'paused_resources':'本机资源暂不足','incomplete_scan':'消息查询未完整覆盖时段',
                            'material_limit_reached':'观察到的材料超过本次处理上限'}[reason]+'。')
            pause=self.lifecycle.pause();value=self._save(reason);value['pause_notice']=pause
            return value
