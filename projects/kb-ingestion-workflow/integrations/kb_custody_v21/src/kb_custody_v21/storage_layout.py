"""Explicit content locations; operational state always stays in the DEV runtime.

No discovery, mount, format or fallback directory is created here. A mounted
ordinary APFS volume and a separately initialized store marker are mandatory.
"""
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import stat
import subprocess
import time

from .contracts import ContractError
from .runtime import no_symlinks
from .safe_publish import directory, open_root, parts, root_identity

CONTENT_DIRS = ('inbound', 'source-staging', 'cache', 'conversion-work/output',
                'conversion-work/delivery', 'conversion-work/extractions',
                'conversion-work/publications', 'conversion-work/pdf-chunks', 'reading-library')
REFERENCE = 'P43-storage-v1'
MARKER = '.kb-content-store.json'
GIB = 1024 ** 3


class StorageUnavailable(ContractError):
    """An observable storage pause, never permission to use the former copy."""
    def __init__(self, reason):
        self.reason = reason
        super().__init__('资料存储暂不可用：' + reason + '；已暂停，未改用旧副本。')


def content_relative(relative):
    value = '/'.join(parts(str(relative)))
    return any(value == prefix or value.startswith(prefix + '/') for prefix in CONTENT_DIRS)


def absolute(value):
    if not isinstance(value, str) or not value or not Path(value).is_absolute() or str(Path(value)) != value or '..' in Path(value).parts:
        raise ContractError('explicit normalized absolute storage path required')
    return no_symlinks(value)


def validate_config(config):
    required = {'reference', 'mount_path', 'volume_uuid', 'root', 'store_id', 'minimum_free_bytes'}
    if not isinstance(config, dict) or set(config) != required or config['reference'] != REFERENCE:
        raise ContractError('complete explicit P43 storage binding required')
    mount, root = absolute(config['mount_path']), absolute(config['root'])
    if root.parent != mount:
        raise ContractError('content store must be a direct child of the selected volume')
    for key in ('volume_uuid', 'store_id'):
        if not isinstance(config[key], str) or not re.fullmatch(r'[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}', config[key]):
            raise ContractError('explicit UUID required for ' + key)
    if type(config['minimum_free_bytes']) is not int or config['minimum_free_bytes'] < GIB:
        raise ContractError('storage reserve must be at least 1 GiB; it is not a quota')
    return mount, root


def mac_volume(config):
    """Read native plist facts, including APFS roles absent from diskutil info."""
    mount, _ = validate_config(config)
    if mount.parent != Path('/Volumes') or not os.path.ismount(mount):
        raise StorageUnavailable('volume_not_mounted')
    def command(*args):
        try:
            result = subprocess.run(['/usr/sbin/diskutil', *args], check=True,
                capture_output=True, timeout=8, env={'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LC_ALL': 'C'})
            return plistlib.loads(result.stdout)
        except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException, ValueError) as error:
            raise StorageUnavailable('volume_probe_failed') from error
    info = command('info', '-plist', str(mount))
    apfs = command('apfs', 'list', '-plist')
    matches = [v for c in apfs.get('Containers', []) for v in c.get('Volumes', [])
               if str(v.get('APFSVolumeUUID', '')).upper() == config['volume_uuid'].upper()]
    if len(matches) != 1 or not isinstance(matches[0].get('Roles'), list):
        raise StorageUnavailable('volume_roles_unknown')
    try:
        free_bytes = shutil.disk_usage(mount).free
    except OSError as error:
        raise StorageUnavailable('volume_unavailable') from error
    return dict(volume_uuid=info.get('VolumeUUID'), mount_path=info.get('MountPoint'),
        filesystem=info.get('FilesystemType'), external=info.get('Internal') is False,
        writable=info.get('WritableMedia') is True and info.get('WritableVolume') is True,
        roles=matches[0]['Roles'], free_bytes=free_bytes)


class ContentStore:
    def __init__(self, config, *, probe=mac_volume):
        self.config = dict(config)
        self.mount, self.root = validate_config(self.config)
        self.probe = probe
        self._sample = None
        self._sample_at = 0
        self._identity = None

    def volume(self, required_bytes=0, *, fresh=False):
        if type(required_bytes) is not int or required_bytes < 0:
            raise ContractError('nonnegative storage requirement required')
        now = time.monotonic()
        if fresh or self._sample is None or now - self._sample_at >= 1:
            self._sample = self.probe(self.config)
            self._sample_at = now
        value = self._sample
        if (str(value.get('volume_uuid', '')).upper() != self.config['volume_uuid'].upper()
                or value.get('mount_path') != str(self.mount)):
            raise StorageUnavailable('wrong_volume')
        if value.get('filesystem') != 'apfs' or value.get('external') is not True or value.get('roles') != []:
            raise StorageUnavailable('ordinary_external_apfs_required')
        if value.get('writable') is not True:
            raise StorageUnavailable('volume_read_only')
        if type(value.get('free_bytes')) is not int or value['free_bytes'] < self.config['minimum_free_bytes'] + required_bytes:
            raise StorageUnavailable('insufficient_space')
        try:
            no_symlinks(self.mount)
            if not self.mount.is_dir():
                raise StorageUnavailable('volume_not_mounted')
        except OSError as error:
            raise StorageUnavailable('volume_unavailable') from error
        return value

    def check(self, required_bytes=0, *, fresh=False):
        value = self.volume(required_bytes, fresh=fresh)
        try:
            no_symlinks(self.root)
            identity = root_identity(self.root)
            if self.root.stat().st_dev != self.mount.stat().st_dev:
                raise StorageUnavailable('nested_mount_forbidden')
            if self._identity is not None and identity != self._identity:
                raise StorageUnavailable('store_replaced_restart_required')
            fd = open_root(self.root, identity)
            try:
                marker_fd = os.open(MARKER, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                with os.fdopen(marker_fd, 'rb') as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode) or os.fstat(stream.fileno()).st_size > 4096:
                        raise StorageUnavailable('invalid_store_marker')
                    marker = json.loads(stream.read(4097))
            finally:
                os.close(fd)
            expected = dict(reference=REFERENCE, store_id=self.config['store_id'], volume_uuid=self.config['volume_uuid'])
            if marker != expected:
                raise StorageUnavailable('wrong_content_store')
            self._identity = identity
        except StorageUnavailable:
            raise
        except (OSError, ValueError, ContractError) as error:
            raise StorageUnavailable('store_missing_or_unreadable') from error
        return dict(value, root=identity)

    def path(self, relative):
        if not content_relative(relative):
            raise ContractError('path is not managed content')
        self.check()
        target = no_symlinks(self.root / str(relative))
        # Existing nested mount points must not redirect any part of a tree.
        for item in (target, *target.parents):
            if item == self.root:
                break
            if item.exists() and item.stat().st_dev != self._identity['device']:
                raise StorageUnavailable('nested_mount_forbidden')
        return target

    def ensure_directory(self, relative):
        target = self.path(relative)
        fd = open_root(self.root, self._identity)
        try:
            child = directory(fd, str(relative), create=True)
            os.close(child)
            self.check()
        finally:
            os.close(fd)
        return target


def initialize_store(config, *, approved_store_id, probe=mac_volume):
    """Explicit one-folder creation only. Never create a mount or APFS volume."""
    store = ContentStore(config, probe=probe)
    if approved_store_id != config['store_id']:
        raise ContractError('explicit selected store identity required')
    store.volume(fresh=True)
    parent_identity = root_identity(store.mount)
    fd = open_root(store.mount, parent_identity)
    child = None
    try:
        os.mkdir(store.root.name, 0o700, dir_fd=fd)  # Nonempty or unknown existing roots are never adopted.
        os.fsync(fd)
        child = directory(fd, store.root.name)
        marker_fd = os.open(MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=child)
        with os.fdopen(marker_fd, 'w') as stream:
            json.dump(dict(reference=REFERENCE, store_id=config['store_id'], volume_uuid=config['volume_uuid']), stream, sort_keys=True)
            stream.flush(); os.fsync(stream.fileno())
        os.fsync(child)
    finally:
        if child is not None: os.close(child)
        os.close(fd)
    return store.check(fresh=True)
