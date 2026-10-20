"""Synthetic completed libraries: cautious search and reciprocal publication."""
import hashlib
import json
from pathlib import Path
import unittest

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.daily_store import DailyStore
from kb_custody_v21.reading_bundle import build_bundle as reading_bundle, frontmatter, md
from kb_custody_v21.reading_contract import sha
from kb_custody_v21.reading_publish import prepare_plan, publish_plan, snapshot_library
from kb_custody_v21.reading_store import ReadingStore
from kb_custody_v21.relation_catalog import Catalog, RelationProblem, candidates, doi, source_url
from kb_custody_v21.relation_library import GRAPH, INDEX, build_bundle, pair_id
from test_daily_store import policy
from test_reading_content import sample, reviewed, assets
import test_reading_foundation as foundation


class RelationTests(unittest.TestCase):
    def setUp(self):
        self.fixture=foundation.ReadingProfileTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.profile=self.fixture.profile()
        self.daily=DailyStore(self.profile.runtime.root/'queue.sqlite3',policy(),clock=lambda:1000000)
        self.reading=ReadingStore(self.daily);self.catalog=Catalog(self.profile,self.daily)
        self.check=lambda:None

    def material(self, number, title='合成的测试测量方法', *, doi_value='', keywords=('测试','测量'), authors=()):
        source,draft=sample();source['binding'].update(document_id='om_synthetic'+str(number),
            source_id='source_synthetic'+str(number),source_sha256=sha(number),rules_sha256=self.profile.rules_sha256)
        row=self.reading.approve(source['binding'],checked_access=self.check);source['material_id']=row['material_id']
        source['provenance']=dict(source_record=dict(work=dict(title=title,doi=doi_value,
            authors=[dict(name=v) for v in authors]),release_context=dict(channel_type='journal' if doi_value else 'website')))
        draft.update(title=title,tags=list(keywords),cards=[],topics=[])
        snap=snapshot_library(self.profile)
        existing={p:b'' for p in snap['available_paths']};existing.update(snap['existing_files'])
        bundle=reading_bundle(source,draft,reviewed(source,draft),assets=assets(source),date='2026-10-11',existing_files=existing)
        job=self.profile.runtime.root/('reading-jobs/synthetic'+str(number))
        plan=prepare_plan(self.profile,bundle,job,checked_access=self.check)
        receipt=publish_plan(self.profile,job,plan,checked_access=self.check)
        # Fixture represents a completed reviewed publication, without model calls.
        with self.daily.transaction() as db:
            db.execute("UPDATE reading_materials SET state='completed',detail=? WHERE material_id=?",(
                json.dumps(dict(group=bundle['group'],review_receipt=bundle['review_receipt'],
                    publication_receipt=sha(receipt),quality_receipt=sha(receipt))),row['material_id']))
        return self.catalog.get(row['material_id'])

    def graph(self):
        return json.loads((self.profile.root/GRAPH).read_text())

    def plan(self,a,b,*,kind='interpretation',remove=False,operation='synthetic_decision'):
        path=self.profile.root/GRAPH
        edge=self.graph()['edges'].get(pair_id(a['material_id'],b['material_id'])) if path.exists() else None
        bundle=build_bundle(self.profile,self.catalog,operation_id=sha(operation),left=a['material_id'],right=b['material_id'],
            kind=kind,remove=remove,expected_edge=sha(edge) if edge else None,
            evidence=dict(command='本人确认合成关系',event_id=operation),checked_access=self.check)
        job=self.profile.runtime.root/('relation-jobs/'+sha(operation))
        plan=prepare_plan(self.profile,bundle,job,checked_access=self.check)
        return job,plan

    def publish(self,plan,**kw):
        return publish_plan(self.profile,*plan,checked_access=self.check,**kw)

    def test_no_match_is_normal_and_incomplete_materials_never_enter_catalog(self):
        a=self.material(1,'植物的季节观察',keywords=())
        b=self.material(2,'编译器优化研究',keywords=())
        self.assertEqual(candidates(a,self.catalog.all()),[])
        with self.daily.transaction() as db:db.execute("UPDATE reading_materials SET state='reviewing' WHERE material_id=?",(b['material_id'],))
        self.assertNotIn(b['material_id'],self.catalog.all())
        with self.assertRaises(RelationProblem):self.plan(a,b)

    def test_doi_candidates_have_reasons_and_no_inferred_relation_type(self):
        a=self.material(1,'某项研究的公众号解读',doi_value='https://doi.org/10.1234/EXAMPLE')
        b=self.material(2,'Synthetic Original Paper',doi_value='10.1234/example')
        rows=candidates(a,self.catalog.all())
        self.assertEqual(rows[0]['material_id'],b['material_id'])
        self.assertIn('DOI',rows[0]['evidence'][0]);self.assertNotIn('kind',rows[0])
        self.assertFalse((self.profile.root/GRAPH).exists())
        self.assertEqual(self.daily.summary()['model_turns'],0)

    def test_one_author_or_one_keyword_does_not_force_match(self):
        a=self.material(1,'植物的季节观察',keywords=(),authors=('同一作者',))
        self.material(2,'编译器优化研究',keywords=(),authors=('同一作者',))
        self.assertEqual(candidates(a,self.catalog.all()),[])

    def test_search_is_bounded_deterministic_and_does_not_read_originals(self):
        a=self.material(0)
        for i in range(1,7):self.material(i)
        original=self.profile.root/a['group']/'原文.md'
        original.write_text('此正文不参与候选检索。')
        rows=candidates(a,self.catalog.all())
        self.assertEqual(len(rows),5)
        self.assertEqual(rows,candidates(a,self.catalog.all()))
        self.assertNotIn(a['material_id'],[v['material_id'] for v in rows])

    def test_corrupt_completed_metadata_is_error_not_zero_matches(self):
        a=self.material(1)
        (self.profile.root/a['group']/'metadata.json').write_text('{}')
        with self.assertRaises(RelationProblem):self.catalog.all()

    def test_reciprocal_links_original_labels_and_original_bytes_are_preserved(self):
        a,b=self.material(1),self.material(2,doi_value='10.1234/paper')
        originals={p:p.read_bytes() for item in (a,b) for p in (self.profile.root/item['group']).rglob('*')
                   if p.is_file() and p.name not in ('metadata.json','来源卡片.md')}
        plan=self.plan(a,b);receipt=self.publish(plan)
        self.assertEqual(receipt,self.publish(plan))
        self.assertEqual(len(self.graph()['history']),1)
        for first,second,label in ((a,b,'论文原文'),(b,a,'解读')):
            meta,body=frontmatter((self.profile.root/first['group']/'来源卡片.md').read_text())
            self.assertEqual(meta['related'][0]['material_id'],second['material_id']);self.assertIn(label,body)
        self.assertIn('资料关联',(self.profile.root/INDEX).read_text())
        self.assertTrue(all(p.read_bytes()==raw for p,raw in originals.items()))

    def test_crash_recovers_before_commit_marker_without_duplicate_edges(self):
        a,b=self.material(1),self.material(2);plan=self.plan(a,b)
        def crash(path):raise OSError('synthetic crash')
        with self.assertRaises(OSError):self.publish(plan,after_write=crash)
        self.assertFalse((self.profile.root/GRAPH).exists())
        self.publish(plan);self.assertEqual(len(self.graph()['history']),1)

    def test_correction_and_revocation_keep_history_and_other_relations(self):
        a,b,c=self.material(1),self.material(2),self.material(3)
        self.publish(self.plan(a,b));self.publish(self.plan(a,c,kind='topic',operation='second'))
        self.publish(self.plan(b,a,kind='revision',operation='correct'))
        self.assertEqual(self.graph()['edges'][pair_id(a['material_id'],b['material_id'])]['left'],b['material_id'])
        self.publish(self.plan(b,a,kind='revision',remove=True,operation='revoke'))
        self.assertEqual(len(self.graph()['history']),4)
        meta,body=frontmatter((self.profile.root/a['group']/'来源卡片.md').read_text())
        self.assertEqual([v['material_id'] for v in meta['related']],[c['material_id']])
        self.assertNotIn('旧版',body)

    def test_user_edits_and_stale_concurrent_plan_are_never_overwritten(self):
        a,b=self.material(1),self.material(2);plan=self.plan(a,b)
        target=self.profile.root/a['group']/'来源卡片.md'
        original=target.read_bytes();target.write_bytes(original+'\n用户编辑\n'.encode())
        with self.assertRaises(ContractError):self.publish(plan)
        self.assertIn('用户编辑',target.read_text());self.assertFalse((self.profile.root/GRAPH).exists())
        target.write_bytes(original);self.publish(plan)
        text=(self.profile.root/INDEX).read_text();(self.profile.root/INDEX).write_text(text+'\n用户编辑\n')
        with self.assertRaises(RelationProblem):self.plan(a,b,operation='second')

    def test_source_symlink_cannot_write_to_personal_files(self):
        a,b=self.material(1),self.material(2)
        target=self.profile.root/a['group']/'来源卡片.md'
        outside=self.fixture.root/'personal.md';outside.write_text('personal')
        target.unlink();target.symlink_to(outside)
        with self.assertRaises((ContractError,OSError)):self.plan(a,b)
        self.assertEqual(outside.read_text(),'personal')

    def test_identifier_normalization_preserves_query_identity(self):
        self.assertEqual(doi('DOI: 10.1234/AbC'),'10.1234/abc')
        self.assertNotEqual(source_url('https://example.test/article?id=1'),source_url('https://example.test/article?id=2'))
        self.assertEqual(source_url('not a URL'),'')

    def test_handwritten_yaml_date_is_preserved_as_visible_local_problem(self):
        a,b=self.material(1),self.material(2)
        target=self.profile.root/a['group']/'来源卡片.md'
        raw=target.read_text().replace('"created": "2026-10-11"','"created": 2026-10-11')
        target.write_text(raw)
        with self.assertRaisesRegex(RelationProblem,'元数据无法自动改写'):self.plan(a,b)
        self.assertEqual(target.read_text(),raw)
        self.assertFalse((self.profile.root/GRAPH).exists())


if __name__=='__main__':unittest.main()
