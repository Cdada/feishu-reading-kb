"""Fixed, one-use QA actions; callback values cannot provide a new prompt."""
from .contracts import ContractError
from .qa_time import CHOICES

COMMANDS = ('查看原文依据', '本周周报', '新问题')
HEADER = '知识库问答'


def commands(binding):
    if binding.get('menu')=='result':
        return ('查看原文依据','取阅原件','本周周报','新问题')
    if binding.get('menu')=='weekly':
        return ('周报解读','新问题')
    return tuple(CHOICES) if binding.get('menu')=='time' else COMMANDS


def checked_fields(binding):
    if (set(binding)-{'menu'} != {'purpose', 'document_id', 'source_sha256', 'target_message_id', 'job_id', 'details'}
            or ('menu' in binding and binding['menu'] not in ('time','weekly','result'))
            or binding['purpose'] != 'qa' or not isinstance(binding['job_id'], str)
            or len(binding['job_id']) != 64 or any(c not in '0123456789abcdef' for c in binding['job_id'])
            or not isinstance(binding['details'], str) or not 0 < len(binding['details'].encode()) <= 12000):
        raise ContractError('完整的问答卡片绑定必需。')
    return binding
