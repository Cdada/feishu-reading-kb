"""Synthetic process boundary used only by offline conversion recovery tests."""
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch

PACKAGE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(PACKAGE / 'src'), str(PACKAGE.parent / 'local_conversion_mcp/src'), str(PACKAGE)]
from dev import offline_guard
from kb_custody_v21.conversion import CustodyConversion
from kb_custody_v21.registry import Registry
from kb_custody_v21.runtime import Runtime, DEFAULTS

sys.addaudithook(offline_guard)
if sys.argv[1] == 'stage-child':
    marker = Path(sys.argv[2])
    marker.write_text('ready')
    deadline = time.monotonic() + 20
    while not marker.with_suffix('.release').exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    sys.exit(0)

worktree, root, mode = sys.argv[1:]
conversion = CustodyConversion(Registry(Runtime(worktree, DEFAULTS, root)))

def stop(*args, **kwargs):
    print('leased', flush=True)
    if mode == 'hold':
        sys.stdin.readline()
    os._exit(73)

if mode in {'hold', 'die'}:
    with patch.object(conversion.worker, '_run_stage', side_effect=stop):
        conversion.worker.run_once()
elif mode == 'terminal-gap':
    with patch.object(conversion.recovery, 'release_finished', side_effect=lambda *_: os._exit(74)):
        conversion.worker.run_once()
elif mode == 'child':
    original = conversion.worker._run_stage
    def stage(task_id, stage, command, log_path, **kwargs):
        return original(task_id, stage, [sys.executable, __file__, 'stage-child',
                                        str(Path(root) / 'stage-child.ready')], log_path, **kwargs)
    with patch.object(conversion.worker, '_run_stage', side_effect=stage):
        conversion.worker.run_once()
else:
    raise ValueError(mode)
