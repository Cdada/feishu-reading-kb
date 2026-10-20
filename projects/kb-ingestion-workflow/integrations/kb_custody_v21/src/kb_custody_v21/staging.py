"""Stream → durable partial → atomic non-overwriting link → re-read → DB commit.

The hard-link promotion is the portable no-replace equivalent of renameat2.
Both names are in the same asset directory/filesystem. Recovery handles either
name; it never invents a received hash from an untrusted final file.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import os
import stat

from .contracts import ContractError, new_id
from .registry import encoded, now
from .locking import open_lock

BLOCK = 128 * 1024


class IntegrityError(ValueError):
    pass


def digest_fd(fd):
    sha, md5, size = hashlib.sha256(), hashlib.md5(), 0
    while True:
        chunk = os.read(fd, BLOCK)
        if not chunk:
            break
        sha.update(chunk)
        md5.update(chunk)
        size += len(chunk)
    return {"size_bytes": size, "sha256": sha.hexdigest(), "md5": md5.hexdigest()}


def write_all(fd, chunk, writer=os.write):
    offset = 0
    while offset < len(chunk):
        count = writer(fd, memoryview(chunk)[offset:])
        if not isinstance(count, int) or not 0 < count <= len(chunk) - offset:
            raise OSError("write made no progress or returned an invalid length")
        offset += count


class Staging:
    def __init__(self, registry, hook=lambda point: None, writer=os.write):
        self.registry, self.runtime = registry, registry.runtime
        self.hook, self.writer = hook, writer

    @contextmanager
    def asset_lock(self, row, shared=False):
        root = self.runtime.checked(self.runtime.path('source-staging'))
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        directory = lock = None
        try:
            try:
                os.mkdir(row["asset_id"], 0o700, dir_fd=root_fd)
                os.fsync(root_fd)
            except FileExistsError:
                pass  # Opening below still rejects symlinks and non-directories.
            directory = os.open(row["asset_id"], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
            lock = open_lock(".custody.lock", dir_fd=directory)
            fcntl.flock(lock, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
            yield directory
        finally:
            if lock is not None:
                os.close(lock)
            if directory is not None:
                os.close(directory)
            os.close(root_fd)

    @staticmethod
    def file_evidence(directory, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise IntegrityError("staging asset is not a regular file")
            return digest_fd(fd)
        finally:
            os.close(fd)

    @staticmethod
    def compare(actual, expected):
        for field in ("size_bytes", "sha256", "md5"):
            if field in expected and actual[field] != expected[field]:
                raise IntegrityError(f"{field} mismatch")

    def copy(self, directory, row, source):
        import json
        partial = ".original.partial"
        # Only an uncommitted, baseline-free partial from OUR asset may be restarted.
        try:
            info = os.stat(partial, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            info = None
        if info is not None:
            if not stat.S_ISREG(info.st_mode):
                raise IntegrityError("invalid partial file")
            os.unlink(partial, dir_fd=directory)
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        sha, md5, size = hashlib.sha256(), hashlib.md5(), 0
        try:
            with source() as stream:
                while True:
                    chunk = stream.read(BLOCK)
                    if not isinstance(chunk, bytes):
                        raise IntegrityError("source must yield bytes and signal read errors")
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > self.runtime.config["max_asset_bytes"]:
                        raise IntegrityError("asset exceeds configured DEV limit")
                    write_all(fd, chunk, self.writer)
                    sha.update(chunk)
                    md5.update(chunk)
            evidence = {"size_bytes": size, "sha256": sha.hexdigest(), "md5": md5.hexdigest()}
            self.compare(evidence, json.loads(row["expected_json"]))
            os.fsync(fd)
            os.fsync(directory)
        finally:
            os.close(fd)
        # Persist receive baseline before promotion. A crash can never re-baseline
        # unknown final bytes. Hashes are immutable after this transaction.
        with self.registry.transaction() as db:
            db.execute("UPDATE assets SET size_bytes=?,sha256=?,md5=?,transport_length_source=? WHERE asset_id=?",
                       (size, evidence["sha256"], evidence["md5"],
                        "provided_length" if "size_bytes" in json.loads(row["expected_json"]) else "error_free_stream_eof", row["asset_id"]))
        self.hook("after_baseline_commit")
        return evidence

    def ingest(self, entry_id, source=None):
        row = self.registry.entry(entry_id)
        try:
            with self.asset_lock(row) as directory:
                row = self.registry.entry(entry_id)  # Another process may have completed while waiting.
                if row["status"] == "accepted":
                    self.verify_locked(row, directory)
                    return self.registry.receipt(entry_id)
                self.registry.begin_attempt(entry_id)
                if row["sha256"] is None:
                    # An unexplained final file is NOT an acceptable receive baseline.
                    try:
                        os.stat(row["safe_filename"], dir_fd=directory, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        raise IntegrityError("unaccounted final file; refusing overwrite or adoption")
                    if source is None:
                        raise IntegrityError("no durable receive baseline; original transport still required")
                    expected = self.copy(directory, row, source)
                else:
                    expected = {k: row[k] for k in ("size_bytes", "sha256", "md5")}
                self.registry.attempt_phase(entry_id, "verifying")
                try:
                    actual = self.file_evidence(directory, row["safe_filename"])
                except FileNotFoundError:
                    self.compare(self.file_evidence(directory, ".original.partial"), expected)
                    os.link(".original.partial", row["safe_filename"], src_dir_fd=directory,
                            dst_dir_fd=directory, follow_symlinks=False)
                    os.fsync(directory)
                    actual = self.file_evidence(directory, row["safe_filename"])
                self.compare(actual, expected)
                # Remove only our matching temporary name, never another asset.
                try:
                    partial = os.stat(".original.partial", dir_fd=directory, follow_symlinks=False)
                    final = os.stat(row["safe_filename"], dir_fd=directory, follow_symlinks=False)
                    if (partial.st_dev, partial.st_ino) != (final.st_dev, final.st_ino):
                        raise IntegrityError("unexpected temporary file alongside final asset")
                    os.unlink(".original.partial", dir_fd=directory)
                    os.fsync(directory)
                except FileNotFoundError:
                    pass
                self.hook("after_durable")
                self.registry.accept(entry_id, {"expected": expected, "observed": actual,
                                     "reopened_at": now(), "durable": True}, self.hook)
                self.hook("after_accept_commit")
                return self.registry.receipt(entry_id)
        except (OSError, IntegrityError, ContractError) as exc:
            if self.registry.rows("SELECT 1 FROM staging_cleanup WHERE asset_id=? AND state IN ('cleaning','cleaned')", (row['asset_id'],)):
                raise  # Intentional removal never revokes acceptance or emits a failure notice.
            # DB exceptions propagate unchanged: do not pretend a failed DB can record failure.
            if self.registry.entry(entry_id)["status"] == "accepted" and not isinstance(exc, IntegrityError):
                self.registry.health(entry_id, "suspect", "accepted asset directory or lock is unsafe")
            self.registry.failed(entry_id, exc)
            raise

    def verify_locked(self, row, directory):
        if self.registry.rows("SELECT 1 FROM staging_cleanup WHERE asset_id=? AND state IN ('cleaning','cleaned')", (row['asset_id'],)):
            raise ContractError('staging intentionally cleaned or cleanup interrupted; use verified cloud retrieval')
        try:
            actual = self.file_evidence(directory, row["safe_filename"])
            self.compare(actual, {k: row[k] for k in ("size_bytes", "sha256", "md5")})
        except FileNotFoundError:
            self.registry.health(row["entry_id"], "missing", "accepted original missing")
            raise IntegrityError("accepted original missing")
        except (OSError, IntegrityError) as exc:
            self.registry.health(row["entry_id"], "suspect", type(exc).__name__)
            raise IntegrityError("accepted original verification failed") from exc
        self.registry.health(row["entry_id"], "healthy", "original reverified")
        return actual

    @contextmanager
    def input_binding(self, entry_id, purpose="mock_downstream_read", *, conversion_task_id=None):
        """Lease the verified accepted original, exposing its open descriptor."""
        row = self.registry.entry(entry_id)
        if row["status"] != "accepted":
            raise IntegrityError("downstream input requires committed acceptance")
        with self.asset_lock(row, shared=True) as directory:
            self.verify_locked(row, directory)
            lease = new_id("lease")
            with self.registry.transaction() as db:
                db.execute("INSERT INTO asset_leases VALUES (?,?,?,?,NULL)", (lease, row["asset_id"], purpose, now()))
                if conversion_task_id is not None:
                    owner = db.execute('SELECT * FROM conversion_bindings WHERE task_id=?',
                                       (conversion_task_id,)).fetchone()
                    if not owner or owner['asset_id'] != row['asset_id'] or owner['entry_id'] != entry_id or owner['released_at']:
                        raise ContractError('conversion lease requires its active task binding')
                    db.execute('INSERT INTO conversion_lease_owners VALUES (?,?)', (lease, conversion_task_id))
            binding = {"staging_asset_id": row["asset_id"], "acceptance_id": row["acceptance_id"],
                       "path": str(self.runtime.path(row["relative_path"])), "lease_id": lease,
                       "retain_lease": False}
            try:
                # Yield an already opened descriptor too, so callers needn't reopen a mutable path.
                fd = os.open(row["safe_filename"], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
                with os.fdopen(fd, "rb") as stream:
                    binding['stream'] = stream
                    yield binding
            finally:
                # Cross-ledger submission can intentionally retain protection
                # when ownership recording fails after a queued task was saved.
                if not binding['retain_lease']:
                    with self.registry.transaction() as db:
                        db.execute("UPDATE asset_leases SET released_at=? WHERE lease_id=?", (now(), lease))
