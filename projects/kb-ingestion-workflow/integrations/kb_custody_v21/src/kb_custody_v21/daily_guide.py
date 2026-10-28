"""Reuse P27 body/image preparation with durable P32 generation ownership.

The material grant is a finite model grant. It has no HTTP capability. Reopening
it for validated local results does not renew its original model consent.
"""
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
import signal
import subprocess
import time
from pathlib import Path
from .document_formats import daily_formats, unsupported_message

from .contracts import ContractError, stable_key
from .daily_store import Deferred
from .daily_transport import durable_file
from .f05 import file_sha
from .pilot_guide_preparation import prepare_delegated_guide
from .pilot_inbox_guide import PilotInboxGuide
from .pilot_notices import PilotExecutionSession
from .pilot_text import CodexTextWire, MAX_BYTES, TEXT_ENVIRONMENT
from .pilot_vision import object_json
from .s6 import instant
from .registry import encoded
from .runtime import no_symlinks


class MaterialGrant(PilotExecutionSession):
    def __init__(self, authority, pipeline, entry_id, attempt, *, store):
        self.authority, self.pipeline, self.store = authority, pipeline, store
        self.entry_id, self.attempt = entry_id, attempt
        self.root = no_symlinks(authority.root/'model-grants'/entry_id/str(attempt)/'p11-requests')
        self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.session = self.root/'approval.local.json'
        self.clock = lambda: datetime.fromtimestamp(store.now(),timezone.utc)
        guide = Path(authority.config['guide_plan'])
        row = pipeline.registry.entry(entry_id)
        custody = json.loads(pipeline.registry.rows('SELECT payload_json FROM custody_events WHERE dedupe_key=?',
            ('pilot-inbox-custody:'+entry_id,))[0]['payload_json'])
        original = dict(custody,sha256=row['sha256'],size_bytes=row['size_bytes'])
        self.run_path = self.root/'material.local.json'
        durable_file(self.run_path,dict(reference='P30-image-choice-service-v1',previous_materials=[original]))
        if not self.session.exists():
            now=self.clock()
            durable_file(self.session,dict(reference='P32-model-parent-v1',approved=True,
                rules_path=str(authority.path),rules_sha256=authority.digest,attempt=attempt,entry_id=entry_id,
                root=str(self.root),operation='inbox-service',**authority.policy.scope,
                starts_at=now.isoformat(),expires_at=(now+timedelta(hours=10)).isoformat(),
                run_plan=str(self.run_path),guide_plan=str(guide),limits=dict(download=2),
                inbox=dict(reference='P23-service-inbox-v1',formats=daily_formats(authority.config),
                    max_asset_bytes=authority.policy.max_asset_bytes,max_files=1),
                new_material_guide=dict(reference='P23-service-guide-v1',plan_sha256=file_sha(guide),max_business_turns=1)))
        self.approval=json.loads(self.session.read_bytes())
        self.approval_sha=file_sha(self.session)
        if (file_sha(Path(self.approval['rules_path']))!=self.approval.get('rules_sha256') or self.approval.get('entry_id')!=entry_id
                or self.approval.get('attempt')!=attempt):
            raise ContractError('saved model grant identity changed')
        self.path=self.root/'journal.local.jsonl'
        self.lock=no_symlinks(self.root/'execution.lock').open('a+')
        try:
            fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.events=[json.loads(line) for line in self.path.read_text().splitlines()] if self.path.exists() else []
            self.token=None
            if not self.events:
                self.append(dict(approval=self.approval))
            elif self.events[0]!=dict(approval=self.approval):
                raise ContractError('saved material grant journal changed')
            self.bind('service-resume:'+entry_id,original)
        except BaseException:
            self.close()
            raise

    def checked_attempt(self):
        digest=self.pipeline.registry.entry(self.entry_id)['sha256']
        rows=self.store.rows('SELECT * FROM daily_models WHERE source_sha=? ORDER BY attempt',(digest,))
        if not rows or rows[-1]['attempt']!=self.attempt or rows[-1]['state'] not in ('started','unknown','validated'):
            raise ContractError('generation attempt superseded or not reserved')
        return True

    def binding(self):
        self.checked_attempt()
        frozen=self.authority.check()
        if file_sha(self.session)!=self.approval_sha:
            raise ContractError('finite model parent changed')
        row=self.pipeline.registry.entry(self.entry_id)
        original=self.pipeline.registry.runtime.path(row['relative_path'])
        if row['status']!='accepted' or row['custody_health']!='healthy' or file_sha(original)!=row['sha256']:
            raise ContractError('daily guide original changed')
        frozen.update({str(self.session):self.approval_sha,str(self.run_path):file_sha(self.run_path),str(original):row['sha256']})
        return frozen

    def check(self):
        return self.binding()

    def _call(self,*args,**kwargs):
        raise ContractError('material model grant cannot make HTTP calls')


def generate_daily_guide(authority, store, pipeline, entry_id, attempt, *, wire=None):
    grant=MaterialGrant(authority,pipeline,entry_id,attempt,store=store)
    try:
        c=authority.config
        wire=wire if wire is not None else CodexTextWire(c['text_node'],c['text_script'],c['codex_binary'],authority.runtime.worktree)
        guide=PilotInboxGuide(grant,c['guide_plan'],wire=wire)
        # Only an already-started pipeline may build its *same* request at the
        # old authorization time. A fresh model call still checks the live clock.
        historical = instant(grant.approval['starts_at']) if pipeline._event(
            entry_id,'daily:'+str(attempt)+':text_started') else None
        frozen,derived,consent,args,record=guide._material(pipeline,entry_id,authorization_time=historical)
        claim=dict(parent_approval_sha256=grant.approval_sha,guide_plan_sha256=guide.digest,
                   entry_id=entry_id,original_sha256=derived['original_sha256'])
        with pipeline.registry.transaction() as db:
            key='pilot-inbox-guide-parent:'+grant.approval_sha+':'+entry_id
            previous=db.execute('SELECT payload_json FROM custody_events WHERE dedupe_key=?',(key,)).fetchone()
            if previous and json.loads(previous[0])!=claim:
                raise ContractError('daily parent material reservation changed')
            if not previous:
                pipeline.registry.event(db,'pilot_inbox_guide_reserved',entry_id,key,claim)
        plan=dict(guide.profile,record=record)
        if guide.preserves_images:
            plan.update(policy=derived['image_material']['policy'],images=derived['image_material']['images'])
        def inputs(current_pipeline,current_entry):
            current,binding,current_consent,current_args,current_record=guide._material(
                current_pipeline,current_entry,authorization_time=historical)
            if current_record!=record:
                raise ContractError('source record changed')
            return current,binding,current_consent,current_args
        return prepare_delegated_guide(pipeline,entry_id,session=grant,plan=plan,plan_digest=guide.digest,
            font=guide.font,wire=wire,check=guide.check,inputs=inputs,
            daily_attempt=attempt,checked_attempt=grant.checked_attempt)
    finally:
        grant.close()


class DailyTextWire(CodexTextWire):
    """Stop only our own client process group, retaining unknown remote status."""
    def __init__(self, *args, stop, authority=None):
        super().__init__(*args)
        self.stop, self.authority = stop, authority

    def exchange(self, packet, before_access):
        if os.environ.get('KB_V21_OFFLINE_TEST')=='1':
            raise ContractError('real daily text wire disabled in offline tests')
        packet=dict(packet,codexBinary=self.codex_binary,cwd=self.cwd)
        if self.authority is not None and self.authority.policy.standing_daily:
            packet['binding'] = dict(packet['binding'], dailyRulesPath=str(self.authority.path),
                dailyRulesSha=self.authority.digest)
        before_access()
        if self.stop.is_set():
            raise OSError('daily model stopped before client launch')
        with subprocess.Popen([self.node,self.script],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,start_new_session=True,
                env={k:os.environ[k] for k in TEXT_ENVIRONMENT if k in os.environ}) as process:
            deadline=time.monotonic()+packet['waitMs']/1000+180
            first=encoded(packet).encode()
            try:
                while True:
                    if self.stop.is_set() or time.monotonic()>=deadline:
                        raise OSError('daily text client stopped; remote result remains unknown')
                    try:
                        output,_=process.communicate(first,timeout=min(1,max(.01,deadline-time.monotonic())))
                        break
                    except subprocess.TimeoutExpired:
                        first=None
            except BaseException:
                if process.poll() is None:
                    os.killpg(process.pid,signal.SIGKILL)
                process.communicate()
                raise
        if len(output)>MAX_BYTES:
            raise ContractError('daily text response exceeds limit')
        return object_json(output)
