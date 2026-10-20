"""Read only one pre-existing application record, never enumerate or configure.

No keychain access occurs on import. The future explicit B01 executor must check
its approved source, application and time window before calling this component.
A callback is an integration boundary, not proof of human authorization.
"""
import ctypes
from dataclasses import dataclass
import os
from pathlib import Path
import re
import sys

from .baidu_protocol import json_object
from .contracts import ContractError

SERVICE = b'kb-ingestion-v21.baidu.application'
MAX_RECORD = 8192


@dataclass(frozen=True, repr=False)
class ApplicationCredentials:
    app_key: str
    secret_key: str

    def __repr__(self):
        return 'ApplicationCredentials(sensitive fields omitted)'


def application_identity(app_id, app_name):
    if (not isinstance(app_id, str) or not re.fullmatch('[1-9][0-9]{0,19}', app_id)
            or not isinstance(app_name, str) or not 1 <= len(app_name) <= 64
            or app_name.strip() != app_name
            or any(c in '/\\\x00' or not c.isprintable() for c in app_name)):
        raise ContractError('exact application identity required')


def parse_application_record(data, app_id, app_name):
    application_identity(app_id, app_name)
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_RECORD:
        raise ContractError('application record exceeds bounded contract')
    record = json_object(data)
    if (set(record) != {'schema_version', 'provider', 'app_id', 'app_name',
                       'app_key', 'secret_key', 'sign_key'}
            or type(record['schema_version']) is not int or record['schema_version'] != 1
            or record['provider'] != 'baidu_netdisk'
            or (record['app_id'], record['app_name']) != (app_id, app_name)):
        raise ContractError('saved credential belongs to a different application or schema')
    for key in ('app_key', 'secret_key', 'sign_key'):
        value = record[key]
        if key == 'sign_key' and value == '':
            continue
        if (not isinstance(value, str) or not 8 <= len(value) <= 512
                or not value.isascii() or any(c.isspace() or not c.isprintable() for c in value)):
            raise ContractError('saved credential field has invalid format')
    # The reviewed OAuth code exchange uses AppKey/SecretKey only. SignKey and
    # any platform session/refresh tokens are never exposed to the caller.
    return ApplicationCredentials(record['app_key'], record['secret_key'])


class LoginKeychainReader:
    """Security.framework exact-service/account lookup; no shell or fallback."""
    def __init__(self):
        if sys.platform != 'darwin':
            raise ContractError('login keychain reader requires macOS')
        self.path = Path.home() / 'Library/Keychains/login.keychain-db'
        if not self.path.is_file():
            raise ContractError('existing login keychain required; no fallback storage')
        self.security = ctypes.CDLL('/System/Library/Frameworks/Security.framework/Security')
        self.core = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        self.security.SecKeychainOpen.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
        self.security.SecKeychainOpen.restype = ctypes.c_int32
        self.security.SecKeychainFindGenericPassword.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32,
            ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p)]
        self.security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
        self.security.SecKeychainItemFreeContent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.security.SecKeychainItemFreeContent.restype = ctypes.c_int32
        self.core.CFRelease.argtypes = [ctypes.c_void_p]
        self.core.CFRelease.restype = None

    def read_application(self, app_id, app_name, *, before_read):
        application_identity(app_id, app_name)
        if not callable(before_read) or before_read() is not True:
            raise ContractError('credential access was not explicitly confirmed')
        keychain, content = ctypes.c_void_p(), ctypes.c_void_p()
        length = ctypes.c_uint32()
        status = self.security.SecKeychainOpen(os.fsencode(self.path), ctypes.byref(keychain))
        if status != 0 or not keychain.value:
            raise ContractError('login keychain could not be opened')
        try:
            # Opening/unlocking the keychain can itself wait for the operator.
            if before_read() is not True:
                raise ContractError('credential access expired or changed while waiting')
            # A locked keychain or its system access prompt may wait for the
            # operator. Recheck after the read before returning any credential.
            account = app_id.encode('ascii')
            status = self.security.SecKeychainFindGenericPassword(keychain,
                len(SERVICE), SERVICE, len(account), account, ctypes.byref(length),
                ctypes.byref(content), None)
            if status != 0:
                raise ContractError('application credential unavailable; no retry or overwrite')
            try:
                if not content.value or not 0 < length.value <= MAX_RECORD:
                    raise ContractError('keychain record exceeds bounded contract')
                if before_read() is not True:
                    raise ContractError('credential access expired or changed while waiting')
                return parse_application_record(ctypes.string_at(content, length.value), app_id, app_name)
            finally:
                if content.value:
                    status = self.security.SecKeychainItemFreeContent(None, content)
                    if status != 0:
                        raise ContractError('keychain buffer release failed')
        finally:
            self.core.CFRelease(keychain)
