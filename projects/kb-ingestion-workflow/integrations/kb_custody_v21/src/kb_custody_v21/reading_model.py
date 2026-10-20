"""One bounded organization/review call, explicitly enabled by a finite grant.

Missing grants leave a reviewable request locally without spending a turn. An
intent and a received envelope are immutable; restarts reuse receipts and never
automatically resubmit an unknown model turn.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re

from .contracts import ContractError
from .daily_store import Deferred
from .daily_transport import durable_file
from .f05 import file_sha
from .pilot_text import MODEL, MAX_BYTES, safe_diagnostic
from .reading_contract import require, request, sha, validate_draft, validate_review
from .registry import encoded
from .runtime import no_symlinks
from .s6 import instant


class ReadingModel:
    def __init__(self, authority, reading_store, wire, *, checked_source):
        self.authority,self.store,self.wire=authority,reading_store,wire
        self.checked_source=checked_source

    def run(self,purpose,source,draft=None):
        profile=self.authority.reading
        request_value=request(purpose,source,profile.rule_texts(),draft)
        raw=encoded(request_value)
        request_sha=hashlib.sha256(raw.encode()).hexdigest()
        material_id=source['material_id']
        require(bool(re.fullmatch('[A-Za-z0-9_-]{1,200}',material_id)), 'safe material identity required')
        root=no_symlinks(self.authority.root/'reading-jobs'/material_id/'model'/request_sha)
        root.mkdir(parents=True,exist_ok=True,mode=0o700)
        durable_file(root/'request.local.json',request_value)
        approval_path=root/'approval.local.json'
        receipt_path=root/'response.local.json'
        def check_source():
            frozen=self.authority.check()
            profile.check()
            evidence=self.checked_source(source)
            require(isinstance(evidence,dict) and bool(evidence), 'frozen original and publication evidence required')
            for path,digest in evidence.items():
                require(file_sha(no_symlinks(Path(path)))==digest, 'reading source changed')
            return dict(frozen,**evidence,**{str(root/'request.local.json'):request_sha})
        check_source()
        existing=self.store.call(material_id,purpose)
        if existing is not None:
            require(existing['request_sha256']==request_sha, 'saved reading call no longer matches current input')
            if existing['state']=='validated':
                result=existing['result']
                require(receipt_path.exists() and file_sha(receipt_path)==result['response_sha256'], 'saved reading receipt changed')
                require(approval_path.exists() and file_sha(approval_path)==result['approval_sha256'], 'saved reading approval changed')
                output=result['output']
                self._validate(purpose,output,source,draft)
                return output
            if existing['state']=='rejected':
                raise Deferred('reading_result_requires_codex_review')
            if not receipt_path.exists():
                raise Deferred('reading_model_result_unknown_no_resubmit')
        if not approval_path.exists() and profile.generation is not None:
            # Enabled only by a newly reviewed standing reading configuration.
            # Default/legacy profiles never enter this branch. The same original
            # approval/answer time bounds both organization and review turns.
            material=self.store.get(material_id)
            answers=material['detail'].get('answers',[])
            accepted=answers[-1]['accepted_at'] if answers else material['created']
            start=datetime.fromtimestamp(accepted,timezone.utc)
            g=profile.generation
            durable_file(approval_path,dict(reference='P41-reading-model-v1',approved=True,
                purpose=purpose,material_id=material_id,source_sha256=source['binding']['source_sha256'],
                rules_sha256=profile.rules_sha256,daily_rules_sha256=self.authority.digest,
                model=MODEL,effort='medium',billing='chatgpt_subscription_only',max_business_turns=1,
                automatic_business_retries=0,request_sha256=[request_sha],account_sha256=g['account_sha256'],
                starts_at=start.isoformat(),expires_at=(start+timedelta(hours=10)).isoformat(),
                minimum_remaining_percent=g['minimum_remaining_percent'],wait_seconds=g['wait_seconds']))
        if not approval_path.exists():
            durable_file(root/'approval-draft.local.json',dict(reference='P41-reading-model-v1',approved=False,
                purpose=purpose,material_id=material_id,source_sha256=source['binding']['source_sha256'],
                rules_sha256=profile.rules_sha256,daily_rules_sha256=self.authority.digest,
                model=MODEL,effort='medium',billing='chatgpt_subscription_only',max_business_turns=1,
                automatic_business_retries=0,request_sha256=[request_sha],account_sha256='',
                starts_at='',expires_at='',minimum_remaining_percent=5,wait_seconds=1200))
            raise Deferred('reading_model_approval_required')
        approval_raw=no_symlinks(approval_path).read_bytes()
        require(len(approval_raw)<=65536,'oversized reading approval')
        approval=json.loads(approval_raw); approval_sha=hashlib.sha256(approval_raw).hexdigest()
        expected=dict(reference='P41-reading-model-v1',approved=True,purpose=purpose,material_id=material_id,
            source_sha256=source['binding']['source_sha256'],rules_sha256=profile.rules_sha256,
            daily_rules_sha256=self.authority.digest,model=MODEL,effort='medium',billing='chatgpt_subscription_only',
            max_business_turns=1,automatic_business_retries=0,request_sha256=[request_sha])
        require(all(approval.get(k)==v for k,v in expected.items()),'explicit reading call approval required')
        require(isinstance(approval.get('account_sha256'),str) and bool(re.fullmatch('[a-f0-9]{64}',approval['account_sha256'])), 'approved account required')
        require(type(approval.get('wait_seconds')) is int and 1<=approval['wait_seconds']<=3300, 'bounded wait required')
        require(type(approval.get('minimum_remaining_percent')) is int and 5<=approval['minimum_remaining_percent']<100, 'quota reserve required')
        start,end=instant(approval['starts_at']),instant(approval['expires_at'])
        require(0<(end-start).total_seconds()<=36000,'bounded approval window required')
        def before_access():
            frozen=check_source()
            require(file_sha(approval_path)==approval_sha,'reading approval changed')
            now=datetime.fromtimestamp(self.store.store.now(),timezone.utc)
            require(start<=now<end,'reading model approval expired')
            return frozen
        if existing is None:
            frozen=before_access()
            self.store.reserve_call(material_id,purpose,request_sha,checked_access=before_access)
            binding=dict(approvalPath=str(approval_path),approvalSha=approval_sha,startsAt=approval['starts_at'],
                expiresAt=approval['expires_at'],consentExpiresAt=approval['expires_at'],accountSha=approval['account_sha256'],
                requestSha=request_sha,files=frozen)
            packet=dict(model=MODEL,effort='medium',waitMs=approval['wait_seconds']*1000,
                request=raw,requestId='reading-'+request_sha[:40],binding=binding)
            try:
                envelope=self.wire.exchange(packet,before_access)
            except (OSError,ContractError) as error:
                self.store.save_call_result(material_id,purpose,'unknown',dict(reason=type(error).__name__),checked_access=check_source)
                raise Deferred('reading_model_result_unknown_no_resubmit') from error
            require(len(encoded(envelope).encode())<=MAX_BYTES,'oversized reading response')
            durable_file(receipt_path,envelope)
        else:
            require(receipt_path.stat().st_size<=MAX_BYTES,'oversized retained reading response')
            envelope=json.loads(receipt_path.read_bytes())
        if envelope.get('status')!='received':
            state='rejected' if envelope.get('status') in ('blocked_before_turn','received_rejected') else 'unknown'
            self.store.save_call_result(material_id,purpose,state,dict(diagnostic=safe_diagnostic(envelope),response_sha256=file_sha(receipt_path)),checked_access=check_source)
            raise Deferred('reading_model_result_'+state+'_no_resubmit')
        try:
            output=json.loads(envelope['text'])
            self._validate(purpose,output,source,draft)
        except (ValueError,KeyError,TypeError,ContractError) as error:
            self.store.save_call_result(material_id,purpose,'rejected',dict(reason=type(error).__name__,response_sha256=file_sha(receipt_path)),checked_access=check_source)
            raise Deferred('reading_result_requires_codex_review') from error
        self.store.save_call_result(material_id,purpose,'validated',dict(output=output,request_sha256=request_sha,
            response_sha256=file_sha(receipt_path),approval_sha256=approval_sha),checked_access=check_source)
        return output

    @staticmethod
    def _validate(purpose,output,source,draft):
        return validate_draft(output,source) if purpose=='organize' else validate_review(output,draft,source)
