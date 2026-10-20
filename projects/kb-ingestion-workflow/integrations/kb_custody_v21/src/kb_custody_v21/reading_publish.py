"""Resumable, compare-before-write publication inside the isolated test root.

The private immutable plan is retained. A restart accepts only the old or exact
planned bytes, never overwrites an unexpected user edit, and writes completed
metadata last. An advisory lock serializes all reading-library writers.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat

from .contracts import ContractError
from .daily_transport import durable_file
from .locking import open_lock
from .reading_bundle import CARDS, TOPICS, BRIEFS, check_links
from .reading_contract import require, sha
from .registry import encoded
from .runtime import no_symlinks
from .safe_publish import directory, inspect_tree, manifest, open_root, parts, rename_exclusive, root_identity, write_tree

MAX_LIBRARY_BYTES = 128 * 1024**2
MAX_CONTEXT_BYTES = 512 * 1024


def read_at(root_fd, relative, *, limit=MAX_LIBRARY_BYTES):
    segments = parts(relative)
    try:
        parent = directory(root_fd, '/'.join(segments[:-1])) if len(segments)>1 else os.dup(root_fd)
    except FileNotFoundError:
        return None
    try:
        try:
            fd = os.open(segments[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_size<=limit, 'unexpected or oversized reading file')
            raw = stream.read(limit+1)
            require(len(raw)<=limit, 'reading file grew beyond bound')
            return raw
    finally:
        os.close(parent)


def snapshot_library(profile):
    """Read cards/topics/briefs only; inventory source paths without their bytes."""
    profile.check()
    if not profile.root.exists():
        return dict(existing_files={}, available_paths=[])
    identity = root_identity(profile.root)
    fd = open_root(profile.root, identity)
    files, available, count = {}, [], 0
    def visit(parent, prefix=''):
        nonlocal count
        for name in sorted(os.listdir(parent)):
            path = prefix+name
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            require(not stat.S_ISLNK(info.st_mode), 'test library contains a symlink')
            if stat.S_ISDIR(info.st_mode):
                child=directory(parent,name)
                try: visit(child,path+'/')
                finally: os.close(child)
            else:
                require(stat.S_ISREG(info.st_mode), 'special files are not reading content')
                available.append(path)
                require(len(available)<=20000, 'test library inventory exceeds bound')
                if path.split('/')[0] in (CARDS,TOPICS,BRIEFS) and path.endswith('.md'):
                    raw=read_at(fd,path,limit=MAX_CONTEXT_BYTES)
                    count+=len(raw)
                    require(count<=MAX_CONTEXT_BYTES, 'reading context needs an explicit narrowed review')
                    files[path]=raw
    try:
        visit(fd)
        require(root_identity(profile.root)==identity, 'test root changed during snapshot')
        profile.check()
        return dict(existing_files=files, available_paths=available)
    finally:
        os.close(fd)


def prepare_plan(profile, bundle, job_root, *, checked_access):
    checked_access(); profile.check()
    require(job_root.is_relative_to(profile.runtime.root) and not job_root.is_relative_to(profile.root), 'private runtime plan required')
    no_symlinks(job_root).mkdir(parents=True,exist_ok=True,mode=0o700)
    files=bundle['files']
    require(sum(map(len,files.values()))<=MAX_LIBRARY_BYTES, 'reading bundle exceeds bounded publication size')
    for path in files:
        profile.target(path)
    checked_access()
    profile.runtime.ensure_directory('reading-library')
    identity=root_identity(profile.root)
    payload=no_symlinks(job_root/'payload')
    payload.mkdir(mode=0o700,exist_ok=True)
    payload_id=root_identity(payload)
    fd=open_root(payload,payload_id)
    wanted=manifest(files)
    try:
        old=inspect_tree(fd,max_bytes=MAX_LIBRARY_BYTES)
        require(all(path in wanted and wanted[path]==value for path,value in old.items()), 'retained reading payload changed')
        write_tree(fd,{path:raw for path,raw in files.items() if path not in old})
        require(inspect_tree(fd,max_bytes=MAX_LIBRARY_BYTES,expected_files=wanted)==wanted, 'reading payload differs')
    finally:
        os.close(fd)
    plan=dict(reference='P41-reading-publication-v1',root=identity,payload=payload_id,
        files=wanted,expected=bundle['expected'],generated=bundle['generated'],available_paths=bundle['available_paths'],
        metadata_path=bundle['metadata_path'],group=bundle['group'],
        review_receipt=bundle['review_receipt'],input_sha256=bundle['input_sha256'],rules_sha256=profile.rules_sha256)
    checked_access(); profile.check()
    durable_file(job_root/'plan.local.json',plan)
    return sha(plan)


def publish_plan(profile, job_root, plan_sha, *, checked_access, after_write=None):
    checked_access(); profile.check()
    no_symlinks(job_root)
    lock=open_lock(profile.runtime.root/'reading-library.lock')
    try:
        fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
        raw=no_symlinks(job_root/'plan.local.json').read_bytes()
        require(len(raw)<=4*1024*1024, 'oversized reading plan')
        plan=json.loads(raw)
        require(sha(plan)==plan_sha and plan['reference']=='P41-reading-publication-v1'
            and plan['rules_sha256']==profile.rules_sha256, 'reading plan or rules changed')
        require(plan['root']['path']==str(profile.root) and plan['payload']['path']==str(job_root/'payload'), 'reading plan target changed')
        root_fd=open_root(profile.root,plan['root'])
        payload_fd=open_root(job_root/'payload',plan['payload'])
        try:
            expected_manifest=plan['files']
            require(inspect_tree(payload_fd,max_bytes=MAX_LIBRARY_BYTES,expected_files=expected_manifest)==expected_manifest, 'reading payload changed')
            for path in expected_manifest:
                profile.target(path)
                current=read_at(root_fd,path)
                current_sha=hashlib.sha256(current).hexdigest() if current is not None else None
                require(current_sha in (plan['expected'][path],expected_manifest[path]['sha256']), 'reading target changed; preserve user edits')
            order=sorted(set(expected_manifest)-{plan['metadata_path']})+[plan['metadata_path']]
            for path in order:
                checked_access(); profile.check()
                require(root_identity(profile.root)==plan['root'], 'test root changed')
                content=read_at(payload_fd,path)
                require(hashlib.sha256(content).hexdigest()==expected_manifest[path]['sha256'], 'payload changed during publish')
                current=read_at(root_fd,path)
                if current==content:
                    continue
                old_sha=hashlib.sha256(current).hexdigest() if current is not None else None
                require(old_sha==plan['expected'][path], 'reading file changed during publication')
                if path==plan['metadata_path']:
                    _verify_files(root_fd,payload_fd,plan,exclude={path})
                segments=parts(path)
                parent=directory(root_fd,'/'.join(segments[:-1]),create=True)
                temporary='.reading-'+plan_sha[:20]+'-'+hashlib.sha256(path.encode()).hexdigest()[:16]+'.tmp'
                try:
                    prior=read_at(parent,temporary)
                    if prior is None:
                        write_tree(parent,{temporary:content})
                    else:
                        require(prior==content, 'retained temporary output differs')
                    checked_access()
                    latest=read_at(parent,segments[-1])
                    require(latest==current, 'reading target changed immediately before replace')
                    if current is None:
                        rename_exclusive(parent,temporary,parent,segments[-1])
                    else:
                        os.replace(temporary,segments[-1],src_dir_fd=parent,dst_dir_fd=parent)
                        os.fsync(parent)
                finally:
                    os.close(parent)
                if after_write is not None:
                    after_write(path)
            _verify_files(root_fd,payload_fd,plan)
            checked_access(); profile.check()
            require(root_identity(profile.root)==plan['root'], 'test root changed after publication')
            receipt=dict(reference='P41-reading-published-v1',plan_sha256=plan_sha,
                files=expected_manifest,review_receipt=plan['review_receipt'],root=plan['root'])
            durable_file(job_root/'published.local.json',receipt)
            return receipt
        finally:
            os.close(payload_fd); os.close(root_fd)
    finally:
        os.close(lock)


def _verify_files(root_fd,payload_fd,plan,exclude=frozenset()):
    for path,value in plan['files'].items():
        if path in exclude:
            continue
        current=read_at(root_fd,path)
        require(current is not None and len(current)==value['size_bytes']
            and hashlib.sha256(current).hexdigest()==value['sha256'], 'published reading file differs')
    # Check links against actual files, including prior sources referenced by
    # an appended card. Never follow a link outside the descriptor root.
    class Available:
        def __contains__(self,path):
            return read_at(root_fd,path) is not None or path in exclude
        def __iter__(self):
            return iter(set(plan['files']) | set(plan.get('available_paths',[])))
    for path in plan['generated']:
        check_links(path,read_at(payload_fd,path).decode(),Available())
