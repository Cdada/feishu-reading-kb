#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import html
import json
import plistlib
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


IGNORED_HTML_TAGS = {"script", "style", "svg", "noscript", "template"}
HTML_MIME_TYPES = {"text/html", "application/xhtml+xml"}
TEXT_MIME_TYPES = {"text/plain", "text/markdown", "text/x-markdown"}


@dataclass(frozen=True)
class WebArchiveConversionResult:
    status: str
    markdown_path: Path | None
    metadata_path: Path
    metadata: dict[str, Any]
    notes: str


def convert_webarchive_to_markdown(source: Path, output_dir: Path, *, preserve_images: bool = False) -> WebArchiveConversionResult:
    output_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = output_dir / f"{safe_file_stem(source)}.metadata.json"
    raw = source.read_bytes()
    base_metadata: dict[str, Any] = {
        "source_type": "webarchive",
        "source_url": "",
        "mime_type": "",
        "encoding": "",
        "original_file": source.name,
        "original_sha256": hashlib.sha256(raw).hexdigest(),
        "main_resource_bytes": 0,
        "subresource_count": 0,
        "conversion_method": "plistlib + stdlib_html_parser",
        "conversion_limits": "Only WebMainResource is converted; scripts are not executed; external resources are not fetched.",
        "conversion_status": "failed",
        "conversion_warnings": [],
        "text_length": 0,
    }

    try:
        archive = plistlib.loads(raw)
    except Exception as exc:  # noqa: BLE001
        return write_failure(metadata_path, base_metadata, f"plistlib 解析失败: {type(exc).__name__}: {exc}")

    if not isinstance(archive, dict):
        return write_failure(metadata_path, base_metadata, "webarchive 顶层结构不是字典")

    main = archive.get("WebMainResource")
    if not isinstance(main, dict):
        return write_failure(metadata_path, base_metadata, "缺少 WebMainResource")

    data = main.get("WebResourceData")
    if not isinstance(data, bytes):
        return write_failure(metadata_path, base_metadata, "缺少 WebMainResource.WebResourceData")

    subresources = archive.get("WebSubresources")
    subresource_count = len(subresources) if isinstance(subresources, list) else 0
    source_url = string_or_empty(main.get("WebResourceURL"))
    mime_type = normalize_mime(string_or_empty(main.get("WebResourceMIMEType")))
    declared_encoding = string_or_empty(main.get("WebResourceTextEncodingName"))
    decoded, used_encoding, decode_warnings = decode_resource(data, declared_encoding)
    sniffed_html = looks_like_html(decoded)
    if not mime_type and sniffed_html:
        mime_type = "text/html"
    elif not mime_type and decoded.strip():
        mime_type = "text/plain"

    metadata = {
        **base_metadata,
        "source_url": source_url,
        "mime_type": mime_type,
        "encoding": used_encoding,
        "main_resource_bytes": len(data),
        "subresource_count": subresource_count,
        "conversion_warnings": decode_warnings,
    }

    if is_html_resource(mime_type, decoded):
        title = extract_html_title(decoded) or source.stem
        images = None
        body_html = decoded
        if preserve_images:
            from webarchive_images import ArchivedImages, article_fragment
            images = ArchivedImages(archive, output_dir, source_url)
            metadata["conversion_limits"] = "Offline main HTML and saved raster images only; no scripts executed or external fetches."
            if urlsplit(source_url).hostname == "mp.weixin.qq.com":
                try:
                    body_html = article_fragment(decoded, "js_content")
                    metadata["body_selection"] = "unique_complete_js_content"
                except ValueError:
                    metadata["body_selection"] = "whole_html_requires_review"
                    metadata["conversion_warnings"].append("未找到唯一完整的公众号正文容器，暂保留整页文字，需复核")
        body = html_to_markdown(body_html, images.render if images else None).strip()
        if images:
            metadata["image_inventory"] = images.items
            missing = sum(item["status"] == "unavailable" for item in images.items)
            fallbacks = sum(bool(item.get("fallback_reasons")) for item in images.items)
            if missing:
                metadata["conversion_warnings"].append(f"{missing} 个网页图片未取得原始字节；未联网补抓，需复核")
            if fallbacks:
                metadata["conversion_warnings"].append(f"{fallbacks} 个图片使用同一标签的归档备用源，详见 image_inventory")
        conversion_kind = "html"
    elif is_text_resource(mime_type):
        title = source.stem
        body = decoded.strip()
        conversion_kind = "text"
    else:
        metadata["conversion_status"] = "needs_manual_review"
        metadata["conversion_warnings"].append(f"不支持的主资源 MIME: {mime_type or 'unknown'}")
        write_metadata(metadata_path, metadata)
        return WebArchiveConversionResult(
            status="needs_manual_review",
            markdown_path=None,
            metadata_path=metadata_path,
            metadata=metadata,
            notes=f"WEBARCHIVE 主资源不是可直接入库的 HTML/TEXT: {mime_type or 'unknown'}",
        )

    if len(body) < 40:
        metadata["conversion_warnings"].append("正文抽取结果偏短，需要人工复核")

    markdown_path = output_dir / f"{safe_file_stem(source)}.md"
    markdown = build_markdown(title, body, metadata, conversion_kind)
    markdown_path.write_text(markdown, encoding="utf-8")

    metadata["conversion_status"] = "processed"
    metadata["text_length"] = len(body)
    write_metadata(metadata_path, metadata)

    note = "WEBARCHIVE 转 Markdown"
    if metadata["conversion_warnings"]:
        note += "；" + "；".join(str(w) for w in metadata["conversion_warnings"])
    return WebArchiveConversionResult(
        status="processed",
        markdown_path=markdown_path,
        metadata_path=metadata_path,
        metadata=metadata,
        notes=note,
    )


def write_failure(metadata_path: Path, metadata: dict[str, Any], reason: str) -> WebArchiveConversionResult:
    metadata = dict(metadata)
    metadata["conversion_status"] = "failed"
    metadata["conversion_warnings"] = [reason]
    write_metadata(metadata_path, metadata)
    return WebArchiveConversionResult(
        status="failed",
        markdown_path=None,
        metadata_path=metadata_path,
        metadata=metadata,
        notes=reason,
    )


def write_metadata(path: Path, metadata: dict[str, Any]) -> None:
    path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_markdown(title: str, body: str, metadata: dict[str, Any], conversion_kind: str) -> str:
    lines = [
        "---",
        "source_type: webarchive",
        f"source_url: {json.dumps(metadata.get('source_url', ''), ensure_ascii=False)}",
        f"mime_type: {json.dumps(metadata.get('mime_type', ''), ensure_ascii=False)}",
        f"encoding: {json.dumps(metadata.get('encoding', ''), ensure_ascii=False)}",
        f"original_file: {json.dumps(metadata.get('original_file', ''), ensure_ascii=False)}",
        f"sha256: {json.dumps(metadata.get('original_sha256', ''), ensure_ascii=False)}",
        f"conversion_method: {json.dumps(metadata.get('conversion_method', ''), ensure_ascii=False)}",
        f"conversion_kind: {json.dumps(conversion_kind, ensure_ascii=False)}",
        f"conversion_limits: {json.dumps(metadata.get('conversion_limits', ''), ensure_ascii=False)}",
        "---",
        "",
        f"# {title.strip() or '网页归档'}",
        "",
    ]
    source_url = metadata.get("source_url")
    if source_url:
        lines.extend([f"来源 URL：{source_url}", ""])
    warnings = metadata.get("conversion_warnings") or []
    if warnings:
        lines.append("> 转换提醒：" + "；".join(str(w) for w in warnings))
        lines.append("")
    lines.append(body.strip())
    lines.append("")
    return "\n".join(lines)


def decode_resource(data: bytes, declared_encoding: str) -> tuple[str, str, list[str]]:
    warnings: list[str] = []
    candidates = [declared_encoding, "utf-8", "gb18030", "big5"]
    seen: set[str] = set()
    for encoding in candidates:
        encoding = encoding.strip()
        if not encoding or encoding.lower() in seen:
            continue
        seen.add(encoding.lower())
        try:
            decoded = data.decode(encoding)
            return decoded, encoding, warnings
        except LookupError:
            warnings.append(f"未知编码 {encoding}，已尝试备用编码")
        except UnicodeDecodeError:
            continue
    fallback = declared_encoding or "utf-8"
    decoded = data.decode(fallback if declared_encoding else "utf-8", errors="replace")
    warnings.append("使用 errors=replace 解码，可能存在少量乱码")
    return decoded, fallback, warnings


def normalize_mime(value: str) -> str:
    return value.split(";", 1)[0].strip().lower()


def is_html_resource(mime_type: str, text: str) -> bool:
    return mime_type in HTML_MIME_TYPES or (not mime_type and looks_like_html(text)) or (
        mime_type.startswith("text/") and looks_like_html(text)
    )


def is_text_resource(mime_type: str) -> bool:
    return mime_type in TEXT_MIME_TYPES or (mime_type.startswith("text/") and mime_type not in HTML_MIME_TYPES)


def looks_like_html(text: str) -> bool:
    head = text.lstrip()[:500].lower()
    return any(marker in head for marker in ("<!doctype html", "<html", "<head", "<body", "<article"))


def extract_html_title(text: str) -> str:
    match = re.search(r"(?is)<title[^>]*>(.*?)</title>", text)
    if not match:
        return ""
    title = re.sub(r"\s+", " ", strip_tags(match.group(1))).strip()
    return html.unescape(title)


def strip_tags(text: str) -> str:
    return re.sub(r"(?s)<[^>]+>", "", text)


def html_to_markdown(text: str, image_renderer=None) -> str:
    parser = ConservativeMarkdownParser(image_renderer)
    parser.feed(text)
    parser.close()
    return parser.markdown()


class ConservativeMarkdownParser(HTMLParser):
    def __init__(self, image_renderer=None) -> None:
        super().__init__(convert_charrefs=True)
        self.image_renderer = image_renderer
        self.lines: list[str] = []
        self.current: list[str] = []
        self.skip_stack: list[str] = []
        self.heading_stack: list[int] = []
        self.list_stack: list[str] = []
        self.link_stack: list[str] = []
        self.in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self.skip_stack:
            if tag in IGNORED_HTML_TAGS:
                self.skip_stack.append(tag)
            return
        if tag in IGNORED_HTML_TAGS:
            self.flush()
            self.skip_stack.append(tag)
            return
        attrs_dict = {key.lower(): value or "" for key, value in attrs}
        if tag == "title":
            self.in_title = True
            return
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.flush()
            self.heading_stack.append(int(tag[1]))
            return
        if tag in {"p", "div", "article", "section", "main", "blockquote", "tr"}:
            self.flush()
            return
        if tag == "br":
            self.flush()
            return
        if tag in {"ul", "ol"}:
            self.flush()
            self.list_stack.append(tag)
            return
        if tag == "li":
            self.flush()
            prefix = "1. " if self.list_stack and self.list_stack[-1] == "ol" else "- "
            self.current.append(prefix)
            return
        if tag == "a":
            self.link_stack.append(attrs_dict.get("href", "").strip())
            return
        if tag == "img":
            if self.image_renderer is not None:
                self.append_text(self.image_renderer(attrs_dict))
                return
            alt = attrs_dict.get("alt", "").strip()
            src = attrs_dict.get("src", "").strip()
            if alt or src:
                if src.startswith(("http://", "https://")):
                    self.append_text(f"[图片：{alt or src}]({src})")
                elif alt:
                    self.append_text(f"图片：{alt}")
            return
        if tag in {"td", "th"}:
            if self.current:
                self.current.append(" | ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self.skip_stack:
            if tag == self.skip_stack[-1]:
                self.skip_stack.pop()
            return
        if tag == "title":
            self.in_title = False
            self.current = []
            return
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            level = self.heading_stack.pop() if self.heading_stack else int(tag[1])
            self.flush(prefix="#" * level + " ")
            return
        if tag in {"p", "div", "article", "section", "main", "blockquote", "li", "tr"}:
            self.flush()
            return
        if tag in {"ul", "ol"}:
            if self.list_stack:
                self.list_stack.pop()
            self.flush()
            return
        if tag == "a":
            href = self.link_stack.pop() if self.link_stack else ""
            if href and href.startswith(("http://", "https://")):
                self.append_text(f"（{href}）")

    def handle_data(self, data: str) -> None:
        if self.skip_stack or self.in_title:
            return
        self.append_text(data)

    def append_text(self, value: str) -> None:
        normalized = re.sub(r"\s+", " ", html.unescape(value)).strip()
        if not normalized:
            return
        if self.current and not self.current[-1].endswith((" ", "\n", "| ")):
            self.current.append(" ")
        self.current.append(normalized)

    def flush(self, prefix: str = "") -> None:
        text = "".join(self.current).strip()
        self.current = []
        if not text:
            return
        line = prefix + text
        if line and (not self.lines or self.lines[-1] != line):
            self.lines.append(line)

    def markdown(self) -> str:
        self.flush()
        cleaned: list[str] = []
        for line in self.lines:
            line = re.sub(r"\s+", " ", line).strip()
            if not line:
                continue
            cleaned.append(line)
            if line.startswith("#") or line.startswith(("- ", "1. ")):
                continue
            cleaned.append("")
        return "\n".join(cleaned).strip()


def safe_file_stem(path: Path) -> str:
    stem = path.name[: -len(path.suffix)] if path.suffix else path.name
    stem = re.sub(r"[\\/:*?\"<>|]+", "-", stem)
    stem = re.sub(r"\s+", "_", stem).strip("._-")
    return stem or "webarchive"


def string_or_empty(value: Any) -> str:
    return value if isinstance(value, str) else ""
