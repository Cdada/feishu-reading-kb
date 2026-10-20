"""Source-bound Pilot text generation; no live activation is shipped here.

The mock-only DEV adapter stays unchanged. This explicit composition reuses its
source/quotation validator and keeps transport receipt distinct from validation.
"""
from datetime import datetime, timezone
from copy import deepcopy
import fcntl
import json
import os
import signal
from pathlib import Path
import subprocess
from types import SimpleNamespace

from .content import ReadingGuideAdapter
from .contracts import ContractError
from .conversion import durable_new, sha
from .f05 import file_sha
from .locking import open_lock
from .pilot_vision import object_json, read_bytes
from .pilot_text_grounding import EVIDENCE_PROTOCOL, ground_response
from .registry import encoded
from .runtime import no_symlinks
from .s6 import instant

MODEL = 'gpt-6.1-sol'
MAX_BYTES = 2 * 1024**2
TEXT_ENVIRONMENT = ('HOME', 'PATH', 'TMPDIR', 'LANG', 'LC_ALL', 'USER', 'LOGNAME', 'CODEX_HOME')


def load_validated_guide(root, approval_sha, record, content, mapping, consent, *, checked_source, **kwargs):
    """Read an already validated local result, including after the live window.

    This path has no transport. An intent/received response without a terminal
    validation receipt cannot authorize a new call or be promoted to success.
    """
    root = no_symlinks(Path(root).absolute())
    if '.dev-runtime' not in root.parts or not root.name.startswith('pilot-text-'):
        raise ContractError('dedicated private Pilot text root required')
    lock = open_lock(root/'execution.lock')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        def check():
            if file_sha(root/'approval.local.json') != approval_sha:
                raise ContractError('saved text approval changed')
            files = checked_source()
            if not isinstance(files, dict) or not files:
                raise ContractError('local source and evidence binding required')
            for path, digest in files.items():
                if file_sha(Path(path)) != digest:
                    raise ContractError('saved text source or evidence changed')
        check()
        approval = object_json(read_bytes(root/'approval.local.json', 65536))
        events = [object_json(line) for line in read_bytes(root/'journal.local.jsonl').splitlines()]
        if (not events or events[0] != dict(approval_sha256=approval_sha)
                or approval.get('approved') is not True or approval.get('root') != str(root)
                or approval.get('model') != MODEL or approval.get('effort') != 'medium'):
            raise ContractError('saved text approval identity mismatch')
        protocol = approval.get('evidence_protocol')
        if protocol not in (None, EVIDENCE_PROTOCOL):
            raise ContractError('unsupported saved text evidence protocol')
        provider = PilotTextSession if protocol else SimpleNamespace(model=MODEL, version='pilot-text/v1', is_mock=False)
        validator = ReadingGuideAdapter(provider)
        # Consent ID and source identity select an existing immutable request;
        # no new timestamp or generation identity is invented for the receipt.
        request = validator.prepare_request(record, content, mapping, consent,
            authorization_time=instant(approval['starts_at']), **kwargs)
        key = sha(encoded(request).encode())
        chain = [v for v in events if v.get('request') == key]
        if (key not in approval['request_sha256'] or [v.get('state') for v in chain] != ['intent','received','validated']
                or read_bytes(root/(key+'.request.local.json'),MAX_BYTES) != encoded(request).encode()):
            raise ContractError('text attempt has no complete validated receipt; no resubmission')
        at = instant(chain[0]['at'])
        if not instant(approval['starts_at']) <= at < min(instant(approval['expires_at']),instant(consent['expires_at'])):
            raise ContractError('saved text attempt was outside its approved window')
        response = read_bytes(root/(key+'.response.local.json'),MAX_BYTES)
        output = read_bytes(root/(key+'.validated.local.json'),MAX_BYTES)
        if sha(response) != chain[1]['response_sha256'] or sha(output) != chain[2]['output_sha256']:
            raise ContractError('saved text response or output changed')
        saved = object_json(output)
        response_value = object_json(response)
        if protocol:
            response_value, grounding = ground_response(record,content,mapping,request,response_value)
            for suffix, value in [('grounded',response_value),('grounding',grounding)]:
                raw = read_bytes(root/(key+'.'+suffix+'.local.json'),MAX_BYTES)
                if sha(raw) != chain[2].get(suffix+'_sha256') or raw != encoded(value).encode():
                    raise ContractError('saved quotation grounding changed')
            if chain[2].get('evidence_protocol') != protocol:
                raise ContractError('saved quotation grounding protocol changed')
        fresh = validator.validate_response(record,content,mapping,consent,request,response_value)
        expected = dict(record=fresh[0],curation=fresh[1],guide=fresh[2])
        compared = deepcopy(saved)
        # Only generated identifiers/timestamps vary when rechecking identical
        # response bytes. Keep all of the original values in the returned data.
        expected['record']['source_reading']['text']['read_at'] = compared['record']['source_reading']['text']['read_at']
        expected['curation']['guide_generation']['guide_id'] = compared['curation']['guide_generation']['guide_id']
        expected['curation']['review_summary']['reviewed_at'] = compared['curation']['review_summary']['reviewed_at']
        if expected != compared:
            raise ContractError('saved text output no longer agrees with its response')
        check()
        return saved['record'], saved['curation'], saved['guide']
    except ContractError:
        raise
    except (ValueError, TypeError, KeyError, OSError) as exc:
        if isinstance(exc, BlockingIOError):
            raise
        raise ContractError('saved text evidence cannot be reused') from None
    finally:
        os.close(lock)


class CodexTextWire:
    """Stdio only; the official client owns login. Never inspect its secrets."""
    def __init__(self, node, script, codex_binary, cwd):
        self.node, self.script, self.codex_binary, self.cwd = map(str, (node, script, codex_binary, cwd))

    def exchange(self, packet, before_access):
        if os.environ.get('KB_V21_OFFLINE_TEST') == '1':
            raise ContractError('real text wire disabled in offline tests')
        packet = dict(packet, codexBinary=self.codex_binary, cwd=self.cwd)
        before_access()
        # Only this child is stopped on timeout. The parent retains the unknown
        # business intent and does not infer that remote computation was undone.
        try:
            with subprocess.Popen([self.node, self.script], stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True,
                    # Node processes NODE_OPTIONS before any JavaScript can filter it.
                    env={key: os.environ[key] for key in TEXT_ENVIRONMENT if key in os.environ}) as process:
                try:
                    output, _ = process.communicate(encoded(packet).encode(), timeout=packet['waitMs']/1000 + 180)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate()
                    raise
        except (OSError, subprocess.TimeoutExpired):
            raise OSError('text client stopped without a complete receipt') from None
        if len(output) > MAX_BYTES:
            raise ContractError('text client response exceeds limit')
        return object_json(output)


def safe_diagnostic(envelope):
    """Retain only bounded enums, never upstream messages, account data or text."""
    allowed = {
        'stage': {'setup', 'preflight', 'generation', 'received'},
        'checkpoint': {'setup', 'input_validation', 'source_binding', 'client_start',
            'process_pin', 'account_read', 'quota_read', 'subscription_check',
            'thread_setup', 'generation', 'response_validation', 'cleanup'},
        'reason': {'boundary_rejected', 'configuration_rejected', 'account_binding_rejected',
            'quota_metadata_rejected', 'paid_credit_policy_rejected', 'quota_threshold_reached',
            'operation_failed'},
        'cleanup': {'failed'},
    }
    return {key: envelope[key] for key, values in allowed.items()
            if isinstance(envelope.get(key), str) and envelope[key] in values}


class PilotTextSession:
    is_mock, model, version = False, MODEL, 'pilot-text/v2'

    def __init__(self, root, approval_sha, *, checked_source, wire, clock=None):
        self.root = no_symlinks(Path(root).absolute())
        if '.dev-runtime' not in self.root.parts or not self.root.name.startswith('pilot-text-'):
            raise ContractError('dedicated private Pilot text root required')
        self.approval_path = self.root/'approval.local.json'
        self.approval_sha = approval_sha
        if file_sha(self.approval_path) != approval_sha:
            raise ContractError('text approval binding changed')
        self.approval = a = object_json(read_bytes(self.approval_path, 65536))
        if (a.get('approved') is not True or a.get('root') != str(self.root)
                or a.get('model') != MODEL or a.get('effort') != 'medium'
                or a.get('billing') != 'chatgpt_subscription_only'
                or a.get('evidence_protocol') != EVIDENCE_PROTOCOL
                or type(a.get('minimum_remaining_percent',5)) is not int
                or not 0 <= a.get('minimum_remaining_percent',5) < 100
                or a.get('automatic_business_retries') != 0
                or type(a.get('max_business_turns')) is not int or not 1 <= a['max_business_turns'] <= 6
                or type(a.get('wait_seconds')) is not int or not 1 <= a['wait_seconds'] <= 3300
                or not isinstance(a.get('request_sha256'), list) or not a['request_sha256']
                or any(not isinstance(v, str) or len(v) != 64 for v in a['request_sha256'])
                or not isinstance(a.get('account_sha256'), str) or len(a['account_sha256']) != 64
                or not 0 < (instant(a['expires_at'])-instant(a['starts_at'])).total_seconds() <= 36000):
            raise ContractError('exact text inputs, account, window and bounded subscription scope required')
        self.checked_source, self.wire = checked_source, wire
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.path = self.root/'journal.local.jsonl'
        self.lock = open_lock(self.root/'execution.lock')
        self.events = []
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self.path.exists():
                self.events = [object_json(line) for line in read_bytes(self.path).splitlines()]
                if not self.events or self.events[0] != dict(approval_sha256=approval_sha):
                    raise ContractError('text budget binding changed; never reset')
            else:
                self.append(dict(approval_sha256=approval_sha))
            self.check()
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.lock is not None:
            os.close(self.lock)
            self.lock = None

    def append(self, event):
        fd = os.open(no_symlinks(self.path), os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'ab') as out:
            out.write((encoded(event)+'\n').encode()); out.flush(); os.fsync(out.fileno())
        parent = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
        self.events.append(event)

    def check(self):
        if self.lock is None:
            raise ContractError('text session closed')
        def window():
            if not instant(self.approval['starts_at']) <= self.clock() < instant(self.approval['expires_at']):
                raise ContractError('outside approved text window')
        window()
        if file_sha(self.approval_path) != self.approval_sha:
            raise ContractError('text approval changed')
        files = self.checked_source()
        if not isinstance(files, dict) or not files:
            raise ContractError('full source and material evidence binding required')
        for path, digest in files.items():
            if file_sha(Path(path)) != digest:
                raise ContractError('text source or evidence changed')
        if file_sha(self.approval_path) != self.approval_sha:
            raise ContractError('text approval changed during source check')
        window()
        return files

    def generate_guide(self, record, content, mapping, consent, **kwargs):
        """Validate final output before success. Any incomplete intent stays used."""
        validator = ReadingGuideAdapter(self)
        request = validator.prepare_request(record, content, mapping, consent, **kwargs)
        raw = encoded(request).encode()
        key = sha(raw)
        a = self.approval
        if len(raw) > MAX_BYTES or key not in a['request_sha256']:
            raise ContractError('model request differs from the approved source/metadata/skill payload')
        if any(e.get('request') == key for e in self.events):
            raise ContractError('existing text attempt retained; no automatic resubmission')
        intents = [e for e in self.events if e.get('state') == 'intent']
        if len(intents) >= a['max_business_turns']:
            raise ContractError('text business budget exhausted')
        if any(not any(v.get('request') == e['request'] and v.get('state') in
                       {'validated', 'blocked_before_turn', 'received_rejected'} for v in self.events) for e in intents):
            raise ContractError('unresolved text generation retained')
        def access():
            files = self.check()
            if not self.clock() < instant(consent['expires_at']):
                raise ContractError('content consent expired')
            return files
        files = access()
        durable_new(self.root/(key+'.request.local.json'), raw)
        access()
        self.append(dict(request=key, state='intent', model=MODEL, effort='medium',
            internal_http_attempts='not_observable', at=self.clock().isoformat()))
        state = 'unknown'
        diagnostic = {}
        try:
            packet = dict(model=MODEL, effort='medium', request=raw.decode(), requestId=key,
                waitMs=a['wait_seconds']*1000,
                binding=dict(approvalPath=str(self.approval_path), approvalSha=self.approval_sha,
                    startsAt=a['starts_at'], expiresAt=a['expires_at'], accountSha=a['account_sha256'],
                    consentExpiresAt=consent['expires_at'], requestSha=key, files=files))
            envelope = self.wire.exchange(packet, access)
            diagnostic = safe_diagnostic(envelope)
            status = envelope.get('status')
            if status not in {'received', 'blocked_before_turn', 'received_rejected', 'unknown'}:
                raise ContractError('invalid text client status')
            if status != 'received':
                state = status
                raise ContractError('text generation did not produce a usable response')
            state = 'received_rejected'
            text = envelope.get('text')
            if not isinstance(text, str) or len(text.encode()) > MAX_BYTES:
                raise ContractError('bounded text response required')
            response_bytes = text.encode()
            durable_new(self.root/(key+'.response.local.json'), response_bytes)
            self.append(dict(request=key, state='received', response_sha256=sha(response_bytes)))
            access()
            grounded, grounding = ground_response(record,content,mapping,request,object_json(response_bytes))
            result = validator.validate_response(record, content, mapping, consent, request, grounded)
            # Contract validation can itself cross the window or expose changed evidence.
            access()
            output = encoded(dict(record=result[0], curation=result[1], guide=result[2])).encode()
            derived_bytes = {suffix: encoded(value).encode()
                             for suffix, value in [('grounded',grounded),('grounding',grounding)]}
            if any(len(raw) > MAX_BYTES for raw in [output, *derived_bytes.values()]):
                raise ContractError('derived text evidence exceeds the local receipt limit')
            durable_new(self.root/(key+'.validated.local.json'), output)
            derived_hashes = {}
            for suffix, raw in derived_bytes.items():
                durable_new(self.root/(key+'.'+suffix+'.local.json'), raw)
                derived_hashes[suffix+'_sha256'] = sha(raw)
            access()
            self.append(dict(request=key, state='validated', output_sha256=sha(output),
                quality='pending_personal_review', internal_http_attempts='not_observable',
                evidence_protocol=EVIDENCE_PROTOCOL, **derived_hashes))
            return result
        except (ContractError, OSError, ValueError, TypeError, KeyError):
            self.append(dict(request=key, state=state, **({'diagnostic': diagnostic} if diagnostic else {})))
            raise ContractError('text attempt retained: '+state+'; no automatic resubmission') from None
