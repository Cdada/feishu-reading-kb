"""Test-only paths, durable progress and shared cumulative model accounting."""
from dataclasses import replace
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.daily_store import DailyStore, Deferred
from kb_custody_v21.reading_profile import ReadingProfile
from kb_custody_v21.reading_store import ReadingStore
from test_daily_store import policy


def binding():
    return dict(document_id='om_synthetic', source_id='source_synthetic', extraction_id='extract_synthetic',
        source_sha256='a'*64, publication_id='publication_synthetic', preview_sha256='b'*64,
        preview_version=1, decision_sha256='c'*64, rules_sha256='d'*64)


class ReadingProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()
        self.runtime=SimpleNamespace(worktree=self.root,root=self.root/'.dev-runtime/test')
        self.runtime.root.mkdir(parents=True)
        self.runtime.path=lambda relative:self.runtime.root/relative
        self.runtime.check_storage=lambda:None
        def ensure_directory(relative):
            target=self.runtime.path(relative);target.mkdir(parents=True,exist_ok=True);return target
        self.runtime.ensure_directory=ensure_directory
        self.script=self.root/'organize.mjs';self.script.write_text('// synthetic\n')
        self.rules={}
        self.frozen={str(self.script):hashlib.sha256(self.script.read_bytes()).hexdigest()}
        for role in ('archive_rules','organize_prompt','nlp_skill'):
            p=self.root/(role+'.md');p.write_text('# synthetic normative reference\n')
            h=hashlib.sha256(p.read_bytes()).hexdigest()
            self.rules[role]=dict(path=str(p),sha256=h);self.frozen[str(p)]=h
        self.config=dict(reference='P41-reading-v1',root=str(self.runtime.root/'reading-library'),
                         rules=self.rules,script=str(self.script))

    def profile(self):
        return ReadingProfile(self.runtime,self.config,self.frozen)

    def test_explicit_profile_does_not_create_or_scan_a_library(self):
        p=self.profile()
        self.assertFalse(p.root.exists())
        self.assertEqual(set(p.rule_texts()),set(self.rules))

    def test_formal_legacy_parent_and_other_runtime_targets_are_rejected(self):
        for root in (self.root/'个人知识库', self.runtime.root, self.runtime.root/'source-staging',
                     self.runtime.root.parent/'reading-library'):
            self.config['root']=str(root)
            with self.subTest(root=root),self.assertRaises(ContractError):self.profile()

    def test_missing_root_and_relative_root_do_not_fall_back_to_personal_defaults(self):
        for root in ('', 'reading-library', str(self.runtime.root/'..'/'reading-library')):
            self.config['root']=root
            with self.subTest(root=root),self.assertRaises(ContractError):self.profile()
        del self.config['root']
        with self.assertRaises(ContractError):self.profile()

    def test_rules_and_model_script_must_be_frozen_and_cannot_change(self):
        p=self.profile()
        Path(self.rules['archive_rules']['path']).write_text('# changed\n')
        with self.assertRaisesRegex(ContractError,'version changed'):p.check()
        with self.assertRaises(ContractError):ReadingProfile(self.runtime,self.config,{})

    def test_symlink_target_or_output_cannot_escape(self):
        p=self.profile()
        outside=self.root/'unrelated';outside.mkdir()
        p.root.symlink_to(outside,target_is_directory=True)
        with self.assertRaises(ContractError):p.check()
        p.root.unlink();p.root.mkdir()
        (p.root/'20_Cards_知识卡片').symlink_to(outside,target_is_directory=True)
        with self.assertRaises(ContractError):p.target('20_Cards_知识卡片/item.md')

    def test_output_cannot_target_state_credentials_or_traverse(self):
        p=self.profile()
        for relative in ('../escape.md','/absolute.md','10_Reading_读书摘录/../escape.md',
                         '.codex/config.toml','50_Daily_Briefs_知识简报\\escape.md','10_Reading_读书摘录//a.md'):
            with self.subTest(relative=relative),self.assertRaises(ContractError):p.target(relative)
        self.assertEqual(p.target('10_Reading_读书摘录/source/来源卡片.md').parent.name,'source')


class ReadingStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name).resolve()/'queue.sqlite3'
        self.daily=DailyStore(self.path,policy(),clock=lambda:1000000)
        self.reading=ReadingStore(self.daily)
        self.check=lambda:None

    def material(self):
        return self.reading.approve(binding(),checked_access=self.check)

    def advance(self,key,old,new,**details):
        return self.reading.advance(key,old,new,detail=details,checked_access=self.check)

    def reserve(self,key,purpose='organize'):
        return self.reading.reserve_call(key,purpose,'e'*64,checked_access=self.check)

    def test_no_legacy_documents_are_automatically_marked_complete(self):
        self.assertEqual(self.daily.rows('SELECT * FROM reading_materials'),[])
        self.assertEqual(self.material()['state'],'approved')

    def test_approval_is_idempotent_but_new_version_or_rules_require_a_new_decision(self):
        row=self.material()
        self.assertEqual(self.material(),row)
        for field,value in (('preview_version',2),('source_sha256','f'*64),('rules_sha256','f'*64)):
            with self.subTest(field=field),self.assertRaises(ContractError):
                self.reading.approve(dict(binding(),**{field:value}),checked_access=self.check)
        self.assertEqual(len(self.daily.rows('SELECT * FROM reading_materials')),1)

    def test_approval_requires_current_access_before_any_write(self):
        def revoked():raise ContractError('revoked')
        with self.assertRaisesRegex(ContractError,'revoked'):
            self.reading.approve(binding(),checked_access=revoked)
        self.assertEqual(self.daily.rows('SELECT * FROM reading_materials'),[])

    def test_restart_preserves_progress_and_stale_workers_cannot_overwrite(self):
        key=self.material()['material_id'];self.advance(key,'approved','organizing')
        reopened=ReadingStore(DailyStore(self.path,policy(),clock=lambda:1000000))
        self.assertEqual(reopened.get(key)['state'],'organizing')
        with self.assertRaisesRegex(ContractError,'stale'):
            self.advance(key,'approved','failed',failure='old failure')

    def test_clarification_requires_a_question_and_keeps_original_binding(self):
        row=self.material();key=row['material_id']
        with self.assertRaises(ContractError):self.advance(key,'approved','needs_user_input')
        waiting=self.advance(key,'approved','needs_user_input',question='资料来自课程还是讲座？')
        self.assertEqual(waiting['binding'],row['binding'])
        self.assertEqual(self.advance(key,'needs_user_input','approved',answer='讲座')['state'],'approved')

    def test_completion_requires_reviewed_bundle_and_verified_output(self):
        key=self.material()['material_id']
        with self.assertRaises(ContractError):self.advance(key,'approved','completed')
        self.advance(key,'approved','organizing');self.advance(key,'organizing','reviewing')
        with self.assertRaises(ContractError):self.advance(key,'reviewing','ready')
        self.advance(key,'reviewing','ready',review_receipt='review.json',bundle_sha256='a'*64)
        self.advance(key,'ready','publishing')
        with self.assertRaises(ContractError):self.advance(key,'publishing','completed')
        done=self.advance(key,'publishing','completed',publication_receipt='manifest.json',quality_receipt='quality.json')
        self.assertEqual(done['state'],'completed')
        with self.assertRaises(ContractError):self.advance(key,'completed','approved')

    def test_guide_and_reading_calls_share_one_cumulative_cap(self):
        self.daily=DailyStore(self.path,replace(policy(),model_turn_limit=2),clock=lambda:1000000)
        self.reading=ReadingStore(self.daily)
        self.daily.reserve_model('a'*64)
        key=self.material()['material_id'];self.advance(key,'approved','organizing')
        call=self.reserve(key)
        self.assertNotEqual(call['model_key'],'a'*64)
        self.assertEqual(self.daily.summary()['model_turns'],2)
        self.advance(key,'organizing','reviewing')
        with self.assertRaisesRegex(Deferred,'model_limit'):self.reserve(key,'review')
        self.assertIsNone(self.reading.call(key,'review'))

    def test_unresolved_call_is_not_automatically_retried_after_restart(self):
        key=self.material()['material_id'];self.advance(key,'approved','organizing')
        self.reserve(key)
        self.reading.save_call_result(key,'organize','unknown',dict(reason='timeout'),checked_access=self.check)
        self.reading=ReadingStore(DailyStore(self.path,policy(),clock=lambda:1000000))
        with self.assertRaisesRegex(Deferred,'already_reserved'):self.reserve(key)
        self.assertEqual(self.daily.summary()['model_turns'],1)
        self.reading.save_call_result(key,'organize','validated',dict(receipt='saved.json'),checked_access=self.check)
        self.assertEqual(self.reading.call(key,'organize')['result'],dict(receipt='saved.json'))
        with self.assertRaises(ContractError):
            self.reading.save_call_result(key,'organize','rejected',{},checked_access=self.check)

    def test_revocation_rolls_back_both_call_intent_and_cumulative_reservation(self):
        key=self.material()['material_id'];self.advance(key,'approved','organizing')
        calls=[]
        def revoke_before_write():
            calls.append(1)
            if len(calls)==2:raise ContractError('revoked')
        with self.assertRaisesRegex(ContractError,'revoked'):
            self.reading.reserve_call(key,'organize','e'*64,checked_access=revoke_before_write)
        self.assertEqual(self.daily.summary()['model_turns'],0)
        self.assertIsNone(self.reading.call(key,'organize'))

    def test_model_receipt_preserves_original_source_and_purpose(self):
        key=self.material()['material_id'];self.advance(key,'approved','organizing');self.reserve(key)
        self.reading.save_call_result(key,'organize','validated',dict(receipt='saved.json'),checked_access=self.check)
        import json
        row=self.daily.rows('SELECT * FROM daily_models')[0]
        evidence=json.loads(row['evidence'])
        self.assertEqual(evidence['original_sha256'],'a'*64)
        self.assertEqual(evidence['purpose'],'reading_organize')
        self.assertEqual(row['state'],'validated')


if __name__=='__main__':unittest.main()
