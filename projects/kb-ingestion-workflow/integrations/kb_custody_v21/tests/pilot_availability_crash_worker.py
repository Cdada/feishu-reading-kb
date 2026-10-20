"""Local mock accepts a service notice, then this child exits without cleanup."""
import os
from pathlib import Path
import sys

PACKAGE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(PACKAGE), str(PACKAGE/'src'), str(PACKAGE/'tests'),
               str(PACKAGE.parent/'local_conversion_mcp/src')]
from dev import offline_guard
sys.addaudithook(offline_guard)
from kb_custody_v21.pilot_availability import PilotServiceNotices
from kb_custody_v21.pilot_intake import PilotIntake
from kb_custody_v21.pilot_preview import PilotScope
from kb_custody_v21.notifications import PersistentMockSender
from kb_custody_v21.registry import Registry
from kb_custody_v21.runtime import Runtime, DEFAULTS
from test_custody import WORKTREE

runtime = Runtime(WORKTREE, DEFAULTS, Path(sys.argv[1]))
flow = PilotIntake(Registry(runtime), PilotScope('cli_synthetic', 'oc_synthetic', 'ou_synthetic'),
                   max_attachments=2, max_pending=8)
notices = PilotServiceNotices(flow)
sender = PersistentMockSender(runtime)
send = sender.send

def die(item, attempt):
    send(item, attempt)
    os._exit(77)

sender.send = die
notices.dispatch(sys.argv[2], sender, checked_access=lambda: None, checked_state=lambda phase: True)
