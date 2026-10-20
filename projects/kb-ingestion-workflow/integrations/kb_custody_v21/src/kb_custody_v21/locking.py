"""Open persistent lock files without racing non-exclusive creation."""
import os
import stat

from .contracts import ContractError


def open_lock(path, *, dir_fd=None):
    # The winning creator and all subsequent openers use the same inode.
    # Lock files are never removed. Do not retry missing/unsafe existing paths.
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW,
                     0o600, dir_fd=dir_fd)
    except FileExistsError:
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW, dir_fd=dir_fd)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ContractError('lock must be a regular file')
    return fd
