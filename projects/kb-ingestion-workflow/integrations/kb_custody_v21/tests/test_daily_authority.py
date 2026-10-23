"""No activation, changed rules and changed source fail before live composition."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.daily_authority import DailyAuthority,draft_rules
from kb_custody_v21.f05 import file_sha
from kb_custody_v21.runtime import DEFAULTS
from test_daily_store import policy


class AuthorityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.w=Path(self.tmp.name).resolve()
        self.root=self.w/'.dev-runtime/kb-v21';self.root.mkdir(parents=True)
        self.source=self.w/'source.py';self.source.write_text('value=1\n')
        self.manifest=self.root/'manifest.json'
        self.manifest.write_text(json.dumps(dict(reference='P32-source-v1',worktree=str(self.w),source_commit='a'*40,files={str(self.source):file_sha(self.source)})))
        self.path=self.root/'rules.local.json'
        self.value=draft_rules(self.w,DEFAULTS,scope=policy().scope)
        self.value.update(approved=True,approval_note='synthetic offline authorization',initial_history_start=1,
            source_manifest=str(self.manifest),source_manifest_sha256=file_sha(self.manifest),
            guide_plan=str(self.source),cli_guard=str(self.source),text_node=str(self.source),text_script=str(self.source),codex_binary=str(self.source))
        self.runtime=SimpleNamespace(worktree=self.w,root=self.root,data_root=self.root,gate=lambda:None)
        self.mock=patch('kb_custody_v21.daily_authority.Runtime',return_value=self.runtime)
        self.mock.start();self.addCleanup(self.mock.stop)
        self.source_names=b'source.py\0'
        self.dirty=b''
        self.git=patch('kb_custody_v21.daily_authority.subprocess.check_output',side_effect=lambda args: self.dirty if 'status' in args else b'a'*40 if 'rev-parse' in args else self.source_names)
        self.git.start();self.addCleanup(self.git.stop)

    def save(self):
        self.path.write_text(json.dumps(self.value))
        return file_sha(self.path)

    def test_archive_mentions_require_explicit_profile_and_cards(self):
        self.value['archive_mentions'] = dict(reference='P46-archive-mention-v1', enabled=True)
        with self.assertRaisesRegex(ContractError, 'archive mention profile'):
            DailyAuthority(self.path, self.save())
        self.value['cards'] = dict(reference='P40-cards-v1', enabled=True)
        self.assertIsNotNone(DailyAuthority(self.path, self.save()))
        self.value['archive_mentions']['enabled'] = False
        with self.assertRaisesRegex(ContractError, 'archive mention profile'):
            DailyAuthority(self.path, self.save())

    def test_unapproved_template_cannot_create_live_authority(self):
        self.value['approved']=False
        with self.assertRaisesRegex(ContractError,'not been approved'):DailyAuthority(self.path,self.save())

    def test_explicit_hash_is_required_and_cannot_follow_a_mutation(self):
        digest=self.save()
        self.value['policy']['model_turn_limit']=100
        self.save()
        with self.assertRaisesRegex(ContractError,'rules changed'):DailyAuthority(self.path,digest)

    def test_invalid_history_boundary_stops_before_any_runtime_access(self):
        for value in (None,True,float('nan'),float('inf'),-1,999999999999):
            self.value['initial_history_start']=value
            with self.subTest(value=value),self.assertRaises(ContractError):DailyAuthority(self.path,self.save())

    def test_same_size_source_edit_with_restored_mtime_is_detected(self):
        authority=DailyAuthority(self.path,self.save())
        before=self.source.stat()
        authority.check()
        self.source.write_text('value=2\n')
        os.utime(self.source,ns=(before.st_atime_ns,before.st_mtime_ns))
        with self.assertRaisesRegex(ContractError,'source or dependency changed'):authority.check()

    def test_added_unbound_source_is_rejected(self):
        authority=DailyAuthority(self.path,self.save())
        self.source_names=b'source.py\0new.py\0'
        with self.assertRaisesRegex(ContractError,'omits current source'):authority.check()

    def test_dirty_source_refuses_real_execution_even_with_same_hashes(self):
        authority=DailyAuthority(self.path,self.save())
        self.dirty=b' M source.py'
        with self.assertRaisesRegex(ContractError,'clean source commit'):authority.check()


if __name__=='__main__':unittest.main()
