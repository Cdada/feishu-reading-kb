"""Official Baidu HTTP contract composition, currently restricted to mock Wire.

This is not a live executor. No sockets, keychain access, refresh, retries or
redirect following. ArchiveSession supplies the durable per-HTTP request meter.
The bounded protocol supports one small file in one dedicated directory. An
explicit mock-only bootstrap can observe/bind a first account and create that
directory. Rename and unknown recovery remain outside this first experiment.
"""
from dataclasses import dataclass
import hashlib
import io
import json
from pathlib import PurePosixPath
import re
import secrets
from urllib.parse import parse_qsl, urlencode, urlsplit

from .baidu import CHUNK, RemoteFailure, fs_id, remote_path
from .contracts import ContractError

PAN = 'pan.baidu.com'
OAUTH = 'openapi.baidu.com'
PCS = 'd.pcs.baidu.com'
FILE = '/rest/2.0/xpan/file'
META = '/rest/2.0/xpan/multimedia'
JSON_LIMIT = 512 * 1024


@dataclass(frozen=True, repr=False)
class HttpRequest:
    label: str
    method: str
    host: str
    target: str
    headers: tuple
    body: bytes
    response_limit: int

    def __repr__(self):
        return f'HttpRequest({self.label}, {self.method}, sensitive fields omitted)'


@dataclass(frozen=True, repr=False)
class HttpResponse:
    status: int
    body: bytes


@dataclass(frozen=True, repr=False)
class AuthorizationCode:
    app_key: str
    secret_key: str
    code: str
    redirect_uri: str = 'oob'


def json_object(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise RemoteFailure('duplicate JSON field')
            result[key] = value
        return result
    try:
        result = json.loads(data, object_pairs_hook=unique)
    except RemoteFailure:
        raise
    except (ValueError, RecursionError):
        # Includes the interpreter's integer-digit limit and Unicode decoding.
        # Keep that limit enabled and never reflect provider input in the error.
        raise RemoteFailure('invalid HTTP JSON response') from None
    if not isinstance(result, dict):
        raise RemoteFailure('HTTP response must be a JSON object')
    return result


def create_observation(value, target, expected_size):
    """Record field states only; this is never permission to accept a file.

    Create replies can report business success while failing the subsequent
    identity contract. Keep enough diagnosis without retaining remote paths,
    identifiers, hashes, account/session fields or response bodies.
    """
    result = {}
    for key, expected in (('path', target), ('size', expected_size), ('isdir', 0)):
        result[key] = ('absent' if key not in value else
                       'invalid_type' if type(value[key]) is not type(expected) else
                       'match' if value[key] == expected else 'mismatch')
    identifier = value.get('fs_id')
    if 'fs_id' not in value:
        result['fs_id'] = 'absent'
    else:
        try:
            fs_id(identifier)
        except (ContractError, ValueError):
            result['fs_id'] = 'invalid'
        else:
            result['fs_id'] = 'valid_format'
    md5 = value.get('md5')
    result['md5'] = ('absent' if 'md5' not in value else 'null' if md5 is None else
                     'hex32' if isinstance(md5, str) and re.fullmatch('[0-9a-fA-F]{32}', md5) else 'invalid')
    return result


def slice_contract(value, expected_md5):
    """Allowlisted diagnosis and decision for PCS only; never retain raw text/IDs.

    The official SDK permits omitted numeric error fields for PCS. A matching
    chunk hash is still required here before normalizing to the adapter's
    internal errno=0 contract. Xpan endpoints keep their stricter errno check.
    """
    result = {}
    for field in ('errno', 'error_code', 'error_no'):
        if field not in value:
            result[field] = {'state': 'absent'}
        elif type(value[field]) is int and -(2**31) <= value[field] < 2**31:
            result[field] = dict(state='zero' if value[field] == 0 else 'nonzero', code=value[field])
        else:
            result[field] = {'state': 'invalid'}
    for field in ('error', 'error_msg', 'errmsg', 'error_description'):
        result[field] = ('absent' if field not in value else
                         'invalid' if not isinstance(value[field], str) else
                         'present' if value[field] else 'empty')
    md5 = value.get('md5')
    result['md5'] = ('absent' if 'md5' not in value else
                     'invalid' if not isinstance(md5, str) or not re.fullmatch('[0-9a-f]{32}', md5) else
                     'match' if md5 == expected_md5 else 'mismatch')
    result['accepted'] = (
        all(result[f]['state'] in ('absent', 'zero') for f in ('errno', 'error_code', 'error_no'))
        and all(result[f] in ('absent', 'empty') for f in ('error', 'error_msg', 'errmsg', 'error_description'))
        and result['md5'] == 'match')
    return result


class BaiduProtocolTransport:
    physical_request_composition = True
    is_mock = True

    def __init__(self, wire, account_ref, root, target, *, token=None,
                 authorization=None, authorized_write=False, bootstrap=False):
        if getattr(wire, 'is_mock', False) is not True:
            raise ContractError('live Baidu Wire requires separately approved executor')
        self._initialize(wire, account_ref, root, target, token=token,
                         authorization=authorization, authorized_write=authorized_write, bootstrap=bootstrap)

    def _initialize(self, wire, account_ref, root, target, *, token=None,
                    authorization=None, authorized_write=False, bootstrap=False):
        # Shared validation for the default mock factory and explicit B01
        # composition. The latter must check approval before reaching here.
        if not root.startswith('/apps/') or len(PurePosixPath(root).parts) < 4:
            raise ContractError('dedicated test directory required')
        if type(bootstrap) is not bool or (bootstrap and (account_ref is not None
                or len(PurePosixPath(root).parts) != 4)):
            raise ContractError('bootstrap requires unbound account and direct application child directory')
        self.wire, self.account_ref = wire, None if bootstrap else fs_id(account_ref)
        self.root, self.target = remote_path(root, root), remote_path(target, root)
        if str(PurePosixPath(target).parent) != root:
            raise ContractError('first protocol batch requires one direct child file')
        if (token is None) == (authorization is None):
            raise ContractError('provide exactly one token or authorization-code binding')
        if token is not None and (not isinstance(token, str) or not token):
            raise ContractError('invalid token binding')
        if authorization is not None and (not isinstance(authorization, AuthorizationCode) or
                any(not isinstance(v, str) or not v for v in (
                    authorization.app_key, authorization.secret_key, authorization.code,
                    authorization.redirect_uri))):
            raise ContractError('invalid authorization-code binding')
        self._token, self._authorization = token, authorization
        self.authorized_write = authorized_write is True
        self._upload = None
        self._objects = {}
        self._bootstrap = bootstrap
        self._observed_account = None
        self._identity_attempted = self._mkdir_attempted = False
        self._directory = None

    def request(self, method, payload):
        raise ContractError('use ArchiveSession physical accounting, not a direct request')

    def _exchange(self, count, label, method, host, path, query=(), body=b'',
                  content_type=None, *, limit=JSON_LIMIT, authenticated=True):
        query = list(query)
        if authenticated:
            if not self._token:
                raise ContractError('authentication required')
            query = [(key, value) for key, value in query if key != 'access_token']
            query.append(('access_token', self._token))
        target = path + (('?' + urlencode(query)) if query else '')
        headers = (('User-Agent', PAN),)
        if content_type:
            headers += (('Content-Type', content_type),)
        if method == 'POST':
            # PCS does not accept chunked multipart uploads.
            headers += (('Content-Length', str(len(body))),)
        req = HttpRequest(label, method, host, target, headers, body, limit)
        response = count(label, lambda: self.wire.request(req))
        if not isinstance(response, HttpResponse) or type(response.status) is not int:
            raise RemoteFailure('invalid wire response')
        if response.status != 200:
            # No response URL/body is reflected in errors. 3xx is NOT followed.
            raise RemoteFailure(f'HTTP status {response.status}; no automatic retry or redirect')
        if not isinstance(response.body, bytes) or len(response.body) > limit:
            raise RemoteFailure('HTTP response exceeds bounded byte contract')
        return response.body

    def _json(self, count, label, method, host, path, query=(), form=None, *, oauth=False):
        data = self._exchange(count, label, method, host, path, query,
            b'' if form is None else urlencode(form).encode(),
            None if form is None else 'application/x-www-form-urlencoded', authenticated=not oauth)
        result = json_object(data)
        if oauth:
            if 'error' in result:
                raise RemoteFailure('OAuth exchange not successful; no automatic retry')
        elif type(result.get('errno')) is not int or result['errno'] != 0:
            raise RemoteFailure('Baidu API did not confirm success')
        return result

    def _authenticate(self, count):
        if self._token:
            return
        binding = self._authorization
        result = self._json(count, 'oauth_token', 'GET', OAUTH, '/oauth/2.0/token', [
            ('grant_type', 'authorization_code'), ('code', binding.code),
            ('client_id', binding.app_key), ('client_secret', binding.secret_key),
            ('redirect_uri', binding.redirect_uri)], oauth=True)
        scope = result.get('scope')
        if (not isinstance(result.get('access_token'), str) or not result['access_token']
                or type(result.get('expires_in')) is not int or result['expires_in'] <= 0
                or not isinstance(scope, str) or set(re.split(r'[ ,]+', scope.strip())) != {'basic', 'netdisk'}):
            raise RemoteFailure('OAuth token/scope not bound to required contract')
        self._token = result['access_token']
        # Do not retain refresh/session tokens or refresh automatically.
        self._authorization = None

    def identify_account(self, count):
        """Observe identity once; an observation does not authorize any write."""
        if not self._bootstrap or self._identity_attempted:
            raise ContractError('one explicit first-account observation required')
        self._identity_attempted = True
        self._authenticate(count)
        result = self._json(count, 'uinfo', 'GET', PAN, '/rest/2.0/xpan/nas', [('method', 'uinfo')])
        self._observed_account = fs_id(result.get('uk'))
        return self._observed_account

    def bind_account(self, expected):
        """Caller must obtain the operator's exact identity check before this."""
        expected = fs_id(expected)
        if not self._bootstrap or self._observed_account != expected:
            raise ContractError('first account differs from explicitly checked identity')
        if self.account_ref is not None and self.account_ref != expected:
            raise ContractError('first account binding is immutable')
        self.account_ref = expected

    def create_test_directory(self, count):
        if (not self._bootstrap or self.account_ref is None or not self.authorized_write
                or self._mkdir_attempted):
            raise ContractError('bound account and one approved directory attempt required')
        # No automatic reuse, rename, or retry after -8, timeout, or malformed
        # receipt. The eventual executor must also persist this intent so that
        # restarting this in-memory object cannot repeat an uncertain write.
        self._mkdir_attempted = True
        result = self._json(count, 'mkdir', 'POST', PAN, FILE, [('method', 'create')],
            {'path': self.root, 'isdir': 1, 'rtype': 0})
        if (result.get('path') != self.root or type(result.get('isdir')) is not int
                or result['isdir'] != 1):
            raise RemoteFailure('directory receipt differs from fixed test directory')
        self._directory = dict(fs_id=fs_id(result.get('fs_id')), path=self.root)
        return dict(self._directory)

    def _preflight(self, payload, count):
        if self._bootstrap and (self.account_ref is None or self._directory is None):
            raise ContractError('first account and dedicated directory are not bound')
        if (payload.get('account_ref'), payload.get('root'), payload.get('target')) != (
                self.account_ref, self.root, self.target):
            raise ContractError('preflight object outside bound scope')
        self._authenticate(count)
        user = self._json(count, 'uinfo', 'GET', PAN, '/rest/2.0/xpan/nas', [('method', 'uinfo')])
        if fs_id(user.get('uk')) != self.account_ref:
            raise RemoteFailure('authenticated account differs from bound account')
        quota = self._json(count, 'quota', 'GET', PAN, '/api/quota')
        total, used = quota.get('total'), quota.get('used')
        if type(total) is not int or type(used) is not int or not 0 <= used <= total:
            raise RemoteFailure('capacity not verified')
        exists, seen = False, set()
        for page in range(10):
            listing = self._json(count, 'list', 'GET', PAN, FILE, [
                ('method', 'list'), ('dir', self.root), ('start', page * 100),
                ('limit', 100), ('order', 'name'), ('desc', 0)])
            rows = listing.get('list')
            if not isinstance(rows, list) or len(rows) > 100:
                raise RemoteFailure('invalid directory page')
            for row in rows:
                if not isinstance(row, dict):
                    raise RemoteFailure('invalid directory row')
                path = remote_path(row.get('path'), self.root)
                identifier = fs_id(row.get('fs_id'))
                if str(PurePosixPath(path).parent) != self.root or identifier in seen:
                    raise RemoteFailure('incomplete or out-of-scope directory listing')
                seen.add(identifier)
                exists |= path == self.target
            if len(rows) < 100:
                return dict(errno=0, account_ref=self.account_ref, root=self.root,
                    free_bytes=total-used, target_exists=exists,
                    writable=self.authorized_write,
                    platform_write_permission='NOT_VERIFIED')
        raise RemoteFailure('directory listing exceeded bounded pages; absence not proven')

    def _form(self, payload, create=False):
        if self._bootstrap and (self.account_ref is None or self._directory is None):
            raise ContractError('first account and dedicated directory are not bound')
        if not self.authorized_write or payload.get('path') != self.target:
            raise ContractError('write outside confirmed file scope')
        if any(type(payload.get(k)) is not int or payload[k] != 0 for k in ('rtype', 'isdir', 'is_revision')):
            raise ContractError('overwrite/rename/revision flags forbidden')
        if type(payload.get('size')) is not int or not 0 < payload['size'] <= CHUNK:
            raise ContractError('first protocol batch supports at most one nonempty 4 MiB block')
        try:
            blocks = json.loads(payload.get('block_list', ''))
        except (TypeError, ValueError):
            raise ContractError('invalid block hashes') from None
        if not isinstance(blocks, list) or len(blocks) != 1 or not isinstance(blocks[0], str) or not re.fullmatch('[0-9a-f]{32}', blocks[0]):
            raise ContractError('invalid block hashes')
        form = {k: payload[k] for k in ('path', 'size', 'isdir', 'rtype', 'is_revision', 'block_list')}
        if create:
            if not self._upload or (form, payload.get('uploadid')) != self._upload:
                raise ContractError('create differs from successful precreate')
            form['uploadid'] = payload['uploadid']
        else:
            if payload.get('autoinit') != 1:
                raise ContractError('precreate requires autoinit=1')
            form['autoinit'] = 1
        return form

    def _meta(self, count, identifier, dlink):
        identifier = fs_id(identifier)
        if identifier not in self._objects:
            raise ContractError('file detail outside confirmed successful create')
        result = self._json(count, 'download_meta' if dlink else 'filemetas', 'GET', PAN, META,
            [('method', 'filemetas'), ('fsids', json.dumps([int(identifier)])), ('dlink', int(dlink))])
        rows = result.get('list')
        if (not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict)
                or fs_id(rows[0].get('fs_id')) != identifier
                or (rows[0].get('path'), rows[0].get('size')) != self._objects[identifier]
                or type(rows[0].get('size')) is not int or type(rows[0].get('isdir')) is not int
                or rows[0]['isdir'] != 0):
            raise RemoteFailure('file detail differs from created identity')
        return result

    def compose(self, method, payload, count):
        if method == 'preflight':
            return self._preflight(payload, count)
        if method == 'precreate':
            form = self._form(payload)
            result = self._json(count, 'precreate', 'POST', PAN, FILE, [('method', 'precreate')], form)
            if result.get('return_type') == 1 and isinstance(result.get('uploadid'), str) and result['uploadid']:
                self._upload = ({k: v for k, v in form.items() if k != 'autoinit'}, result['uploadid'])
            return result
        if method == 'slice':
            if (not self._upload or payload.get('path') != self.target or payload.get('uploadid') != self._upload[1]
                    or type(payload.get('partseq')) is not int or payload['partseq'] != 0
                    or payload.get('type') != 'tmpfile' or not isinstance(payload.get('file'), bytes)):
                raise ContractError('slice outside successful precreate')
            content = payload['file']
            if len(content) != self._upload[0]['size'] or hashlib.md5(content).hexdigest() != json.loads(self._upload[0]['block_list'])[0]:
                raise ContractError('slice differs from original bytes')
            boundary = 'kb-' + secrets.token_hex(24)
            body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="file"\r\n'
                    'Content-Type: application/octet-stream\r\n\r\n').encode() + content + f'\r\n--{boundary}--\r\n'.encode()
            data = self._exchange(count, 'slice', 'POST', PCS, '/rest/2.0/pcs/superfile2', [
                ('method', 'upload'), ('type', 'tmpfile'), ('path', self.target),
                ('uploadid', self._upload[1]), ('partseq', 0)], body, 'multipart/form-data; boundary=' + boundary)
            result = json_object(data)
            if not slice_contract(result, hashlib.md5(content).hexdigest())['accepted']:
                raise RemoteFailure('slice error fields or chunk MD5 did not confirm success')
            # Normalize only this validated PCS response. Do not carry arbitrary
            # provider fields into downstream receipts or weaken Xpan parsing.
            return {'errno': 0, 'md5': result['md5']}
        if method == 'create':
            result = self._json(count, 'create', 'POST', PAN, FILE, [('method', 'create')], self._form(payload, True))
            if (result.get('path') != self.target or type(result.get('size')) is not int
                    or result['size'] != self._upload[0]['size']
                    or ('isdir' in result and (type(result['isdir']) is not int or result['isdir'] != 0))):
                raise RemoteFailure('created file differs from upload intent')
            # Official SDK tests accept sparse create receipts. This binds a
            # locator only: _meta must still observe explicit isdir=0 before
            # download, and Archive requires its independent content check.
            # Do not invent a missing provider field in the returned receipt.
            self._objects[fs_id(result.get('fs_id'))] = (self.target, result['size'])
            return result
        if method == 'filemetas':
            try:
                identifiers = json.loads(payload.get('fsids', ''))
            except (TypeError, ValueError):
                raise ContractError('invalid file detail request') from None
            if not isinstance(identifiers, list) or len(identifiers) != 1 or payload.get('dlink') != 0:
                raise ContractError('one bound file detail required')
            result = self._meta(count, identifiers[0], False)
            return {'errno': 0, 'list': [{k: v for k, v in result['list'][0].items() if k != 'dlink'}]}
        if method == 'download':
            item = self._meta(count, payload.get('fs_id'), True)['list'][0]
            link = item.get('dlink')
            if not isinstance(link, str) or any(c.isspace() or ord(c) < 32 for c in link):
                raise RemoteFailure('invalid signed download link')
            try:
                parsed = urlsplit(link)
            except ValueError:
                raise RemoteFailure('invalid signed download link') from None
            if (parsed.scheme != 'https' or parsed.netloc != PCS or parsed.fragment
                    or not parsed.path.startswith('/file/')):
                raise RemoteFailure('download destination outside reviewed HTTPS host')
            # Official SDK token transport also authenticates downloads. Keep
            # signed query values, replace stale tokens, and never log the URL.
            # Host validation above prevents sending credentials elsewhere.
            data = self._exchange(count, 'download', 'GET', PCS,
                parsed.path, parse_qsl(parsed.query, keep_blank_values=True),
                limit=item['size'])
            if len(data) != item['size']:
                raise RemoteFailure('download length differs from bound file')
            return {'errno': 0, 'stream': io.BytesIO(data)}
        raise ContractError('operation not enabled in first Baidu protocol batch')
