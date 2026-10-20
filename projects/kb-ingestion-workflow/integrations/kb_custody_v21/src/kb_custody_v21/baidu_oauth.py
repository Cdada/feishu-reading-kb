"""Reusable OAuth composition, offline factory only; no file-operation authority.

The future approved executor supplies durable physical-request accounting and a
source/identity/window check. Neither a saved token nor the check callback grants
permission. Existing consumed B01/B02/B03 executors are intentionally unchanged.
"""
from dataclasses import dataclass, asdict
import hashlib
import json
import re
import time
from urllib.parse import urlencode

from .baidu import fs_id
from .baidu_credentials import ApplicationCredentials, application_identity, MAX_RECORD
from .baidu_protocol import HttpRequest, HttpResponse, OAUTH, PAN, json_object
from .contracts import ContractError


def secret(value):
    if (not isinstance(value, str) or not 8 <= len(value) <= 2048
            or not value.isascii() or any(c.isspace() or not c.isprintable() for c in value)):
        raise ContractError('OAuth sensitive field has invalid format')
    return value


def account_id(value):
    if isinstance(value, str) and len(value) > 20:
        raise ContractError('OAuth account identifier exceeds bounded contract')
    return fs_id(value)


@dataclass(frozen=True, repr=False)
class OAuthIdentity:
    app_id: str
    app_name: str
    account_ref: str
    app_key_sha256: str

    def __post_init__(self):
        application_identity(self.app_id, self.app_name)
        if (not isinstance(self.account_ref, str) or account_id(self.account_ref) != self.account_ref
                or not isinstance(self.app_key_sha256, str)
                or not re.fullmatch('[0-9a-f]{64}', self.app_key_sha256)):
            raise ContractError('exact OAuth application/account binding required')

    @property
    def key(self):
        return hashlib.sha256(encode(asdict(self))).hexdigest()


def encode(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode()


def parse_record(data, identity):
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_RECORD:
        raise ContractError('OAuth record exceeds bounded contract')
    value = json_object(data)
    common = {'schema_version', 'identity', 'state'}
    state = value.get('state')
    fields = common | ({'access_token', 'refresh_token', 'issued_at', 'expires_at'} if state == 'ready' else set())
    if (set(value) != fields or type(value.get('schema_version')) is not int
            or value['schema_version'] != 1 or value.get('identity') != asdict(identity)
            or state not in ('ready', 'enrollment_pending', 'refresh_pending')):
        raise ContractError('OAuth record identity or schema differs')
    if state == 'ready':
        secret(value['access_token']); secret(value['refresh_token'])
        if (type(value['issued_at']) is not int or type(value['expires_at']) is not int
                or not 0 <= value['issued_at'] < value['expires_at'] <= 2**53):
            raise ContractError('OAuth record lifetime invalid')
    return value


class OAuthSession:
    """One bounded attempt; a new session cannot bypass a persistent pending record.

    Store contract: exclusive transaction, bounded read, compare-and-swap plus
    verified persistence. Pending replaces tokens *before* the exchange. Thus a
    crash cannot silently reuse an old refresh token after possible rotation.
    """
    def __init__(self, identity, store, wire, *, check, count, clock=time.time):
        if getattr(store, 'is_mock', False) is not True or getattr(wire, 'is_mock', False) is not True:
            raise ContractError('live reusable OAuth requires a separately approved executor')
        self._initialize(identity, store, wire, check=check, count=count, clock=clock)

    def _initialize(self, identity, store, wire, *, check, count, clock):
        if not isinstance(identity, OAuthIdentity) or store.identity != identity:
            raise ContractError('OAuth store identity differs')
        if not all(callable(x) for x in (check, count, clock)):
            raise ContractError('OAuth authorization and physical accounting required')
        self.identity, self.store, self.wire = identity, store, wire
        self.check, self.count, self.clock = check, count, clock
        self.used = False

    def _check(self):
        if self.check() is not True:
            raise ContractError('OAuth scope, source or window check failed')

    def _now(self):
        value = self.clock()
        if type(value) not in (int, float) or not 0 <= value < 2**52:
            raise ContractError('OAuth clock invalid')
        return int(value)

    def _request(self, label, host, path, query):
        self._check()
        request = HttpRequest(label, 'GET', host, path + '?' + urlencode(query),
                              (('User-Agent', PAN),), b'', MAX_RECORD)
        # Meter must persist intent before invoking wire and receipt afterward.
        # Errors never cause another connection here. Wire also checks at connect.
        try:
            result = self.count(label, lambda: self.wire.request(request))
        except OSError:
            raise ContractError('OAuth transport stopped; no automatic retry') from None
        self._check()
        if (not isinstance(result, HttpResponse) or type(result.status) is not int
                or result.status != 200 or not isinstance(result.body, bytes)
                or not 0 < len(result.body) <= MAX_RECORD):
            raise ContractError('OAuth HTTP response rejected; no automatic retry')
        value = json_object(result.body)
        if any(k in value for k in ('error', 'error_code', 'error_description')):
            raise ContractError('OAuth provider rejected request; reconnect requires explicit action')
        return value

    def _account(self, token):
        value = self._request('uinfo', PAN, '/rest/2.0/xpan/nas',
                              [('method', 'uinfo'), ('access_token', token)])
        if (type(value.get('errno')) is not int or value['errno'] != 0
                or account_id(value.get('uk')) != self.identity.account_ref):
            raise ContractError('OAuth authenticated account differs or query failed')

    def access_token(self, *, credentials=None, code=None):
        """Initial code once; later cached access or one renewal, then account check.

        credentials is lazy: cached reuse does not request AppKey/Secret again.
        No refresh is triggered by a business request, and no file is retried.
        """
        if self.used:
            raise ContractError('OAuth session already attempted')
        self.used = True
        self._check()
        with self.store.transaction():
            self._check()
            old = self.store.read()
            record = parse_record(old, self.identity) if old is not None else None
            if record is not None and record['state'] != 'ready':
                raise ContractError('OAuth previous exchange unresolved; explicit reconnect required')
            if record is not None and code is not None:
                raise ContractError('OAuth existing record cannot be replaced by a new code')
            now = self._now()
            if record is not None and now < record['issued_at']:
                raise ContractError('OAuth clock moved backwards; stop before request')
            if record is not None and record['expires_at'] > now + 60:
                self._account(record['access_token'])
                self._check()
                if self._now() + 60 >= record['expires_at'] or self._now() < record['issued_at']:
                    raise ContractError('OAuth cached token expired while checking account')
                return record['access_token']
            if not callable(credentials):
                raise ContractError('OAuth application credential reader required')
            self._check()
            app = credentials()
            self._check()
            if (not isinstance(app, ApplicationCredentials)
                    or hashlib.sha256(secret(app.app_key).encode()).hexdigest() != self.identity.app_key_sha256):
                raise ContractError('OAuth application credential differs from binding')
            secret(app.secret_key)
            if record is None:
                secret(code)
                label, state = 'oauth_token', 'enrollment_pending'
                query = [('grant_type', 'authorization_code'), ('code', code), ('redirect_uri', 'oob')]
            else:
                label, state = 'oauth_refresh', 'refresh_pending'
                query = [('grant_type', 'refresh_token'), ('refresh_token', record['refresh_token'])]
            query += [('client_id', app.app_key), ('client_secret', app.secret_key)]
            pending = encode(dict(schema_version=1, identity=asdict(self.identity), state=state))
            self._check()
            self.store.compare_and_swap(old, pending)
            started = self._now()
            value = self._request(label, OAUTH, '/oauth/2.0/token', query)
            scope, lifetime = value.get('scope'), value.get('expires_in')
            if (not isinstance(scope, str) or sorted(scope.split()) != ['basic', 'netdisk']
                    or type(lifetime) is not int or not 60 < lifetime < 2**31):
                raise ContractError('OAuth grant scope or lifetime rejected')
            access, refresh = secret(value.get('access_token')), secret(value.get('refresh_token'))
            self._account(access)
            ready = encode(dict(schema_version=1, identity=asdict(self.identity), state='ready',
                access_token=access, refresh_token=refresh, issued_at=started, expires_at=started+lifetime))
            self._check()
            if not started <= self._now() < started + lifetime - 60:
                raise ContractError('OAuth new grant expired or clock changed before save')
            self.store.compare_and_swap(pending, ready)
            self._check()
            if not started <= self._now() < started + lifetime - 60:
                raise ContractError('OAuth new grant expired while saving')
            return access
