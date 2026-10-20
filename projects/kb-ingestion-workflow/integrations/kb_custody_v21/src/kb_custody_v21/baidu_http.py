"""Bounded one-request HTTPS primitive for a future approved Baidu executor.

No CLI, credential loading, retries, redirects or import-time connections. The
default Archive/BaiduProtocol DEV factories still refuse this non-mock wire.
The caller must supply an authorization check that runs before every connection.
"""
import http.client
import ssl
from urllib.parse import parse_qs, urlsplit

from .baidu import CHUNK
from .baidu_protocol import HttpRequest, HttpResponse, JSON_LIMIT, OAUTH, PAN, PCS, FILE, META
from .contracts import ContractError

ROUTES = {
    'oauth_token': ('GET', OAUTH, '/oauth/2.0/token', None),
    'oauth_refresh': ('GET', OAUTH, '/oauth/2.0/token', None),
    'uinfo': ('GET', PAN, '/rest/2.0/xpan/nas', 'uinfo'),
    'quota': ('GET', PAN, '/api/quota', None),
    'list': ('GET', PAN, FILE, 'list'),
    'mkdir': ('POST', PAN, FILE, 'create'),
    'precreate': ('POST', PAN, FILE, 'precreate'),
    'slice': ('POST', PCS, '/rest/2.0/pcs/superfile2', 'upload'),
    'create': ('POST', PAN, FILE, 'create'),
    'filemetas': ('GET', PAN, META, 'filemetas'),
    'download_meta': ('GET', PAN, META, 'filemetas'),
    'download': ('GET', PCS, '/file/', None),
}


class BaiduHttpsWire:
    is_mock = False

    def __init__(self, before_connect):
        if not callable(before_connect):
            raise ContractError('per-request authorization check required')
        self.before_connect = before_connect

    def request(self, request):
        self._validate(request)
        # Approval/source/window checks belong to the explicit executor. A
        # failed check must happen before DNS, TLS, or request bytes are sent.
        if self.before_connect(request) is not True:
            raise ContractError('per-request authorization did not explicitly confirm')
        connection = http.client.HTTPSConnection(request.host, timeout=20,
                                                 context=ssl.create_default_context())
        try:
            # Local TLS/context preparation can consume the remaining window.
            if self.before_connect(request) is not True:
                raise ContractError('per-request authorization did not explicitly confirm')
            connection.request(request.method, request.target, body=request.body,
                               headers=dict(request.headers))
            response = connection.getresponse()
            if type(response.status) is not int:
                raise OSError('Baidu HTTP status invalid')
            if response.status != 200:
                # Error/redirect bodies may contain secrets or be unbounded.
                # Preserve the status only; Protocol rejects it, without retry.
                return HttpResponse(response.status, b'')
            headers = response.getheaders()
            lengths = [v for k, v in headers if k.lower() == 'content-length']
            if len(lengths) > 1 or any(not v.isascii() or not v.isdecimal() for v in lengths):
                raise OSError('Baidu response length invalid')
            expected = int(lengths[0]) if lengths else None
            transfers = [v.lower() for k, v in headers if k.lower() == 'transfer-encoding']
            if transfers and (transfers != ['chunked'] or lengths):
                raise OSError('Baidu response transfer framing ambiguous or unsupported')
            if expected is not None and expected > request.response_limit:
                raise OSError('Baidu response exceeds bounded byte contract')
            encodings = [v.lower() for k, v in headers if k.lower() == 'content-encoding']
            if encodings and encodings != ['identity']:
                raise OSError('Baidu compressed response not enabled')
            data = bytearray()
            while True:
                chunk = response.read(min(65536, request.response_limit + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > request.response_limit:
                    raise OSError('Baidu response exceeds bounded byte contract')
            if expected is not None and len(data) != expected:
                raise OSError('Baidu response truncated or length inconsistent')
            return HttpResponse(response.status, bytes(data))
        except (OSError, http.client.HTTPException):
            # Low-level errors may echo a target containing tokens. Preserve
            # uncertainty with an OSError, but never reflect provider text.
            raise OSError('Baidu HTTPS exchange failed; no automatic retry') from None
        finally:
            connection.close()

    @staticmethod
    def _validate(request):
        if not isinstance(request, HttpRequest) or request.label not in ROUTES:
            raise ContractError('unreviewed Baidu HTTP request')
        method, host, path, operation = ROUTES[request.label]
        if (request.method, request.host) != (method, host):
            raise ContractError('HTTP method/host outside reviewed route')
        if (not isinstance(request.target, str) or not request.target.isascii()
                or any(ord(c) <= 32 or ord(c) == 127 for c in request.target)):
            raise ContractError('invalid HTTP target')
        parsed = urlsplit(request.target)
        if parsed.scheme or parsed.netloc or parsed.fragment or not parsed.path.startswith('/'):
            raise ContractError('relative HTTP request target required')
        if ((request.label == 'download' and not parsed.path.startswith(path))
                or (request.label != 'download' and parsed.path != path)):
            raise ContractError('HTTP path outside reviewed route')
        query = parse_qs(parsed.query, keep_blank_values=True)
        if request.label == 'oauth_refresh' and (
                set(query) != {'grant_type', 'client_id', 'client_secret', 'refresh_token'}
                or query['grant_type'] != ['refresh_token']
                or any(len(v) != 1 or not v[0] for v in query.values())):
            raise ContractError('OAuth refresh query outside reviewed contract')
        if operation is not None and query.get('method') != [operation]:
            raise ContractError('HTTP API operation differs from reviewed route')
        if not isinstance(request.body, bytes) or len(request.body) > CHUNK + 4096:
            raise ContractError('HTTP request body exceeds bounded upload')
        if method == 'GET' and request.body:
            raise ContractError('GET body forbidden')
        maximum = CHUNK if request.label == 'download' else JSON_LIMIT
        if type(request.response_limit) is not int or not 0 < request.response_limit <= maximum:
            raise ContractError('bounded response limit required')
        headers = dict(request.headers)
        if (len(headers) != len(request.headers) or any(k not in {'User-Agent', 'Content-Type', 'Content-Length'} for k in headers)
                or headers.get('User-Agent') != PAN
                or any(not isinstance(v, str) or not v.isascii() or '\r' in v or '\n' in v for v in headers.values())):
            raise ContractError('HTTP headers outside reviewed contract')
        if method == 'POST' and (headers.get('Content-Length') != str(len(request.body)) or not headers.get('Content-Type')):
            raise ContractError('POST exact length and content type required')
