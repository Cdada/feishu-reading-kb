"""Synthetic subprocess dies after remote send receipt, before local completion."""
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
from kb_custody_v21.pilot_delivery import deliver_preview
from kb_custody_v21.registry import Registry
from kb_custody_v21.runtime import Runtime,DEFAULTS
from test_custody import WORKTREE
from test_pilot_delivery import delivery_response

c=json.loads(Path(sys.argv[1]).read_text())
def runner(control):
    result=delivery_response(control)
    if control['kind']=='send':os._exit(88)
    return result
registry=Registry(Runtime(WORKTREE,DEFAULTS,Path(c['runtime'])))
s=PilotReadSession(Path(c['root']),c['digest'],runner=runner,checked_source=lambda:c['frozen'],clock=lambda:datetime.fromisoformat(c['now']))
deliver_preview(s,PilotPreviews(registry,PilotScope('cli_synthetic','oc_synthetic','ou_synthetic')),c['pid'])
