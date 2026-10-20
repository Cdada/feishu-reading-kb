"""Offline worker dies after atomic publication rename, before ledger success."""
import sys
from pathlib import Path
PACKAGE=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(PACKAGE),str(PACKAGE/'src'),str(PACKAGE/'tests'),str(PACKAGE.parent/'local_conversion_mcp/src')]
from dev import offline_guard
sys.addaudithook(offline_guard)
import json,os
from datetime import datetime
from kb_custody_v21.pilot_feishu import PilotReadSession
from kb_custody_v21.pilot_preview import PilotPreviews,PilotScope
from kb_custody_v21.pilot_decision import apply_owner_reply
from kb_custody_v21.registry import Registry
from kb_custody_v21.runtime import Runtime,DEFAULTS
from test_custody import WORKTREE

c=json.loads(Path(sys.argv[1]).read_text())
def runner(control):
    raise AssertionError('saved physical receipts must not perform another request')
registry=Registry(Runtime(WORKTREE,DEFAULTS,Path(c['runtime'])))
s=PilotReadSession(Path(c['root']),c['digest'],runner=runner,checked_source=lambda:c['frozen'],clock=lambda:datetime.fromisoformat(c['now']))
pilot=PilotPreviews(registry,PilotScope('cli_synthetic','oc_synthetic','ou_synthetic'))
tree=pilot.publisher._tree
def crash(row,name):
    if name==c['pid']:os._exit(89)
    return tree(row,name)
pilot.publisher._tree=crash
apply_owner_reply(s,pilot,'scan_synthetic','om_owner_reply')
