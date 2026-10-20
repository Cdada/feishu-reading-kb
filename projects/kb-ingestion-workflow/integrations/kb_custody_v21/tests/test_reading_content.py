"""Synthetic evidence, reading standards and interrupted publication regressions."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.reading_contract import REVIEW_CHECKS, request, sha, validate_draft, validate_review
from kb_custody_v21.reading_bundle import build_bundle, check_links, frontmatter, md, CARDS, TOPICS, BRIEFS
from kb_custody_v21.reading_profile import ReadingProfile
from kb_custody_v21.reading_publish import prepare_plan, publish_plan, snapshot_library
from test_reading_foundation import binding


def sample():
    original='这是一篇在线教程。测试测量在相同条件下进行。\n\n样本不足时只能说明观察结果，不能推广。\n'
    source=dict(material_id='synthetic_material',binding=binding(),filename='在线教程.md',source_type='markdown',
        original=original,excerpts='',provenance={},image_boundary='',user_answers=[],existing_cards={},existing_topics={})
    evidence=dict(source='original',quote='测试测量在相同条件下进行。')
    draft=dict(status='ready',question='',title='测量与边界',category='04_网页文章与在线资源',
        content_nature='nonfiction',source_completeness='full_text',
        identity_evidence=[dict(source='original',quote='这是一篇在线教程。')],collection='',tags=['测试'],
        keywords=dict(attention=[],objective=[dict(term='测量',kind='direct',evidence=evidence)],entities=[]),
        claims=[dict(id='c1',text='教程提出在相同条件下测量。',basis='source_statement',evidence=[evidence],conditions=['仅适用于文中测试情境'])],
        objective=[dict(heading='测量条件',claim_ids=['c1'])],synthesis=[dict(heading='阅读结论',claim_ids=['c1'])],chapters=[],
        cards=[dict(title='测量条件',kind='method_card',existing_path='',reuse_reason='用于检查测量条件是否可比较。',claim_ids=['c1'])],
        topics=[dict(title='实验测量',existing_path='')],limitations=[])
    return source,draft


def reviewed(source,draft,**changes):
    return dict(dict(status='approve',question='',input_sha256=sha(source),draft_sha256=sha(draft),
        validation_status='human_reviewed_deep_read',review_scope='Codex 核对了给定合成全文与全部整理条目。',
        checks={k:True for k in REVIEW_CHECKS},checked_claim_ids=[c['id'] for c in draft['claims']],findings=[]),**changes)


def assets(source):
    return {'提取/source.md':source['original'].encode(),'导读.md':b'# Preserved independent guide\n','原件/online.md':source['original'].encode()}


class ReadingContentTests(unittest.TestCase):
    def setUp(self):
        self.source,self.draft=sample()

    def test_explicit_excerpt_cannot_claim_complete_original(self):
        self.source['provenance']['input_role']='excerpt_only'
        self.source['excerpts']=self.source['original']
        with self.assertRaises(ContractError):validate_draft(self.draft,self.source)
        self.draft['source_completeness']='excerpt_only';self.draft['limitations']=['仅提供摘录。']
        validate_draft(self.draft,self.source)

    def test_existing_collection_and_chapter_navigation_are_preserved(self):
        self.source['provenance']['existing_collections']={'04_网页文章与在线资源':['测试专栏']}
        self.draft['collection']='测试专栏'
        self.draft['chapters']=[dict(title='第一章',sections=self.draft['objective'])]
        bundle=self.bundle()
        self.assertIn('/测试专栏/',bundle['group'])
        note=bundle['files'][bundle['group']+'/综合读书笔记.md'].decode()
        self.assertIn('章节深读',note)
        self.draft['collection']='自行发明的集合'
        with self.assertRaises(ContractError):validate_draft(self.draft,self.source)

    def test_owner_clarification_can_identify_source_but_cannot_supply_factual_claims(self):
        self.source['user_answers']=[dict(text='这是课堂讲义。',receipt_sha256='a'*64)]
        e=dict(source='user_answer',quote='这是课堂讲义。')
        self.draft['identity_evidence']=[e];self.draft['category']='02_课程'
        validate_draft(self.draft,self.source)
        self.draft['claims'][0]['evidence']=[e]
        with self.assertRaises(ContractError):validate_draft(self.draft,self.source)

    def test_full_grounded_draft_and_review_are_bound(self):
        validate_draft(self.draft,self.source)
        validate_review(reviewed(self.source,self.draft),self.draft,self.source)
        wrong=reviewed(self.source,self.draft);wrong['draft_sha256']='0'*64
        with self.assertRaises(ContractError):validate_review(wrong,self.draft,self.source)

    def test_completeness_requires_an_option_before_review_or_publication(self):
        self.draft['source_completeness']='已整理本次提供的全部正文。'
        with self.assertRaisesRegex(ContractError,'source completeness required'):
            validate_draft(self.draft,self.source)
        self.draft.update(status='needs_user_input',question='这是全文还是摘录？',
            category='',content_nature='',source_completeness='')
        validate_draft(self.draft,self.source)
        self.draft.update(status='ready',question='')
        with self.assertRaises(ContractError):validate_draft(self.draft,self.source)

    def test_review_instructions_separate_positive_evidence_from_open_findings(self):
        rules={key:'synthetic' for key in ('archive_rules','organize_prompt','nlp_skill')}
        instruction=request('review',self.source,rules,self.draft)['instruction']
        self.assertIn('findings 必须为空数组',instruction)
        self.assertIn('依据与说明写入 review_scope',instruction)
        review=reviewed(self.source,self.draft,findings=['引文核对通过。'])
        with self.assertRaisesRegex(ContractError,'approval cannot retain open findings'):
            validate_review(review,self.draft,self.source)

    def test_modified_and_ambiguous_quotes_are_rejected(self):
        for q in ('并不存在的结论','。'):
            with self.subTest(q=q):
                draft=deepcopy(self.draft);draft['claims'][0]['evidence'][0]['quote']=q
                with self.assertRaises(ContractError):validate_draft(draft,self.source)

    def test_unresolved_source_cannot_pass_to_rendering(self):
        draft=deepcopy(self.draft);draft.update(status='needs_user_input',question='这是课程章节还是独立文章？')
        self.assertEqual(validate_draft(draft,self.source)['status'],'needs_user_input')
        with self.assertRaises(ContractError):validate_review(reviewed(self.source,draft),draft,self.source)

    def test_fiction_cannot_create_or_merge_factual_card(self):
        self.draft['content_nature']='fiction'
        with self.assertRaises(ContractError):validate_draft(self.draft,self.source)
        self.draft['cards'][0]['kind']='fiction_interpretation_card'
        bundle=self.bundle()
        card=next(v for p,v in bundle['files'].items() if p.startswith(CARDS))
        meta,_=frontmatter(card.decode())
        self.assertEqual(meta['knowledge_status'],'hypothesis_not_verified')
        topic=next(v for p,v in bundle['files'].items() if p.startswith(TOPICS))
        self.assertIn('虚构作品中的思辨材料',topic.decode())

    def test_mixed_speculation_cannot_be_factual_card(self):
        self.draft['content_nature']='mixed';self.draft['claims'][0]['basis']='scenario'
        with self.assertRaises(ContractError):validate_draft(self.draft,self.source)

    def test_no_excerpts_no_invented_personal_attention(self):
        self.draft['keywords']['attention']=deepcopy(self.draft['keywords']['objective'])
        with self.assertRaises(ContractError):validate_draft(self.draft,self.source)

    def test_incomplete_and_unread_images_cannot_be_full_deep_read(self):
        self.draft['source_completeness']='excerpt_only';self.draft['limitations']=['仅有摘录。']
        with self.assertRaises(ContractError):validate_review(reviewed(self.source,self.draft),self.draft,self.source)
        validate_review(reviewed(self.source,self.draft,validation_status='human_reviewed_source_incomplete'),self.draft,self.source)
        self.draft['source_completeness']='full_text';self.source['image_boundary']='图片尚未读取。'
        with self.assertRaises(ContractError):validate_review(reviewed(self.source,self.draft),self.draft,self.source)

    def test_review_must_cover_all_claims_and_pass_all_checks(self):
        review=reviewed(self.source,self.draft);review['checks']['coverage']=False
        with self.assertRaises(ContractError):validate_review(review,self.draft,self.source)
        review=reviewed(self.source,self.draft);review['checked_claim_ids']=[]
        with self.assertRaises(ContractError):validate_review(review,self.draft,self.source)

    def test_full_input_is_never_silently_truncated(self):
        self.source['original']='长正文'*400000
        with self.assertRaises(ContractError):request('organize',self.source,{})

    def test_drafts_do_not_control_filenames_or_completion_flags(self):
        self.draft['status']='completed'
        with self.assertRaises(ContractError):validate_draft(self.draft,self.source)
        self.draft['status']='ready';self.draft['cards'][0]['existing_path']='../../personal.md'
        with self.assertRaises(ContractError):validate_draft(self.draft,self.source)

    def bundle(self,**kwargs):
        return build_bundle(self.source,self.draft,reviewed(self.source,self.draft),assets=assets(self.source),date='2026-10-11',existing_files={},**kwargs)

    def test_complete_file_set_preserves_original_and_guide(self):
        bundle=self.bundle();group=bundle['group'];files=bundle['files']
        for name in ('来源卡片.md','原文.md','关键词提取.md','文章客观整理.md','摘录关注整理.md','综合读书笔记.md','metadata.json'):
            self.assertIn(group+'/'+name,files)
        self.assertEqual(files[group+'/原文.md'],self.source['original'].encode())
        self.assertEqual(files[group+'/入库依据/导读.md'],assets(self.source)['导读.md'])
        self.assertNotIn(group+'/摘录.md',files)
        self.assertIn('不能推断个人偏好',files[group+'/摘录关注整理.md'].decode())
        meta=json.loads(files[bundle['metadata_path']]);self.assertEqual(meta['review_method'],'codex')
        self.assertEqual(meta['status'],'completed')

    def test_private_source_is_completely_absent_from_brief(self):
        self.draft['category']='06_个人数据与资料包'
        bundle=self.bundle()
        self.assertFalse(any(p.startswith(BRIEFS) for p in bundle['files']))
        meta=json.loads(bundle['files'][bundle['metadata_path']]);self.assertEqual(meta['brief_visibility'],'exclude')

    def test_no_card_needed_is_valid(self):
        self.draft['cards']=[];self.draft['topics']=[]
        bundle=self.bundle();self.assertFalse(any(p.startswith(CARDS) or p.startswith(TOPICS) for p in bundle['files']))

    def test_bad_local_link_and_yaml_are_rejected(self):
        for target in ('../../../../personal.md','missing.md','file:///private/personal.md'):
            with self.subTest(target=target),self.assertRaises(ContractError):
                check_links('20_Cards_知识卡片/test.md',md({},'[link]('+target+')').decode(),{})
        for raw in ('---\na: 1\na: 2\n---\nbody','---\na: [\n---\nbody','---\n!!python/object/apply:os.system [whoami]\n---\nbody'):
            with self.assertRaises(ContractError):frontmatter(raw)


class ReadingPublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name).resolve()
        runtime=SimpleNamespace(root=self.base/'.dev-runtime/test',worktree=self.base);runtime.root.mkdir(parents=True)
        runtime.path=lambda relative:runtime.root/relative
        runtime.check_storage=lambda:None
        def ensure_directory(relative):
            target=runtime.path(relative);target.mkdir(parents=True,exist_ok=True);return target
        runtime.ensure_directory=ensure_directory
        script=self.base/'organize.mjs';script.write_text('// fixture')
        frozen={str(script):hashlib.sha256(script.read_bytes()).hexdigest()};rules={}
        for role in ('archive_rules','organize_prompt','nlp_skill'):
            p=self.base/(role+'.md');p.write_text('synthetic rule')
            h=hashlib.sha256(p.read_bytes()).hexdigest();frozen[str(p)]=h;rules[role]=dict(path=str(p),sha256=h)
        self.profile=ReadingProfile(runtime,dict(reference='P41-reading-v1',root=str(runtime.root/'reading-library'),rules=rules,script=str(script)),frozen)
        self.job=runtime.root/'reading-jobs/synthetic'
        self.source,self.draft=sample()
        self.source['binding']['rules_sha256']=self.profile.rules_sha256
        self.bundle=build_bundle(self.source,self.draft,reviewed(self.source,self.draft),assets=assets(self.source),date='2026-10-11',existing_files={})
        self.plan=prepare_plan(self.profile,self.bundle,self.job,checked_access=lambda:None)

    def publish(self,**kwargs):
        return publish_plan(self.profile,self.job,self.plan,checked_access=lambda:None,**kwargs)

    def test_restart_finishes_same_plan_without_duplicate_brief(self):
        writes=[]
        def crash(path):
            writes.append(path)
            if len(writes)==3:raise OSError('synthetic interruption')
        with self.assertRaises(OSError):self.publish(after_write=crash)
        self.assertFalse((self.profile.root/self.bundle['metadata_path']).exists())
        receipt=self.publish();self.assertEqual(self.publish(),receipt)
        brief=self.profile.root/(BRIEFS+'/2026-10-11.md')
        meta,_=frontmatter(brief.read_text());self.assertEqual(meta['reading_materials'],[self.source['material_id']])

    def test_user_edit_is_preserved_before_any_further_writes(self):
        self.publish()
        card=next(p for p in self.bundle['files'] if p.startswith(CARDS))
        target=self.profile.root/card;target.write_text('user edit')
        with self.assertRaises(ContractError):self.publish()
        self.assertEqual(target.read_text(),'user edit')

    def test_symlink_and_swapped_root_do_not_redirect_publication(self):
        external=self.base/'personal';external.mkdir()
        (self.profile.root/CARDS).symlink_to(external,target_is_directory=True)
        with self.assertRaises(ContractError):self.publish()
        self.assertEqual(list(external.iterdir()),[])

    def test_mutated_payload_is_rejected(self):
        path=self.job/'payload'/self.bundle['metadata_path'];path.write_text('{}')
        with self.assertRaises(ContractError):self.publish()
        self.assertFalse((self.profile.root/self.bundle['metadata_path']).exists())

    def test_existing_wikilink_survives_incremental_card_publication(self):
        self.publish()
        card_path=next(p for p in self.bundle['files'] if p.startswith(CARDS))
        card_file=self.profile.root/card_path
        support=self.profile.root/self.bundle['group']/'支持.md';support.write_text('prior supporting source')
        card_file.write_text(card_file.read_text()+'\n[[支持]]\n')
        snap=snapshot_library(self.profile)
        source,draft=sample();source['material_id']='synthetic_wiki';source['binding']['source_id']='source_wiki'
        source['binding']['rules_sha256']=self.profile.rules_sha256
        source['existing_cards']={card_path:dict(sha256=hashlib.sha256(card_file.read_bytes()).hexdigest(),text=card_file.read_text())}
        draft['cards'][0]['existing_path']=card_path;draft['topics']=[]
        existing={p:b'' for p in snap['available_paths']};existing.update(snap['existing_files'])
        bundle=build_bundle(source,draft,reviewed(source,draft),assets=assets(source),date='2026-10-11',existing_files=existing)
        job=self.job.parent/'wiki';plan=prepare_plan(self.profile,bundle,job,checked_access=lambda:None)
        publish_plan(self.profile,job,plan,checked_access=lambda:None)
        self.assertIn('[[支持]]',card_file.read_text())
        support.unlink()
        with self.assertRaises(ContractError):publish_plan(self.profile,job,plan,checked_access=lambda:None)

    def test_incremental_card_preserves_old_content_and_sources(self):
        self.publish();snap=snapshot_library(self.profile)
        source,draft=sample();source['material_id']='synthetic_second';source['binding']['source_id']='source_second'
        source['binding']['rules_sha256']=self.profile.rules_sha256
        for category,key in [(CARDS,'existing_cards'),(TOPICS,'existing_topics')]:
            source[key]={p:dict(sha256=hashlib.sha256(raw).hexdigest(),text=raw.decode()) for p,raw in snap['existing_files'].items() if p.startswith(category+'/')}
        card=next(iter(source['existing_cards']));topic=next(iter(source['existing_topics']))
        draft['cards'][0]['existing_path']=card;draft['topics'][0]['existing_path']=topic
        # Every earlier source link remains resolvable in the combined view.
        existing={p:b'' for p in snap['available_paths']};existing.update(snap['existing_files'])
        bundle=build_bundle(source,draft,reviewed(source,draft),assets=assets(source),date='2026-10-11',existing_files=existing)
        job=self.job.parent/'second';plan=prepare_plan(self.profile,bundle,job,checked_access=lambda:None)
        publish_plan(self.profile,job,plan,checked_access=lambda:None)
        meta,body=frontmatter((self.profile.root/card).read_text())
        self.assertEqual(meta['sources'],[self.source['binding']['source_id'],'source_second'])
        self.assertEqual(body.count('## 新增来源补充'),2)
        brief,_=frontmatter((self.profile.root/(BRIEFS+'/2026-10-11.md')).read_text())
        self.assertEqual(len(brief['reading_materials']),2)


if __name__=='__main__':unittest.main()
