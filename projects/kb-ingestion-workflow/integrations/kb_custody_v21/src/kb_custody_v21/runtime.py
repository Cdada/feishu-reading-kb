"""Explicit DEV configuration. No production imports or environment fallbacks."""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from datetime import datetime, timezone
from contextlib import contextmanager
import fcntl

from .contracts import ContractError
from .locking import open_lock

DEFAULTS = {
    "mode": "dev", "branch": "codex/kb-ingestion-v2.1",
    "runtime_relative": ".dev-runtime/kb-v21", "sender": "persistent_mock",
    "real_feishu_sending": False, "baidu_enabled": False,
    "destructive_cleanup": False, "listen": False, "reserved_port": 18791,
    "max_asset_bytes": 67108864,
}
DIRECTORIES = ("inbound", "source-staging", "cache", "cloud-assets/exports",
               "archive-plans", "conversion-work", "logs", "fixtures", "test-output")


def no_symlinks(path):
    path = Path(path).absolute()
    for node in (path, *path.parents):
        if node.is_symlink():
            raise ContractError(f"symlink forbidden: {node}")
    return path


class Runtime:
    def __init__(self, worktree, config, runtime_override=None, *, storage_probe=None):
        self.worktree = no_symlinks(worktree)
        self.config = dict(config)
        if set(config) not in (set(DEFAULTS), set(DEFAULTS) | {'storage'}):
            raise ContractError("DEV configuration must contain exactly the documented fields")
        for key in ("mode", "branch", "sender", "real_feishu_sending", "baidu_enabled",
                    "destructive_cleanup", "listen"):
            if config[key] != DEFAULTS[key] or type(config[key]) is not type(DEFAULTS[key]):
                raise ContractError(f"unsafe DEV setting: {key}")
        relative = Path(config["runtime_relative"])
        if relative.is_absolute() or ".." in relative.parts or relative.parts[:1] != (".dev-runtime",):
            raise ContractError("runtime must be inside this worktree/.dev-runtime")
        if type(config["max_asset_bytes"]) is not int or config["max_asset_bytes"] < 1:
            raise ContractError("invalid max_asset_bytes")
        if type(config["reserved_port"]) is not int or not 1024 <= config["reserved_port"] <= 65535:
            raise ContractError("invalid reserved port")
        self.base = no_symlinks(self.worktree / relative)
        self.root = no_symlinks(runtime_override or self.base)
        if self.root != self.base and not self.root.is_relative_to(self.base / "test-output"):
            raise ContractError("test runtime must be under DEV test-output")
        self.storage = None
        if 'storage' in config:
            from .storage_layout import ContentStore
            self.storage = ContentStore(config['storage'], **({'probe': storage_probe} if storage_probe else {}))
            if self.storage.root.is_relative_to(self.root) or self.root.is_relative_to(self.storage.root):
                raise ContractError('content and operational roots must not overlap')
        self.data_root = self.storage.root if self.storage else self.root
        self.db = self.root / "cloud-assets/registry.sqlite3"
        self.gate()

    @classmethod
    def load(cls, worktree, config_path=None):
        if config_path is None:
            raise ContractError("explicit DEV configuration required")
        return cls(worktree, json.loads(Path(config_path).read_text()))

    def gate(self):
        for key in ("mode", "branch", "sender", "real_feishu_sending", "baidu_enabled",
                    "destructive_cleanup", "listen"):
            if self.config[key] != DEFAULTS[key] or type(self.config[key]) is not type(DEFAULTS[key]):
                raise ContractError(f"unsafe DEV setting: {key}")
        # Reject a missing directory or primary checkout before invoking Git.
        if not (self.worktree / ".git").is_file():
            raise ContractError("an isolated linked worktree is required")
        def git(*args):
            return subprocess.check_output(["git", "-C", str(self.worktree), *args], text=True).strip()
        if Path(git("rev-parse", "--show-toplevel")) != self.worktree:
            raise ContractError("not the specified worktree root")
        if git("branch", "--show-current") != self.config["branch"]:
            raise ContractError("not the V2.1 feature branch")
        no_symlinks(self.root)
        no_symlinks(self.db)
        for suffix in ("-wal", "-shm", "-journal"):
            no_symlinks(str(self.db) + suffix)
        for name in DIRECTORIES:
            no_symlinks(self.root / name)
        return {"worktree": str(self.worktree), "branch": self.config["branch"],
                "runtime": str(self.root), "registry": str(self.db),
                "sender": "persistent_mock", "network_enabled": False,
                "destructive_cleanup": False, "listening": False,
                "reserved_port": self.config["reserved_port"]}

    def initialize(self):
        self.gate()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        from .storage_layout import CONTENT_DIRS, content_relative
        for name in DIRECTORIES:
            if not self.storage or not content_relative(name):
                (self.root / name).mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.storage:
            for name in CONTENT_DIRS:
                self.storage.ensure_directory(name)
        return self.gate()

    def check_storage(self):
        return self.storage.check() if self.storage else None

    def path(self, relative):
        from .safe_publish import parts
        from .storage_layout import content_relative
        value = '/'.join(parts(str(relative)))
        if self.storage and content_relative(value):
            return self.storage.path(value)
        return self.checked(self.root / value)

    def relative(self, path):
        path = self.checked(path)
        base = self.data_root if path.is_relative_to(self.data_root) else self.root
        return path.relative_to(base).as_posix()

    def ensure_directory(self, relative):
        from .storage_layout import content_relative
        if self.storage and content_relative(relative):
            return self.storage.ensure_directory(relative)
        path = self.path(relative)
        from .safe_publish import directory, open_root, root_identity
        fd = open_root(self.root, root_identity(self.root))
        try:
            child = directory(fd, str(relative), create=True)
            os.close(child)
        finally:
            os.close(fd)
        return path

    def checked(self, path):
        from .storage_layout import content_relative
        path = no_symlinks(path)
        if '..' in path.parts:
            raise ContractError('path is outside DEV runtime')
        if self.storage and path.is_relative_to(self.data_root):
            relative = path.relative_to(self.data_root).as_posix()
            return self.storage.path(relative)
        if not path.is_relative_to(self.root):
            raise ContractError('path is outside configured runtime and content store')
        if self.storage and path != self.root and content_relative(path.relative_to(self.root)):
            raise ContractError('old local content path is inactive; use the selected content store')
        return path

    def make_sender(self):
        """DEV composition only. Outbox core is provider-independent."""
        from .notifications import PersistentMockSender
        self.gate()
        if self.config["sender"] != "persistent_mock":
            raise ContractError("DEV can instantiate only persistent_mock")
        return PersistentMockSender(self)

    @contextmanager
    def initializing(self, name):
        if name not in ("registry", "mock-provider"):
            raise ContractError("invalid initialization lock")
        path = self.checked(self.root / "cloud-assets" / (name + ".init.lock"))
        fd = open_lock(path)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def record_fault(self, phase, error):
        """Diagnostic log, never a replacement receipt, event, queue or notification."""
        prompt = "本地账本暂不可写，当前操作已暂停；未确认接管时，请先保留飞书附件。"
        record = {"at": datetime.now(timezone.utc).isoformat(), "phase": phase,
                  "error_code": type(error).__name__, "error_summary": str(error)[:500],
                  "human_message": prompt, "diagnostic_only": True}
        try:
            path = self.checked(self.root / "logs/custody-faults.jsonl")
            fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            try:
                with os.fdopen(fd, "ab", closefd=False) as log:
                    log.write((json.dumps(record, ensure_ascii=False) + "\n").encode())
                    log.flush()
                    os.fsync(fd)
            finally:
                os.close(fd)
        except (OSError, ContractError) as logging_error:
            print(f"{prompt} 诊断日志也不可写：{type(logging_error).__name__}", file=sys.stderr)
        else:
            print(prompt, file=sys.stderr)

    def read_fd(self, path):
        """Walk directory descriptors with O_NOFOLLOW to prevent symlink races."""
        path = self.checked(path)
        base = self.data_root if path.is_relative_to(self.data_root) else self.root
        parts = path.relative_to(base).parts
        from .safe_publish import open_root, root_identity
        fd = open_root(base, root_identity(base))
        try:
            for part in parts[:-1]:
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = nxt
            result = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
            if not stat.S_ISREG(os.fstat(result).st_mode):
                os.close(result)
                raise ContractError("source must be a regular file")
            return result
        finally:
            os.close(fd)
