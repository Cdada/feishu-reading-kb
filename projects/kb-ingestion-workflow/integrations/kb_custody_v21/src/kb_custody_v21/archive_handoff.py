"""Small private handoff document from recorded facts; no AI or recovery writes."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re

from .contracts import ContractError
from .registry import encoded
from .runtime import no_symlinks

NAME = '待处理材料与接续说明.local.md'
RESOURCE = {'paused_resources','paused_capacity','stopping','storage_unavailable',
            'model_limit','request_rate','request_daily_limit','retry_wait','card_listener_not_ready'}


def safe_text(value, maximum=900):
    text=str(value or '').replace('\x00','')
    text=re.sub(r'(?i)bearer\s+\S+','Bearer [已隐藏]',text)
    text=re.sub(r'(?i)(api[_ -]?key|access[_ -]?token|authorization|password|secret)\s*[:=][^\n\r]*',r'\1=[已隐藏]',text)
    text=re.sub(r'(?i)\bsk-[A-Za-z0-9_-]{8,}','[已隐藏]',text)
    text=re.sub(r'<!--[\s\S]*?-->','[注释已省略]',text)
    return text[:maximum].replace('\r',' ').replace('\n',' ')


def category(document, defer, display):
    phase=document['phase'];reason=(defer or {}).get('reason','')
    if display and display.get('disabled'):
        return 'codex', '主卡无法更新；业务成果需与显示状态分别核对。'
    if phase in ('held','model_unknown'):
        return 'codex', document.get('reason') or reason or '模型结果尚未核清，不能额外重发。'
    if phase in ('reading_wait','needs_upload'):
        return 'user', document.get('reason') or '等待补充来源信息或完整原件。'
    if reason in RESOURCE or reason.startswith('subscription_'):
        return 'resource', reason
    if reason.startswith('reading_model_result_') or reason in ('reading_result_requires_codex_review','reading_model_approval_required'):
        return 'codex',reason
    return None, None


class ArchiveHandoff:
    def __init__(self, backend):
        self.b,self.store=backend,backend.store
        self.path=no_symlinks(backend.authority.root/NAME)

    def facts(self, document):
        mid=document['message_id'];data=document['data']
        defer=self.store.get('defer:'+mid) or {}
        # A due time only permits another attempt; it is not proof that the
        # resource problem disappeared. Require a later recorded work result.
        advanced=document['updated']
        if data.get('reading_material_id'):
            stamps=self.store.rows('SELECT updated FROM reading_materials WHERE material_id=?',(data['reading_material_id'],))
            if stamps:advanced=max(advanced,stamps[0]['updated'])
        if defer.get('until') and advanced>defer['until'] and document['phase'] not in ('held','model_unknown'):
            defer={}
        display=self.store.get('archive-main:'+mid)
        kind,reason=category(document,defer,display)
        previous=self.store.get('archive-handoff:'+mid)
        if kind is None and previous is None:
            return None
        identity='KB-EX-'+hashlib.sha256(mid.encode()).hexdigest()[:12].upper()
        material=None;calls=[]
        material_id=data.get('reading_material_id')
        if material_id:
            rows=self.store.rows('SELECT state,detail FROM reading_materials WHERE material_id=?',(material_id,))
            if rows:material=rows[0]
            calls=self.store.rows('SELECT purpose,model_key,state FROM reading_calls WHERE material_id=?',(material_id,))
        keys=[document['source_sha']] if document.get('source_sha') else []
        keys.extend(c['model_key'] for c in calls)
        models=[]
        if keys:
            models=self.store.rows('SELECT state,attempt FROM daily_models WHERE source_sha IN ('+','.join('?' for _ in keys)+')',keys)
        progress=self.store.get('pdf-progress:'+mid)
        locations=dict(queue=str(self.store.path),registry=str(self.b.registry.runtime.db))
        if material_id:locations['reading_job']=str(self.b.authority.root/'reading-jobs'/material_id)
        if document.get('source_sha') and Path(document['filename']).suffix.lower()=='.pdf':
            locations['pdf_chunks']=str(self.b.authority.runtime.path('conversion-work/pdf-chunks')/document['source_sha'])
        recovery='暂无通用自动恢复入口。由 Codex 核对本记录指向的账本、回执与已保存成果，按既有接口逐项处理。'
        if (document['phase']=='held' and Path(document['filename']).suffix.lower()=='.pdf'
                and document.get('entry_id') and not document.get('publication_id') and not document.get('canonical_id') and not models):
            recovery='候选入口：部署包中的 pdf_recover.py（daily_pdf_recovery.resume_repaired_pdf）。先确认服务和 MinerU 已停止，再核对原件健康、无模型预留、无既有转换任务及修复页证据；入口自身会再次检查。不自动执行。'
        if kind=='resource':recovery='由现有调度器在资源或订阅条件恢复后继续；不要手动重置失败次数或调用账本。'
        if kind=='user':recovery='在飞书回复对应补充信息消息，或发送完整原件；不能代替用户确认。'
        recent=self.store.rows("SELECT at,kind,payload FROM daily_events WHERE json_extract(payload,'$.message_id')=? ORDER BY id DESC LIMIT 6",(mid,))
        history=[]
        for row in reversed(recent):
            event=json.loads(row['payload'])
            history.append(dict(at=row['at'],kind=row['kind'],before=event.get('before'),after=event.get('after')))
        return dict(id=identity,filename=safe_text(document['filename'],240),document_id=mid,
            category=kind or 'resumed',reason=safe_text(reason),phase=document['phase'],
            reading_state=material['state'] if material else None,
            confirmed=data.get('decision',{}).get('command','尚未记录用户决定'),
            recorded_outputs={k:document.get(k) for k in ('entry_id','source_sha','publication_id','preview_message_id') if document.get(k)},
            reading_completed=bool(data.get('reading_completed')),model_attempts=len(models),
            model_states=[m['state'] for m in models],reading_calls=[dict(purpose=c['purpose'],state=c['state']) for c in calls],
            page_progress={k:progress.get(k) for k in ('stage','completed_pages','total_pages','first_page','last_page')} if progress else None,
            locations=locations,recovery=recovery,history=history)

    def observe(self, document, *, flush=True):
        facts=self.facts(document)
        if facts is None:return None
        key='archive-handoff:'+document['message_id'];old=self.store.get(key)
        if old is None or old['facts']!=facts:
            self.store.put(key,dict(facts=facts,updated=self.store.now()))
        if flush:self.render()
        return facts

    def block(self, record):
        f=record['facts'];stamp=datetime.fromtimestamp(record['updated'],timezone.utc).astimezone().isoformat(timespec='seconds')
        title={'resource':'等待资源恢复','user':'等待用户补充','codex':'需要 Codex 核查','resumed':'已恢复或已完成，见当前状态'}[f['category']]
        lines=[f"- 状态：{title}；更新时间：{stamp}",f"- 材料：{f['filename']}",
            f"- 阶段：{f['phase']}；整理状态：{f['reading_state'] or '未开始'}；整理完成：{f['reading_completed']}",
            f"- 用户决定：{safe_text(f['confirmed'])}"]
        if f['category']=='resumed':
            lines.append('- 本次阻塞已不在当前状态中；不据此宣称全文整理已完成。处理备注保留。')
        else:
            lines += [f"- 原因：{f['reason']}",f"- 已记录成果标识：`{encoded(f['recorded_outputs'])}`（记录不代替再次核对原件）",
                f"- 模型尝试：{f['model_attempts']}；状态：{encoded(f['model_states'])}；整理调用：{encoded(f['reading_calls'])}",
                '- 模型结果不明时先查回执；本说明不授予新调用或自动重试权限。',
                f"- 页面记录：`{encoded(f['page_progress'])}`（仅成功页；无记录时不估算）",
                '- 请求次数不能完整归因到单篇；按 queue.sqlite3 的 daily_operations / daily_requests 查询，不将模型次数当作全部操作次数。',
                f"- 接续方法：{f['recovery']}"]
            lines += [f'- {k}：`{v}`' for k,v in f['locations'].items()]
        lines += [f"- 任务标识：`{f['document_id']}`",f"- 最近处理记录：`{encoded(f['history'])}`"]
        return '\n'.join(lines)+'\n'

    def render(self):
        rows=self.store.rows("SELECT value FROM daily_meta WHERE key LIKE 'archive-handoff:%' ORDER BY key")
        if not rows:return
        path=no_symlinks(self.path)
        if path.exists() and path.stat().st_size>4*1024*1024:
            raise ContractError('handoff document exceeds safe edit size')
        original=path.read_text() if path.exists() else ''
        if len(original.encode())>4*1024*1024:raise ContractError('handoff document exceeds safe edit size')
        text=original or '# 待处理材料与接续说明\n\n本文件仅保存本项目的接续摘要。机器状态块自动更新；Codex 处理备注由人工维护。不得凭本文件绕过逐篇确认或重置历史账本。\n'
        for row in rows:
            record=json.loads(row['value']);identity=record['facts']['id']
            start=f'<!-- BEGIN MACHINE {identity} -->';end=f'<!-- END MACHINE {identity} -->'
            body=start+'\n'+self.block(record)+end
            if start in text or end in text:
                if text.count(start)!=1 or text.count(end)!=1 or text.index(start)>text.index(end):
                    raise ContractError('handoff machine markers need manual repair; notes were not overwritten')
                begin=text.index(start);finish=text.index(end)+len(end)
                text=text[:begin]+body+text[finish:]
            else:
                text+=f'\n## {identity}\n\n'+body+'\n\n### Codex 处理备注\n\n待补充处理结论与依据；请在本区追加，不改机器状态块。\n'
        if text==original:return
        if (path.read_text() if path.exists() else '')!=original:
            raise ContractError('handoff changed during rendering; retry without overwriting user notes')
        temp=no_symlinks(path.with_name(path.name+'.pending'))
        fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        try:
            with os.fdopen(fd,'w') as stream:stream.write(text);stream.flush();os.fsync(stream.fileno())
            if (path.read_text() if path.exists() else '')!=original:
                raise ContractError('handoff changed before replacement; user notes retained')
            os.replace(temp,path)
        finally:
            if temp.exists():temp.unlink()
