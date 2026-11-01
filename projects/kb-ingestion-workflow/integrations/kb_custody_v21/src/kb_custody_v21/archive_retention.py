"""On-demand, read-only retention inventory. No deletion implementation.

Only known service-owned areas are inspected. A seven-day age is a suggestion,
never proof that a dependency can be removed. No file bodies are exported.
"""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

from .contracts import ContractError
from .daily_launcher import DailyLauncher
from .runtime import no_symlinks

GRACE_SECONDS=7*86400
MAX_ENTRIES=100000
PROTECTED={
    'requests':'请求回执、确认与恢复仍依赖原文件；不自动删减。',
    'model-grants':'模型授权、输入及结果用于去重和追溯；保留。',
    'reading-jobs':'整理输入、独立复核和发布清单仍有哈希引用；保留。',
    'pdf-chunks':'成功解析成果及失败尝试仍由不可变清单引用；保留。'}

def usage(path, budget):
    total=files=0;latest=0;issues=[]
    stack=[path]
    while stack:
        current=stack.pop()
        budget[0]-=1
        if budget[0]<0:
            issues.append('entry_limit');break
        try:
            value=current.lstat()
            if stat.S_ISLNK(value.st_mode):
                issues.append('symlink_skipped');continue
            if stat.S_ISDIR(value.st_mode):
                with os.scandir(current) as entries:
                    for entry in entries:
                        if len(stack)>MAX_ENTRIES:
                            issues.append('entry_limit');break
                        stack.append(Path(entry.path))
            elif stat.S_ISREG(value.st_mode):
                files+=1;total+=value.st_size;latest=max(latest,value.st_mtime)
            else:issues.append('special_file_skipped')
        except OSError:
            issues.append('unreadable_or_changed');continue
    return dict(logical_bytes=total,file_count=files,latest_modified=latest,
                complete=not issues,issues=sorted(set(issues)))

def preview(daily_root, *, now=None):
    root=no_symlinks(Path(daily_root).absolute())
    if root.name!='daily-service' or root.parent.name!='conversion-work':
        raise ContractError('explicit existing project daily-service directory required')
    queue=no_symlinks(root/'queue.sqlite3')
    if not queue.is_file():raise ContractError('existing daily ledger required; nothing is initialized')
    at=time.time() if now is None else now
    with closing(sqlite3.connect(queue.as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        docs=[dict(row) for row in db.execute('SELECT message_id,entry_id,source_sha,phase,updated,data,user_action FROM daily_documents')]
        waits={row[0]:json.loads(row[1]) for row in db.execute("SELECT key,value FROM daily_meta WHERE key LIKE 'defer:%'")}
        uncertain_operations=db.execute("SELECT count(*) FROM daily_operations WHERE state IN ('started','unknown')").fetchone()[0]
        models_unknown=db.execute("SELECT count(*) FROM daily_models WHERE state IN ('reserved','started','unknown')").fetchone()[0]
    by_reading={json.loads(d['data']).get('reading_material_id'):d for d in docs}
    by_sha={}
    for d in docs:
        if d['source_sha']:by_sha.setdefault(d['source_sha'],[]).append(d)
    state=DailyLauncher(root).status()
    budget=[MAX_ENTRIES];rows=[]
    areas=[(name,root/name) for name in ('requests','model-grants','reading-jobs')]
    areas.append(('pdf-chunks',root.parent/'pdf-chunks'))
    for name,path in areas:
        no_symlinks(path)
        if not path.exists():continue
        # Group immutable shared evidence, but expose per-material source bundles.
        paths=sorted(path.iterdir()) if name in {'reading-jobs','pdf-chunks'} else [path]
        for item in paths:
            matches=([by_reading[item.name]] if name=='reading-jobs' and item.name in by_reading
                     else by_sha.get(item.name,[]) if name=='pdf-chunks' else [])
            measured=usage(item,budget)
            active=any(d['phase'] not in {'notified','rejected','duplicate'} or d['user_action']
                or waits.get('defer:'+d['message_id']) for d in matches)
            last=max([measured['latest_modified'],*[d['updated'] for d in matches]])
            rows.append(dict(area=name,relative_path=name+('/'+item.name if item!=path else ''),
                task_ids=[d['message_id'] for d in matches],task_states=[d['phase'] for d in matches],
                suggested_grace_elapsed=at-last>=GRACE_SECONDS,
                classification='retain',eligible_for_review=False,
                reason='未完成或待核查任务，保留接续依赖。' if active else PROTECTED[name],
                rollback_dependency=True,**measured))
    # Only launcher-owned atomic-write remnants are even considered for review.
    # All material evidence, caches, model downloads and arbitrary *.tmp are excluded.
    outstanding=any(d['phase'] not in {'notified','rejected','duplicate'} or d['user_action']
        or waits.get('defer:'+d['message_id']) for d in docs) or models_unknown>0 or uncertain_operations>0
    for item in sorted(root.iterdir()):
        match=re.fullmatch(r'(process|stop)\.local\.json\.[a-f0-9]{32}\.tmp',item.name)
        if not match:continue
        measured=usage(item,budget);target=root/(match[1]+'.local.json')
        safe_target=target.is_file() and not target.is_symlink() and target.stat().st_mtime>measured['latest_modified']
        aged=at-max([measured['latest_modified'],*[d['updated'] for d in docs]])>=GRACE_SECONDS
        eligible=bool(measured['complete'] and not item.is_symlink() and safe_target and aged
                      and not state['running'] and not outstanding)
        rows.append(dict(area='launcher_atomic_temp',relative_path=item.name,task_ids=[],task_states=[],
            suggested_grace_elapsed=aged,classification='review_candidate' if eligible else 'retain',
            eligible_for_review=eligible,rollback_dependency=not safe_target,
            reason='已被较新状态文件替代；仍需核对清单并批准删除。' if eligible else '尚不能排除任务、进程、时间或回退依赖。',
            **measured))
    return dict(reference='archive-retention-preview/v1',read_only=True,deleted_files=0,
        grace_days_suggestion=7,grace_policy_approved=False,service_running=state['running'],
        complete=all(r['complete'] for r in rows),rows=rows,
        candidates=sum(r['eligible_for_review'] for r in rows),
        measurement='logical_bytes_not_reclaimable_disk_space',
        excluded=['originals','published_library','shared_caches','models','historical_development_artifacts','unrecognized_files'],
        warning='没有遍历全部引用图；不可变成果及其回执均保留。候选仅供人工审核，此入口不提供删除选项。')

def main():
    parser=argparse.ArgumentParser(description='只读过程文件保留与清理候选清单')
    parser.add_argument('--daily-root',required=True)
    args=parser.parse_args()
    print(json.dumps(preview(args.daily_root),ensure_ascii=False,indent=2))

if __name__=='__main__':main()
