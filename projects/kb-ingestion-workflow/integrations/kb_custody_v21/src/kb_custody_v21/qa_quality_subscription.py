"""Bind an evaluation call to the existing subscription-only Node transport."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import uuid

from .contracts import ContractError
from .qa_quality import require, sha, text
from .registry import encoded
from .runtime import no_symlinks

MODEL = 'gpt-6.1-sol'


def file_digest(path):
    digest = hashlib.sha256()
    with no_symlinks(Path(path)).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def save_new(path, value):
    descriptor = os.open(no_symlinks(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        stream.write(encoded(value) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


class SubscriptionAdapter:
    """One fresh, tool-free turn. No ledger reset, retries or service activation.

    `checked_access` supplied by EvaluationCalls validates the parent grant.
    The child approval binds that grant, exact input and immutable source files.
    """
    def __init__(self, root, *, approval_path, approval_sha, wire):
        self.root = no_symlinks(Path(root).absolute())
        require('.dev-runtime' in self.root.parts, 'private evaluation output directory required')
        self.approval_path = no_symlinks(Path(approval_path).absolute())
        self.approval_sha, self.wire = approval_sha, wire
        require(sha(approval_sha) and callable(getattr(wire, 'exchange', None)), 'bound subscription transport required')
        self.check()

    def check(self):
        require(file_digest(self.approval_path) == self.approval_sha, 'evaluation transport approval changed')
        a = json.loads(self.approval_path.read_text())
        fields = {'approved', 'model', 'effort', 'billing', 'account_sha256', 'minimum_remaining_percent',
                  'starts_at', 'expires_at', 'wait_seconds', 'max_request_bytes', 'max_response_bytes'}
        require(isinstance(a, dict) and set(a) == fields and a['approved'] is True
                and a['model'] == MODEL and a['effort'] == 'medium'
                and a['billing'] == 'chatgpt_subscription_only' and sha(a['account_sha256']), 'explicit subscription approval required')
        for k, low, high in (('minimum_remaining_percent', 0, 99), ('wait_seconds', 1, 3300),
                             ('max_request_bytes', 1, 131072), ('max_response_bytes', 1, 131072)):
            require(type(a[k]) is int and low <= a[k] <= high, 'bounded evaluation transport required')
        start, end = (datetime.fromisoformat(a[k]) for k in ('starts_at', 'expires_at'))
        require(start.tzinfo is not None and end.tzinfo is not None
                and start <= datetime.now(timezone.utc) < end, 'evaluation transport grant not current')
        return a

    def __call__(self, request, checked_access):
        a = self.check()
        require(request.get('protocol') in ('qa-quality-answer-v1', 'qa-quality-judge-v1'), 'evaluation-only request required')
        raw = encoded(request)
        require(len(raw.encode()) <= a['max_request_bytes'], 'evaluation input too large; never truncate')
        request_sha = hashlib.sha256(raw.encode()).hexdigest()
        proof = checked_access()
        require(isinstance(proof, dict) and proof and all(text(k) and sha(v) for k, v in proof.items()), 'fresh source evidence required')
        # No request can be issued until its files and grant have been verified.
        def access():
            self.check()
            current = checked_access()
            require(current == proof, 'evaluation authority evidence changed during call')
            for path, expected in proof.items():
                require(file_digest(path) == expected, 'evaluation source file changed')
            return dict(proof)
        access()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        run = self.root / uuid.uuid4().hex
        run.mkdir(mode=0o700)
        request_path, child = run/'request.local.json', run/'approval.local.json'
        save_new(request_path, request)
        save_new(child, dict(a, reference='qa-quality-subscription-v1',
            request_sha256=[request_sha], automatic_business_retries=0, max_business_turns=1,
            parent_approval_sha256=self.approval_sha))
        files = dict(proof, **{str(self.approval_path): self.approval_sha,
            str(request_path): file_digest(request_path)})
        packet = dict(model=MODEL, effort='medium', waitMs=a['wait_seconds']*1000,
            request=raw, requestId='qa-quality-'+run.name,
            binding=dict(approvalPath=str(child), approvalSha=file_digest(child),
                startsAt=a['starts_at'], expiresAt=a['expires_at'], consentExpiresAt=a['expires_at'],
                accountSha=a['account_sha256'], requestSha=request_sha, files=files))
        envelope = self.wire.exchange(packet, access)
        require(isinstance(envelope, dict), 'evaluation receipt must be an object')
        require(len(encoded(envelope).encode()) <= a['max_response_bytes'], 'evaluation receipt too large')
        save_new(run/'response.local.json', envelope)
        access()
        require(envelope.get('status') == 'received', 'subscription evaluation did not return a complete receipt')
        require(envelope.get('provider_model') == MODEL, 'subscription evaluation model changed')
        try:
            output = json.loads(envelope['text'])
        except (KeyError, ValueError, TypeError) as error:
            raise ContractError('subscription evaluation returned invalid JSON') from error
        return dict(output=output, provider_model=envelope['provider_model'], usage=envelope.get('usage'))
