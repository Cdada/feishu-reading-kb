"""Lifecycle tests inject processes and health; no socket/model is started."""
from dataclasses import asdict
import fcntl
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.daily_store import Deferred
from kb_custody_v21.mineru_chunks import ChunkFailure, PdfOptions, file_digest
from kb_custody_v21.mineru_service import MineruService, validate_pdf_config


class Process:
    def __init__(self, finished=False):
        self.pid = 123456789  # never signalled; termination is patched below
        self.stdin = io.BytesIO()
        self.returncode = 0 if finished else None

    def poll(self): return self.returncode
    def wait(self, **kwargs): self.returncode = 0; return 0


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.events, self.calls, self.time, self.ready = [], [], 10, True
        self.stop = threading.Event()
        self.config = dict(options=asdict(PdfOptions()), api_binary='/synthetic/mineru-api',
                           client_binary='/synthetic/mineru', dependency_manifest='/synthetic/deps.json',model_config='/synthetic/models.json')
        def spawn(argv, **kwargs):
            p = Process(finished=argv[0].endswith('/mineru'))
            self.calls.append((argv, kwargs, p))
            return p
        self.service = MineruService(self.root/'service', self.config, check=lambda:None, stop=self.stop,
            resources=lambda:dict(state='resources_ready' if self.ready else 'paused_resources'), emit=self.events.append,
            popen=spawn, monotonic=lambda:self.time,
            health=lambda _:dict(status='healthy',version='3.1.15',max_concurrent_requests=1,processing_window_size=5))
        sock = MagicMock(); sock.__enter__.return_value.getsockname.return_value = ('127.0.0.1', 12345)
        network = patch('kb_custody_v21.mineru_service.socket.socket', return_value=sock)
        network.start(); self.addCleanup(network.stop)
        terminate = patch.object(self.service, '_terminate', side_effect=lambda p:p.wait() if p is not None else None)
        terminate.start(); self.addCleanup(terminate.stop)
        self.addCleanup(self.service.close)

    def convert(self, name):
        directory = self.root / name; directory.mkdir()
        self.service.convert(directory/'source.pdf', directory/'raw', mode='auto')

    def test_reuses_one_server_and_enforces_actual_window_and_no_proxies(self):
        self.convert('one'); self.convert('two')
        self.assertEqual(len(self.calls), 3)
        api, first, second = self.calls
        self.assertIn('127.0.0.1', api[0])
        self.assertIn('--api-url', first[0])
        self.assertEqual(first[0][-1], second[0][-1])
        self.assertEqual(api[1]['env']['MINERU_PROCESSING_WINDOW_SIZE'], '5')
        self.assertEqual(api[1]['env']['MINERU_API_MAX_CONCURRENT_REQUESTS'], '1')
        self.assertEqual(api[1]['env']['MINERU_API_SHUTDOWN_ON_STDIN_EOF'], '1')
        self.assertNotIn('HTTPS_PROXY', api[1]['env'])
        self.assertEqual(api[1]['env']['MINERU_TOOLS_CONFIG_JSON'],'/synthetic/models.json')
        self.assertEqual([e['kind'] for e in self.events], ['starting','ready'])

    def test_idle_shutdown_waits_five_minutes_and_keeps_queued_pdf_service(self):
        self.convert('one')
        self.time += 299; self.service.maintain(queued_pdf=False)
        self.assertIsNotNone(self.service.process)
        self.time += 1; self.service.maintain(queued_pdf=True)
        self.assertIsNotNone(self.service.process)
        self.service.maintain(queued_pdf=False)
        self.assertIsNone(self.service.process)
        self.assertEqual(self.events[-1]['reason'], 'idle')
        self.service.maintain(queued_pdf=False)
        self.assertEqual(sum(e['kind']=='stopped' for e in self.events), 1)

    def test_memory_pressure_closes_owned_service_and_pauses_next_conversion(self):
        self.convert('one'); self.ready = False
        self.service.maintain(queued_pdf=True)
        self.assertEqual(self.events[-1]['reason'], 'resources')
        with self.assertRaises(Deferred): self.convert('two')
        self.assertEqual(len(self.calls), 2)

    def test_parent_stop_closes_owned_service_and_refuses_new_clients(self):
        self.convert('one'); self.stop.set()
        with self.assertRaises(Deferred): self.convert('two')
        self.assertIsNone(self.service.process)
        self.assertEqual(self.events[-1]['reason'], 'service_stop')

    def test_previous_lock_blocks_without_signalling_any_process(self):
        fd = os.open(self.service.root/'mineru.lock', os.O_CREAT|os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaises(Deferred): self.service.check_previous()
            with self.assertRaises(Deferred): self.convert('one')
            self.assertEqual(self.calls, [])
        finally: os.close(fd)

    def test_wrong_window_is_not_reported_ready_and_process_is_closed(self):
        self.service.health = lambda _:dict(status='healthy',version='3.1.15',max_concurrent_requests=3,processing_window_size=64)
        with self.assertRaises(ContractError): self.convert('one')
        self.assertNotIn('ready', [e['kind'] for e in self.events])
        self.assertIsNone(self.service.process)

    def test_collector_maintenance_does_not_wait_behind_startup(self):
        self.service.mutex.acquire()
        try: self.service.maintain(queued_pdf=True)
        finally: self.service.mutex.release()
        self.assertEqual(self.calls, [])


class ProfileTests(unittest.TestCase):
    def test_explicit_frozen_dependencies_and_version_are_required(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            source = root/'mineru.py'; source.write_text('synthetic'); source.chmod(0o700)
            manifest = root/'dependencies.json'
            manifest.write_text(json.dumps(dict(reference='P33-mineru-dependencies-v1',mineru_version='3.1.15',files={str(source):file_digest(source)})))
            models = root/'models.json'; models.write_text(json.dumps({'models-dir':{'pipeline':str(root)}}))
            config = dict(options=asdict(PdfOptions()),api_binary=str(source),client_binary=str(source),dependency_manifest=str(manifest),model_config=str(models))
            frozen = {str(source):file_digest(source),str(manifest):file_digest(manifest),str(models):file_digest(models)}
            self.assertEqual(validate_pdf_config(config,frozen).chunk_pages,5)
            self.assertEqual(validate_pdf_config(dict(config,progress=True),frozen).chunk_pages,5)
            with self.assertRaises(ContractError): validate_pdf_config(dict(config,progress='true'),frozen)
            models.write_text(json.dumps({'models-dir':{'pipeline':str(root)},'llm-aided-config':{}}))
            with self.assertRaises(ContractError): validate_pdf_config(config,frozen)
            models.write_text(json.dumps({'models-dir':{'pipeline':str(root)}}))
            with self.assertRaises(ContractError): validate_pdf_config(config,{str(source):file_digest(source)})
            config['options']['chunk_pages'] = 64
            with self.assertRaises(ContractError): validate_pdf_config(config,frozen)


if __name__ == '__main__': unittest.main()
