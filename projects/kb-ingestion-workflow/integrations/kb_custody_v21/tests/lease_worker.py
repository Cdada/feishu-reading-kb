"""Hold a real local asset/cache lease until the test kills this process."""
from pathlib import Path
import sys
from unittest.mock import Mock

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / 'src'))
sys.path.insert(0, str(PACKAGE.parent / 'local_conversion_mcp/src'))
sys.path.insert(0, str(PACKAGE))
from dev import offline_guard
from kb_custody_v21.runtime import Runtime, DEFAULTS
from kb_custody_v21.registry import Registry
from kb_custody_v21.staging import Staging
from kb_custody_v21.archive import Archive
from kb_custody_v21.baidu import BaiduAdapter
from kb_custody_v21.retention import AssetCache

worktree, root, kind, identifier = sys.argv[1:]
sys.addaudithook(offline_guard)
registry = Registry(Runtime(worktree, DEFAULTS, root))
if kind == 'staging':
    binding = Staging(registry).input_binding(identifier, 'synthetic-active-task')
elif kind == 'cache':
    transport = Mock(is_mock=True)
    transport.request.side_effect = AssertionError('lease test must never download')
    archive = Archive(registry, BaiduAdapter(transport, 'synthetic-account', '/apps/synthetic/test'))
    binding = AssetCache(archive).acquire(identifier, None, 'synthetic-cache-use')
else:
    raise ValueError(kind)
with binding:
    print('leased', flush=True)
    sys.stdin.readline()
