"""Exact OAuth Keychain storage; inert until explicitly constructed and called.

Separate service from application credentials. No shell, plaintext token file,
enumeration, delete, ACL change or automatic replacement after a failure.
Native behavior is currently verified with synthetic Security.framework mocks.
"""
from contextlib import contextmanager
import ctypes
import fcntl
import os
import stat

from .baidu_credentials import LoginKeychainReader, MAX_RECORD
from .baidu_oauth import OAuthIdentity, parse_record
from .contracts import ContractError
from .locking import open_lock

SERVICE = b'kb-ingestion-v21.baidu.oauth'


class OAuthKeychainStore(LoginKeychainReader):
    is_mock = False

    def __init__(self, identity, lock_directory, *, before_access):
        if not isinstance(identity, OAuthIdentity) or not callable(before_access):
            raise ContractError('OAuth identity and storage approval check required')
        super().__init__()
        self.identity, self.lock_directory, self.before_access = identity, lock_directory, before_access
        self._locked = False
        self.security.SecKeychainAddGenericPassword.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
            ctypes.c_char_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p)]
        self.security.SecKeychainAddGenericPassword.restype = ctypes.c_int32
        self.security.SecKeychainItemModifyAttributesAndData.argtypes = [ctypes.c_void_p,
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
        self.security.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32

    def _check(self):
        if self.before_access() is not True:
            raise ContractError('OAuth Keychain scope, source or window check failed')

    @contextmanager
    def transaction(self):
        self._check()
        if self._locked:
            raise ContractError('OAuth store transaction already active')
        # Executor must supply one fixed private directory for every instance.
        # The empty persistent lock never carries tokens and is never removed.
        directory = os.open(self.lock_directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        fd = None
        try:
            info = os.fstat(directory)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise ContractError('OAuth lock directory must be private and owned')
            fd = open_lock('oauth-' + self.identity.key + '.lock', dir_fd=directory)
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
                raise ContractError('OAuth lock file must be private, owned and unshared')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ContractError('OAuth another credential operation is active') from None
            self._check()
            self._locked = True
            try:
                yield self
            finally:
                self._locked = False
        finally:
            if fd is not None:
                os.close(fd)
            os.close(directory)

    @contextmanager
    def _item(self):
        if not self._locked:
            raise ContractError('OAuth Keychain access requires an exclusive transaction')
        self._check()
        keychain, content, item = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        length = ctypes.c_uint32()
        status = self.security.SecKeychainOpen(os.fsencode(self.path), ctypes.byref(keychain))
        if status != 0 or not keychain.value:
            raise ContractError('OAuth login Keychain unavailable')
        try:
            account = self.identity.key.encode('ascii')
            self._check()
            status = self.security.SecKeychainFindGenericPassword(keychain, len(SERVICE), SERVICE,
                len(account), account, ctypes.byref(length), ctypes.byref(content), ctypes.byref(item))
            try:
                self._check()
                if status == -25300:
                    yield keychain, None, None
                elif status != 0:
                    raise ContractError('OAuth Keychain access cancelled or unavailable')
                else:
                    if not content.value or not item.value or not 0 < length.value <= MAX_RECORD:
                        raise ContractError('OAuth Keychain record exceeds bounded contract')
                    yield keychain, item, ctypes.string_at(content, length.value)
            finally:
                if item.value:
                    self.core.CFRelease(item)
                if content.value and self.security.SecKeychainItemFreeContent(None, content) != 0:
                    raise ContractError('OAuth Keychain buffer release failed')
        finally:
            self.core.CFRelease(keychain)

    def read(self):
        with self._item() as (_, _, data):
            if data is not None:
                parse_record(data, self.identity)
            return data

    def compare_and_swap(self, expected, replacement):
        parse_record(replacement, self.identity)
        with self._item() as (keychain, item, current):
            if current != expected:
                raise ContractError('OAuth Keychain changed; no overwrite')
            buffer = ctypes.create_string_buffer(replacement)
            self._check()
            if item is None:
                account = self.identity.key.encode('ascii')
                status = self.security.SecKeychainAddGenericPassword(keychain, len(SERVICE), SERVICE,
                    len(account), account, len(replacement), buffer, None)
            else:
                status = self.security.SecKeychainItemModifyAttributesAndData(item, None,
                    len(replacement), buffer)
            if status != 0:
                raise ContractError('OAuth Keychain save failed; no fallback or retry')
        self._check()
        if self.read() != replacement:
            raise ContractError('OAuth Keychain save could not be verified')
