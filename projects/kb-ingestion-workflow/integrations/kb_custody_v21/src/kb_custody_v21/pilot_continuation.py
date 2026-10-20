"""One explicitly approved continuation after a proven pre-turn revision stop."""
from pathlib import Path
import re

from .contracts import ContractError
from .f05 import file_sha
from .pilot_revision import digest, revision_fields, text_root, parent_root, pre_turn_receipt
from .pilot_vision import object_json, read_bytes


def continuation_fields(binding):
    extra = {'previous_parent_root', 'previous_revision_sha256'}
    if not isinstance(binding, dict) or not extra <= binding.keys():
        raise ContractError('explicit pre-turn continuation binding required')
    revision_fields({k:v for k,v in binding.items() if k not in extra})
    if not isinstance(binding['previous_revision_sha256'], str) or not re.fullmatch(
            '[a-f0-9]{64}', binding['previous_revision_sha256']):
        raise ContractError('previous immutable revision fingerprint required')
    parent_root(binding['previous_parent_root'])
    return binding


def blocked_receipt(binding, record, content, mapping, **arguments):
    """Verify the complete stopped child and its executed parent source manifest.

    All returned files must be frozen by the new approval. The old request is
    reconstructed at its original authorization time; no consent is renewed.
    """
    continuation_fields(binding)
    return pre_turn_receipt(binding, record, content, mapping, **arguments)


def check_continuation_approval(root, approval_sha, binding):
    root = text_root(root)
    approval = object_json(read_bytes(root/'approval.local.json', 65536))
    if (str(root) == binding['previous_text_root'] or approval_sha == binding['previous_text_approval_sha256']
            or approval.get('continuation_sha256') != digest(binding)
            or not isinstance(approval.get('parent_approval_sha256'), str)
            or not re.fullmatch('[a-f0-9]{64}', approval['parent_approval_sha256'])
            or approval['parent_approval_sha256'] == file_sha(Path(binding['previous_parent_root'])/'approval.local.json')):
        raise ContractError('new approval must explicitly bind this one pre-turn continuation')
