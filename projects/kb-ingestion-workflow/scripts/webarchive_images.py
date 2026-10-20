"""Opt-in offline raster extraction for V2.1; never fetch or execute a URL."""
import base64
import binascii
import hashlib
import io
from html.parser import HTMLParser
from urllib.parse import urljoin, unquote_to_bytes

from PIL import Image, UnidentifiedImageError


def article_fragment(text, root_id):
    """Keep original bytes of one complete, explicitly marked HTML subtree."""
    class Locate(HTMLParser):
        VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
                "link", "meta", "param", "source", "track", "wbr"}

        def __init__(self):
            super().__init__(convert_charrefs=False)
            self.count, self.start, self.end = 0, None, None
            self.stack = []
            self.offsets = [0]
            for line in text.splitlines(keepends=True):
                self.offsets.append(self.offsets[-1] + len(line))

        def character_offset(self):
            line, column = self.getpos()
            return self.offsets[line - 1] + column

        def handle_starttag(self, tag, attrs):
            if dict(attrs).get("id") == root_id:
                self.count += 1
                if self.start is None:
                    self.start = self.character_offset()
            if self.start is not None and self.end is None and tag not in self.VOID:
                self.stack.append(tag)

        def handle_endtag(self, tag):
            if tag in self.stack:
                index = len(self.stack) - 1 - self.stack[::-1].index(tag)
                self.stack = self.stack[:index]
                if not self.stack:
                    self.end = text.find(">", self.character_offset()) + 1

    parser = Locate()
    parser.feed(text)
    parser.close()
    if parser.count != 1 or parser.start is None or parser.end is None:
        raise ValueError("one complete article container required")
    return text[parser.start:parser.end]


class ArchivedImages:
    MAX_IMAGE_BYTES = 16 * 1024 * 1024
    MAX_TOTAL_BYTES = 64 * 1024 * 1024
    EXTENSIONS = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp",
                  "GIF": ".gif", "TIFF": ".tiff", "BMP": ".bmp"}

    def __init__(self, archive, output, base_url):
        self.output, self.base_url = output, base_url
        self.resources = {}
        for item in archive.get("WebSubresources", []):
            if isinstance(item, dict) and isinstance(item.get("WebResourceData"), bytes):
                url = item.get("WebResourceURL")
                if isinstance(url, str):
                    self.resources.setdefault(url, []).append(item["WebResourceData"])
        self.items, self.saved = [], {}
        self.total = 0

    def _bytes(self, source):
        if source.startswith("data:"):
            header, comma, body = source.partition(",")
            if not comma or not header.lower().startswith("data:image/"):
                raise ValueError("invalid_image_data_uri")
            if len(body) > self.MAX_IMAGE_BYTES * 2:
                raise ValueError("image_too_large")
            return base64.b64decode(body, validate=True) if header.lower().endswith(";base64") else unquote_to_bytes(body)
        options = self.resources.get(urljoin(self.base_url, source), [])
        if not options:
            raise ValueError("image_not_saved_in_archive")
        if len({hashlib.sha256(x).digest() for x in options}) != 1:
            raise ValueError("ambiguous_archived_image_url")
        return options[0]

    def render(self, attrs):
        index = len(self.items) + 1
        # Lazy-load URLs are alternatives only within this same archived img.
        sources = list(dict.fromkeys(attrs.get(k, "").strip() for k in ("src", "data-src", "data-original")))
        reasons = []
        for source in filter(None, sources):
            try:
                data = self._bytes(source)
                if not data or len(data) > self.MAX_IMAGE_BYTES:
                    raise ValueError("image_too_large_or_empty")
                with Image.open(io.BytesIO(data)) as picture:
                    extension = self.EXTENSIONS.get(picture.format)
                    if not extension:
                        raise ValueError("unsupported_raster_format")
                    if picture.width * picture.height > 40_000_000:
                        raise ValueError("image_pixel_budget_exceeded")
                    picture.verify()
            except (ValueError, binascii.Error, UnidentifiedImageError, Image.DecompressionBombError, OSError) as error:
                # Do not disclose embedded URLs/data. Each fallback and omission
                # remains observable in metadata, with publication held for review.
                reasons.append(type(error).__name__ + ":" + (
                    str(error) if isinstance(error, ValueError) else "invalid_or_unwritable_image"))
                continue
            digest = hashlib.sha256(data).hexdigest()
            relative = "images/" + digest + extension
            if digest not in self.saved:
                if self.total + len(data) > self.MAX_TOTAL_BYTES:
                    reasons.append("archive_image_budget_exceeded")
                    continue
                destination = self.output / relative
                destination.parent.mkdir(exist_ok=True)
                with destination.open("xb") as stream:
                    stream.write(data)
                self.saved[digest] = relative
                self.total += len(data)
            else:
                relative = self.saved[digest]
            self.items.append({"index": index, "status": "preserved", "sha256": digest,
                               "relative_path": relative, "bytes": len(data), "fallback_reasons": reasons})
            alt = attrs.get("alt", "").strip() or f"网页图片 {index}"
            for char in "\\[]()":
                alt = alt.replace(char, "\\" + char)
            alt = alt.replace("<", "&lt;").replace(">", "&gt;")
            return f"![{alt}]({relative})"
        self.items.append({"index": index, "status": "unavailable", "reasons": reasons or ["missing_image_source"]})
        return f"【网页图片 {index} 未取得原始字节，需复核】"
