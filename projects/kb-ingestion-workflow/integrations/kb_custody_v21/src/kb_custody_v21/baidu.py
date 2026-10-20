"""Baidu wire contract. DEV accepts an injected mock transport only; no secrets.

Simple transports have one physical request per call. Composed transports use
ArchiveSession.physical_request for every HTTP attempt, including authentication.
The transport owns authentication and streaming; this module never persists a
signed URL. A real HTTP composition remains gated by S12 validation/authorization.
"""
import hashlib
import json
from contextlib import contextmanager
from pathlib import PurePosixPath
import re
from threading import Lock

from .contracts import ContractError

DETAIL_ENDPOINT = '/rest/2.0/xpan/multimedia?method=filemetas'
CHUNK = 4 * 1024 * 1024


def remote_path(value, root):
    if not isinstance(value, str) or not value.startswith('/') or any(c in value for c in ('\\', '\x00', '?', '#')):
        raise ContractError('invalid remote path')
    p, r = PurePosixPath(value), PurePosixPath(root)
    if '..' in p.parts or str(p) != value or not p.is_relative_to(r):
        raise ContractError('remote path outside authorized root')
    return value


def fs_id(value):
    # Floats may have already lost uint64 precision; never cast them silently.
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str) or not re.fullmatch('[1-9][0-9]*', value) or int(value) >= 2 ** 64:
        raise ContractError('invalid uint64 fs_id')
    return value


class RemoteFailure(ContractError):
    pass


class RemoteMissing(RemoteFailure):
    pass


class ResponseValidationError(ContractError):
    """A complete HTTP response was recorded, but subsequent checks rejected it.

    This proves receipt only, never whether a remote write succeeded or failed.
    """


class BaiduAdapter:
    validation_mode = 'offline_mock'

    def __init__(self, transport, account_ref, root, md5_evidence=None):
        if getattr(transport, 'is_mock', False) is not True:
            raise ContractError('real Baidu transport requires separate S12 composition')
        self._initialize(transport, account_ref, root, md5_evidence)

    def _initialize(self, transport, account_ref, root, md5_evidence=None):
        if not root.startswith('/apps/') or len(PurePosixPath(root).parts) < 4:
            raise ContractError('dedicated application test directory required')
        self.transport, self.account_ref, self.root = transport, account_ref, remote_path(root, root)
        # A capability receipt binds the exact endpoint and semantics. Synthetic
        # evidence is explicitly marked and cannot authorize real cleanup.
        self.md5_evidence = md5_evidence
        self.request = None
        self._request_lock = Lock()

    @contextmanager
    def bound_request(self, request):
        # One adapter cannot attribute a concurrent request to another window.
        if not self._request_lock.acquire(blocking=False):
            raise ContractError('adapter already serving a bounded session')
        try:
            self.request = request
            yield
        finally:
            self.request = None
            self._request_lock.release()

    def _call(self, method, payload):
        if self.request is None:
            raise ContractError('manual bounded archive session required')
        result = self.request(method, payload)
        if not isinstance(result, dict) or type(result.get('errno')) is not int or result['errno'] != 0:
            raise RemoteFailure('remote business error')
        return result

    def refresh(self, target):
        remote_path(target, self.root)
        state = self._call('preflight', {'account_ref': self.account_ref, 'root': self.root, 'target': target})
        if (state.get('account_ref') != self.account_ref or state.get('root') != self.root or
                state.get('writable') is not True or type(state.get('free_bytes')) is not int):
            raise RemoteFailure('account/root/capacity preconditions unverified')
        return state

    def upload(self, stream, identity, target):
        remote_path(target, self.root)
        blocks = []
        while chunk := stream.read(CHUNK):
            blocks.append(hashlib.md5(chunk).hexdigest())
        if not blocks:
            raise ContractError('zero-byte upload contract not yet verified')
        stream.seek(0)
        base = {'path': target, 'size': identity['size_bytes'], 'isdir': 0,
                'block_list': json.dumps(blocks), 'rtype': 0, 'is_revision': 0}
        pre = self._call('precreate', {**base, 'autoinit': 1})
        requested = pre.get('block_list')
        if pre.get('return_type') == 2:
            # Official contract: precreate may itself have completed instant
            # upload. Do not create again without a verified locator/recovery.
            raise RemoteFailure('instant upload requires explicit locator verification; create stopped')
        if (type(pre.get('return_type')) is not int or pre['return_type'] != 1 or not isinstance(pre.get('uploadid'), str)
                or not pre['uploadid'] or not isinstance(requested, list)
                or any(type(i) is not int or not 0 <= i < len(blocks) for i in requested)
                or len(set(requested)) != len(requested)):
            raise RemoteFailure('invalid precreate response')
        for i in requested:
            stream.seek(i * CHUNK)
            result = self._call('slice', {'path': target, 'uploadid': pre['uploadid'],
                'partseq': i, 'type': 'tmpfile', 'file': stream.read(CHUNK)})
            if result.get('md5') != blocks[i]:
                raise RemoteFailure('chunk hash mismatch')
        result = self._call('create', {**base, 'uploadid': pre['uploadid']})
        # Create may omit isdir; the normal Archive path immediately queries
        # filemetas before verifying or persisting an archived-success state.
        return self.object(result, expected_path=target, require_file_type=False)

    def object(self, value, expected_path=None, *, require_file_type=True):
        result = {'fs_id': fs_id(value.get('fs_id')), 'path': remote_path(value.get('path'), self.root),
                  'size': value.get('size'), 'md5': value.get('md5')}
        if ((require_file_type or 'isdir' in value) and
                (type(value.get('isdir')) is not int or value['isdir'] != 0)):
            raise RemoteFailure('invalid file type')
        if type(result['size']) is not int or result['size'] < 0:
            raise RemoteFailure('invalid file detail')
        if expected_path and result['path'] != expected_path:
            raise RemoteFailure('server renamed/overwrote unexpectedly')
        if result['md5'] is not None and not re.fullmatch('[0-9a-fA-F]{32}', result['md5']):
            raise RemoteFailure('invalid remote hash')
        return result

    def detail(self, identifier):
        result = self._call('filemetas', {'fsids': json.dumps([int(fs_id(identifier))]), 'dlink': 0})
        rows = result.get('list')
        if rows == []:
            raise RemoteMissing('asset missing')
        if not isinstance(rows, list) or len(rows) != 1:
            raise RemoteFailure('asset missing or ambiguous')
        item = self.object(rows[0])
        if item['fs_id'] != fs_id(identifier):
            raise RemoteFailure('returned a different fs_id')
        return item

    def stream(self, identifier):
        # Contract transport must return a fresh stream for this fs_id; signed
        # URLs exist only inside transport, never in Registry/operation results.
        result = self._call('download', {'fs_id': fs_id(identifier)})
        return result['stream']

    def rename(self, identifier, old_path, target):
        remote_path(old_path, self.root)
        remote_path(target, self.root)
        result = self._call('filemanager', {'opera': 'move', 'async': 0, 'ondup': 'fail',
            'filelist': json.dumps([{'path': old_path, 'dest': str(PurePosixPath(target).parent),
                                    'newname': PurePosixPath(target).name, 'ondup': 'fail'}])})
        if result.get('taskid'):
            raise RemoteFailure('asynchronous acceptance is not completion')
        rows = result.get('info')
        if not isinstance(rows, list) or len(rows) != 1 or rows[0].get('errno') != 0:
            raise RemoteFailure('move item not confirmed')
        return self.detail(identifier)

    def mkdir(self, target):
        remote_path(target, self.root)
        result = self._call('mkdir', {'path': target, 'isdir': 1, 'rtype': 0})
        if result.get('path') != target or result.get('isdir') != 1:
            raise RemoteFailure('directory creation not confirmed')
        return {'path': target}

    def candidates(self, locator, planned):
        # Each tier is read-only, fully paginated, and stops at the first tier
        # with candidates. Multiple candidates are NOT arbitrarily selected.
        paths = {p for p in (locator.get('current_path'), locator.get('path_snapshot')) if p}
        planned = locator.get('planned_path') or planned
        known_paths = paths | {planned}
        for path in known_paths:
            remote_path(path, self.root)
        names = {PurePosixPath(p).name for p in known_paths}
        directory = PurePosixPath(planned).parent
        tiers = [('paths', sorted(paths)),
                 ('planned_directory', [str(PurePosixPath(planned).parent)]),
                 ('prefix', [locator['asset_id'][:20]]), ('list', [self.root])]
        for tier, queries in tiers:
            if not queries:
                continue
            found, cursor, seen = {}, None, set()
            while True:
                result = self._call('recover_candidates', {'tier': tier, 'queries': queries, 'cursor': cursor})
                if not isinstance(result.get('list'), list):
                    raise RemoteFailure('incomplete candidate response')
                for value in result['list']:
                    item = self.object(value)
                    path = PurePosixPath(item['path'])
                    # Custom names are accepted by upload/rename. Exact known
                    # paths and names must also remain recovery candidates.
                    # Names/prefixes only locate: Archive still requires one
                    # candidate and a full streamed SHA-256 before adoption.
                    full_prefix = path.name.startswith(locator['asset_id'] + '-')
                    matches = (item['path'] in paths if tier == 'paths' else
                               path.parent == directory and (path.name in names or full_prefix)
                               if tier == 'planned_directory' else full_prefix if tier == 'prefix'
                               else path.name in names or full_prefix)
                    if matches:
                        found[item['fs_id']] = item
                cursor = result.get('next_cursor')
                if cursor is None:
                    break
                if not isinstance(cursor, str) or cursor in seen:
                    raise RemoteFailure('invalid/incomplete pagination')
                seen.add(cursor)
            if found:
                return list(found.values())
        return []
