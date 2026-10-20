#!/usr/bin/env python3
"""Operator-only B01 entry. No scheduler/CI activation and no secret arguments."""
import getpass
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from urllib.parse import urlencode
import warnings

from f02_execute import PACKAGE, checked_source
from kb_custody_v21.b01 import APPROVAL, RUNTIME, BaiduExperiment, prepare
from kb_custody_v21.baidu_credentials import LoginKeychainReader
from kb_custody_v21.baidu_protocol import json_object
from kb_custody_v21.contracts import ContractError
from kb_custody_v21.f01 import sha
from kb_custody_v21.runtime import no_symlinks


def private_approval(path):
    fd = os.open(no_symlinks(path), os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > 512*1024):
            raise ContractError('private owner-only bounded approval file required')
        return source.read(512*1024+1)


def source_check(worktree, path, frozen):
    approval = json_object(frozen)
    if private_approval(path) != frozen or approval.get('worktree') != str(worktree):
        raise ContractError('B01 private approval or worktree changed')
    checked_source(worktree, approval)
    return True


def authorization_url(credentials):
    # The public AppKey is sent only to official OAuth in the operator's Chrome;
    # SecretKey never appears in the browser link. No shared application keys.
    return 'https://openapi.baidu.com/oauth/2.0/authorize?' + urlencode(dict(
        response_type='code', client_id=credentials.app_key, redirect_uri='oob', scope='basic,netdisk'))


def obtain_code(credentials, approval):
    if not sys.stdin.isatty():
        raise ContractError('operator TTY required for OAuth')
    print('请仅在 Chrome 中打开下列百度官方授权链接。核对页面应用为本次批准应用，登录本人账号。')
    print('应用：'+approval['app_name']+'；AppID：'+approval['app_id'])
    print('如应用、账号、权限不同，或提示付费/升级/额外设置，请取消并停止。')
    print(authorization_url(credentials), flush=True)
    print('完成 basic/netdisk 授权后，仅把本次 code 粘贴到下方；不要发到聊天。', flush=True)
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        try:
            return getpass.getpass('BAIDU CODE (hidden): ')
        except getpass.GetPassWarning:
            raise ContractError('hidden TTY input unavailable; no visible fallback') from None


def confirm_account(observed):
    if not sys.stdin.isatty():
        raise ContractError('operator TTY required for first-account binding')
    print('刚授权账号的唯一 UK：'+observed)
    print('确认刚才在 Chrome 授权的是本人账号后，原样输入以上 UK；任何其他输入将停止。', flush=True)
    return input('CONFIRM UK: ').strip()


def report_result(result):
    print(result['status']+'; preserve all evidence; no automatic retry')
    if result['status'] != 'STOPPED':
        return
    hints = {
        'load_credentials':'读取已保存的应用凭据时停止。无需把Secret发到聊天。',
        'obtain_oauth_code':'等待本人OAuth操作或隐藏输入时停止。请说明是否出现BAIDU CODE提示，不要发送code。',
        'validate_oauth_code':'没有收到符合格式的授权码。应在Chrome打开本次链接完成授权，再将新code粘贴到BAIDU CODE隐藏输入处；本次已停止，请勿重跑。',
        'before_oauth_check':'OAuth前的本地源码、批准、原件或窗口检查未通过。',
        'after_oauth_check':'输入code后的本地源码、批准、原件或窗口检查未通过。',
    }
    stage = result.get('error_stage')
    print(hints.get(stage,'已停止；请由本地审计核对日志中记录的失败阶段。'))


def execute():
    os.umask(0o077)
    worktree = Path(subprocess.check_output(['git','-C',str(PACKAGE),'rev-parse','--show-toplevel'], text=True).strip())
    path = no_symlinks(worktree/APPROVAL)
    frozen = private_approval(path)
    approval = json_object(frozen)
    if (approval.get('approved') is not True or approval.get('runtime_relative') != RUNTIME
            or approval.get('worktree') != str(worktree)):
        raise ContractError('explicit B01 approval and fixed private DEV runtime required')
    check = lambda: source_check(worktree, path, frozen)
    check()  # Before registry preparation, native API loading or a secret read.
    if not sys.stdin.isatty():
        raise ContractError('run the approved one-shot command in an operator TTY')
    registry, _ = prepare(worktree)
    experiment = BaiduExperiment(registry, approval, checked_source=check)
    experiment.append({'binding':'approved_source', 'value':dict(
        commit=approval['source_commit'], source_sha256=approval['source_snapshot']['sha256'], approval_sha256=sha(frozen))})
    def load(app_id, app_name, before):
        before()
        return LoginKeychainReader().read_application(app_id, app_name, before_read=before)
    result = experiment.run(load_credentials=load, obtain_code=obtain_code, confirm_account=confirm_account)
    # Do not reflect platform error messages, responses or credential values.
    report_result(result)
    return 0 if result['status'] == 'BOUNDED_BAIDU_UPLOAD_VERIFIED' else 1


if __name__ == '__main__':
    if sys.argv[1:] != ['--execute']:
        raise SystemExit('explicit --execute and separately approved B01 configuration required')
    try:
        sys.exit(execute())
    except (Exception, KeyboardInterrupt) as error:
        print('STOPPED: '+type(error).__name__+'; preserve existing B01 evidence')
        sys.exit(1)
