"""Restore only the already accepted byte identity; preserve damaged originals."""
import os
from pathlib import Path
import stat
import uuid

from .contracts import ContractError
from .staging import BLOCK, IntegrityError, write_all


def restore_original(staging, entry_id, downloaded):
    row=staging.registry.entry(entry_id)
    if row['status']!='accepted':
        raise ContractError('only an accepted original can be restored')
    downloaded=staging.runtime.checked(Path(downloaded))
    with staging.asset_lock(row) as directory:
        if staging.registry.rows("SELECT 1 FROM staging_cleanup WHERE asset_id=? AND state IN ('cleaning','cleaned')",(row['asset_id'],)):
            raise ContractError('intentionally cleaned originals require the existing retrieval workflow')
        expected={k:row[k] for k in ('size_bytes','sha256','md5')}
        try:
            staging.compare(staging.file_evidence(directory,row['safe_filename']),expected)
        except (FileNotFoundError,IntegrityError):
            pass
        else:
            staging.verify_locked(row,directory)
            return
        temporary='.daily-restore-'+uuid.uuid4().hex
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory)
        try:
            source=os.open(downloaded,os.O_RDONLY|os.O_NOFOLLOW)
            with os.fdopen(source,'rb') as stream:
                size=0
                while chunk:=stream.read(BLOCK):
                    size+=len(chunk)
                    if size>expected['size_bytes']:
                        raise IntegrityError('replacement original exceeds accepted bytes')
                    write_all(fd,chunk)
            os.fsync(fd)
        finally:
            os.close(fd)
        # No existing file changes until every accepted digest has matched.
        staging.compare(staging.file_evidence(directory,temporary),expected)
        try:
            old=os.stat(row['safe_filename'],dir_fd=directory,follow_symlinks=False)
        except FileNotFoundError:
            old=None
        if old:
            if not stat.S_ISREG(old.st_mode):
                raise IntegrityError('unsafe original path cannot be replaced')
            preserved='.damaged-'+uuid.uuid4().hex
            os.rename(row['safe_filename'],preserved,src_dir_fd=directory,dst_dir_fd=directory)
            os.fsync(directory)
        os.link(temporary,row['safe_filename'],src_dir_fd=directory,dst_dir_fd=directory,follow_symlinks=False)
        os.fsync(directory)
        os.unlink(temporary,dir_fd=directory)
        staging.verify_locked(row,directory)
        with staging.registry.transaction() as db:
            staging.registry.event(db,'daily_original_restored',entry_id,'daily-original-restored:'+uuid.uuid4().hex,
                dict(sha256=row['sha256'],preserved_damaged=preserved if old else None))
