import base64
import json
from pathlib import Path
import unittest

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.embedded_markdown import normalize_embedded_markdown
from kb_custody_v21.image_links import image_spans
from test_markdown_bundle import png
import test_pilot_three_formats as fixtures

SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22"><path d="M0 0 L10 10"/></svg>'


def embedded(data, mime='image/png', label='figure'):
    return f'![{label}](data:{mime};base64,{base64.b64encode(data).decode()})'


class EmbeddedMarkdownTests(unittest.TestCase):
    def test_png_and_passive_svg_preserve_exact_bytes_and_order(self):
        raw = ('# Bear style\n\nBefore\n\n' + embedded(png()) + '\n\nBetween\n\n' +
               embedded(SVG, 'image/svg+xml') + '\n\nAfter\n').encode()
        content, assets, report = normalize_embedded_markdown(raw)
        self.assertEqual(report['available_count'], 2)
        self.assertEqual(report['missing_count'], 0)
        self.assertEqual(set(assets.values()), {png(), SVG})
        restored = content.decode()
        for item in reversed(report['occurrences']):
            a, b = item['normalized_range']; c, d = item['original_range']
            restored = restored[:a] + raw.decode()[c:d] + restored[b:]
        self.assertEqual(restored.encode(), raw)
        self.assertEqual(len(image_spans(content.decode())), 2)
        self.assertNotIn(b'base64', content)

    def test_relative_remote_and_corrupt_images_become_explicit_missing_positions(self):
        raw = b'# Text\n\n![a](assets/a.png)\n\n![b](https://example.invalid/b.png)\n\n![c](data:image/png;base64,!!!!)\n'
        content, assets, report = normalize_embedded_markdown(raw)
        self.assertEqual(report['missing_count'], 3)
        self.assertFalse(assets)
        self.assertEqual([i['reason'] for i in report['occurrences']],
                         ['attachment_not_supplied', 'remote_not_embedded', 'invalid_data_uri'])
        self.assertEqual(content.decode().count('【缺图'), 3)
        self.assertFalse(image_spans(content.decode()))

    def test_unsafe_svg_is_not_emitted_as_an_active_asset(self):
        for raw_svg in [b'<svg><script>alert(1)</script></svg>',
                        b'<svg onload="x()"/>', b'<svg><image href="https://example.invalid/x"/></svg>',
                        b'<!DOCTYPE svg [<!ENTITY x "hello">]><svg>&x;</svg>']:
            with self.subTest(svg=raw_svg):
                _, assets, report = normalize_embedded_markdown(embedded(raw_svg, 'image/svg+xml').encode())
                self.assertFalse(assets)
                self.assertEqual(report['occurrences'][0]['reason'], 'unsafe_svg')

    def test_bear_svg_mime_alias_is_preserved(self):
        _, assets, report = normalize_embedded_markdown(embedded(SVG, 'image/svg').encode())
        self.assertEqual(set(assets.values()), {SVG})
        self.assertEqual(report['missing_count'], 0)

    def test_wrong_image_mime_or_bytes_are_unreadable(self):
        for data, mime in [(b'broken', 'image/png'), (png(), 'image/jpeg')]:
            with self.subTest(mime=mime):
                _, assets, report = normalize_embedded_markdown(embedded(data, mime).encode())
                self.assertFalse(assets)
                self.assertEqual(report['occurrences'][0]['reason'], 'unreadable_image')

    def test_code_examples_do_not_become_image_occurrences(self):
        content, assets, report = normalize_embedded_markdown(b'`![x](missing.png)`\n\n```md\n![y](other.png)\n```\n')
        self.assertFalse(report['occurrences']); self.assertFalse(assets)
        self.assertIn(b'missing.png', content)

    def test_reference_image_missing_attachment_is_detected(self):
        _, _, report = normalize_embedded_markdown(b'![x][figure]\n\n[figure]: absent.png\n')
        self.assertEqual(report['missing_count'], 1)

    def test_duplicate_images_retain_all_occurrences_with_one_exact_asset(self):
        _, assets, report = normalize_embedded_markdown((embedded(png()) + '\n\n' + embedded(png())).encode())
        self.assertEqual(len(assets), 1)
        self.assertEqual(report['available_count'], 2)

    def test_input_limits_and_strict_utf8_are_not_waived(self):
        for raw in [b'\xff', ('\n\n'.join('![x](x.png)' for _ in range(201))).encode()]:
            with self.subTest(size=len(raw)), self.assertRaises(ContractError):
                normalize_embedded_markdown(raw)


class EmbeddedGuideTests(unittest.TestCase):
    def fixture(self):
        f = fixtures.ThreeFormatGuideTests(); f.setUp(); self.addCleanup(f.doCleanups)
        return f

    def test_embedded_md_reaches_preview_with_png_and_svg_preserved(self):
        f = self.fixture()
        raw = ('# Article\n\nSynthetic body.\n\n' + embedded(png()) + '\n\n' + embedded(SVG, 'image/svg+xml')).encode()
        entry = f.accept('article.md', raw)
        result = f.g.guide(f.g.pipeline, entry)
        tree = Path(result['preview_path']).parents[1]
        images = list((tree/'提取/images').iterdir())
        self.assertEqual({p.read_bytes() for p in images}, {png(), SVG})
        self.assertEqual(f.g.wire.calls, 1)
        self.assertFalse(f.g.registry.rows('SELECT * FROM publication_confirmations'))
        self.assertNotIn(b'base64', (tree/'提取/source.md').read_bytes())

    def test_missing_image_blocks_before_any_model_reservation(self):
        f = self.fixture()
        entry = f.accept('missing.md', b'# Article\n\nSynthetic body.\n\n![x](missing.png)\n')
        with self.assertRaisesRegex(ContractError, 'owner choice'):
            f.g.guide(f.g.pipeline, entry)
        self.assertEqual(f.g.wire.calls, 0)
        self.assertFalse(f.g.registry.rows("SELECT * FROM custody_events WHERE event_type='pilot_inbox_guide_reserved'"))
        self.assertEqual(f.g.intake.status(entry)['original_status'], 'accepted')


if __name__ == '__main__':
    unittest.main()
