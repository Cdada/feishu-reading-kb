"""QA source/model capability is opt-in and pinned to a frozen script."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.f05 import file_sha
from kb_custody_v21.qa_profile import QAProfile


class QAProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.script=self.root/'qa.mjs';self.script.write_text('// synthetic')
        self.a=SimpleNamespace(reading=object(),config={'cards':{}},runtime=SimpleNamespace(worktree=self.root),frozen={str(self.script):file_sha(self.script)})
        self.config=dict(reference='P43-qa-v1',enabled=True,since=1000,script=str(self.script))

    def test_default_has_zero_model_capability(self):
        self.assertIsNone(QAProfile(self.a,self.config).generation)

    def test_unbound_script_and_missing_test_library_are_rejected(self):
        self.script.write_text('// changed')
        with self.assertRaises(ContractError):QAProfile(self.a,self.config)
        self.a.reading=None
        with self.assertRaises(ContractError):QAProfile(self.a,self.config)

    def test_generation_must_bind_account_and_finite_wait(self):
        self.config['generation']=dict(enabled=True,account_sha256='a'*64,wait_seconds=1200,minimum_remaining_percent=5)
        self.assertIsNotNone(QAProfile(self.a,self.config).generation)
        self.config['generation']['wait_seconds']=3600
        with self.assertRaises(ContractError):QAProfile(self.a,self.config)
