from datetime import datetime,timedelta,timezone
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
import base64
from io import BytesIO
from PIL import Image

from pypdf import PdfReader
from kb_custody_v21.content import ReadingGuideAdapter,empty_record
from kb_custody_v21.contracts import ContractError
from kb_custody_v21.image_policy import ImagePolicies
from kb_custody_v21.publication import Publications
from kb_custody_v21.conversion import sha
from fixtures_guide import SyntheticGuide
import test_conversion_v21 as conversion_fixture


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = conversion_fixture.ConversionTests()
        self.fixture.setUp()
        self.runtime = self.fixture.runtime
        self.registry = self.fixture.registry
        self.entry = self.fixture.receive('街道树荫影响步行路线。\n这是一份合成测试材料。\n'.encode())
        _,self.mapping = self.fixture.convert(self.entry)
        _,_,root = self.fixture.conversion.extraction(self.mapping['extraction_id'])
        self.body = (root/'source.md').read_bytes()
        record = empty_record('街道树荫与步行路线：合成验收样例','synthetic-user-confirmation')
        consent = dict(event_id='synthetic-consent',asset_id=self.mapping['output_asset_id'],sha256=sha(self.body),
            extraction_id=self.mapping['extraction_id'],ranges=[[0,len(self.body.decode())]],model='synthetic-reader',
            expires_at=(datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat())
        self.record,self.curation,self.guide = ReadingGuideAdapter(SyntheticGuide()).generate(record,self.body,self.mapping,consent)
        engine = ImagePolicies(self.registry)
        plan = engine.decide([],dict(material_type='unknown',reliable=False,basis_refs=[]))
        self.policy = engine.execute(plan,[],self.runtime.root/'conversion-work/no-images')
        self.publisher = Publications(self.registry)
        self.font = os.environ.get('KB_V21_PREVIEW_FONT','/System/Library/Fonts/STHeiti Light.ttc')

    def prepare(self,**options):
        return self.publisher.prepare(self.entry,self.record,self.mapping,self.body,self.curation,self.guide,self.policy,
                                      font_path=self.font,**options)

    def publish(self):
        preview = self.prepare()
        self.publisher.confirm(preview['publication_id'],preview['preview_sha256'],'synthetic-reviewer','synthetic-confirmation')
        final = self.publisher.publish(preview['publication_id'])
        return preview,final

    def test_exact_confirmed_pdf_minimal_bundle_no_runtime_text(self):
        preview = self.prepare()
        pdf = Path(preview['preview_path']).read_bytes()
        reader = PdfReader(preview['preview_path'])
        text = '\n'.join(page.extract_text() for page in reader.pages)
        self.assertIn('街道树荫',text)
        for internal in ('sha256','pending','task_','source_staging','cache','/Users/'):
            self.assertNotIn(internal,text)
        self.publisher.confirm(preview['publication_id'],sha(pdf),'synthetic-reviewer','synthetic-confirmation')
        with patch('kb_custody_v21.publication.render_preview',side_effect=AssertionError('must not rerender')):
            root = self.publisher.publish(preview['publication_id'])
        self.assertEqual((root/'发布记录/已确认入库预览.pdf').read_bytes(),pdf)
        metadata = json.loads((root/'metadata.json').read_text())
        self.assertEqual(metadata['assets'][0]['local_path'],None)
        self.assertEqual(sha((root/'.kb-manifest.json').read_bytes()),preview['manifest_sha256'])
        self.assertEqual({p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()},
                         {'.kb-manifest.json','metadata.json','curation.json','导读.md','提取/source.md','提取/extraction-map.json','发布记录/已确认入库预览.pdf'})
        # Convenient stable local pointer to visually inspect this actual sample.
        (self.runtime.base/'test-output/preview-sample.local.json').write_text(json.dumps({'path':str(root/'发布记录/已确认入库预览.pdf')}))

    def test_candidate_change_invalidates_confirmation(self):
        preview = self.prepare()
        Path(preview['preview_path']).write_bytes(b'changed')
        with self.assertRaises(ContractError):
            self.publisher.confirm(preview['publication_id'],preview['preview_sha256'],'synthetic-reviewer','synthetic-confirmation')

    def test_known_custody_damage_blocks_publication_pending_cloud_does_not(self):
        preview = self.prepare()
        self.publisher.confirm(preview['publication_id'],preview['preview_sha256'],'synthetic-reviewer','synthetic-confirmation')
        self.fixture.original = self.runtime.root/self.registry.entry(self.entry)['relative_path']
        self.fixture.original.write_bytes(b'corrupt')
        with self.assertRaises(Exception): self.publisher.publish(preview['publication_id'])
        self.assertFalse((self.publisher.root/preview['publication_id']).exists())

    def test_explicit_revision_preserves_older_publication_and_withdraw_restore(self):
        first,oldroot = self.publish()
        before = (oldroot/'.kb-manifest.json').read_bytes()
        with self.assertRaisesRegex(ContractError,'revision_of'): self.prepare()
        second = self.prepare(revision_of=first['publication_id'])
        self.publisher.confirm(second['publication_id'],second['preview_sha256'],'synthetic-reviewer','synthetic-confirmation-2')
        self.publisher.publish(second['publication_id'])
        self.assertEqual((oldroot/'.kb-manifest.json').read_bytes(),before)
        self.publisher.withdraw(second['publication_id'],'synthetic-withdraw')
        self.assertFalse(self.registry.rows('SELECT * FROM active_publications_v21'))
        self.publisher.restore(second['publication_id'],'synthetic-restore')
        self.assertEqual(self.registry.rows('SELECT * FROM active_publications_v21')[0]['publication_id'],second['publication_id'])

    def test_crash_after_atomic_rename_reconciles_same_publication(self):
        preview = self.prepare()
        pid = preview['publication_id']
        self.publisher.confirm(pid,preview['preview_sha256'],'synthetic-reviewer','synthetic-confirmation')
        original = self.publisher._tree
        def crash(row,name):
            if name == pid: raise RuntimeError('synthetic crash after rename')
            return original(row,name)
        with patch.object(self.publisher,'_tree',side_effect=crash):
            with self.assertRaises(RuntimeError): self.publisher.publish(pid)
        self.assertEqual(self.publisher._row(pid)['state'],'publishing')
        final = self.publisher.publish(pid)
        self.assertEqual(final.name,pid)
        self.assertEqual(len(self.registry.rows("SELECT * FROM custody_events WHERE event_type='publication_published'")),1)

    def test_actual_embedded_image_extract_text_only_reexpression_and_publish(self):
        from kb_custody_v21.image_links import rewrite_images
        data = BytesIO()
        Image.new('RGB',(50,40),'navy').save(data,'PNG')
        markdown = '街道树荫影响步行路线。\n\n![合成示意图](data:image/png;base64,'+base64.b64encode(data.getvalue()).decode()+')\n'
        entry = self.fixture.receive(markdown.encode(),entry_key='image-entry',received_filename='image-synthetic.md')
        _,mapping = self.fixture.convert(entry,'image-convert-0001')
        row,_,root = self.fixture.conversion.extraction(mapping['extraction_id'])
        body = (root/'source.md').read_bytes()
        inventory = json.loads(row['inventory_json'])
        self.assertEqual(len(inventory),1)
        self.assertEqual(inventory[0]['role'],'extracted_original')
        images = [dict(i,path=str(root/i['relative_path']),visual_type='dense',basis_refs=['synthetic-fixture']) for i in inventory]
        engine = ImagePolicies(self.registry)
        plan = engine.decide(images,dict(material_type='public_account_or_web',reliable=True,basis_refs=['synthetic-fixture']),
            current={'strategy':2,'confirmation_ref':'synthetic-text-only'},dense_choices={images[0]['asset_id']:'text_only'},
            capabilities={'archive':'configured_pending_window','preview':True,'reading':False})
        policy = engine.execute(plan,images,self.runtime.root/'conversion-work/image-policy-result')
        replacements = {i['original_target']:dict(asset_id=i['asset_id'],local_path=None,representation_asset_id=None) for i in images}
        revised,revised_map = self.fixture.conversion.reexpress(mapping['extraction_id'],replacements)
        self.assertNotEqual(revised_map['output_asset_id'],mapping['output_asset_id'])
        consent = dict(event_id='synthetic-consent-2',asset_id=revised_map['output_asset_id'],sha256=sha(revised),
            extraction_id=revised_map['extraction_id'],ranges=[[0,len(revised.decode())]],model='synthetic-reader',
            expires_at=(datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat())
        record,curated,guide = ReadingGuideAdapter(SyntheticGuide()).generate(empty_record('合成图文','synthetic-title'),revised,revised_map,consent)
        prepared = self.publisher.prepare(entry,record,revised_map,revised,curated,guide,policy,font_path=self.font,images=images)
        self.publisher.confirm(prepared['publication_id'],prepared['preview_sha256'],'synthetic-reviewer','synthetic-image-confirmation')
        published = self.publisher.publish(prepared['publication_id'])
        self.assertFalse(list(published.rglob('*.png')))
        self.assertIn('需手动获取',(published/'提取/source.md').read_text())
        self.assertTrue(Path(images[0]['path']).exists())
