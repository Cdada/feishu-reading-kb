#!/usr/bin/env python3
"""Unscheduled, separate operator entry; requires private BAUTH01 approval."""
from pathlib import Path
import os
import subprocess
import sys

from b01_execute import PACKAGE, private_approval, obtain_code
from f02_execute import checked_source
from kb_custody_v21.baidu_credentials import LoginKeychainReader
from kb_custody_v21.baidu_oauth_keychain import OAuthKeychainStore
from kb_custody_v21.baidu_oauth_enrollment import APPROVAL, LOCKS, OAuthEnrollment
from kb_custody_v21.baidu_protocol import json_object
from kb_custody_v21.contracts import ContractError
from kb_custody_v21.f01 import sha
from kb_custody_v21.runtime import no_symlinks


def source_check(worktree, path, frozen):
    approval = json_object(frozen)
    if private_approval(path) != frozen or approval.get('worktree') != str(worktree):
        raise ContractError('OAuth private approval changed')
    checked_source(worktree, approval)
    protected = approval.get('protected_files')
    if not isinstance(protected, dict) or not protected:
        raise ContractError('OAuth predecessor evidence binding required')
    for name, digest in protected.items():
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts:
            raise ContractError('OAuth protected path outside worktree')
        if sha(no_symlinks(worktree/relative).read_bytes()) != digest:
            raise ContractError('OAuth predecessor evidence changed')
    return True


def execute():
    os.umask(0o077)
    root = Path(subprocess.check_output(['git', '-C', str(PACKAGE), 'rev-parse', '--show-toplevel'], text=True).strip())
    path = no_symlinks(root/APPROVAL)
    frozen = private_approval(path)
    approval = json_object(frozen)
    check = lambda: source_check(root, path, frozen)
    experiment = OAuthEnrollment(root, approval, checked_source=check)
    locks = no_symlinks(root/LOCKS)
    locks.mkdir(mode=0o700, parents=True, exist_ok=True)
    result = experiment.run(
        load_credentials=lambda: LoginKeychainReader().read_application(
            approval['app_id'], approval['app_name'], before_read=experiment.check),
        store_factory=lambda identity: OAuthKeychainStore(identity, locks, before_access=experiment.check),
        obtain_code=obtain_code)
    print(result['status']+'; credentials stay in local Keychain; no file operation')
    if result['status'] == 'STOPPED':
        print('已停止；请保留证据，不重复运行，也不要把code或密钥发到聊天。')
    return 0 if result['status'] == 'OAUTH_STORED_AND_REUSED' else 1


if __name__ == '__main__':
    if sys.argv[1:] != ['--execute']:
        raise SystemExit('explicit --execute and approved BAUTH01 configuration required')
    try:
        sys.exit(execute())
    except (ContractError, OSError, ValueError, KeyError) as error:
        print('STOPPED before execution setup: '+type(error).__name__)
        sys.exit(1)
