"""Owned local MinerU service; one client, explicit window, no remote backend.

Only an explicitly configured daily authority constructs this adapter. Startup
does not import models in the knowledge-base process. The installed MinerU API
exits on stdin EOF; an inherited lock also fences surviving children after a
parent crash. No PID saved by an earlier process is ever signalled.
"""
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid

from .contracts import ContractError
from .daily_store import Deferred
from .locking import open_lock
from .mineru_chunks import ChunkFailure, PdfOptions
from .runtime import no_symlinks


def validate_pdf_config(config, frozen):
    if not isinstance(config, dict) or set(config)-{'progress'} != {'options', 'api_binary', 'client_binary', 'dependency_manifest', 'model_config'}:
        raise ContractError('complete approved local PDF profile required')
    if 'progress' in config and type(config['progress']) is not bool:
        raise ContractError('PDF progress opt-in must be a boolean')
    if not isinstance(config['options'], dict):
        raise ContractError('PDF options must be an object')
    try:
        options = PdfOptions(**config['options'])
    except TypeError as error:
        raise ContractError('unexpected PDF profile fields') from error
    options.validate()
    for key in ('api_binary', 'client_binary', 'dependency_manifest', 'model_config'):
        value = config[key]
        if not isinstance(value, str) or str(Path(value).absolute()) != value or value not in frozen:
            raise ContractError('PDF executable/dependency manifest must be frozen')
        path = no_symlinks(Path(value))
        if not path.is_file() or key in {'api_binary','client_binary'} and not os.access(path, os.X_OK):
            raise ContractError('MinerU executable or dependency manifest unavailable')
    manifest = Path(config['dependency_manifest'])
    if manifest.stat().st_size > 4 * 1024**2:
        raise ContractError('PDF dependency manifest exceeds bound')
    value = json.loads(manifest.read_bytes())
    if (not isinstance(value, dict) or set(value) != {'reference', 'mineru_version', 'files'}
            or value['reference'] != 'P33-mineru-dependencies-v1' or value['mineru_version'] != options.version
            or not isinstance(value['files'], dict) or not value['files']
            or any(frozen.get(path) != digest for path, digest in value['files'].items())):
        raise ContractError('MinerU runtime dependencies differ from approved manifest')
    model_path = Path(config['model_config'])
    if model_path.stat().st_size > 65536:
        raise ContractError('local MinerU model configuration exceeds bound')
    model = json.loads(model_path.read_bytes())
    if (not isinstance(model,dict) or set(model) != {'models-dir'}
            or not isinstance(model['models-dir'],dict) or set(model['models-dir']) != {'pipeline'}
            or not isinstance(model['models-dir']['pipeline'],str)
            or not Path(model['models-dir']['pipeline']).is_absolute()
            or not Path(model['models-dir']['pipeline']).is_dir()):
        raise ContractError('MinerU requires local pipeline models with no remote or LLM-aided configuration')
    return options


class MineruService:
    def __init__(self, root, config, *, check, stop, resources, emit,
                 popen=subprocess.Popen, monotonic=time.monotonic, health=None):
        self.root = no_symlinks(Path(root).absolute())
        self.root.mkdir(parents=True, exist_ok=True)
        self.config, self.options = config, PdfOptions(**config['options'])
        self.options.validate()
        self.check, self.stop_requested, self.resources, self.emit = check, stop, resources, emit
        self.popen, self.clock = popen, monotonic
        self.health = health or self._health
        self.process = self.client = None
        self.guard = self.log = None
        self.url = self.instance = None
        self.last_used = self.clock()
        self.mutex = threading.Lock()

    def _event(self, kind, reason=None):
        labels = {
            'starting': '正在启动本机 MinerU，论文转换正在等待服务。',
            'ready': 'MinerU 服务已可接收任务；首批仍可能需要加载模型，知识库继续接收文件。',
            'failed': 'MinerU 启动或运行未完成，已保存材料和已完成的批次。',
            'stopped': 'MinerU 服务已关闭。',
        }
        explanations = {'idle': 'PDF 队列已处理完且空闲 5 分钟，知识库仍在运行。',
                        'resources': '为释放内存暂时关闭，知识库保留待处理材料。',
                        'service_stop': '知识库正在停止，队列和已完成结果保留。',
                        'failed': '本次转换未完成，结果已保留待接续。'}
        self.emit(dict(id=uuid.uuid4().hex, kind=kind, instance=self.instance,
                       reason=reason, text=labels[kind] + explanations.get(reason, '')))

    def _health(self, url):
        # Bypass proxies and redirects: the owned loopback service is the only
        # permitted endpoint, including in an environment with proxy variables.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        try:
            with opener.open(url + '/health', timeout=2) as response:
                body = response.read(65537)
            if len(body) > 65536:
                raise ChunkFailure('MinerU 健康响应超过限制')
            return json.loads(body)
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            return None
        except (ValueError, UnicodeError) as error:
            raise ChunkFailure('MinerU 健康响应格式不正确') from error

    def _environment(self):
        env = {k: v for k, v in os.environ.items() if k in {'HOME', 'PATH', 'TMPDIR', 'LANG', 'LC_ALL'}}
        env.update(MINERU_MODEL_SOURCE='local', HF_HUB_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1',
            MINERU_TOOLS_CONFIG_JSON=self.config['model_config'],
            MINERU_API_MAX_CONCURRENT_REQUESTS='1', MINERU_PROCESSING_WINDOW_SIZE=str(self.options.chunk_pages),
            MINERU_API_SHUTDOWN_ON_STDIN_EOF='1', MINERU_API_ENABLE_FASTAPI_DOCS='0',
            MINERU_API_OUTPUT_ROOT=str(self.root / 'api-output'), MINERU_API_TASK_RETENTION_SECONDS='600',
            MINERU_LOCAL_API_STARTUP_TIMEOUT_SECONDS=str(self.options.startup_seconds),
            NO_PROXY='127.0.0.1,localhost', no_proxy='127.0.0.1,localhost', OMP_NUM_THREADS='2')
        return env

    @staticmethod
    def _terminate(process):
        if process is None or process.poll() is not None:
            return
        # A new session belongs to this Popen object. Never reuse a stored PID.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            process.wait(timeout=5)
            return
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass  # The owned process exited between timeout and signal.
            process.wait(timeout=5)

    def _close(self, reason):
        process = self.process
        if self.client is not None:
            self._terminate(self.client)
            self.client = None
        if process is not None:
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
            self._terminate(process)
            if process.poll() is None:
                raise ContractError('owned MinerU process has not exited')
            self.process = None
        if self.log is not None:
            self.log.close()
            self.log = None
        if self.guard is not None:
            os.close(self.guard)
            self.guard = None
        self.url = None
        if process is not None:
            self._event('stopped', reason)

    def _admission(self):
        if self.stop_requested.is_set():
            self._close('service_stop')
            raise Deferred('stopping')
        state = self.resources()
        if state['state'] != 'resources_ready':
            self._close('resources')
            raise Deferred('paused_resources')
        self.check()

    def check_previous(self):
        """Do not read unfinished outputs while an orphan still owns them."""
        if self.process is not None:
            return
        fd = open_lock(self.root / 'mineru.lock')
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise Deferred('previous_mineru_process_still_exiting') from error
        finally:
            os.close(fd)

    def _ensure(self):
        self._admission()
        if self.process is not None:
            if self.process.poll() is None:
                return
            self._event('failed', 'failed')
            self._close('failed')
        self.guard = open_lock(self.root / 'mineru.lock')
        try:
            fcntl.flock(self.guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(self.guard)
            self.guard = None
            raise Deferred('previous_mineru_process_still_exiting') from error
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        self.instance = uuid.uuid4().hex
        self.url = f'http://127.0.0.1:{port}'
        self.log = (self.root / (self.instance + '.log')).open('xb')
        try:
            self._event('starting')
            self.process = self.popen([self.config['api_binary'], '--host', '127.0.0.1', '--port', str(port)],
                stdin=subprocess.PIPE, stdout=self.log, stderr=subprocess.STDOUT, env=self._environment(),
                cwd=self.root, start_new_session=True, pass_fds=(self.guard,))
            deadline, probe_at = self.clock() + self.options.startup_seconds, 0
            while self.clock() < deadline:
                if self.process.poll() is not None:
                    raise ChunkFailure('MinerU 服务在启动期间退出')
                if self.clock() >= probe_at:
                    self._admission()
                    probe_at = self.clock() + 5
                health = self.health(self.url)
                if health is not None:
                    if (not isinstance(health,dict) or health.get('status') != 'healthy' or health.get('version') != self.options.version
                            or health.get('max_concurrent_requests') != 1
                            or health.get('processing_window_size') != self.options.chunk_pages):
                        raise ContractError('MinerU 版本、处理窗口或并发设置与批准规则不一致')
                    if self.process.poll() is not None:
                        raise ChunkFailure('MinerU 就绪时进程已经退出')
                    self._event('ready')
                    self.last_used = self.clock()
                    return
                if self.stop_requested.wait(.5):
                    self._admission()
            raise ChunkFailure('MinerU 启动超过约定等待时间')
        except Deferred:
            self._close('service_stop' if self.stop_requested.is_set() else 'resources')
            raise
        except BaseException:
            try:
                self._event('failed', 'failed')
            finally:
                self._close('failed')
            raise

    def convert(self, source, output, *, mode):
        if mode not in {'auto', 'ocr'}:
            raise ContractError('unsupported MinerU mode')
        with self.mutex:
            self._ensure()
            output.mkdir(parents=True, exist_ok=False)
            log_path = output.parent / 'client.log'
            try:
                with log_path.open('xb') as log:
                    self.client = self.popen([self.config['client_binary'], '-p', str(source), '-o', str(output),
                        '-b', 'pipeline', '-m', mode, '-l', 'ch', '--api-url', self.url],
                        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, env=self._environment(),
                        cwd=output.parent, start_new_session=True, pass_fds=(self.guard,))
                    deadline, probe_at = self.clock() + self.options.chunk_seconds, 0
                    while self.client.poll() is None:
                        if self.clock() >= deadline:
                            raise ChunkFailure('MinerU 单批处理超过约定等待时间')
                        if self.clock() >= probe_at:
                            self._admission()
                            probe_at = self.clock() + 5
                            if self.process.poll() is not None:
                                raise ChunkFailure('MinerU 服务在处理期间退出')
                            if log_path.stat().st_size > 64 * 1024**2 or Path(self.log.name).stat().st_size > 64 * 1024**2:
                                raise ChunkFailure('MinerU 运行日志超过本地保护上限')
                        if self.stop_requested.wait(.5):
                            self._admission()
                    if self.client.returncode != 0:
                        raise ChunkFailure('MinerU 单批未成功完成，请检查本地转换日志')
                    self.client = None
                    self.last_used = self.clock()
            except BaseException:
                self._close('service_stop' if self.stop_requested.is_set() else 'failed')
                raise

    def maintain(self, *, queued_pdf):
        # The collector must never wait behind a model startup or a PDF batch.
        if not self.mutex.acquire(blocking=False):
            return
        try:
            if self.process is None:
                return
            if self.process.poll() is not None:
                self._event('failed', 'failed')
                self._close('failed')
            elif self.resources()['state'] != 'resources_ready':
                self._close('resources')
            elif not queued_pdf and self.clock() - self.last_used >= self.options.idle_seconds:
                self._close('idle')
        finally:
            self.mutex.release()

    def close(self):
        with self.mutex:
            self._close('service_stop')
