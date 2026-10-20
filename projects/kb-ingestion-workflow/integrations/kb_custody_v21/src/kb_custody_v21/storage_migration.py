"""Stopped-service content copy with an immutable disk-backed manifest.

This module never creates a volume, changes the live binding, deletes originals,
or rewrites old receipts. Copy approval names one exact reviewed plan digest.
"""
from contextlib import contextmanager, ExitStack
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import unicodedata
from uuid import uuid4

from .contracts import ContractError
from .locking import open_lock
from .runtime import no_symlinks
from .safe_publish import directory, open_root, parts, rename_exclusive, root_identity
from .storage_layout import CONTENT_DIRS, MARKER, ContentStore, content_relative

BLOCK = 1024 * 1024
REFERENCE = 'P43-migration-v1'


def digest_file(path):
    with no_symlinks(path).open('rb') as stream:
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(BLOCK), b''): digest.update(chunk)
    return digest.hexdigest()


def stamp(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


@contextmanager
def stopped_writers(runtime):
    """Nonblocking fences shared with the actual service/converter/publishers."""
    paths = ('conversion-work/daily-service/service.lock','conversion-work/pilot-controller.lock',
             'conversion-work/execution.lock','conversion-work/admission.lock','reading-library.lock')
    with ExitStack() as stack:
        for name in paths:
            target = runtime.checked(runtime.root/name)
            if not target.parent.is_dir():continue
            fd = open_lock(target);stack.callback(os.close,fd)
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ContractError('请先正常停止归档服务及转换、资料写入任务，再生成或执行迁移计划。') from error
        yield


@contextmanager
def regular_at(root_fd, relative):
    names=parts(relative)
    parent=directory(root_fd,'/'.join(names[:-1])) if len(names)>1 else os.dup(root_fd)
    fd=None
    try:
        fd=os.open(names[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_dev!=os.fstat(root_fd).st_dev:
            raise ContractError('migration accepts ordinary files on the selected filesystem only')
        yield fd,info
    finally:
        if fd is not None:os.close(fd)
        os.close(parent)


def evidence_at(root_fd, relative):
    with regular_at(root_fd,relative) as (fd,before):
        digest=hashlib.sha256();size=0
        while True:
            chunk=os.read(fd,BLOCK)
            if not chunk:break
            digest.update(chunk);size+=len(chunk)
        if stamp(before)!=stamp(os.fstat(fd)) or size!=before.st_size:
            raise ContractError('source changed while calculating migration evidence')
        return size,digest.hexdigest(),stamp(before)


def inventory(root_fd):
    """Yield one item at a time. No knowledge-base body is accumulated in RAM."""
    device=os.fstat(root_fd).st_dev
    def visit(fd,prefix):
        before=os.fstat(fd)
        if before.st_dev!=device:raise ContractError('nested filesystem in managed content')
        yield prefix,'directory',0,'',None
        for name in sorted(os.listdir(fd)):
            relative=prefix+'/'+name;parts(relative)
            info=os.stat(name,dir_fd=fd,follow_symlinks=False)
            if info.st_dev!=device:raise ContractError('nested filesystem in managed content')
            if stat.S_ISDIR(info.st_mode):
                child=directory(fd,name)
                try:yield from visit(child,relative)
                finally:os.close(child)
            elif stat.S_ISREG(info.st_mode):
                size,digest,binding=evidence_at(root_fd,relative)
                yield relative,'file',size,digest,binding
            else:raise ContractError('links and special files cannot enter a migration plan')
        after=os.fstat(fd)
        if (before.st_ino,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_ino,after.st_mtime_ns,after.st_ctime_ns):
            raise ContractError('source directory changed during inventory')
    for prefix in CONTENT_DIRS:
        try:fd=directory(root_fd,prefix)
        except FileNotFoundError:
            # Required empty content directories are created at the destination.
            yield prefix,'directory',0,'',None
            continue
        try:yield from visit(fd,prefix)
        finally:os.close(fd)


def state_evidence(runtime):
    result={}
    for name in ('cloud-assets/registry.sqlite3','conversion-work/conversion.sqlite3','conversion-work/daily-service/queue.sqlite3'):
        for suffix in ('','-wal'):
            path=no_symlinks(runtime.root/(name+suffix))
            if path.exists():result[name+suffix]=digest_file(path)
    return result


def create_plan(runtime,target,*,probe=None):
    if runtime.storage is not None:
        raise ContractError('this migration prepares the existing local store for its first external copy')
    store=ContentStore(target,**({'probe':probe} if probe else {}))
    destination=store.check(fresh=True)
    if store.root.is_relative_to(runtime.root) or runtime.root.is_relative_to(store.root):
        raise ContractError('source and destination must not overlap')
    with stopped_writers(runtime):
        base=runtime.ensure_directory('storage-migrations')
        root=base/uuid4().hex;root.mkdir(mode=0o700)
        plan=root/'plan.sqlite3'
        with plan.open('xb'):pass
        plan.chmod(0o600)
        identity=root_identity(runtime.root);source=open_root(runtime.root,identity)
        before=state_evidence(runtime);count=total=0
        db=sqlite3.connect(plan)
        try:
            db.executescript('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL); CREATE TABLE items(path TEXT PRIMARY KEY,path_key TEXT UNIQUE NOT NULL,kind TEXT NOT NULL,bytes INTEGER NOT NULL,sha256 TEXT NOT NULL,source_stamp TEXT);')
            with db:
                for relative,kind,size,digest,binding in inventory(source):
                    key=unicodedata.normalize('NFC',relative).casefold()
                    db.execute('INSERT INTO items VALUES (?,?,?,?,?,?)',(relative,key,kind,size,digest,json.dumps(binding)))
                    count+=kind=='file';total+=size
                value=dict(reference=REFERENCE,source=identity,target=target,target_identity=destination['root'],
                    state_evidence=before,files=count,total_bytes=total,old_copy_retained=True)
                db.execute('INSERT INTO meta VALUES (?,?)',('plan',json.dumps(value,ensure_ascii=False,sort_keys=True)))
            if state_evidence(runtime)!=before or root_identity(runtime.root)!=identity:
                raise ContractError('operational state changed during migration inventory')
            store.check(total,fresh=True)
        finally:
            db.close();os.close(source)
        return dict(plan=str(plan),plan_sha256=digest_file(plan),**value,
                    required_free_bytes=total+target['minimum_free_bytes'],free_bytes=store.volume(fresh=True)['free_bytes'])


@contextmanager
def checked_plan(runtime,path,expected_sha,*,probe=None):
    path=no_symlinks(Path(path).absolute())
    if not path.is_relative_to(runtime.root/'storage-migrations') or path.name!='plan.sqlite3':
        raise ContractError('private local migration plan required')
    if digest_file(path)!=expected_sha:raise ContractError('reviewed migration plan digest changed')
    db=sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1',uri=True);db.row_factory=sqlite3.Row
    try:
        meta=json.loads(db.execute("SELECT value FROM meta WHERE key='plan'").fetchone()[0])
        if meta['reference']!=REFERENCE or meta['source']!=root_identity(runtime.root) or runtime.storage is not None:
            raise ContractError('migration source no longer matches the selected local runtime')
        if state_evidence(runtime)!=meta['state_evidence']:
            raise ContractError('queue or custody state changed; create a fresh migration preview')
        store=ContentStore(meta['target'],**({'probe':probe} if probe else {}))
        if store.check(fresh=True)['root']!=meta['target_identity']:
            raise ContractError('migration destination identity changed')
        yield db,meta,store
        if digest_file(path)!=expected_sha:raise ContractError('migration plan changed during use')
    finally:db.close()


def verify_source(db,source):
    count=0
    for relative,kind,size,digest,binding in inventory(source):
        row=db.execute('SELECT * FROM items WHERE path=?',(relative,)).fetchone()
        if row is None or (row['kind'],row['bytes'],row['sha256'],json.loads(row['source_stamp']))!=(kind,size,digest,binding):
            raise ContractError('source content changed; migration preview must be regenerated')
        count+=1
    if count!=db.execute('SELECT COUNT(*) FROM items').fetchone()[0]:
        raise ContractError('source content set changed since preview')


def verify_target(db,target):
    count=0
    def visit(fd,prefix=''):
        nonlocal count
        for name in os.listdir(fd):
            relative=prefix+name
            if relative==MARKER:continue
            info=os.stat(name,dir_fd=fd,follow_symlinks=False)
            if info.st_dev!=os.fstat(target).st_dev:raise ContractError('target contains a nested filesystem')
            row=db.execute('SELECT * FROM items WHERE path=?',(relative,)).fetchone()
            if stat.S_ISDIR(info.st_mode):
                if row is None and relative not in {'conversion-work'}:
                    raise ContractError('unexpected directory in target; preserve it for review')
                if row is not None and row['kind']!='directory':raise ContractError('target kind changed')
                child=directory(fd,name)
                try:visit(child,relative+'/')
                finally:os.close(child)
            elif stat.S_ISREG(info.st_mode):
                if row is None or row['kind']!='file':raise ContractError('unexpected file in target; preserve it for review')
                size,digest,_=evidence_at(target,relative)
                if (size,digest)!=(row['bytes'],row['sha256']):raise ContractError('copied file failed digest validation')
                count+=1
            else:raise ContractError('target contains a link or special file')
    visit(target)
    if count!=db.execute("SELECT COUNT(*) FROM items WHERE kind='file'").fetchone()[0]:
        raise ContractError('target content is incomplete')
    for row in db.execute("SELECT path FROM items WHERE kind='directory'"):
        fd=directory(target,row['path']);os.close(fd)


def copy_plan(runtime,path,approved_plan_sha,*,probe=None,hook=lambda point,path:None):
    with stopped_writers(runtime),checked_plan(runtime,path,approved_plan_sha,probe=probe) as (db,meta,store):
        source=open_root(runtime.root,meta['source']);target=open_root(store.root,meta['target_identity'])
        copied=reused=0
        try:
            verify_source(db,source)
            for row in db.execute('SELECT * FROM items ORDER BY path'):
                relative=row['path'];parts(relative)
                if not content_relative(relative):raise ContractError('unmanaged path in migration manifest')
                store.check()
                if row['kind']=='directory':
                    fd=directory(target,relative,create=True);os.close(fd);continue
                if row['kind']!='file':raise ContractError('unknown migration item kind')
                try:existing=evidence_at(target,relative)
                except FileNotFoundError:existing=None
                if existing is not None:
                    if existing[:2]!=(row['bytes'],row['sha256']):
                        raise ContractError('destination file differs; never overwrite it')
                    reused+=1;continue
                names=parts(relative);parent=directory(target,'/'.join(names[:-1]),create=True)
                temporary='.p43-'+approved_plan_sha[:16]+'-'+hashlib.sha256(relative.encode()).hexdigest()[:16]+'.partial'
                try:
                    try:old=os.stat(temporary,dir_fd=parent,follow_symlinks=False)
                    except FileNotFoundError:old=None
                    if old is not None:
                        if not stat.S_ISREG(old.st_mode) or old.st_nlink!=1:
                            raise ContractError('unsafe retained migration partial')
                        os.unlink(temporary,dir_fd=parent)  # Only this exact plan's incomplete output.
                    store.check(row['bytes'],fresh=True)
                    destination=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=parent)
                    with os.fdopen(destination,'wb') as output,regular_at(source,relative) as (input_fd,before):
                        if stamp(before)!=json.loads(row['source_stamp']):raise ContractError('source file changed before copy')
                        digest=hashlib.sha256();size=0
                        while True:
                            chunk=os.read(input_fd,BLOCK)
                            if not chunk:break
                            store.check();output.write(chunk);digest.update(chunk);size+=len(chunk)
                            hook('after_chunk',relative)
                        output.flush();os.fsync(output.fileno())
                        if stamp(os.fstat(input_fd))!=stamp(before) or (size,digest.hexdigest())!=(row['bytes'],row['sha256']):
                            raise ContractError('source changed during copy')
                    store.check();rename_exclusive(parent,temporary,parent,names[-1])
                    copied+=1;hook('after_file',relative)
                finally:os.close(parent)
            verify_source(db,source);verify_target(db,target)
            if state_evidence(runtime)!=meta['state_evidence']:raise ContractError('operational state changed during copy')
            store.check(fresh=True)
            receipt=dict(reference=REFERENCE,plan_sha256=approved_plan_sha,copied=copied,reused=reused,
                verified_files=meta['files'],verified_bytes=meta['total_bytes'],source_retained=True,
                target=meta['target_identity'],live_binding_changed=False)
            from .daily_transport import durable_file
            # The validation receipt is stable across identical resumed copy calls.
            stable={k:v for k,v in receipt.items() if k not in ('copied','reused')}
            durable_file(Path(path).with_name('copy-verified.local.json'),stable)
            return receipt
        finally:os.close(source);os.close(target)


def switch_preview(runtime,path,plan_sha,*,probe=None):
    """A reviewable config proposal, never an automatic runtime/receipt rewrite."""
    with stopped_writers(runtime),checked_plan(runtime,path,plan_sha,probe=probe) as (db,meta,store):
        receipt=Path(path).with_name('copy-verified.local.json')
        if not receipt.is_file() or json.loads(receipt.read_text()).get('plan_sha256')!=plan_sha:
            raise ContractError('copy and digest verification required before a switch proposal')
        source=open_root(runtime.root,meta['source']);target=open_root(store.root,meta['target_identity'])
        try:verify_source(db,source);verify_target(db,target)
        finally:os.close(source);os.close(target)
        pending=[];queue=runtime.root/'conversion-work/daily-service/queue.sqlite3'
        if queue.exists():
            with sqlite3.connect(queue.as_uri()+'?mode=ro',uri=True) as state:
                pending=[dict(message_id=r[0],phase=r[1]) for r in state.execute("SELECT message_id,phase FROM daily_documents WHERE phase NOT IN ('notified','published','rejected','duplicate')")]
        return dict(reference='P43-switch-proposal-v1',plan_sha256=plan_sha,
            runtime_config=dict(runtime.config,storage=meta['target']),reading_root=str(store.root/'reading-library'),
            old_content_root=str(runtime.root),new_content_root=str(store.root),pending_documents=pending,
            applied=False,requires=['阶段5内容备份及空目录恢复验收','实际切换授权','待处理任务和绝对路径回执逐项核对',
                '更新启动器、日常规则与源码绑定','本地原件、正文、关联与链接核验后再启服务'],
            rollback='停止服务并核验切换后新增内容，再恢复此前绑定；保留原副本，不自动删除。')
