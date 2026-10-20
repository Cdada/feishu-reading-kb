"""One explicit successor to a proved failed text attempt, with no transport."""
import fcntl
import json
import re
import os
from pathlib import Path
from types import SimpleNamespace

from .content import ReadingGuideAdapter
from .contracts import ContractError
from .conversion import sha
from .f05 import file_sha
from .locking import open_lock
from .pilot_text import MODEL, MAX_BYTES, PilotTextSession
from .pilot_text_grounding import EVIDENCE_PROTOCOL
from .pilot_vision import object_json, read_bytes
from .registry import encoded
from .runtime import no_symlinks
from .s6 import instant


def digest(value):
    return sha(encoded(value).encode())


def text_root(path):
    root = no_symlinks(Path(path).absolute())
    if str(root) != str(path) or '.dev-runtime' not in root.parts or not root.name.startswith('pilot-text-'):
        raise ContractError('exact private text evidence root required')
    return root


def rejected_receipt(root, approval_sha, record, content, mapping, consent, **arguments):
    """Read existing bytes only; neither revalidate a failure nor renew consent.

    The complete journal, approval, exact request and received bytes form the
    evidence fingerprint. A status label alone cannot authorize a successor.
    Legacy v1 requests are reconstructed with their original provider version.
    """
    root = text_root(root)
    lock = open_lock(root/'execution.lock')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        approval_raw = read_bytes(root/'approval.local.json', 65536)
        approval = object_json(approval_raw)
        journal = read_bytes(root/'journal.local.jsonl', MAX_BYTES)
        events = [object_json(line) for line in journal.splitlines()]
        if (sha(approval_raw) != approval_sha or approval.get('approved') is not True
                or approval.get('root') != str(root) or approval.get('model') != MODEL
                or approval.get('effort') != 'medium'
                or len(events) != 4 or events[0] != dict(approval_sha256=approval_sha)
                or [e.get('state') for e in events[1:]] != ['intent','received','received_rejected']):
            raise ContractError('complete received_rejected text receipt required; no unknown or unfinished attempt')
        protocol = approval.get('evidence_protocol')
        if protocol not in (None, EVIDENCE_PROTOCOL):
            raise ContractError('unsupported previous text protocol')
        provider = PilotTextSession if protocol else SimpleNamespace(model=MODEL, version='pilot-text/v1', is_mock=False)
        request = ReadingGuideAdapter(provider).prepare_request(record, content, mapping, consent,
            authorization_time=instant(approval['starts_at']), **arguments)
        raw = encoded(request).encode()
        key = sha(raw)
        if (approval.get('request_sha256') != [key] or any(e.get('request') != key for e in events[1:])
                or read_bytes(root/(key+'.request.local.json'), MAX_BYTES) != raw
                or not instant(approval['starts_at']) <= instant(events[1]['at']) <
                    min(instant(approval['expires_at']), instant(consent['expires_at']))):
            raise ContractError('previous rejected request identity or window changed')
        response = read_bytes(root/(key+'.response.local.json'), MAX_BYTES)
        if sha(response) != events[2].get('response_sha256'):
            raise ContractError('previous received response changed')
        # A partial validation artifact needs separate investigation even if a
        # later exception appended received_rejected. It is not this entry path.
        if any((root/(key+'.'+suffix+'.local.json')).exists()
               for suffix in ('validated','grounded','grounding')):
            raise ContractError('previous attempt has partial or completed validation artifacts')
        files = {str(root/name):sha(data) for name,data in (
            ('approval.local.json',approval_raw), ('journal.local.jsonl',journal),
            (key+'.request.local.json',raw), (key+'.response.local.json',response))}
        if any(file_sha(Path(path)) != value for path,value in files.items()):
            raise ContractError('previous text evidence changed during inspection')
        return dict(request_sha256=key, files=files)
    except ContractError:
        raise
    except (KeyError, TypeError, ValueError, OSError) as error:
        if isinstance(error, BlockingIOError):
            raise
        raise ContractError('previous rejected evidence is incomplete or unreadable') from None
    finally:
        os.close(lock)


def revision_fields(revision):
    fields = {'previous_plan_sha256','previous_text_root','previous_text_approval_sha256',
              'previous_consent','previous_receipt_sha256'}
    if not isinstance(revision, dict) or set(revision) not in (fields, fields | {'previous_parent_root'}):
        raise ContractError('exact explicit failed revision binding required')
    for key in ('previous_plan_sha256','previous_text_approval_sha256','previous_receipt_sha256'):
        value = revision[key]
        if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
            raise ContractError('explicit previous evidence SHA-256 required')
    text_root(revision['previous_text_root'])
    if 'previous_parent_root' in revision:
        parent_root(revision['previous_parent_root'])
    return revision


def check_new_approval(root, approval_sha, revision):
    root = text_root(root)
    approval = object_json(read_bytes(root/'approval.local.json',65536))
    if (str(root) == revision['previous_text_root']
            or approval_sha == revision['previous_text_approval_sha256']
            or file_sha(root/'approval.local.json') != approval_sha
            or approval.get('approved') is not True or approval.get('root') != str(root)
            or approval.get('revision_sha256') != digest(revision)
            or approval.get('evidence_protocol') != EVIDENCE_PROTOCOL
            or approval.get('max_business_turns') != 1 or approval.get('automatic_business_retries') != 0):
        raise ContractError('a new single-turn approval must explicitly bind this failed revision')
    if 'previous_parent_root' in revision:
        previous = parent_root(revision['previous_parent_root'])/'approval.local.json'
        parent_sha = approval.get('parent_approval_sha256')
        if (not isinstance(parent_sha, str) or not re.fullmatch('[a-f0-9]{64}', parent_sha)
                or parent_sha == file_sha(previous)):
            raise ContractError('pre-turn revision requires a separately approved new parent')



def parent_root(path):
    parent = no_symlinks(Path(path).absolute())
    if str(parent) != path or parent.name != 'p11-requests' or '.dev-runtime' not in parent.parts:
        raise ContractError('exact previous private parent root required')
    return parent


def failed_receipt(binding, record, content, mapping, **arguments):
    """Select the complete receipt type explicitly; never infer a new budget."""
    revision_fields(binding)
    if 'previous_parent_root' in binding:
        return pre_turn_receipt(binding, record, content, mapping, **arguments)
    return rejected_receipt(binding['previous_text_root'], binding['previous_text_approval_sha256'],
        record, content, mapping, binding['previous_consent'], **arguments)


def pre_turn_receipt(binding, record, content, mapping, **arguments):
    """Read a stopped child plus its executed parent source; no transport."""
    root = text_root(binding['previous_text_root'])
    parent = parent_root(binding['previous_parent_root'])
    lock = open_lock(root/'execution.lock')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        files = {}
        def read(path, limit=MAX_BYTES):
            raw = read_bytes(path, limit)
            files[str(path)] = sha(raw)
            return raw
        approval = object_json(read(root/'approval.local.json', 65536))
        approval_sha = binding['previous_text_approval_sha256']
        events = [object_json(line) for line in read(root/'journal.local.jsonl').splitlines()]
        if (files[str(root/'approval.local.json')] != approval_sha
                or approval.get('approved') is not True or approval.get('root') != str(root)
                or approval.get('model') != MODEL or approval.get('effort') != 'medium'
                or approval.get('evidence_protocol') != EVIDENCE_PROTOCOL
                or approval.get('max_business_turns') != 1 or approval.get('automatic_business_retries') != 0
                or len(events) != 3 or events[0] != dict(approval_sha256=approval_sha)
                or [e.get('state') for e in events[1:]] != ['intent', 'blocked_before_turn']):
            raise ContractError('complete intent to blocked_before_turn receipt required')
        consent = binding['previous_consent']
        request = ReadingGuideAdapter(PilotTextSession).prepare_request(record, content, mapping, consent,
            authorization_time=instant(approval['starts_at']), **arguments)
        raw = encoded(request).encode(); key = sha(raw)
        if (approval.get('request_sha256') != [key] or any(e.get('request') != key for e in events[1:])
                or read(root/(key+'.request.local.json')) != raw
                or not instant(approval['starts_at']) <= instant(events[1]['at']) <
                    min(instant(approval['expires_at']), instant(consent['expires_at']))):
            raise ContractError('previous stopped request identity or window changed')
        if any((root/(key+'.'+suffix+'.local.json')).exists()
               for suffix in ('response', 'validated', 'grounded', 'grounding')):
            raise ContractError('a response or validation artifact prevents pre-turn continuation')
        previous = object_json(read(parent/'approval.local.json', 65536))
        if (files[str(parent/'approval.local.json')] != approval.get('parent_approval_sha256')
                or previous.get('approved') is not True or previous.get('reference') != 'P11'
                or previous.get('root') != str(parent)):
            raise ContractError('previous child must bind its exact approved parent')
        manifest_path = no_symlinks(Path(previous['source_manifest']).absolute())
        manifest = object_json(read(manifest_path, 4*1024*1024))
        executed = object_json(read(parent/'executed-source.local.json', 4*1024*1024))
        snapshot = manifest['source_snapshot']
        if (files[str(manifest_path)] != previous['source_manifest_sha256'] or manifest != executed
                or not re.fullmatch('[a-f0-9]{40}', manifest['source_commit'])
                or not isinstance(snapshot['files'], dict) or not snapshot['files']
                or snapshot['sha256'] != sha(json.dumps(snapshot['files'], sort_keys=True).encode())):
            raise ContractError('previous executed source does not match its approved manifest')
        if any(file_sha(Path(path)) != value for path,value in files.items()):
            raise ContractError('previous stopped evidence changed during inspection')
        return dict(request_sha256=key, files=files)
    except ContractError:
        raise
    except (KeyError, TypeError, ValueError, OSError) as error:
        if isinstance(error, BlockingIOError):
            raise
        raise ContractError('previous stopped evidence is incomplete or unreadable') from None
    finally:
        os.close(lock)
