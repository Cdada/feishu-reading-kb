"""Exercise P32 with the production P27 image-preserving guide profile."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from kb_custody_v21.daily_store import Deferred
from kb_custody_v21.registry import encoded
from test_embedded_markdown import embedded
from test_markdown_bundle import png
from test_pilot_download import download_response
from test_pilot_three_formats import profile
from test_webarchive_assets import fixture as webpage
import test_daily_backend as backend_fixture
import test_pilot_inbox_guide as guide_fixture


class DailyImageTests(unittest.TestCase):
    def case(self,name,raw):
        original_accept=guide_fixture.InboxGuideTests.accept
        def accept(g,*,convert=True):
            profile(g)
            g.f.approval['inbox']['formats']=['.md','.webarchive']
            g.f.message['body']['content']=encoded(dict(file_key='file_synthetic',file_name=name))
            respond=g.f.runner.side_effect
            g.f.runner.side_effect=lambda control:download_response(control,raw) if control['kind']=='download' else respond(control)
            g.save()
            return original_accept(g,convert=convert)
        case=backend_fixture.DailyBackendTests()
        with patch.object(guide_fixture.InboxGuideTests,'accept',accept):case.setUp()
        self.addCleanup(case.doCleanups)
        return case

    def test_embedded_md_and_webarchive_keep_images_without_vision_calls(self):
        samples=[('bear.md',('# Synthetic\n\nComplete illustrated source.\n\n'+embedded(png())).encode()),
            ('article.webarchive',webpage()[0])]
        for name,raw in samples:
            with self.subTest(name=name):
                case=self.case(name,raw)
                with patch('kb_custody_v21.pilot_vision.OfficialVisionWire.exchange',side_effect=AssertionError('no image model')):
                    document=case.preview()
                self.assertEqual(document['phase'],'awaiting_confirmation')
                result=document['data']['guide_result']
                tree=Path(result['preview_path']).parents[1]
                images=list((tree/'提取/images').iterdir())
                self.assertTrue(images)
                self.assertEqual(case.g.wire.calls,1)
                meta=json.loads((tree/'metadata.json').read_text())
                self.assertEqual(meta['source_record']['source_reading']['figures']['status'],'not_read')

    def test_missing_image_waits_then_exact_choice_continues_same_material(self):
        raw=('# Synthetic\n\nIncomplete illustrated source.\n'+embedded(png())+'\n![missing](absent.png)').encode()
        case=self.case('partial.md',raw)
        case.advance();case.advance()
        document=case.store.document(case.mid)
        self.assertEqual(document['phase'],'awaiting_images')
        self.assertEqual(case.g.wire.calls,0)
        before=len(case.transport.calls)
        case.owner_reply(dict(preview_message_id=document['data']['image_notice']['message_id']),text='@按缺图版本继续')
        case.backend.advance_light(document)
        case.backend.advance_light(document)
        self.assertEqual(case.store.document(case.mid)['phase'],'awaiting_images')
        self.assertEqual(len(case.transport.calls),before+1)
        self.assertEqual(case.g.wire.calls,0)
        case.owner_reply(dict(preview_message_id=document['data']['image_notice']['message_id']),text='按缺图版本继续')
        case.backend.advance_light(document)
        document=case.preview()
        self.assertEqual(document['phase'],'awaiting_confirmation')
        self.assertEqual(case.g.wire.calls,1)
        tree=Path(document['data']['guide_result']['preview_path']).parents[1]
        self.assertEqual(json.loads((tree/'发布记录/缺图处理决定.json').read_text())['choice'],'continue_without_missing')

    def test_one_regeneration_does_not_duplicate_retained_images(self):
        case=self.case('bear.md',('# Synthetic\n\nSource for recovery.\n'+embedded(png())).encode())
        case.advance()
        case.g.wire.mutate=lambda result:dict(status='unknown')
        with self.assertRaises(Deferred):case.advance()
        case.advance()
        case.g.wire.mutate=lambda result:result
        case.advance()
        self.assertEqual(case.store.document(case.mid)['phase'],'guide_ready')
        self.assertEqual(case.g.wire.calls,2)
        self.assertEqual(len(case.registry.rows('SELECT * FROM publications_v21')),1)


if __name__=='__main__':unittest.main()
