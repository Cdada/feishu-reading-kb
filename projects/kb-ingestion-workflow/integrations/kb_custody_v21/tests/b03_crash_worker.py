"""Offline hard death after B03's synthetic remote create effect."""
import json, os, sys
from pathlib import Path
from unittest.mock import Mock
PACKAGE=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(PACKAGE),str(PACKAGE/'src'),str(PACKAGE.parent/'local_conversion_mcp/src')]
from dev import offline_guard
sys.addaudithook(offline_guard)
from kb_custody_v21.b03 import DistinctArchiveExperiment, CONTENT, prepare
from b01_fixture import callbacks, steps
worktree,root=Path(sys.argv[1]),Path(sys.argv[2])
registry,_=prepare(worktree,root=root)
a=json.loads((root/'approval-fixture.json').read_bytes())
replies=iter(steps(a,CONTENT))
def request(req):
    name,result=next(replies); assert req.label==name
    if name=='create':
        with (root/'synthetic-remote-effect').open('x') as f:
            f.write('create'); f.flush(); os.fsync(f.fileno())
        os._exit(74)
    return result
c=callbacks(); c.pop('confirm_account')
DistinctArchiveExperiment(registry,a,checked_source=lambda:True,wire=Mock(is_mock=True,request=request)).run(**c)
raise SystemExit('expected injected death')
