"""Real child death, portable imports, synthetic transport with no sockets."""
import os
from pathlib import Path
import sys
PACKAGE=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(PACKAGE),str(PACKAGE/'src'),str(PACKAGE/'tests'),str(PACKAGE.parent/'local_conversion_mcp/src')]
from dev import offline_guard
sys.addaudithook(offline_guard)
from datetime import datetime
import json
from test_pilot_download import (PilotReadSession, PilotIntake, PilotScope, Registry,
                                 Runtime, DEFAULTS, WORKTREE, CONTENT, download_response)
c=json.loads(Path(sys.argv[1]).read_text())
session=PilotReadSession(Path(c['root']),c['approval_sha'],
    runner=lambda control:download_response(control,CONTENT),checked_source=lambda:c['frozen'],
    clock=lambda:datetime.fromisoformat(c['now']))
flow=PilotIntake(Registry(Runtime(WORKTREE,DEFAULTS,Path(c['runtime']))),
                 PilotScope('cli_synthetic','oc_synthetic','ou_synthetic'),max_attachments=1,max_pending=3)
flow.staging.hook=lambda point:os._exit(87) if point=='after_baseline_commit' else None
session.receive_file('scan_synthetic','om_synthetic',flow,sample_path=Path(c['sample']))
