"""Metered one-shot official CLI children under P32 standing authorization.

Every attempt has its own immutable approval and physical journal. A retry
never clears a failed journal or reuses a consumed child. The shared SQLite
reservation limits *all* children, including auth, before credentials are read.
"""
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
from .document_formats import daily_formats, unsupported_message
import subprocess

from .contracts import ContractError, stable_key
from .daily_scheduler import MaterialProblem
from .daily_reconcile import reconcile_write
from .daily_store import Deferred
from .f05 import CliProcess, DOWNLOAD_TIMEOUT_SECONDS, file_sha
from .locking import open_lock
from .pilot_feishu import read_json
from .registry import encoded
from .runtime import no_symlinks


def durable_file(path, value):
    path = no_symlinks(Path(path).absolute())
    raw = encoded(value).encode()
    if path.exists():
        if path.read_bytes() != raw:
            raise ContractError('immutable daily evidence changed')
        return
    fd = os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(path.parent, os.O_RDONLY|os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class DailyTransport:
    def __init__(self, authority, store, *, runner=None):
        self.authority, self.store = authority, store
        self.root = authority.root/'requests'
        self.before_write=None
        self.runner = runner if runner is not None else CliProcess(
            authority.config['cli_guard'], authority.frozen[authority.config['cli_guard']])

    def _root(self, operation, attempt):
        return self.root/stable_key('daily-http', operation)/str(attempt)/'p11-requests'

    def _receipt(self, root, kind, download):
        journal = no_symlinks(root/'http.local.jsonl')
        if not journal.exists():
            return None
        raw = journal.read_bytes()
        if len(raw) > 65536:
            raise ContractError('one-shot daily physical journal exceeds bound')
        events = [json.loads(line) for line in raw.splitlines()]
        key = 'p001-'+kind
        starts = [e for e in events if e.get('type')=='intent' and e.get('kind')==kind and e.get('key')==key+':'+kind]
        ends = [e for e in events if e.get('type')=='complete' and e.get('kind')==kind and e.get('key')==key+':'+kind]
        if len(starts)!=1 or len(ends)!=1:
            return None
        path = no_symlinks(root/(key+'.response.local'))
        if not path.exists() or ends[0].get('http_status')!=200 or file_sha(path)!=ends[0].get('response_sha256'):
            raise ContractError('daily physical receipt changed')
        if kind=='download':
            if not download:
                raise ContractError('explicit download contract required')
            maximum = download.get('max_asset_bytes') or download.get('size_bytes', 0)
            if not 0 < path.stat().st_size <= maximum:
                raise ContractError('download exceeds original/PDF byte contract')
            if download.get('sha256') and (file_sha(path)!=download['sha256'] or path.stat().st_size!=download['size_bytes']):
                raise ContractError('downloaded PDF differs from confirmed candidate')
            value = dict(path=str(path), sha256=file_sha(path), size_bytes=path.stat().st_size)
        else:
            value = read_json(path)
            if type(value.get('code')) is not int or value['code']!=0:
                raise ContractError('successful provider receipt required')
        evidence = {str(p):file_sha(p) for p in (path, journal, root/'approval.local.json')}
        # Preview receipts have a per-candidate marker used by the physical guard.
        for marker in root.glob('publication_*-*.started.local'):
            evidence[str(marker)] = file_sha(marker)
        return dict(value=value, receipt=str(path), evidence=evidence)

    def _checked_result(self, value):
        self.authority.check()
        if any(file_sha(Path(path))!=digest for path,digest in value['evidence'].items()):
            raise ContractError('saved daily response evidence changed')
        return value

    def request(self, operation, kind, path, **kwargs):
        directory = no_symlinks(self.root/stable_key('daily-http', operation))
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = open_lock(no_symlinks(directory/'operation.lock'))
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise Deferred('operation_still_running') from error
            return self._request(operation, kind, path, **kwargs)
        finally:
            os.close(fd)

    def _request(self, operation, kind, path, *, mode='read', body='', evidence=None,
                download=None, preview=None, notice_plan=None, confirmation=False, progress=None, card=None):
        self.authority.check()
        evidence = dict(evidence or {})
        if any(file_sha(Path(p))!=h for p,h in evidence.items()):
            raise ContractError('request origin evidence changed')
        if kind not in ('identity','list','message','download','upload','send'):
            raise ContractError('unsupported daily request category')
        request = dict(kind=kind, path=path, mode=mode, body=body, evidence=evidence,
                       download=download, preview=preview, notice_plan=notice_plan)
        if card is not None:
            if (self.authority.config.get('cards') != dict(reference='P40-cards-v1',enabled=True)
                    or progress is not None or preview is not None or notice_plan is not None or download is not None
                    or mode not in ('send','card_update','read')):
                raise ContractError('explicit narrow card profile required')
            request['card'] = card
        elif mode=='card_update':
            raise ContractError('card origin binding required')
        if progress is not None:
            if mode!='update' or kind!='send' or self.authority.config.get('pdf',{}).get('progress') is not True:
                raise ContractError('PDF progress updates require an approved profile')
            request['progress'] = progress
        elif mode=='update':
            raise ContractError('progress origin binding required')
        saved = self.store.operation(operation, kind, request)
        if saved['state']=='failed':
            raise MaterialProblem('该操作已停止自动尝试，请由 Codex 核查。')
        if saved['state']=='complete':
            return self._checked_result(json.loads(saved['result']))
        if saved['state'] in ('started','unknown'):
            # A completed physical receipt may outlive a killed CLI/parent.
            recovered = self._receipt(self._root(operation,saved['attempts']), kind, download)
            if recovered:
                self._checked_result(recovered)
                self.store.complete(operation,saved['attempts'],recovered)
                return recovered
            # A read has no external mutation to duplicate. Its old attempt is
            # retained; the next one still consumes the same cumulative limit.
            if mode=='read':
                self.store.resolve_retry(operation, dict(safe_to_retry=True,
                    receipts=[str(self._root(operation,saved['attempts']))], reason='read_has_no_remote_write'))
            else:
                result=reconcile_write(self,operation,saved,request)
                if result:
                    return result
        expires=None
        if mode=='send' and saved['attempts']:
            first=read_json(self._root(operation,1)/'approval.local.json')
            expires=datetime.fromisoformat(first['starts_at']).timestamp()+3500
            if self.store.now()>=expires:
                raise MaterialProblem('发送去重时限已到，需要由 Codex 核查。')
        if mode in ('send','upload','update','card_update') and self.before_write is not None:
            self.before_write(confirmation=confirmation)
        attempt = self.store.begin(operation, physical_requests=2, kind=kind, confirmation=confirmation)
        root = self._root(operation,attempt)
        root.mkdir(parents=True, exist_ok=False, mode=0o700)
        a = self.authority
        now = datetime.fromtimestamp(self.store.now(), timezone.utc)
        duration=DOWNLOAD_TIMEOUT_SECONDS if kind=='download' and mode=='read' else 600
        deadline=now+timedelta(seconds=duration)
        if expires:
            deadline=min(deadline,datetime.fromtimestamp(expires,timezone.utc))
        reservation = dict(reference='P32-request-v1', rules_path=str(a.path), rules_sha256=a.digest,
            root=str(root), operation_key=operation, attempt=attempt, kind=kind, mode=mode, path=path,
            body_sha256=hashlib.sha256(body.encode()).hexdigest(), maximum_http=2)
        reservation_path = root/'reservation.local.json'
        durable_file(reservation_path, reservation)
        limits = dict(auth=1, identity=0, list=0, message=0, download=0, upload=0, send=0)
        limits[kind] = 1
        approval = dict(reference='P11', approved=True, root=str(root), **a.policy.scope,
            cli_profile=a.config['cli_profile'], starts_at=now.isoformat(),
            expires_at=deadline.isoformat(), limits=limits, maximum_total=2,
            notice_purposes=['service','custody','collection','publication'],
            automatic_retries=0, redirects=0, publish_scope='only_bound_account',
            daily_parent=dict(rules_path=str(a.path), rules_sha256=a.digest, reservation_path=str(reservation_path)))
        if download and download.get('reference')=='P19-inbox-download-v1':
            approval.update(operation='receive-inbox', inbox=dict(reference='P19-inbox-v1',
                formats=daily_formats(a.config),
                max_asset_bytes=a.policy.max_asset_bytes, max_files=1))
        approval_path = root/'approval.local.json'
        durable_file(approval_path, approval)
        frozen = dict(a.check(), **evidence,
                      **{str(approval_path):file_sha(approval_path), str(reservation_path):file_sha(reservation_path)})
        control = dict(reference='P11', mode=mode, root=str(root), profile=a.config['cli_profile'],
            app_id=a.policy.app_id, kind=kind, key='p001-'+kind, path=path, session=str(approval_path),
            starts_at=approval['starts_at'], expires_at=approval['expires_at'], frozen=frozen)
        if card: control['card']=card
        if progress: control['progress']=progress
        if body: control['body']=body
        if download: control['download']=download
        if preview: control['preview']=preview
        if notice_plan:
            plan_path = root/(notice_plan['notification_id']+'.notice.local.json')
            durable_file(plan_path,notice_plan)
            frozen[str(plan_path)] = file_sha(plan_path)
            control['notice'] = dict(notification_id=notice_plan['notification_id'], plan_path=str(plan_path))
        try:
            a.check()
            self.runner(control)
            result = self._receipt(root,kind,download)
            if not result:
                raise ContractError('CLI returned without a verified physical response')
            self._checked_result(result)
            self.store.complete(operation,attempt,result)
            return result
        except (ContractError, OSError, subprocess.SubprocessError) as error:
            self._record_failure(operation,attempt,root,mode,error)
            raise

    def _record_failure(self, operation, attempt, root, mode, error):
        path = root/'http.local.jsonl'
        events = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        stop = next((e for e in reversed(events) if e.get('type')=='stop'), None)
        status = stop.get('http_status',0) if stop else 0
        made_request = any(e.get('type')=='intent' for e in events)
        transient = made_request and (status==0 or status==429 or 500<=status<600)
        business_request=any(e.get('type')=='intent' and e.get('kind')!='auth' for e in events)
        if (mode=='update' and business_request and status==400 and stop
                and stop.get('kind')=='send' and stop.get('key')=='p001-send:send'):
            response=no_symlinks(root/'p001-send.response.local')
            if response.exists() and file_sha(response)==stop.get('response_sha256'):
                rejected=read_json(response)
                if rejected.get('code')==230075:
                    # The provider explicitly rejected this edit. Preserve its
                    # receipt and attempt count; do not reconcile/retry a PUT.
                    self.store.fail(operation,attempt,'progress_edit_expired')
                    raise MaterialProblem('进度消息已超过允许编辑的时间，仅停止更新此消息。') from error
        if (mode=='card_update' and business_request and status in (400,404,410) and stop
                and stop.get('kind')=='send' and stop.get('key')=='p001-send:send'):
            response=no_symlinks(root/'p001-send.response.local')
            if response.exists() and file_sha(response)==stop.get('response_sha256'):
                rejected=read_json(response)
                if type(rejected.get('code')) is int and rejected['code']!=0:
                    self.store.fail(operation,attempt,'card_update_rejected')
                    raise MaterialProblem('卡片更新被平台拒绝，已保存的归档决定不受影响。') from error
        if (mode=='read' or not business_request) and transient:
            self.store.fail(operation,attempt,'temporary_network_failure',temporary=True,
                retry_after=stop.get('retry_after_seconds') if stop else None,
                global_pause=status==0 or bool(stop and stop.get('kind')=='auth'))
            raise Deferred('temporary_network_failure') from error
        if mode!='read' and business_request:
            self.store.fail(operation,attempt,'remote_write_unknown',uncertain=True)
            raise Deferred('remote_result_needs_reconciliation') from error
        self.store.fail(operation,attempt,'request_rejected',global_pause=status in (401,403))
        if status in (400,404,410) and stop and stop.get('kind')!='auth':
            raise MaterialProblem('文件或消息当前不可读取，请保留原件。', 'resend_file') from error
        # Guard/configuration failures and authentication errors stop visibly.
        raise error
