#!/usr/bin/env python3
"""Install a disabled P32 launcher/skill without touching existing service data."""
import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

PACKAGE=Path(__file__).resolve().parent
sys.path.insert(0,str(PACKAGE/'src'))
from kb_custody_v21.contracts import ContractError
from kb_custody_v21.daily_transport import durable_file
from kb_custody_v21.runtime import Runtime,no_symlinks


def install(worktree,runtime_config,*,destination,skills,applications,python,dependencies):
    runtime=Runtime(worktree,runtime_config)
    destination,skills,applications=[no_symlinks(Path(p).absolute()) for p in (destination,skills,applications)]
    skill=skills/'feishu-kb-service'
    if any(p.exists() for p in (destination,skill,applications)):
        raise ContractError('launcher destination already exists; inspect it before updating')
    binding=dict(reference='P32-launcher-v1',worktree=str(runtime.worktree),
        package_relative=str(PACKAGE.relative_to(runtime.worktree)),runtime_relative=runtime_config['runtime_relative'],
        conversion_relative='projects/kb-ingestion-workflow/integrations/local_conversion_mcp/src',
        python=str(no_symlinks(Path(python).resolve(strict=True))),python_dependencies=[str(no_symlinks(Path(p).absolute())) for p in dependencies],
        rules_path=None,rules_sha256=None)
    for p in (destination,skill,applications):p.mkdir(parents=True,mode=0o700)
    durable_file(destination/'launcher.local.json',binding)
    script=destination/'kb.py'
    shutil.copyfile(PACKAGE/'launcher_bootstrap.py',script);script.chmod(0o700)
    template=(PACKAGE/'skills/feishu-kb-service/SKILL.md').read_text()
    (skill/'SKILL.md').write_text(template.replace('{{LAUNCHER}}',str(script)).replace('{{PYTHON}}',binding['python']))
    for name,action in (('启动飞书知识库','start'),('查看知识库状态','status'),('停止飞书知识库','stop')):
        command=' '.join(shlex.quote(str(v)) for v in (binding['python'],script,action))
        path=applications/(name+'.command')
        path.write_text('#!/bin/zsh\n'+command+'\nkb_exit=$?\nprintf "\\n按回车关闭窗口。"\nread -r kb_answer\nexit "$kb_exit"\n')
        path.chmod(0o700)
    return dict(state='installed_not_activated',launcher=str(script),skill=str(skill),applications=str(applications))


def main():
    parser=argparse.ArgumentParser(description='安装本机启动器（保持未激活）')
    parser.add_argument('--worktree',required=True);parser.add_argument('--runtime-config',required=True)
    parser.add_argument('--destination',required=True);parser.add_argument('--skills',required=True)
    parser.add_argument('--applications',required=True);parser.add_argument('--python',required=True)
    parser.add_argument('--dependency',action='append',default=[])
    a=parser.parse_args()
    result=install(a.worktree,json.loads(Path(a.runtime_config).read_text()),destination=a.destination,
        skills=a.skills,applications=a.applications,python=a.python,dependencies=a.dependency)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
