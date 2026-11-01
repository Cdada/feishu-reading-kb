"""Bounded main-card snapshots; the original preview choice stays immutable."""
from .contracts import ContractError

PROFILE = dict(reference='archive-main-v1', enabled=True)


def check(binding):
    from .daily_cards import checked_binding, _digest, _identity
    if set(binding) != {'purpose','document_id','target_message_id','source_sha256','details','decision'}:
        raise ContractError('exact archive main binding required')
    _identity(binding['document_id'], 'om_')
    _digest(binding['source_sha256'])
    if (binding['target_message_id'] != binding['document_id']
            or not isinstance(binding['details'], str) or not 0 < len(binding['details'].encode()) <= 2048):
        raise ContractError('bounded original-bound main card required')
    action = binding['decision']
    if action is not None:
        if not isinstance(action, dict) or set(action) != {'card_id','binding'}:
            raise ContractError('exact main card decision required')
        _digest(action['card_id'])
        b = action['binding']
        if b.get('purpose') not in ('preview','images') or b.get('document_id') != binding['document_id']:
            raise ContractError('decision must belong to the same material')
        checked_binding(b)
    return dict(binding)


def actions(binding):
    from .daily_cards import commands
    return commands(binding['decision']['binding']) if binding['decision'] else ()
