"""Read-only local status; never imports or constructs a live platform adapter."""
import json
from pathlib import Path
import sqlite3

from .daily_launcher import DailyLauncher
from .runtime import no_symlinks


def local_status(root):
    root=no_symlinks(Path(root).absolute())
    result=DailyLauncher(root).status()
    path=no_symlinks(root/'queue.sqlite3')
    if not path.exists():
        return dict(result,summary=dict(last_published=None,continuing_count=0,needs_action=[],needs_upload=[],model_turns=0))
    db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=5)
    db.row_factory=sqlite3.Row
    try:
        meta={r['key']:json.loads(r['value']) for r in db.execute('SELECT key,value FROM daily_meta')}
        rows=[dict(r) for r in db.execute('SELECT * FROM daily_documents ORDER BY updated,message_id')]
        published=[r for r in rows if r['phase'] in ('published','notified')]
        actions=[dict(filename=r['filename'],action=r['user_action'],reason=r['reason'],preview_message_id=r['preview_message_id']) for r in rows if r['user_action']]
        summary=dict(last_published=published[-1]['filename'] if published else meta.get('previous_last_published'),
            continuing_count=sum(r['phase'] not in ('notified','published','duplicate','rejected','needs_upload') and not r['user_action'] for r in rows),
            needs_action=actions,needs_upload=[r['filename'] for r in rows if r['phase']=='needs_upload'],
            pending_notifications=sum(r['phase']=='published' for r in rows),
            model_turns=db.execute('SELECT count(*) FROM daily_models').fetchone()[0],
            history_gaps=[dict(r) for r in db.execute('SELECT channel,gap_start,gap_end FROM daily_scans WHERE gap_start IS NOT NULL')])
        tasks=[v for k,v in meta.items() if k.startswith('relation-task:') and isinstance(v,dict)]
        if tasks:
            summary['relations']=dict(
                waiting=sum(v['state']=='waiting' and v['kind'] in ('choose','type') for v in tasks),
                processing=sum(v['state']=='active' for v in tasks),
                blocked=[dict(document_id=v['document_id'],reason=v.get('error')) for v in tasks if v['state']=='blocked'],
                write_pending=bool(meta.get('relation-write-pending')),
                wait=meta.get('relation-wait'))
        return dict(result,summary=summary,heartbeat=meta.get('heartbeat'),network_pause=meta.get('network_pause'),
            collection_wait=meta.get('collection_wait'),storage_wait=meta.get('storage_wait'),policy=meta.get('policy'))
    finally:
        db.close()


def user_summary(value, *, max_items=None):
    def title(text):
        return text if max_items is None or len(text)<=80 else text[:77]+'…'
    running=value.get('running',False)
    lines=['飞书知识库：'+('正在运行' if running else '未运行')]
    if value.get('stop_request')=='stop_requested' or value.get('stop_requested'):
        lines.append('已请求停止，正在保存当前阶段；进程退出后状态会变为未运行。')
    if value.get('state')=='starting':
        lines[0]='飞书知识库：正在启动，尚未确认就绪'
    heartbeat=value.get('heartbeat') or {}
    if heartbeat.get('state')=='interrupted':
        lines.append('上次运行已暂停，需要核查：'+str(heartbeat.get('detail','未知原因')))
    if value.get('storage_wait'):
        lines.append('资料存储已暂停：'+str(value['storage_wait'].get('message','请连接原资料盘并检查空间。')))
    summary=value.get('summary',{})
    if summary.get('last_published'):
        lines.append('最近入库：'+title(summary['last_published']))
    lines.append('可自动继续：'+str(summary.get('continuing_count',0))+' 份')
    actions=summary.get('needs_action',[])
    if actions:
        lines.append('需要后续操作：')
        for row in actions if max_items is None else actions[:max_items]:
            action=row['action']
            label={'reply_to_original_pdf':'回复对应 PDF 确认', 'reply_to_image_warning':'回复缺图提示选择如何处理',
                'reply_to_reading_question':'回复对应整理问题补充来源信息', 'resend_file':'补传原件','resend_supported_file':'补传支持的格式','contact_codex':'由 Codex 核查'}.get(action,action)
            lines.append('  '+title(row['filename'])+'：'+label)
        if max_items is not None and len(actions)>max_items:
            lines.append('  另有 '+str(len(actions)-max_items)+' 份，请打开本机“查看知识库状态”查看完整名称。')
    if summary.get('needs_upload'):
        uploads=summary['needs_upload']
        lines.append('需要补传：'+'、'.join(title(t) for t in (uploads if max_items is None else uploads[:max_items])))
        if max_items is not None and len(uploads)>max_items:
            lines.append('另有 '+str(len(uploads)-max_items)+' 份需补传，完整名单可在本机查看。')
    if summary.get('pending_notifications'):
        lines.append('已入库待补通知：'+str(summary['pending_notifications'])+' 份')
    if summary.get('history_gaps'):
        lines.append('有停机时段超出自动补查范围，请查看飞书记录补传尚未登记的文件。')
    relations=summary.get('relations',{})
    if relations.get('waiting'):
        lines.append('关联待选择：'+str(relations['waiting'])+' 项；这些资料均已完成入库。')
    if relations.get('blocked') or relations.get('write_pending'):
        lines.append('有资料关联需要核查或恢复；其他资料可继续归档。')
    if value.get('network_pause'):
        lines.append('网络入口已暂停；已登记材料和确认记录保留。')
    if value.get('collection_wait'):
        lines.append('收件正在等待：'+str(value['collection_wait']['reason']))
    lines.append('正文已使用：'+str(summary.get('model_turns',0))+' 次（累计）')
    return '\n'.join(lines)
