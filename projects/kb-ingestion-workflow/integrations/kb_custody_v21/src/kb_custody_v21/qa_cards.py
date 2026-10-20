"""Fixed, one-use QA actions; callback values cannot provide a new prompt."""
from .contracts import ContractError

COMMANDS = ('查看原文依据', '本周周报', '新问题')
HEADER = '知识库问答'


def checked_fields(binding):
    if (set(binding) != {'purpose', 'document_id', 'source_sha256', 'target_message_id', 'job_id', 'details'}
            or binding['purpose'] != 'qa' or not isinstance(binding['job_id'], str)
            or len(binding['job_id']) != 64 or any(c not in '0123456789abcdef' for c in binding['job_id'])
            or not isinstance(binding['details'], str) or not 0 < len(binding['details'].encode()) <= 12000):
        raise ContractError('完整的问答卡片绑定必需。')
    return binding
