"""Explicit test-only reading target and versioned references to the user's rules.

No personal-library default is inherited from the referenced organizer prompt.
The target is a dedicated child of the explicitly bound content store. Merely constructing this profile does not create a library.
"""
import hashlib
from pathlib import Path
import re

from .contracts import ContractError
from .f05 import file_sha
from .registry import encoded
from .runtime import no_symlinks

REFERENCE = 'P41-reading-v1'
RULE_ROLES = {'archive_rules', 'organize_prompt', 'nlp_skill'}
LIBRARY_DIRS = ('10_Reading_读书摘录', '20_Cards_知识卡片', '30_Topics_主题索引',
                '40_Projects_专项引用', '50_Daily_Briefs_知识简报')


def absolute_path(value):
    if not isinstance(value, str) or not value or '\x00' in value:
        raise ContractError('explicit absolute reading path required')
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts or str(path) != value:
        raise ContractError('normalized absolute reading path required')
    return no_symlinks(path)


class ReadingProfile:
    def __init__(self, runtime, config, frozen):
        if (not isinstance(config, dict) or set(config) not in ({'reference', 'root', 'rules', 'script'}, {'reference', 'root', 'rules', 'script', 'generation'})
                or config['reference'] != REFERENCE):
            raise ContractError('complete explicit P41 reading profile required')
        self.generation = config.get('generation')
        if self.generation is not None:
            g = self.generation
            if (not isinstance(g, dict) or set(g) != {'enabled', 'account_sha256', 'wait_seconds', 'minimum_remaining_percent'}
                    or g['enabled'] is not True or not isinstance(g['account_sha256'], str)
                    or not re.fullmatch('[a-f0-9]{64}', g['account_sha256'])
                    or type(g['wait_seconds']) is not int or not 1 <= g['wait_seconds'] <= 3300
                    or type(g['minimum_remaining_percent']) is not int or not 5 <= g['minimum_remaining_percent'] < 100):
                raise ContractError('explicit bounded standing reading generation profile required')
        self.runtime, self.frozen = runtime, dict(frozen)
        self.config = config
        self.root = absolute_path(config['root'])
        # A dedicated direct child avoids overlap with originals, queues,
        # legacy archives, personal libraries, and their locks or briefs.
        if self.root != runtime.path('reading-library'):
            raise ContractError('reading target must be the dedicated DEV reading-library')
        self.script = absolute_path(config['script'])
        if not self.script.is_relative_to(runtime.worktree):
            raise ContractError('reading model adapter must belong to the reviewed worktree')
        rules = config['rules']
        if not isinstance(rules, dict) or set(rules) != RULE_ROLES:
            raise ContractError('archive rules, organizer and NLP references required')
        self.rules = {}
        for role, item in rules.items():
            if not isinstance(item, dict) or set(item) != {'path', 'sha256'}:
                raise ContractError('versioned reading rule reference required')
            path = absolute_path(item['path'])
            digest = item['sha256']
            if (not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest)
                    or self.frozen.get(str(path)) != digest
                    or path.is_relative_to(self.root)):
                raise ContractError('reading rules must be frozen external references')
            self.rules[role] = path
        self.rules_sha256 = hashlib.sha256(encoded(rules).encode()).hexdigest()
        self.check()

    def check(self):
        self.runtime.check_storage()
        no_symlinks(self.runtime.root)
        no_symlinks(self.root)
        for name in LIBRARY_DIRS:
            no_symlinks(self.root / name)
        for path in (self.script, *self.rules.values()):
            no_symlinks(path)
            if not path.is_file() or file_sha(path) != self.frozen.get(str(path)):
                raise ContractError('reading source or rule version changed')
        return self

    def target(self, relative):
        if not isinstance(relative, str) or not relative or '\\' in relative or '\x00' in relative:
            raise ContractError('relative reading output required')
        path = Path(relative)
        if path.is_absolute() or '..' in path.parts or str(path) != relative:
            raise ContractError('reading output must remain inside the test library')
        if path.parts[0] not in LIBRARY_DIRS:
            raise ContractError('unsupported reading output directory')
        self.check()
        return no_symlinks(self.root / path)

    def rule_texts(self):
        self.check()
        result = {}
        for role, path in self.rules.items():
            if path.stat().st_size > 256 * 1024:
                raise ContractError('reading rule reference exceeds size limit')
            result[role] = path.read_text(encoding='utf-8')
        self.check()
        return result
