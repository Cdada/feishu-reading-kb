"""Local process ownership for P32. Status and stop require no platform call."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

from .contracts import ContractError
from .locking import open_lock
from .runtime import no_symlinks


def write_state(path, value):
    path = no_symlinks(Path(path).absolute())
    temporary = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    fd = os.open(temporary, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY|os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


class DailyLauncher:
    def __init__(self, root):
        self.root = no_symlinks(Path(root).absolute())
        self.lock_path = self.root/'service.lock'
        self.state_path = self.root/'process.local.json'
        self.stop_path = self.root/'stop.local.json'

    def _initialize(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _state(self):
        if not self.state_path.exists():
            return {}
        raw = no_symlinks(self.state_path).read_bytes()
        if len(raw) > 65536:
            raise ContractError('process state exceeds limit')
        return json.loads(raw)

    def status(self):
        if not self.root.exists():
            return dict(state='not_started', running=False)
        if not self.lock_path.exists():
            return dict(state='stopped', running=False, previous=self._state())
        fd = open_lock(no_symlinks(self.lock_path))
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
                running = False
            except BlockingIOError:
                running = True
            state = self._state()
            # PID alone is not used: it may belong to an unrelated process now.
            return dict(state='running' if running else 'stopped', running=running,
                        process=state, stop_requested=self.stop_requested(state.get('instance')))
        finally:
            os.close(fd)

    def stop_requested(self, instance):
        if not instance or not self.stop_path.exists():
            return False
        raw = no_symlinks(self.stop_path).read_bytes()
        if len(raw) > 4096:
            raise ContractError('stop marker exceeds limit')
        return json.loads(raw).get('instance') == instance

    def request_stop(self):
        status = self.status()
        if not status['running']:
            return status
        instance = status['process'].get('instance')
        if not instance:
            raise ContractError('process is starting; retry stop after initialization')
        write_state(self.stop_path, dict(instance=instance, at=time.time()))
        return dict(state='stop_requested', running=True, instance=instance)

    @contextmanager
    def own(self, *, configuration_sha):
        self._initialize()
        fd = open_lock(no_symlinks(self.lock_path))
        try:
            fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
            instance = uuid.uuid4().hex
            state = dict(instance=instance, pid=os.getpid(), configuration_sha=configuration_sha,
                         started_at=time.time())
            write_state(self.state_path, state)
            try:
                yield instance
            finally:
                write_state(self.state_path, dict(state, ended_at=time.time()))
        finally:
            os.close(fd)

    def start(self, argv, *, env, readiness_seconds=10):
        """Spawn only the fixed local entrypoint supplied by the reviewed CLI.

        A startup lock spans the parent/child handshake. The child retains the
        service lock, so two launcher invocations cannot create two services.
        """
        self._initialize()
        fd = open_lock(no_symlinks(self.root/'startup.lock'))
        child = None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            status = self.status()
            if status['running']:
                return dict(status, already_running=True)
            log_path = no_symlinks(self.root/'launcher.local.log')
            log_fd = os.open(log_path, os.O_WRONLY|os.O_CREAT|os.O_APPEND|os.O_NOFOLLOW, 0o600)
            with os.fdopen(log_fd, 'ab') as log:
                child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                         start_new_session=True, env=env, close_fds=True)
            until = time.monotonic()+readiness_seconds
            while time.monotonic() < until:
                status = self.status()
                process = status.get('process', {})
                # The child takes the lock before atomically writing its state.
                # During that gap, the old instance can still be on disk.
                if (status['running'] and process.get('pid') == child.pid
                        and process.get('instance') and 'ended_at' not in process):
                    return dict(status, already_running=False)
                if child.poll() is not None:
                    raise ContractError('daily process exited before readiness; inspect private launcher log')
                time.sleep(.05)
            # No kill by stale PID. The live child may still be initializing;
            # report uncertainty and let its lock arbitrate the next start.
            return dict(state='starting', running=False, pid=child.pid)
        finally:
            os.close(fd)
