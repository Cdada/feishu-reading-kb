import unittest
from kb_custody_v21.contracts import ContractError
from kb_custody_v21.conversion import sha
from kb_custody_v21.image_links import image_spans, rewrite_images


class ImageLinkTests(unittest.TestCase):
    def test_parser_ignores_code_and_handles_reference_nested_parentheses(self):
        text = '`![fake](fake.png)`\r\n\r\n正文😀 ![图][ref]\r\n\r\n[ref]: <a(b).png>\r\n'
        spans = image_spans(text)
        self.assertEqual(len(spans), 1)
        self.assertEqual(text[spans[0]['char_start']:spans[0]['char_end']], '![图][ref]')
        self.assertEqual(spans[0]['original_path'], 'a(b).png')

    def test_rewrite_creates_new_expression_and_preserves_unrelated_text(self):
        body = '# 标题\r\n\r\n正文😀 ![图](picture.png) 结束。\r\n'.encode()
        old = dict(extraction_id='old', output_asset_id='body-old', transformations=[], output_sha256=sha(body))
        out, mapping = rewrite_images(body, old, {'picture.png':dict(asset_id='image-1',local_path=None,representation_asset_id=None)})
        self.assertTrue(out.startswith('# 标题\r\n\r\n正文😀 '.encode()))
        self.assertTrue(out.endswith(' 结束。\r\n'.encode()))
        self.assertNotEqual(mapping['extraction_id'], 'old')
        self.assertEqual(old['output_sha256'], sha(body))
        self.assertEqual(mapping['output_sha256'], sha(out))
        self.assertIsNone(mapping['image_mappings'][0]['local_representation_asset_id'])

    def test_external_and_missing_image_binding_rejected(self):
        for text in ('![x](https://example.test/x.png)', '![x](../x.png)', '![x](x.png?token=sample)'):
            with self.assertRaises(ContractError): image_spans(text)
        with self.assertRaises(ContractError): rewrite_images(b'![x](x.png)', {}, {})

    def test_unicode_target_normalizes_parser_url_encoding_for_local_inventory(self):
        self.assertEqual(image_spans('![图](提取图片/示意图.png)')[0]['original_path'],'提取图片/示意图.png')
