"""Reuse Archive and its injected mock transport; die after durable remote create."""
import hashlib
import json
import os
from pathlib import Path
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / 'src'))
sys.path.insert(0, str(PACKAGE))
from dev import offline_guard
from kb_custody_v21.archive import Archive
from kb_custody_v21.baidu import BaiduAdapter
from kb_custody_v21.registry import Registry
from kb_custody_v21.runtime import Runtime, DEFAULTS

worktree, root, operation_id = sys.argv[1:4]
protocol_mode = sys.argv[4:] in (['protocol'], ['protocol-bounded'])
sys.addaudithook(offline_guard)
registry = Registry(Runtime(worktree, DEFAULTS, root))
transport = Mock(is_mock=True)
adapter = BaiduAdapter(transport, '1234567' if protocol_mode else 'synthetic-account', '/apps/synthetic/test')
archive = Archive(registry, adapter)
plan = json.loads(archive.operation(operation_id)['plan_json'])
remote = Path(root) / 'test-output/remote-created.bin'
calls = Path(root) / 'test-output/remote-calls.jsonl'
parts = []

def request(method, payload):
    with calls.open('a') as stream:
        stream.write(json.dumps({'method': method}) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    if method == 'preflight':
        return dict(errno=0, account_ref=adapter.account_ref, root=adapter.root,
                    writable=True, free_bytes=10**8, target_exists=False)
    if method == 'precreate':
        return dict(errno=0, uploadid='synthetic-upload', return_type=1, block_list=[0])
    if method == 'slice':
        parts.append(payload['file'])
        return dict(errno=0, md5=hashlib.md5(payload['file']).hexdigest())
    if method == 'create':
        with remote.open('xb') as stream:
            stream.write(b''.join(parts))
            stream.flush()
            os.fsync(stream.fileno())
        # No Python finally blocks run: running intent and active lease survive.
        os._exit(73)
    raise AssertionError(method)

transport.request.side_effect = request
if protocol_mode:
    from email import policy
    from email.parser import BytesParser
    from kb_custody_v21.baidu_protocol import BaiduProtocolTransport, HttpResponse
    wire = Mock(is_mock=True)
    def exchange(req):
        if req.label in ('uinfo', 'quota', 'list'):
            value = {'uinfo': dict(errno=0, uk=1234567),
                     'quota': dict(errno=0, total=10**8, used=0),
                     'list': dict(errno=0, list=[])}[req.label]
        elif req.label == 'slice':
            msg = BytesParser(policy=policy.default).parsebytes(
                ('Content-Type: '+dict(req.headers)['Content-Type']+'\r\n\r\n').encode()+req.body)
            value = request('slice', {'file': list(msg.iter_parts())[0].get_payload(decode=True)})
        else:
            value = request(req.label, {})
        return HttpResponse(200, json.dumps(value).encode())
    wire.request.side_effect = exchange
    adapter = BaiduAdapter(BaiduProtocolTransport(wire, '1234567', adapter.root,
        plan['target'], token='synthetic-token', authorized_write=True), '1234567', adapter.root)
    archive = Archive(registry, adapter)
window_policy = {}
if sys.argv[4:] == ['protocol-bounded']:
    window_policy = dict(starts_at=(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat(),
        physical_limits=dict(uinfo=1,quota=1,list=1,precreate=1,slice=1,create=1,
                             filemetas=2,download_meta=1,download=1))
archive.session('synthetic-crash-window', [operation_id],
    (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), 10, **window_policy).execute(operation_id)
