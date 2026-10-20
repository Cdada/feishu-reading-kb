import base64
import io
import json
from pathlib import Path
import plistlib
import sys
import tempfile
import unittest

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from webarchive_converter import convert_webarchive_to_markdown
from webarchive_images import article_fragment
import test_conversion_v21 as conversion_tests
from kb_custody_v21.contracts import ContractError


def fixture(missing=False):
    stream = io.BytesIO()
    Image.new("RGB", (8, 8), "blue").save(stream, format="PNG")
    png = stream.getvalue()
    html = '<p>合成文章正文足够长度，用于测试网页保存图片与转换记录。' * 3 + '</p>'
    html += '<img src="data:image/png;base64,' + base64.b64encode(png).decode() + '" alt="蓝色方块">'
    html += '<img data-src="/picture.png" alt="同一方块">'
    if missing:
        html += '<img src="https://example.invalid/missing.png">'
    html += '<script>fetch("https://example.invalid/forbidden")</script>'
    return plistlib.dumps({"WebMainResource": {"WebResourceData": html.encode(),
        "WebResourceMIMEType": "text/html", "WebResourceURL": "https://example.invalid/article"},
        "WebSubresources": [{"WebResourceURL": "https://example.invalid/picture.png",
                            "WebResourceData": png, "WebResourceMIMEType": "image/png"}]}), png


class ArchivedImageTests(unittest.TestCase):
    def test_article_scope_excludes_platform_controls_and_scripts(self):
        html = '<div>Platform controls<img src="/ui.png"></div><div id="js_content"><p>Actual article<br>text</p><script>ignore</script></div><div>Comments</div>'
        fragment = article_fragment(html, "js_content")
        self.assertIn("Actual article", fragment)
        self.assertNotIn("Platform controls", fragment)
        self.assertNotIn("Comments", fragment)
        self.assertNotIn("/ui.png", fragment)

    def test_incomplete_or_ambiguous_article_container_is_not_silently_chosen(self):
        for html in ('<div id="js_content">unfinished', '<div id="js_content">a</div><div id="js_content">b</div>'):
            with self.subTest(html=html), self.assertRaises(ValueError):
                article_fragment(html, "js_content")

    def test_preserves_exact_bytes_deduplicates_and_reports_missing(self):
        data, png = fixture(missing=True)
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "synthetic.webarchive"
            source.write_bytes(data)
            result = convert_webarchive_to_markdown(source, Path(tmp) / "out", preserve_images=True)
            inventory = result.metadata["image_inventory"]
            self.assertEqual([x["status"] for x in inventory], ["preserved", "preserved", "unavailable"])
            self.assertEqual(inventory[0]["relative_path"], inventory[1]["relative_path"])
            self.assertEqual((result.markdown_path.parent / inventory[0]["relative_path"]).read_bytes(), png)
            self.assertTrue(result.metadata["conversion_warnings"])
            text = result.markdown_path.read_text()
            self.assertIn("未取得原始字节", text)
            self.assertNotIn("fetch(", text)
            self.assertNotIn("missing.png", text)
            self.assertEqual(source.read_bytes(), data)

    def test_legacy_default_does_not_enable_new_image_route(self):
        data, _ = fixture()
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "synthetic.webarchive"
            source.write_bytes(data)
            result = convert_webarchive_to_markdown(source, Path(tmp) / "out")
            self.assertNotIn("image_inventory", result.metadata)
            self.assertFalse((result.markdown_path.parent / "images").exists())

    def test_conflicting_resource_url_never_selects_arbitrary_bytes(self):
        data, _ = fixture()
        archive = plistlib.loads(data)
        archive["WebSubresources"].append({**archive["WebSubresources"][0], "WebResourceData": b"different"})
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "synthetic.webarchive"
            source.write_bytes(plistlib.dumps(archive))
            result = convert_webarchive_to_markdown(source, Path(tmp) / "out", preserve_images=True)
            self.assertEqual(result.metadata["image_inventory"][1]["status"], "unavailable")
            self.assertIn("ambiguous", str(result.metadata["image_inventory"][1]))


class WebArchiveWorkerTests(unittest.TestCase):
    setUp = conversion_tests.ConversionTests.setUp
    receive = conversion_tests.ConversionTests.receive
    convert = conversion_tests.ConversionTests.convert
    def test_real_worker_preserves_webarchive_images_in_asset_inventory(self):
        data, png = fixture()
        entry = self.receive(data, received_filename="synthetic.webarchive")
        _, mapping = self.convert(entry)
        row, actual, root = self.conversion.extraction(mapping["extraction_id"])
        images = json.loads(row["inventory_json"])
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["role"], "extracted_original")
        self.assertEqual((root / images[0]["relative_path"]).read_bytes(), png)
        self.assertEqual(len(actual["image_mappings"]), 2)

    def test_missing_archived_image_stays_review_and_cannot_publish(self):
        data, _ = fixture(missing=True)
        entry = self.receive(data, received_filename="synthetic-missing.webarchive")
        inspected = self.conversion.inspect(entry)
        task = self.conversion.submit(inspected["inspection_id"], "missing-image")
        self.conversion.worker.run_once()
        row = self.conversion.service.ledger.get_task(task["task_id"])
        self.assertEqual(row["status"], "needs_review")
        extraction = json.loads(row["quality_json"])["extraction"]
        with self.assertRaisesRegex(ContractError, "quality review"):
            self.conversion.publication_extraction(extraction["extraction_id"])
