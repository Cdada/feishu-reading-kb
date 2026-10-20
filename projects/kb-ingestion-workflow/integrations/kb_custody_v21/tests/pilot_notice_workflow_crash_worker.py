"""Kill a real local child after a synthetic receipt; never access the network."""
import json
import os
from pathlib import Path
import sys
from unittest.mock import Mock

PACKAGE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(PACKAGE), str(PACKAGE/'src'), str(PACKAGE/'tests'),
               str(PACKAGE.parent/'local_conversion_mcp/src')]
from dev import offline_guard
sys.addaudithook(offline_guard)
from kb_custody_v21.f01 import encoded
from kb_custody_v21.pilot_intake import PilotIntake
from kb_custody_v21.pilot_lifecycle import PilotLifecycle
from kb_custody_v21.pilot_notice_workflow import PilotNoticeWorkflow
from kb_custody_v21.pilot_notices import PilotExecutionSession
from kb_custody_v21.pilot_preview import PilotScope
from kb_custody_v21.pilot_resources import GIB, PilotResources, ResourceSample
from kb_custody_v21.registry import Registry
from kb_custody_v21.runtime import DEFAULTS, Runtime
import test_pilot_feishu as platform
from test_custody import WORKTREE

cfg = json.loads(Path(sys.argv[1]).read_bytes())
f = platform.PilotFeishuTests()
f.root = Path(cfg['session_root'])
def hook(c, value):
    value.clear(); value.update(code=0, data=dict(message_id='om_notice', chat_id='oc_synthetic',
        msg_type='text', deleted=False, updated=False,
        sender=dict(id='ou_synthetic_bot', id_type='open_id', sender_type='app'),
        body=dict(content=json.loads(c['body'])['content'])))
f.hook = hook
def runner(c):
    result = f.respond(c)
    if c['kind']=='send' and cfg['mode'] in {'custody', 'collection'}:
        os._exit(77)  # Receipt exists; higher-level finish and lease cleanup never run.
    return dict(result, mode=c['mode'])
session = PilotExecutionSession(f.root, cfg['approval_sha'], runner=runner,
    checked_source=lambda: cfg['frozen'])
runtime = Runtime(WORKTREE, DEFAULTS, Path(cfg['runtime']))
intake = PilotIntake(Registry(runtime), PilotScope('cli_synthetic','oc_synthetic','ou_synthetic'),
                     max_attachments=2, max_pending=8)
if cfg['mode']=='custody':
    PilotNoticeWorkflow(session, intake).custody(cfg['entry_id'])
elif cfg['mode']=='collection':
    PilotNoticeWorkflow(session, intake).collection('scan_synthetic')
else:
    lifecycle = PilotLifecycle(session, intake, resources=PilotResources(intake.registry,
        probe=lambda root:ResourceSample(60,'normal',10*GIB)))
    with lifecycle.running():
        lifecycle.start(checked_ready=lambda: True)
        os._exit(77)
raise AssertionError('expected an abrupt child exit')
