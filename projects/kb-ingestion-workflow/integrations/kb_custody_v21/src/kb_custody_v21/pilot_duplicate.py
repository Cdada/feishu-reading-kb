"""Explicit local conversion choice; never clears custody or archive protection."""
import re

from .contracts import ContractError, validate_id
from .registry import encoded


def validate_selection(value):
    if (not isinstance(value, dict)
            or set(value) not in ({'action', 'sha256', 'prior_task_ids'}, {'action', 'sha256', 'prior_task_ids', 'qualified_prior'})
            or value['action'] != 'convert_as_new_version'
            or not isinstance(value['sha256'], str)
            or not re.fullmatch('[a-f0-9]{64}', value['sha256'])
            or not isinstance(value['prior_task_ids'], list)
            or not 1 <= len(value['prior_task_ids']) <= 20
            or any(not isinstance(tid, str) for tid in value['prior_task_ids'])
            or len(set(value['prior_task_ids'])) != len(value['prior_task_ids'])):
        raise ContractError('explicit bounded duplicate conversion selection required')
    for tid in value['prior_task_ids']:
        validate_id(tid, 'task')
    qualified=value.get('qualified_prior',{})
    if not isinstance(qualified,dict) or not set(qualified)<=set(value['prior_task_ids']):
        raise ContractError('qualified prior tasks must be explicitly selected')
    for binding in qualified.values():
        if not isinstance(binding,dict) or set(binding)!={'extraction_id','review_id'}:
            raise ContractError('exact prior extraction and review required')
        validate_id(binding['extraction_id'],'extraction');validate_id(binding['review_id'],'review')
    return value


def selected_action(intake, entry, inspection, selection):
    """Match the whole observed prior-task set before recording a local choice.

    This is called only by an explicit operator selection or a source-bound run
    plan. Messages and model output cannot supply a duplicate selection.
    """
    validate_selection(selection)
    duplicate = inspection['duplicate']
    matches = [m['last_task_id'] for m in duplicate['matches']]
    if (entry['sha256'] != selection['sha256'] or inspection['sha256'] != selection['sha256']
            or duplicate['kind'] not in {'exact', 'same_content'}
            or not duplicate['requires_confirmation']
            or set(matches) != set(selection['prior_task_ids']) or len(matches) != len(set(matches))):
        raise ContractError('duplicate content or complete prior-task set differs from explicit selection')
    for tid in matches:
        task, binding = intake.conversion.recovery.bound_task(tid)
        prior = intake.registry.entry(binding['entry_id'])
        qualified=False
        if tid in selection.get('qualified_prior',{}):
            from .material_review import MaterialReviews
            selected=selection['qualified_prior'][tid]
            saved=MaterialReviews(intake.registry).require_qualified(selected['extraction_id'])
            qualified=(saved['review_id']==selected['review_id'] and saved['binding']['task_id']==tid
                       and saved['binding']['entry_id']==prior['entry_id'])
            if not qualified:raise ContractError('qualified prior review differs from selection')
        if (not (task['status'] in {'ready_candidate', 'partial_ready'} or task['status']=='needs_review' and qualified)
                or prior['sha256'] != selection['sha256']
                or prior['status'] != 'accepted' or prior['custody_health'] != 'healthy'):
            raise ContractError('selected prior conversion is not healthy and terminal')
    with intake.registry.transaction() as db:
        key = 'pilot-duplicate:' + inspection['inspection_id']
        old = db.execute('SELECT payload_json FROM custody_events WHERE dedupe_key=?', (key,)).fetchone()
        payload = dict(entry_id=entry['entry_id'], inspection_id=inspection['inspection_id'], selection=selection)
        if old and old[0] != encoded(payload):
            raise ContractError('saved duplicate decision changed')
        intake.registry.event(db, 'pilot_duplicate_selection', entry['entry_id'], key, payload)
    return selection['action']
