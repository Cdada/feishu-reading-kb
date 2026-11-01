"""Four natural archive samples, with no extra network/model/resource probes.

The existing queue owns selection and transitions. Nested checks accumulate in
memory during a step, then flush once; they are inclusive detail, not additional
wall time. A killed step has unknown elapsed time, never an invented duration.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
import json
import math
from pathlib import Path
import sqlite3
import time
import uuid

from .contracts import ContractError
from .registry import encoded
from .runtime import no_symlinks

PROFILE = dict(reference='archive-timing-v1', sample_limit=4)
MAX_SEGMENTS = 2048
MAX_MARKS = 512
TERMINAL = {'notified', 'rejected', 'duplicate'}
WAIT_PHASES = {'awaiting_confirmation', 'awaiting_images', 'reading_wait'}
CURRENT = ContextVar('archive_timing', default=None)
SCHEMA = """
CREATE TABLE IF NOT EXISTS archive_timing_samples(
 message_id TEXT PRIMARY KEY, ordinal INTEGER NOT NULL UNIQUE,
 selected_at REAL NOT NULL, source_time REAL, media_kind TEXT NOT NULL,
 size_bytes INTEGER, total_pages INTEGER, first_feedback_at REAL,
 phase TEXT NOT NULL, updated_at REAL NOT NULL, truncated INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS archive_timing_segments(
 id TEXT PRIMARY KEY, message_id TEXT NOT NULL, session TEXT NOT NULL,
 stage TEXT NOT NULL, started_at REAL NOT NULL, ended_at REAL, elapsed_seconds REAL,
 outcome TEXT NOT NULL, version TEXT NOT NULL, metrics TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 1);
CREATE INDEX IF NOT EXISTS archive_timing_segments_task ON archive_timing_segments(message_id,started_at);
CREATE TABLE IF NOT EXISTS archive_timing_marks(
 id INTEGER PRIMARY KEY, message_id TEXT NOT NULL, at REAL NOT NULL,
 kind TEXT NOT NULL, details TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS archive_timing_marks_task ON archive_timing_marks(message_id,at);
"""


@contextmanager
def measure(name, kind='step'):
    frame = CURRENT.get()
    if frame is None:
        yield
        return
    start = frame['owner'].clock()
    outcome = 'complete'
    try:
        yield
    except BaseException as error:
        # Record only the exception class, never the message or source content.
        outcome = type(error).__name__
        raise
    finally:
        elapsed = frame['owner'].clock()-start
        if not math.isfinite(elapsed) or elapsed < 0:
            raise ContractError('invalid monotonic archive timing')
        key = kind+':'+name
        value = frame['metrics'].setdefault(key, dict(count=0,seconds=0.0,max_seconds=0.0,outcomes={}))
        value['count'] += 1
        value['seconds'] += elapsed
        value['max_seconds'] = max(value['max_seconds'],elapsed)
        value['outcomes'][outcome] = value['outcomes'].get(outcome,0)+1


def timed(name, kind='step'):
    def decorate(function):
        @wraps(function)
        def run(*args, **kwargs):
            with measure(name, kind):
                return function(*args, **kwargs)
        return run
    return decorate


def document_step(function):
    @wraps(function)
    def run(backend, document, *args, **kwargs):
        main=getattr(backend,'main_cards',None)
        active=main is not None and main.handles(document['message_id'])
        if active:
            main.activity(document,'正在核对运行条件与资料完整性。')
        try:
            timing = backend.store.timing
            if timing is None:
                return function(backend,document,*args,**kwargs)
            with timing.step(document['message_id'], document['phase']):
                return function(backend,document,*args,**kwargs)
        finally:
            if active:
                backend.store.put('archive-main-active:'+document['message_id'],None)
    return run


def stage(name):
    frame = CURRENT.get()
    if frame is not None:
        frame['stage'] = name


def observed(name, details):
    frame = CURRENT.get()
    if frame is not None:
        frame['observations'] += 1
        frame['owner'].mark(frame['mid'],name,dict(details,segment_id=frame['id'],
            elapsed_seconds=frame['owner'].clock()-frame['start']))


class ArchiveTiming:
    def __init__(self, store, version, *, monotonic=time.monotonic):
        self.store, self.version, self.clock = store, version, monotonic
        self.session = uuid.uuid4().hex
        with store.connection() as db:
            db.executescript(SCHEMA)
        with store.transaction() as db:
            # Reopening after deployment/restart never replenishes sample slots.
            if store._get(db, 'archive-timing-v1') is None:
                store._put(db,'archive-timing-v1',dict(PROFILE,enabled_at=store.now()))

    def recover(self):
        with self.store.transaction() as db:
            db.execute("UPDATE archive_timing_segments SET outcome='interrupted',ended_at=?,elapsed_seconds=NULL "
                       "WHERE outcome='started' AND session!=?",(self.store.now(),self.session))

    def register(self, db, mid, item, filename):
        if db.execute('SELECT 1 FROM archive_timing_samples WHERE message_id=?',(mid,)).fetchone():
            return
        count=db.execute('SELECT count(*) FROM archive_timing_samples').fetchone()[0]
        if count >= PROFILE['sample_limit']:
            return
        raw=item.get('create_time')
        source_time=float(raw)/1000 if isinstance(raw,str) and raw.isdigit() and len(raw)<=16 else None
        if source_time is not None and not 0 <= source_time <= self.store.now():
            source_time=None
        # Keep type/size only; no filename, file key, paths or body copies.
        kind=Path(filename).suffix.lower()
        if kind not in {'.md','.pdf','.epub','.txt','.docx','.xlsx','.doc','.xls'}:
            kind='other'
        at=self.store.now()
        db.execute('INSERT INTO archive_timing_samples(message_id,ordinal,selected_at,source_time,media_kind,phase,updated_at) '
                   "VALUES (?,?,?,?,?,'registered',?)",(mid,count+1,at,source_time,kind,at))
        self._mark(db,mid,'phase',dict(after='registered'))

    def _mark(self, db, mid, kind, details):
        if not db.execute('SELECT 1 FROM archive_timing_samples WHERE message_id=?',(mid,)).fetchone():
            return
        count=db.execute('SELECT count(*) FROM archive_timing_marks WHERE message_id=?',(mid,)).fetchone()[0]
        if count>=MAX_MARKS:
            db.execute('UPDATE archive_timing_samples SET truncated=1 WHERE message_id=?',(mid,))
            return
        db.execute('INSERT INTO archive_timing_marks(message_id,at,kind,details) VALUES (?,?,?,?)',
                   (mid,self.store.now(),kind,encoded(details)))

    def transition(self, db, mid, before, after):
        db.execute('UPDATE archive_timing_samples SET phase=?,updated_at=? WHERE message_id=?',(after,self.store.now(),mid))
        self._mark(db,mid,'phase',dict(before=before,after=after))

    def defer(self, db, mid, value):
        if not value:
            return
        reason=value.get('reason','')
        category=('resources' if reason in ('paused_resources','paused_capacity','previous_mineru_process_still_exiting')
                  else 'service_stop' if reason=='stopping' else 'storage' if reason=='storage_unavailable'
                  else 'model_or_subscription' if reason.startswith(('model_','reading_','subscription_'))
                  else 'other_wait')
        previous=db.execute("SELECT kind,details FROM archive_timing_marks WHERE message_id=? AND kind IN ('deferred','resumed','phase') ORDER BY id DESC LIMIT 1",(mid,)).fetchone()
        # Repeated waiting polls do not create an ever-growing diagnostics log.
        details=dict(category=category)
        if previous is None or previous['kind']!='deferred' or json.loads(previous['details'])!=details:
            self._mark(db,mid,'deferred',details)

    def service(self, db, value):
        state=value.get('state')
        if state not in ('running','stopping','stopped','interrupted'):
            return
        previous=self.store._get(db,'archive-timing-service')
        if state==previous:
            return
        self.store._put(db,'archive-timing-service',state)
        for row in db.execute("SELECT message_id FROM archive_timing_samples WHERE phase NOT IN ('notified','rejected','duplicate')"):
            self._mark(db,row[0],'service',dict(state=state))

    def mark(self, mid, kind, details):
        with self.store.transaction() as db:
            self._mark(db,mid,kind,details)

    def feedback(self, mid):
        with self.store.transaction() as db:
            db.execute('UPDATE archive_timing_samples SET first_feedback_at=COALESCE(first_feedback_at,?) WHERE message_id=?',
                       (self.store.now(),mid))

    def characteristics(self, mid, *, size=None, pages=None):
        if size is not None and (type(size) is not int or size<0):
            raise ContractError('invalid timing size')
        if pages is not None and (type(pages) is not int or not 1<=pages<=100):
            raise ContractError('invalid timing page count')
        with self.store.transaction() as db:
            db.execute('UPDATE archive_timing_samples SET size_bytes=COALESCE(?,size_bytes),total_pages=COALESCE(?,total_pages) WHERE message_id=?',
                       (size,pages,mid))

    @contextmanager
    def step(self, mid, phase):
        with self.store.transaction() as db:
            row=db.execute('SELECT phase FROM archive_timing_samples WHERE message_id=?',(mid,)).fetchone()
            count=db.execute('SELECT count(*) FROM archive_timing_segments WHERE message_id=?',(mid,)).fetchone()[0] if row else 0
            selected=row is not None and row[0] not in TERMINAL and count<MAX_SEGMENTS
            if row and count>=MAX_SEGMENTS:
                db.execute('UPDATE archive_timing_samples SET truncated=1 WHERE message_id=?',(mid,))
            if selected:
                sid=uuid.uuid4().hex
                db.execute('INSERT INTO archive_timing_segments VALUES (?,?,?,?,?,NULL,NULL,?,?,?,1)',
                    (sid,mid,self.session,phase,self.store.now(),'started',encoded(self.version),'{}'))
        if not selected:
            yield
            return
        started_at=self.store.now()
        frame=dict(owner=self,mid=mid,id=sid,stage=phase,metrics={},observations=0,start=self.clock())
        token=CURRENT.set(frame)
        start=frame['start'];outcome='complete'
        try:
            yield
        except BaseException as error:
            outcome=type(error).__name__
            raise
        finally:
            elapsed=self.clock()-start
            CURRENT.reset(token)
            if not math.isfinite(elapsed) or elapsed<0:
                raise ContractError('invalid monotonic archive step timing')
            with self.store.transaction() as db:
                db.execute('UPDATE archive_timing_segments SET stage=?,ended_at=?,elapsed_seconds=?,outcome=?,metrics=? WHERE id=?',
                    (frame['stage'],self.store.now(),elapsed,outcome,encoded(frame['metrics']),sid))
                previous=db.execute("SELECT kind FROM archive_timing_marks WHERE message_id=? AND kind IN ('deferred','resumed','phase') ORDER BY id DESC LIMIT 1",(mid,)).fetchone()
                if outcome=='complete' and previous and previous[0]=='deferred':
                    self._mark(db,mid,'resumed',dict(successful_attempt_started_at=started_at))
                current=db.execute('SELECT phase FROM archive_timing_samples WHERE message_id=?',(mid,)).fetchone()[0]
                # Long user waits must not fill the detail budget before parsing
                # or publication. Combine only unchanged successful wait polls;
                # elapsed remains a sum of measured execution, not a time span.
                if not frame['observations'] and current==phase and (phase in WAIT_PHASES and outcome=='complete' or outcome=='Deferred'):
                    old=db.execute("SELECT * FROM archive_timing_segments WHERE message_id=? AND stage=? AND outcome=? AND version=? AND id!=? ORDER BY started_at DESC LIMIT 1",
                        (mid,frame['stage'],outcome,encoded(self.version),sid)).fetchone()
                    if old:
                        metrics=json.loads(old['metrics'])
                        for key,v in frame['metrics'].items():
                            m=metrics.setdefault(key,dict(count=0,seconds=0.0,max_seconds=0.0,outcomes={}))
                            m['count']+=v['count'];m['seconds']+=v['seconds'];m['max_seconds']=max(m['max_seconds'],v['max_seconds'])
                            for k,n in v['outcomes'].items():m['outcomes'][k]=m['outcomes'].get(k,0)+n
                        db.execute('UPDATE archive_timing_segments SET ended_at=?,elapsed_seconds=elapsed_seconds+?,metrics=?,attempts=attempts+1 WHERE id=?',
                            (self.store.now(),elapsed,encoded(metrics),old['id']))
                        db.execute('DELETE FROM archive_timing_segments WHERE id=?',(sid,))


def report(path):
    """Read-only snapshot; no authority, credentials, service, or model creation."""
    path=no_symlinks(Path(path).absolute())
    if not path.is_file():
        return dict(state='not_enabled',samples=[])
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        db.execute('BEGIN')
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='archive_timing_samples'").fetchone():
            return dict(state='not_enabled',samples=[])
        samples=[]
        for row in db.execute('SELECT * FROM archive_timing_samples ORDER BY ordinal'):
            item=dict(row);mid=item.pop('message_id')
            item['task_id']='sample-'+str(item['ordinal'])
            segments=[];totals={};phases={}
            for saved in db.execute('SELECT * FROM archive_timing_segments WHERE message_id=? ORDER BY started_at,id',(mid,)):
                segment=dict(saved)
                for k in ('message_id','session'): segment.pop(k)
                segment['segment_id']=segment.pop('id')
                segment['version']=json.loads(segment['version']);segment['metrics']=json.loads(segment['metrics'])
                phase=phases.setdefault(segment['stage'],dict(attempts=0,measured_seconds=0.0,incomplete=0))
                phase['attempts']+=segment['attempts']
                if segment['elapsed_seconds'] is None:
                    phase['incomplete']+=1
                else:
                    phase['measured_seconds']+=segment['elapsed_seconds']
                for name,value in segment['metrics'].items():
                    total=totals.setdefault(name,dict(count=0,seconds=0.0,max_seconds=0.0))
                    total['count']+=value['count'];total['seconds']+=value['seconds']
                    total['max_seconds']=max(total['max_seconds'],value['max_seconds'])
                segments.append(segment)
            item['segments']=segments;item['phase_totals']=phases;item['check_and_step_totals']=totals
            item['timeline']=[dict(at=m['at'],kind=m['kind'],details=json.loads(m['details'])) for m in db.execute(
                'SELECT at,kind,details FROM archive_timing_marks WHERE message_id=? ORDER BY at,id',(mid,))]
            item['first_feedback_from_registration_seconds']=(item['first_feedback_at']-item['selected_at'] if item['first_feedback_at'] is not None else None)
            item['first_feedback_from_source_seconds']=(item['first_feedback_at']-item['source_time'] if item['first_feedback_at'] is not None and item['source_time'] is not None else None)
            command=next((m['details']['observed_at'] for m in item['timeline'] if m['kind']=='archive_command'),None)
            item['first_feedback_from_command_observation_seconds']=(item['first_feedback_at']-command if item['first_feedback_at'] is not None and command is not None else None)
            item['completed']=item['phase'] in TERMINAL
            item['total_elapsed_seconds']=(item['updated_at']-item['selected_at'] if item['completed'] else None)
            transitions=[m for m in item['timeline'] if m['kind']=='phase']
            waits={'awaiting_confirmation':'user_confirmation','awaiting_images':'image_choice',
                   'reading_wait':'source_clarification','held':'manual_handling','needs_upload':'replacement_file'}
            item['waiting_intervals']=[]
            for index,mark in enumerate(transitions):
                phase=mark['details']['after']
                if phase in waits:
                    end=transitions[index+1]['at'] if index+1<len(transitions) else None
                    item['waiting_intervals'].append(dict(kind=waits[phase],started_at=mark['at'],ended_at=end,
                        wall_seconds=end-mark['at'] if end is not None else None))
            for index,mark in enumerate(item['timeline']):
                if mark['kind']=='deferred':
                    later=next((m for m in item['timeline'][index+1:] if m['kind'] in ('resumed','phase','deferred')),None)
                    end=(later['details'].get('successful_attempt_started_at',later['at']) if later else None)
                    item['waiting_intervals'].append(dict(kind=mark['details']['category'],started_at=mark['at'],ended_at=end,
                        wall_seconds=end-mark['at'] if end is not None else None))
                elif mark['kind']=='service' and mark['details']['state'] in ('stopped','interrupted'):
                    later=next((m for m in item['timeline'][index+1:] if m['kind']=='service' and m['details']['state']=='running'),None)
                    end=later['at'] if later else None
                    item['waiting_intervals'].append(dict(kind='service_'+mark['details']['state'],started_at=mark['at'],ended_at=end,
                        wall_seconds=end-mark['at'] if end is not None else None))
            item['index_status']='not_observed_by_archive; refreshed_on_next_question'
            item['dispatch_intervals']=[]
            for index,mark in enumerate(transitions):
                phase=mark['details']['after']
                if phase in TERMINAL or phase in waits:
                    continue
                until=transitions[index+1]['at'] if index+1<len(transitions) else float('inf')
                attempt=next((s for s in segments if mark['at']<=s['started_at']<=until),None)
                if attempt:
                    item['dispatch_intervals'].append(dict(phase=phase,started_at=mark['at'],ended_at=attempt['started_at'],
                        wall_seconds=attempt['started_at']-mark['at']))
            samples.append(item)
    return dict(state='sample_limit_reached' if len(samples)==4 else 'collecting',sample_limit=4,samples=samples,
        notes=['仅自然新登记任务；暂停和失败不补样，重启不重置名额。',
               'elapsed_seconds 是同进程单调时钟的实测值；中断、未完成字段为 null。',
               '各检查与子步骤可能嵌套，不能加到阶段耗时或直接求和为总处理时间。',
               '未记录的时段不可默认为处理或资源等待；用户等待期间的轮询也可能占用时间。',
               'dispatch_intervals 为阶段就绪至首次尝试的调度间隔，可能含排队、停机或共享检查，不能当作纯排队时间。',
               '等待区间可能重叠，不得相加；资源恢复以成功重试的开始为观察终点，并非精确资源恢复时刻。',
               '服务正常停止与恢复使用实际心跳状态；进程突然消失期间无法分解为停机或排队。',
               '等待轮询合并为紧凑记录；attempts 是实测调用次数，elapsed_seconds 不包含两次轮询间隔。',
               '归档不自动重建问答索引；索引在下次问题检索时刷新，本记录不宣称已经索引。',
               '首次反馈只表示服务收到平台回执，不等于手机已显示；来源时间可能不可用。',
               'MinerU 首批解析可能包含延迟加载，不能把启动健康检查等同于模型已全部加载。',
               '仅记录任务上下文内的检查；跨任务后台收件和共享检查不强行分摊。',
               '隔离 CLI／模型子进程的内部检查未逐项测量，包含在相应调用往返时间内。',
               'truncated=true 表示触及详细记录上限，后续细项不完整；任务状态继续保留。'])


def markdown(value):
    lines=['# 归档阶段耗时记录','', '状态：'+value['state'],'']
    for item in value['samples']:
        lines += ['## '+item['task_id'],'',
            f"类型：{item['media_kind']}；页数：{item['total_pages']}；字节数：{item['size_bytes']}；当前阶段：{item['phase']}",'',
            '登记时间（UTC）：'+datetime.fromtimestamp(item['selected_at'],timezone.utc).isoformat(),
            '首次反馈（登记后秒数）：'+str(item['first_feedback_from_registration_seconds']),
            '@命令本机收件至核验反馈（秒）：'+str(item['first_feedback_from_command_observation_seconds']),
            '总历时（含等待；未结束为 None）：'+str(item['total_elapsed_seconds']),
            '详细记录达到上限：'+str(bool(item['truncated'])),'',
            '| 阶段 | 执行次数 | 已测处理秒数 | 不完整次数 |','| --- | --- | --- | --- |']
        for name,v in item['phase_totals'].items():
            lines.append(f"| {name} | {v['attempts']} | {v['measured_seconds']:.3f} | {v['incomplete']} |")
        lines += ['', '| 等待／暂停类型 | 历时秒数（含轮询，可能重叠） |','| --- | --- |']
        for wait in item['waiting_intervals']:
            lines.append(f"| {wait['kind']} | {wait['wall_seconds']} |")
        lines += ['', '| 阶段就绪至首次尝试 | 间隔秒数（非纯排队） |','| --- | --- |']
        for gap in item['dispatch_intervals']:
            lines.append(f"| {gap['phase']} | {gap['wall_seconds']:.3f} |")
        lines += ['', '| 检查／子步骤 | 次数 | 累计秒数 | 最长秒数 |','| --- | --- | --- | --- |']
        for name,v in sorted(item['check_and_step_totals'].items(),key=lambda kv:kv[1]['seconds'],reverse=True):
            lines.append(f"| {name} | {v['count']} | {v['seconds']:.3f} | {v['max_seconds']:.3f} |")
        lines.append('')
    lines += value.get('notes',[])
    return '\n'.join(lines)+'\n'
