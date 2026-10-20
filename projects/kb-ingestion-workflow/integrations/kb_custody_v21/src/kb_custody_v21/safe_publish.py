"""Small descriptor-relative publisher. No overwrite and no recursive deletion."""
import ctypes
import errno
import hashlib
import os
from pathlib import PurePosixPath
import stat
import sys
import unicodedata

from .contracts import ContractError


def fail(code, message):
    raise ContractError(f"{code}: {message}")


def parts(relative):
    if (not isinstance(relative, str) or not relative or '\\' in relative or '\x00' in relative
            or relative.startswith('/') or any(x in {'', '.', '..'} for x in relative.split('/'))):
        fail('UNSAFE_PUBLICATION_PATH', '入库路径不安全')
    return relative.split('/')


def root_identity(root):
    if root.is_symlink() or not root.is_dir():
        fail('KB_ROOT_UNAVAILABLE', '知识库根目录不存在或为链接；请先明确目标目录')
    canonical = root.resolve(strict=True)
    info = canonical.stat()
    return {'path': str(canonical), 'device': info.st_dev, 'inode': info.st_ino}


def open_root(root, identity):
    if root_identity(root) != identity:
        fail('KB_ROOT_CHANGED', '知识库根目录已变化，旧预览不再有效')
    # Walk canonical absolute path so an ancestor link swap cannot redirect writes.
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in PurePosixPath(identity['path']).parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = child
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != (identity['device'], identity['inode']):
            fail('KB_ROOT_CHANGED', '知识库根目录已变化')
        return fd
    except BaseException:
        os.close(fd)
        raise


def directory(fd, relative, *, create=False):
    current = os.dup(fd)
    try:
        for part in parts(relative):
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=current)
                    os.fsync(current)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            os.close(current); current = child
        return current
    except BaseException:
        os.close(current)
        raise


def rename_exclusive(source_fd, source, destination_fd, destination):
    if len(parts(source)) != 1 or len(parts(destination)) != 1:
        fail('UNSAFE_PUBLICATION_PATH', '移动操作只接受同层文件名')
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == 'darwin':
        function, flag = library.renameatx_np, 4  # RENAME_EXCL, macOS SDK sys/stdio.h
    elif sys.platform == 'linux' and hasattr(library, 'renameat2'):
        function, flag = library.renameat2, 1  # RENAME_NOREPLACE
    else:
        fail('ATOMIC_PUBLISH_UNSUPPORTED', '本平台缺少无覆盖原子发布接口')
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    if function(source_fd, source.encode(), destination_fd, destination.encode(), flag) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    os.fsync(source_fd); os.fsync(destination_fd)


def manifest(files):
    seen = set()
    result = {}
    for path, raw in files.items():
        parts(path)
        key = unicodedata.normalize('NFC', path).casefold()
        if key in seen:
            fail('PATH_COLLISION', '文件名存在大小写或 Unicode 冲突')
        seen.add(key)
        result[path] = {'sha256': hashlib.sha256(raw).hexdigest(), 'size_bytes': len(raw)}
    return result


def write_tree(fd, files):
    manifest(files)
    for path, raw in files.items():
        segments = parts(path)
        parent = directory(fd, '/'.join(segments[:-1]), create=True) if len(segments) > 1 else os.dup(fd)
        try:
            target = os.open(segments[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            with os.fdopen(target, 'wb') as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            os.fsync(parent)
        finally:
            os.close(parent)
    os.fsync(fd)


def inspect_tree(fd, *, max_bytes=100 * 1024**2, expected_files=None):
    result, total = {}, 0
    allowed_dirs = None if expected_files is None else {prefix for name in expected_files
        for prefix in ['/'.join(parts(name)[:index]) for index in range(1, len(parts(name)))]}
    def visit(parent, prefix=''):
        nonlocal total
        for name in os.listdir(parent):
            path = prefix + name
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                if allowed_dirs is not None and path not in allowed_dirs:
                    fail('PUBLICATION_CHANGED', '目录中出现新增子目录，拒绝自动移动')
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                try: visit(child, path + '/')
                finally: os.close(child)
            elif stat.S_ISREG(info.st_mode):
                source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                with os.fdopen(source, 'rb') as stream:
                    actual = os.fstat(stream.fileno())
                    if not stat.S_ISREG(actual.st_mode) or total + actual.st_size > max_bytes:
                        fail('PUBLICATION_CHANGED', '入库文件类型或总大小异常')
                    raw = stream.read(max_bytes - total + 1)
                total += len(raw)
                if total > max_bytes: fail('PUBLICATION_CHANGED', '入库内容过大')
                result[path] = {'sha256': hashlib.sha256(raw).hexdigest(), 'size_bytes': len(raw)}
            else:
                fail('PUBLICATION_CHANGED', '目录中出现链接或特殊文件，拒绝自动处理')
            if len(result) > 10000: fail('PUBLICATION_CHANGED', '目录文件数量异常')
    visit(fd)
    return result
