"""Opt-in QA scope; no implicit source expansion or model allowance."""
from pathlib import Path
import math
import re

from .contracts import ContractError
from .f05 import file_sha
from .runtime import no_symlinks


class QAProfile:
    def __init__(self, authority, config):
        required = {'reference', 'enabled', 'since', 'script'}
        if 'generation' in config:
            required.add('generation')
        if (set(config) != required or config['reference'] != 'P43-qa-v1' or config['enabled'] is not True
                or authority.reading is None or 'cards' not in authority.config):
            raise ContractError('问答需要显式 P43 配置、隔离测试库和卡片服务。')
        self.since = config['since']
        if type(self.since) not in (int, float) or not math.isfinite(self.since) or self.since < 0:
            raise ContractError('问答必须明确启用时间，不重放历史提问。')
        self.script = no_symlinks(Path(config['script']))
        if not self.script.is_relative_to(authority.runtime.worktree) or str(self.script) not in authority.frozen:
            raise ContractError('问答脚本须属于冻结的开发版本。')
        if file_sha(self.script) != authority.frozen[str(self.script)]:
            raise ContractError('问答脚本版本发生变化。')
        self.generation = config.get('generation')
        if self.generation is not None:
            g = self.generation
            if (set(g) != {'enabled', 'account_sha256', 'wait_seconds', 'minimum_remaining_percent'}
                    or g['enabled'] is not True or not isinstance(g['account_sha256'], str)
                    or not re.fullmatch('[a-f0-9]{64}', g['account_sha256'])
                    or type(g['wait_seconds']) is not int or not 1 <= g['wait_seconds'] <= 1200
                    or type(g['minimum_remaining_percent']) is not int or not 5 <= g['minimum_remaining_percent'] < 100):
                raise ContractError('问答模型必须显式绑定账号、时间和额度保留。')
