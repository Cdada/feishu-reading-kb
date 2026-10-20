"""Explicit standing authorization, checked before credentials or real calls."""
from dataclasses import asdict
from pathlib import Path
import re
import math
import time
import subprocess
import threading

from .contracts import ContractError
from .daily_policy import DailyPolicy
from .f05 import file_sha
from .pilot_feishu import read_json
from .runtime import Runtime, no_symlinks
from .pilot_publication_inputs import reject_moving_inputs


class DailyAuthority:
    def __init__(self, path, expected_sha):
        self.path = no_symlinks(Path(path).absolute())
        if not isinstance(expected_sha, str) or not re.fullmatch('[a-f0-9]{64}', expected_sha):
            raise ContractError('explicit approved daily configuration hash required')
        self.digest = expected_sha
        self._source_cache={}
        self._source_lock=threading.Lock()
        if file_sha(self.path) != expected_sha:
            raise ContractError('daily operating rules changed')
        self.config = c = read_json(self.path, 65536)
        required = {'reference', 'approved', 'approval_note', 'policy', 'worktree',
                    'runtime_config', 'source_manifest', 'source_manifest_sha256',
                    'guide_plan', 'cli_guard', 'cli_profile', 'administration',
                    'text_node', 'text_script', 'codex_binary', 'initial_history_start'}
        if 'reading' in c:
            required.add('reading')
        if 'relations' in c:
            required.add('relations')
            if c['relations'] != dict(reference='P42-relations-v1', enabled=True) or not {'reading','cards'} <= set(c):
                raise ContractError('explicit P42 relations with reading and cards required')
        if 'pdf' in c:
            required.add('pdf')
        if 'cards' in c:
            required.add('cards')
            if c['cards'] != dict(reference='P40-cards-v1', enabled=True):
                raise ContractError('explicit P40 card profile required')
        if set(c) != required or c['reference'] != 'P32-standing-v1':
            raise ContractError('complete P32 standing rules required')
        if c['approved'] is not True or not isinstance(c['approval_note'], str) or not c['approval_note'].strip():
            raise ContractError('daily rules have not been approved for real execution')
        initial=c['initial_history_start']
        if type(initial) not in (int,float) or not math.isfinite(initial) or not 0 <= initial <= time.time():
            raise ContractError('explicit valid initial history boundary required before real calls')
        self.policy = DailyPolicy(**c['policy'])
        self.runtime = Runtime(c['worktree'], c['runtime_config'])
        if not self.path.is_relative_to(self.runtime.worktree / '.dev-runtime'):
            raise ContractError('daily rules must stay in private development storage')
        self.root = self.runtime.root / 'conversion-work/daily-service'
        self.manifest_path = no_symlinks(Path(c['source_manifest']).absolute())
        if file_sha(self.manifest_path) != c['source_manifest_sha256']:
            raise ContractError('daily source manifest changed')
        manifest = read_json(self.manifest_path, 4*1024*1024)
        if set(manifest) != {'reference', 'worktree', 'source_commit', 'files'} or manifest['reference'] != 'P32-source-v1' or manifest['worktree'] != str(self.runtime.worktree):
            raise ContractError('daily source manifest identity differs')
        self.source_commit=manifest['source_commit']
        if not isinstance(self.source_commit,str) or not re.fullmatch('[a-f0-9]{40}',self.source_commit):
            raise ContractError('explicit reviewed source commit required')
        self.frozen = manifest['files']
        if not isinstance(self.frozen, dict) or not self.frozen:
            raise ContractError('complete source/dependency manifest required')
        self.frozen = dict(self.frozen, **{str(self.path): self.digest,
                                         str(self.manifest_path): c['source_manifest_sha256']})
        reject_moving_inputs(self.runtime,self.frozen)
        for key in ('guide_plan', 'cli_guard', 'text_node', 'text_script', 'codex_binary'):
            if c[key] not in self.frozen:
                raise ContractError('daily dependency is not bound: '+key)
        self.check()
        self.reading = None
        if 'reading' in c:
            from .reading_profile import ReadingProfile
            self.reading = ReadingProfile(self.runtime, c['reading'], self.frozen)
        if 'pdf' in c:
            from .mineru_service import validate_pdf_config
            validate_pdf_config(c['pdf'], self.frozen)

    def check(self):
        self.runtime.gate()
        if file_sha(self.path) != self.digest:
            raise ContractError('daily authorization changed or revoked')
        dirty=subprocess.check_output(['git','-C',str(self.runtime.worktree),'status','--porcelain'])
        head=subprocess.check_output(['git','-C',str(self.runtime.worktree),'rev-parse','HEAD']).decode().strip()
        if dirty or head!=self.source_commit:
            raise ContractError('reviewed clean source commit required before daily execution')
        names = subprocess.check_output(['git','-C',str(self.runtime.worktree),'ls-files',
            '-z','--cached','--others','--exclude-standard']).decode().split('\0')
        if any(str(self.runtime.worktree/name) not in self.frozen for name in set(names)-{''}):
            raise ContractError('daily manifest omits current source files')
        with self._source_lock:
            for name, digest in self.frozen.items():
                if not isinstance(name, str) or str(Path(name).absolute()) != name:
                    raise ContractError('absolute daily source path required')
                path=no_symlinks(Path(name))
                info=path.stat()
                stamp=(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns,info.st_mode)
                # Avoid repeatedly hashing the large pinned runtime each idle
                # second. Actual HTTP children still hash every frozen file at
                # the physical access boundary using the unchanged Go guard.
                if self._source_cache.get(name)!=(stamp,digest):
                    actual=file_sha(path)
                    after=path.stat()
                    current=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns,after.st_mode)
                    if actual!=digest or current!=stamp:
                        raise ContractError('daily source or dependency changed')
                    self._source_cache[name]=(stamp,digest)
        return dict(self.frozen)


def draft_rules(worktree, runtime_config, *, scope, model_turn_limit=0):
    """Reviewable template only. Zero model budget and approved=False are real."""
    policy = DailyPolicy(**scope, model_turn_limit=model_turn_limit)
    return dict(reference='P32-standing-v1', approved=False, approval_note='',
                policy=asdict(policy), worktree=str(worktree), runtime_config=runtime_config,
                source_manifest='', source_manifest_sha256='', guide_plan='', cli_guard='',
                cli_profile='', administration={}, text_node='', text_script='',
                codex_binary='', initial_history_start=None)
