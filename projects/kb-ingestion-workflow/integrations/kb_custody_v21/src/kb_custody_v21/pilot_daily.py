"""One finite new-material run through receipt, preview and exact owner decision.

No sample hash, daemon, automatic retry or new approval is invented. Each
external category has one parent budget; the original and PDF downloads differ.
"""
from datetime import timedelta
from pathlib import Path
import time

from .contracts import ContractError, stable_key
from .f05 import file_sha
from .pilot_decision import apply_owner_reply, observed_reply
from .pilot_delivery import deliver_preview
from .pilot_feishu import read_json
from .pilot_inbox import inbox_policy, receive_inbox
from .pilot_inbox_guide import PilotInboxGuide
from .pilot_lifecycle import PilotLifecycle
from .pilot_notice_workflow import PilotNoticeWorkflow
from .pilot_notices import PilotNoticeSender
from .pilot_pipeline import PilotPipeline
from .registry import encoded
from .runtime import no_symlinks
from .s6 import instant


def daily_limits(scans):
    if type(scans) is not int or not 2 <= scans <= 34:
        raise ContractError('daily run requires 2 to 34 explicitly budgeted owner scans')
    # 3 identity, 1 intake list, N owner lists, 2 details, 2 downloads,
    # 1 upload, at most 7 sends; each HTTP business request has one auth.
    return dict(auth=scans+16,identity=3,list=scans+1,message=2,download=2,upload=1,send=7)


class PilotDailyRun:
    def __init__(self,session,intake,plan_path,*,wire,checked_ready,resources=None,wait=time.sleep):
        self.session,self.intake=session,intake
        self.path=no_symlinks(Path(plan_path).absolute());self.digest=file_sha(self.path)
        frozen=session.binding();self.plan=p=read_json(self.path,65536)
        if (set(p) != {'reference','inbox_plan_path','guide_plan_path','max_owner_scans','poll_interval_seconds'}
                or p['reference'] != 'P22-daily-run-v1'
                or session.approval.get('operation') != 'inbox-daily'
                or frozen.get(str(self.path)) != self.digest
                or session.limits != daily_limits(p['max_owner_scans'])
                or type(p['poll_interval_seconds']) is not int or not 30 <= p['poll_interval_seconds'] <= 3600
                or not callable(checked_ready) or not callable(wait)):
            raise ContractError('explicit finite daily run plan, budget and readiness required')
        if inbox_policy(session)['max_files'] != 1:
            raise ContractError('daily run processes exactly one new original')
        self.inbox_path=no_symlinks(Path(p['inbox_plan_path']).absolute())
        self.guide_path=no_symlinks(Path(p['guide_plan_path']).absolute())
        if (str(self.inbox_path) != p['inbox_plan_path'] or str(self.guide_path) != p['guide_plan_path']
                or session.approval.get('guide_plan') != str(self.guide_path)
                or any(frozen.get(str(x)) != file_sha(x) for x in (self.inbox_path,self.guide_path))):
            raise ContractError('daily intake and guide plans must be frozen explicitly')
        scan=read_json(self.inbox_path,65536)
        self.guide=PilotInboxGuide(session,self.guide_path,wire=wire)
        if (set(scan) != {'reference','lookback_seconds','legacy_catalog'}
                or scan['reference'] != 'P20-receive-inbox-v2'
                or scan['legacy_catalog'] != self.guide.profile['legacy_catalog']
                or type(scan['lookback_seconds']) is not int or not 1 <= scan['lookback_seconds'] <= 86400):
            raise ContractError('daily run requires the same explicit V1 catalog before conversion')
        self.pipeline=PilotPipeline(intake,resources=resources)
        self.lifecycle=PilotLifecycle(session,intake,resources=self.pipeline.resources)
        self.notices=PilotNoticeWorkflow(session,intake)
        self.checked_ready,self.wait=checked_ready,wait
        self.prefix='daily:'+session.approval_sha+':'
        self.base_source=session.checked_source;self.binding_callback=self._source;self.closed=False
        session.checked_source=self.binding_callback
        try:session.check()
        except BaseException:
            self.close();raise

    def _source(self):
        frozen=dict(self.base_source())
        if file_sha(self.path) != self.digest or read_json(self.path,65536) != self.plan:
            raise ContractError('daily plan changed')
        accepted=[e['value'] for e in self.session.events if str(e.get('binding','')).startswith('inbox-accepted:')]
        if len(accepted)>1:raise ContractError('daily run cannot bind more than one original')
        for original in accepted:
            row=self.intake.registry.entry(original['entry_id'])
            if (row['status'] != 'accepted' or row['custody_health'] != 'healthy'
                    or row['sha256'] != original['sha256'] or row['size_bytes'] != original['size_bytes']):
                raise ContractError('daily accepted original is no longer healthy')
            self.notices._custody_item(row['entry_id'])
            with self.intake.staging.asset_lock(row,shared=True) as directory:
                actual=self.intake.staging.file_evidence(directory,row['safe_filename'])
                self.intake.staging.compare(actual,{k:row[k] for k in ('size_bytes','sha256','md5')})
            frozen[str(self.intake.runtime.path(row['relative_path']))]=row['sha256']
        return frozen

    def close(self):
        if not self.closed:
            if self.session.checked_source is not self.binding_callback:
                raise ContractError('daily source checker was replaced')
            self.session.checked_source=self.base_source;self.closed=True

    def saved_observation(self):
        """Preserve already observed publication when a later notice fails."""
        for phase in ('result','outcome','decision','preview'):
            value=self.session.value(self.prefix+phase)
            if value is not None:
                return dict(value,observed_before_stop=True)
        return dict(state='stopped_before_preview',published=False,observed_before_stop=True)

    def _report(self,code):
        messages={
            'preview': '本次预览已发送。请直接回复那条 PDF 消息“确认入库”；也可以回复“暂不入库”。本次只在已批准的有限时间内等待，暂停后不会自动发布。',
            'review_existing': '本次原件已保存。旧知识库存在相关记录，或目录查询不完整，需要先核对是否重复及能否复用。正文尚未生成，请保留原文件。',
            'material_needs_review': '本次原件已保存。旧库记录、转换结果、文件类型、图片或正文范围需要技术复核，正文尚未生成。请保留原文件。',
            'no_material': '本次查询没有观察到可处理的文件。这不证明停机期间没有投递；本次运行即将暂停，请保留原文件。',
            'batch_limit_exceeded': '本次查询观察到多份文件，超过批准的一份材料上限，尚未下载。请核对投递时间和下一次处理范围。',
            'incomplete_scan': '本次查询未覆盖整个时段，尚未下载可见文件。请保留原文件，核对后再安排新的有限查询。',
            'paused_resources': '本次处理因本机资源或处理队列暂不允许继续而暂停。已经接管的原件保持保存，正文或预览未继续生成，请保留原文件。',
        }
        if code not in messages:raise ContractError('explicit daily status required')
        s=self.session;s.check()
        nid=stable_key(self.prefix,'status',code)
        plan=dict(notification_id=nid,provider_request_key='daily-status-'+nid[:36],
                  target_chat_id=s.approval['chat_id'],payload=messages[code])
        s.bind(self.prefix+'status-plan',plan)
        def authoritative():
            s.check()
            if s.value(self.prefix+'status-plan') != plan:
                raise ContractError('daily status plan changed')
            return plan
        mid=PilotNoticeSender(s,'collection',authoritative).send(plan,nid)
        return dict(delivery_state='delivered',message_id=mid)

    def _finish(self,result):
        result=dict(result,service_admission_open=False,waiting_active=False,automatically_restarts=False)
        # Preserve publication truth before a possible failure of the pause notice.
        self.session.bind(self.prefix+'outcome',dict(result))
        result['pause_notice']=self.lifecycle.pause()
        self.session.bind(self.prefix+'result',result)
        return result

    def run(self):
        if self.closed:raise ContractError('daily controller closed')
        s=self.session;s.check()
        if s.value(self.prefix+'started') is not None:
            raise ContractError('daily run already consumed; inspect evidence without restarting')
        s.bind(self.prefix+'started',dict(plan_sha256=self.digest))
        capacity=self.pipeline.resources.check()
        if capacity['state'] != 'resources_ready':
            result=dict(capacity,service_admission_open=False,waiting_active=False,published=False)
            s.bind(self.prefix+'result',result);return result
        with self.lifecycle.running():
            s.verify_isolation()
            ready=self.lifecycle.start(checked_ready=self.checked_ready)
            if ready['delivery_state'] != 'delivered':
                return dict(state='stopped_notice_unknown',notice=ready,service_admission_open=False)
            received=receive_inbox(s,self.intake,self.inbox_path,resources=self.pipeline.resources)
            state=received['state'];accepted=received.get('accepted',[])
            if state != 'intake_finished':
                self._report(state if state in {'batch_limit_exceeded','incomplete_scan'} else 'paused_resources')
                return self._finish(dict(state=state,intake=received,published=False))
            sid='scan_inbox_'+s.approval_sha[:24]
            if not accepted:
                self.notices.collection(sid);self._report('no_material')
                return self._finish(dict(state='no_material',published=False))
            material=accepted[0];eid=material['original']['entry_id']
            notice=self.notices.custody(eid)
            if notice['delivery_state'] != 'delivered':
                return dict(state='stopped_notice_unknown',entry_id=eid,notice=notice,service_admission_open=False)
            notice=self.notices.collection(sid)
            if notice['delivery_state'] != 'delivered':
                return dict(state='stopped_notice_unknown',entry_id=eid,notice=notice,service_admission_open=False)
            if material['next_action'] != 'prepare_conversion':
                self._report('review_existing')
                return self._finish(dict(state='review_existing',entry_id=eid,existing=material['existing'],published=False))
            self.lifecycle.admit()
            converted=self.pipeline.convert_once(entry_id=eid)
            state=self.intake.status(eid)
            if converted.get('state') in {'paused_resources','paused_capacity','busy'}:
                self._report('paused_resources')
                return self._finish(dict(state=converted['state'],entry_id=eid,published=False))
            if (state['processing_state'] != 'ready_candidate'
                    or Path(material['original']['filename']).suffix.lower() not in self.guide.profile['formats']):
                self._report('material_needs_review')
                return self._finish(dict(state='material_needs_review',entry_id=eid,conversion=state,published=False))
            self.lifecycle.admit()
            try:
                # Local eligibility is checked before a model intent. Known
                # holds can be reported while a healthy session remains usable.
                self.guide._material(self.pipeline,eid)
            except ContractError as error:
                s.bind(self.prefix+'eligibility-hold',dict(error_type=type(error).__name__,reason=str(error)))
                s.check()
                self._report('material_needs_review')
                return self._finish(dict(state='material_needs_review',entry_id=eid,published=False))
            candidate=self.guide(self.pipeline,eid)
            if candidate.get('state') in {'paused_resources','paused_capacity','busy'}:
                self._report('paused_resources')
                return self._finish(dict(candidate,entry_id=eid,published=False))
            s.check()
            start=s.clock()-timedelta(seconds=1)
            delivered=deliver_preview(s,self.pipeline.previews,candidate['publication_id'])
            s.bind(self.prefix+'preview',delivered)
            self._report('preview')
            decision=self._wait_owner(delivered,start)
            return self._finish(dict(decision,entry_id=eid))

    def _wait_owner(self,delivery,start):
        s=self.session;p=self.plan;previews=self.pipeline.previews;pid=delivery['publication_id']
        expires=instant(s.approval['expires_at']);margin=timedelta(seconds=60);scans=0
        def pending(reason):
            return dict(state='awaiting_owner_decision',publication_id=pid,published=False,
                        scans_completed=scans,reason=reason)
        for index in range(p['max_owner_scans']):
            s.check()
            with self.intake.registry.transaction() as db:
                previews._candidate(db,pid);previews._current(db,pid)
            row=previews.publisher._row(pid)
            if row['state'] != 'prepared' or self.intake.registry.rows(
                    'SELECT * FROM pilot_preview_decisions WHERE publication_id=?',(pid,)):
                raise ContractError('current delivered candidate must remain undecided')
            previews.publisher._tree(row,pid+'.candidate')
            if s.clock()+margin >= expires:return pending('insufficient_window')
            sid='scan_'+stable_key(self.prefix,'owner',index)
            result=s.scan(sid,start=start,end=s.clock(),max_pages=1);scans+=1
            if result['status'] != 'COMPLETE_INTERVAL':
                raise ContractError('owner scan incomplete; daily wait stopped')
            choices=[]
            for receipt in result['receipts']:
                for item in read_json(Path(receipt))['data']['items']:
                    if item['message_id'] not in result['message_ids'] or item.get('parent_id') != delivery['message_id']:
                        continue
                    if observed_reply(item,s.approval['bot_id'])[1] in previews.COMMANDS:
                        choices.append(item)
            if len(choices)>1:raise ContractError('conflicting owner replies; daily wait stopped')
            if choices:
                value=apply_owner_reply(s,previews,sid,choices[0]['message_id'],observed=choices[0])
                value=dict(value,state=value['status'],published=value['status']=='published')
                s.bind(self.prefix+'decision',dict(value))
                if value['published']:value['notification']=self.notices.publication(pid)
                return value
            if index+1 == p['max_owner_scans']:return pending('scan_limit_reached')
            target=s.clock()+timedelta(seconds=p['poll_interval_seconds'])
            if target+margin >= expires:return pending('insufficient_window')
            remaining=p['poll_interval_seconds']
            while remaining:
                s.check();seconds=min(30,remaining);self.wait(seconds);remaining-=seconds;s.check()
            if s.clock()<target:raise ContractError('owner wait returned before the planned interval')
        raise AssertionError('validated daily wait must return')
