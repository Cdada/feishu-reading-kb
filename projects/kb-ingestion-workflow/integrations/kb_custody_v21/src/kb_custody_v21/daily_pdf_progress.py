"""Coalesced PDF observations and one owned text notice per material.

Heavy conversion writes local facts only. The existing light reporting loop
does HTTP, with durable in-flight payloads and the same global request ledger.
"""
import json
from pathlib import Path

from .contracts import ContractError, stable_key
from .daily_scheduler import MaterialProblem
from .daily_store import Deferred
from .f01 import PREFIX, _validated_message_content
from .registry import encoded


PREFIX_TEXT = 'PDF 处理进度\n'
FINAL = {'awaiting_confirmation', 'notified', 'published', 'rejected', 'duplicate', 'held', 'needs_upload'}


def validate_progress(item, policy, message_id, text):
    return _validated_message_content(item, app_id=policy.app_id, bot_id=policy.bot_id,
        chat_id=policy.chat_id, kind='text', name='', message_id=message_id,
        text=text, observe_updated_text=True)


def reconcile_progress(transport, operation, saved, request):
    """Read the exact owned message before repeating the identical PUT."""
    binding = request['progress']
    mid = binding['message_id']
    proof = transport.request('progress-reconcile:'+stable_key(operation,saved['attempts']),
        'message', PREFIX+'/im/v1/messages/'+mid, evidence=request['evidence'])
    items = proof['value'].get('data',{}).get('items')
    if not isinstance(items,list) or len(items)!=1:
        raise MaterialProblem('进度消息暂时无法核对，已停止编辑该消息。')
    text = json.loads(items[0]['body']['content']).get('text')
    validate_progress(items[0],transport.authority.policy,mid,text)
    desired = json.loads(json.loads(request['body'])['content'])['text']
    if text == desired:
        result = dict(proof,value=dict(code=0,data=items[0]),reconciled_from='verified_progress_message')
        transport._checked_result(result)
        transport.store.complete(operation,saved['attempts'],result)
        return result
    if text != binding['previous_text']:
        raise MaterialProblem('进度消息内容已变化，已停止自动编辑；资料处理继续。')
    transport.store.resolve_retry(operation,dict(safe_to_retry=True,receipts=[proof['receipt']],
        reason='known_message_retains_previous_text; repeat_identical_PUT_within_edit_budget'))
    return None


def render_progress(document, snapshot, defer, mineru, now):
    phase = document['phase']
    stage = phase
    detail = ''
    total, done = (snapshot or {}).get('total_pages'), (snapshot or {}).get('completed_pages',0)
    labels = {
        'registered':'已登记，等待保存原件', 'original_saved':'原件已保存，等待转换',
        'converted':'页面转换完成，等待生成导读', 'generating':'页面转换完成，正在生成导读',
        'model_unknown':'页面转换完成，正在核对导读生成结果',
        'guide_ready':'导读已生成，正在准备预览 PDF', 'preview_ready':'正在发送导读 PDF',
        'send_unknown':'正在核对预览发送结果', 'awaiting_confirmation':'导读 PDF 已发送，请回复对应 PDF 确认入库',
        'awaiting_images':'等待你回复缺图提示', 'confirmed':'已收到确认，正在入库',
        'reading':'已收到确认，正在按知识库规范整理',
        'reading_wait':'已收到确认，完整整理等待你补充来源信息',
        'published':'已入库，完成通知待发送', 'notified':'已入库',
        'rejected':'已按你的选择暂不入库', 'duplicate':'同一原件已有入库结果',
        'held':'本篇已暂停，需要后续处理', 'needs_upload':'需要补传文件',
    }
    label = labels.get(stage)
    if label is None:
        raise ContractError('unknown PDF progress phase')
    if phase=='original_saved' and snapshot:
        stage = snapshot['stage']
        label = {'converting':'正在转换页面', 'retrying':'正在补救识别当前批次',
                 'merging':'页面转换完成，正在校验和合并', 'converted':'页面转换完成，正在整理结果'}[stage]
        if snapshot.get('first_page'):
            detail = f"当前批次：原 PDF 第 {snapshot['first_page']}–{snapshot['last_page']} 页。"
        if mineru and mineru.get('kind')=='starting':
            stage,label = 'starting','正在启动 MinerU，等待页面转换'
    if phase not in FINAL and defer and defer.get('until',0)>now:
        reason=defer.get('reason')
        if reason in ('paused_resources','paused_capacity','stopping'):
            stage,label = 'paused','资源不足或服务暂停，已完成的页面会保留'
        elif reason=='storage_unavailable':
            stage,label = 'storage_unavailable','资料盘暂不可用，已暂停；请连接原资料盘并检查可用空间'
        elif reason=='model_limit':
            stage,label = 'model_limit','页面已保留，等待补充模型生成额度'
    if phase in ('held','needs_upload'):
        detail = document.get('reason') or ''
    filename = document['filename'].replace('\n',' ').replace('\r',' ')[:180]
    lines = [PREFIX_TEXT+filename,label]
    bucket = 0
    if total is not None:
        if type(total) is not int or type(done) is not int or not 0<=done<=total<=100 or total<1:
            raise ContractError('invalid PDF progress page count')
        bucket = done*10//total
        bar = '■'*bucket+'□'*(10-bucket)
        lines.append(f'页面转换：{bar} {done}/{total} 页（{done*100//total}%）')
    else:
        lines.append('页面转换：页数待读取')
    if snapshot and snapshot.get('compatibility') == 'pdf-identical-adjacent-mediabox/v1':
        lines.append('已兼容处理 PDF 重复的页面尺寸字段；原件保持不变。')
    if detail:
        lines.append(detail[:500])
    lines.append('此进度只表示页面转换；导读生成和确认入库另行显示。')
    return dict(text='\n'.join(lines),stage=stage,bucket=bucket)


class PdfProgress:
    def __init__(self, backend):
        self.backend,self.store,self.transport = backend,backend.store,backend.transport

    def record(self, mid, event):
        document = self.store.document(mid)
        if event['source_sha']!=document['source_sha'] or event['stage'] not in {'converting','retrying','merging','converted'}:
            raise ContractError('PDF progress must bind the accepted original')
        # Local only: reporting latency/failure cannot interrupt a MinerU batch.
        self.store.put('pdf-progress:'+mid,dict(event,at=self.store.now()))
        deferred = self.store.get('defer:'+mid)
        if deferred and deferred.get('reason') in ('paused_resources','paused_capacity','stopping'):
            self.store.put('defer:'+mid,None)

    def report(self):
        # Historical terminal documents do not acquire unsolicited notices.
        rows=self.store.rows('SELECT message_id,filename,phase FROM daily_documents ORDER BY created,message_id')
        for row in rows:
            if Path(row['filename']).suffix.lower()!='.pdf':
                continue
            mid=row['message_id']
            display=self.store.get('pdf-display:'+mid)
            if display is None and row['phase'] in FINAL:
                continue
            if display and display.get('disabled'):
                if row['phase'] in {'published','notified','rejected','duplicate'}:
                    if not display.get('fallback_sent') and not display.get('fallback_suppressed'):
                        display['fallback_suppressed']='material_finished'
                        self.store.put('pdf-display:'+mid,display)
                    continue
                if not display.get('fallback_sent'):
                    key='pdf-progress-unavailable:'+mid
                    saved=self.store.rows('SELECT payload FROM daily_operations WHERE op_key=?',('notice:'+key,))
                    text=(json.loads(saved[0]['payload'])['notice_plan']['payload'] if saved else
                        '“'+row['filename']+'”仅进度消息无法继续更新，不代表资料处理或入库失败。请以导读 PDF 和入库完成通知为准。')
                    self.backend.notice(key,text)
                    display['fallback_sent']=True
                    self.store.put('pdf-display:'+mid,display)
                continue
            document=self.store.document(mid)
            snapshot=self.store.get('pdf-progress:'+mid)
            desired=render_progress(document,snapshot,self.store.get('defer:'+mid),
                                    self.store.get('mineru_status'),self.store.now())
            display=display or dict(sequence=0,last_at=0)
            if not display.get('pending'):
                if (display.get('stage'),display.get('bucket')) == (desired['stage'],desired['bucket']):
                    continue
                if display.get('origin') and row['phase'] not in FINAL and self.store.now()-display['last_at']<30:
                    continue
                desired['sequence']=display['sequence']+1
                display['pending']=desired
                self.store.put('pdf-display:'+mid,display)
            try:
                self._send(mid,display)
            except MaterialProblem as error:
                display.update(disabled=True,error=error.reason)
                self.store.put('pdf-display:'+mid,display)
            except Deferred as error:
                self.store.put('pdf-progress-wait:'+mid,dict(reason=error.reason,until=error.until))
                if error.reason in {'progress_update_limit','attempts_exhausted'}:
                    display.update(disabled=True,error=error.reason)
                    self.store.put('pdf-display:'+mid,display)
            # At most one progress write per existing reporting tick. Collection
            # and owner confirmation retain their own request budget/reserve.
            return

    def _send(self, mid, display):
        desired=display['pending']
        origin=display.get('origin')
        if origin is None:
            sent=self.backend.notice('pdf-progress:'+mid,desired['text'])
            display['origin']=sent
        else:
            self.transport._checked_result(origin['receipt'])
            p=self.backend.authority.policy
            body=encoded(dict(msg_type='text',content=encoded(dict(text=desired['text']))))
            progress=dict(message_id=origin['message_id'],origin_receipt=origin['receipt']['receipt'],
                initial_text=origin['plan']['payload'],previous_text=display['text'])
            receipt=self.transport.request('pdf-progress-update:'+mid+':'+str(desired['sequence']),
                'send',PREFIX+'/im/v1/messages/'+origin['message_id'],mode='update',body=body,
                progress=progress,evidence=origin['receipt']['evidence'])
            validate_progress(receipt['value'].get('data'),p,origin['message_id'],desired['text'])
        display.update(desired,last_at=self.store.now())
        display.pop('pending')
        self.store.put('pdf-display:'+mid,display)
        self.store.put('pdf-progress-wait:'+mid,None)
