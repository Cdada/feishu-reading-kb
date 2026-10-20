"""Hard process death after a synthetic remote side effect, never a platform."""
import json
import os
from pathlib import Path
import sys
from unittest.mock import Mock

PACKAGE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(PACKAGE), str(PACKAGE/'src'), str(PACKAGE.parent/'local_conversion_mcp/src')]
from dev import offline_guard
sys.addaudithook(offline_guard)
from kb_custody_v21.b01 import BaiduExperiment, prepare
from b01_fixture import callbacks, steps

worktree, root, phase = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
registry, _ = prepare(worktree, root=root)
a = json.loads((root/'approval-fixture.json').read_text())
responses = iter(steps(a))
def request(req):
    name, reply = next(responses)
    assert name == req.label
    if name == phase:
        with (root/'synthetic-remote-effect').open('x') as output:
            output.write(name)
            output.flush()
            os.fsync(output.fileno())
        os._exit(74)
    return reply
BaiduExperiment(registry, a, checked_source=lambda: True,
    wire=Mock(is_mock=True, request=request)).run(**callbacks())
raise SystemExit('expected injected hard exit')
