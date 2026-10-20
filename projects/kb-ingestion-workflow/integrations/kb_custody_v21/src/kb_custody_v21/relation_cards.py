"""Fixed P42 card text and commands, mirrored by the physical HTTP guard."""
import re

from .contracts import ContractError

RELATION_COMMANDS = {
    'relation_candidates': ('候选 1','候选 2','候选 3','候选 4','候选 5','结束关联'),
    'relation_type': ('本篇是解读，候选是原文','候选是解读，本篇是原文',
        '本篇是摘要或摘录，候选是原文','候选是摘要或摘录，本篇是原文',
        '本篇是修订版，候选是旧版','候选是修订版，本篇是旧版','仅主题相关','取消本次关联'),
    'relation_manage': ('更正关联','撤销关联'),
}
HEADERS = dict(relation_candidates='选择可能关联的资料', relation_type='确认资料关系', relation_manage='管理已确认关联')


def checked_fields(binding):
    extra = {'material_id','snapshot_sha256','details','revision'}
    extra.add('candidate_count' if binding['purpose']=='relation_candidates' else 'pair_id')
    if set(binding) != {'purpose','document_id','source_sha256','target_message_id'} | extra:
        raise ContractError('exact relation card binding required')
    for field in ('material_id','snapshot_sha256') + (() if 'candidate_count' in binding else ('pair_id',)):
        if not isinstance(binding[field],str) or not re.fullmatch('[a-f0-9]{64}',binding[field]):
            raise ContractError('relation card requires stable SHA-256 identities')
    if type(binding['revision']) is not int or not 0 <= binding['revision'] <= 999999999:
        raise ContractError('bounded relation card revision required')
    if not isinstance(binding['details'],str) or not binding['details'].strip() or len(binding['details'].encode()) > 12000:
        raise ContractError('bounded relation card details required')
    if 'candidate_count' in binding and (type(binding['candidate_count']) is not int or not 0 <= binding['candidate_count'] <= 5):
        raise ContractError('at most five relation candidates required')


def commands(binding):
    if binding['purpose']=='relation_candidates':
        n=binding['candidate_count']
        return RELATION_COMMANDS['relation_candidates'][:n]+('结束关联',) if n else ()
    return RELATION_COMMANDS[binding['purpose']]


def selection(command, current, other):
    index=RELATION_COMMANDS['relation_type'].index(command)
    if index==7:
        return None
    kind=('interpretation','excerpt','revision','topic')[index//2]
    return dict(left=other if index%2 else current, right=current if index%2 else other, kind=kind)
