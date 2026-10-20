"""Bounded, offline image inventory for a single self-contained Markdown file.

No filesystem or network resolver is used: a local/remote image reference is
unavailable when only the MD was delivered. The untouched upload remains the
authority; normalized text records every replaced occurrence and byte hash.
"""
import base64
import binascii
import io
import re
import warnings
from xml.etree import ElementTree

from PIL import Image, UnidentifiedImageError

from .contracts import ContractError
from .conversion import sha
from .image_links import image_spans

MAX_INPUT_BYTES = 32 * 1024 * 1024
MAX_IMAGE_BYTES = 10_000_000
MAX_OCCURRENCES = 200
REASONS = {
    'attachment_not_supplied': '图片使用文件路径，但本次只收到了 Markdown 文件',
    'remote_not_embedded': '图片使用网络链接，未包含在文件中',
    'invalid_data_uri': '内嵌图片的数据格式不完整',
    'unreadable_image': '图片数据损坏或格式不受支持',
    'unsafe_svg': 'SVG 含有首版不支持的活动内容或外部依赖',
}


def _image_extension(data, mime):
    if mime in {'image/svg+xml', 'image/svg'}:  # Bear also exports the latter alias.
        # Preserve passive SVG bytes, without rendering or resolving resources.
        if re.search(br'<!\s*(?:DOCTYPE|ENTITY)', data, re.I):
            return None, 'unsafe_svg'
        try:
            root = ElementTree.fromstring(data)
        except ElementTree.ParseError:
            return None, 'unreadable_image'
        tags = {'svg', 'g', 'path', 'rect', 'circle', 'ellipse', 'line', 'polyline',
                'polygon', 'defs', 'linearGradient', 'radialGradient', 'stop',
                'clipPath', 'text', 'tspan', 'title', 'desc'}
        if root.tag not in {'svg', '{http://www.w3.org/2000/svg}svg'}:
            return None, 'unreadable_image'
        for node in root.iter():
            if node.tag not in tags | {'{http://www.w3.org/2000/svg}' + t for t in tags}:
                return None, 'unsafe_svg'
            for key, value in node.attrib.items():
                name = key.rsplit('}', 1)[-1].lower()
                if name.startswith('on') or name in {'href', 'src', 'style', 'base'} or re.search(r'url\s*\(', value, re.I):
                    return None, 'unsafe_svg'
        return '.svg', None
    formats = {'image/png': ('PNG', '.png'), 'image/jpeg': ('JPEG', '.jpg'),
               'image/gif': ('GIF', '.gif'), 'image/webp': ('WEBP', '.webp'),
               'image/tiff': ('TIFF', '.tiff'), 'image/bmp': ('BMP', '.bmp')}
    if mime not in formats:
        return None, 'unreadable_image'
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format != formats[mime][0] or image.width * image.height > 40_000_000:
                    return None, 'unreadable_image'
                image.verify()
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError,
            Image.DecompressionBombWarning, Image.DecompressionBombError):
        return None, 'unreadable_image'
    return formats[mime][1], None


def normalize_embedded_markdown(raw):
    if len(raw) > MAX_INPUT_BYTES:
        raise ContractError('self-contained Markdown exceeds bounded input size')
    try:
        text = raw.decode('utf-8')
    except UnicodeError as error:
        raise ContractError('self-contained Markdown must use valid UTF-8') from error
    spans = image_spans(text, allow_external=True)
    if len(spans) > MAX_OCCURRENCES:
        raise ContractError('Markdown image occurrence count exceeds retention scope')
    assets, occurrences, pieces = {}, [], []
    cursor = length = decoded_total = 0
    for number, span in enumerate(spans, 1):
        target = span['original_path']
        reason, path, data = None, None, None
        if target.lower().startswith('data:'):
            match = re.fullmatch(r'data:(image/[a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/=]+)', target)
            if not match:
                reason = 'invalid_data_uri'
            else:
                if len(match[2]) > (MAX_IMAGE_BYTES + 2) // 3 * 4:
                    raise ContractError('embedded image exceeds retention byte scope')
                try:
                    data = base64.b64decode(match[2], validate=True)
                except (binascii.Error, ValueError):
                    reason = 'invalid_data_uri'
                if reason is None:
                    decoded_total += len(data)
                    if decoded_total > MAX_IMAGE_BYTES:
                        raise ContractError('embedded images exceed total retention byte scope')
                    extension, reason = _image_extension(data, match[1].lower())
                    if reason is None:
                        path = 'embedded-images/' + sha(data) + extension
                        assets[path] = data
        elif re.match(r'(?i)(?:https?:)?//', target):
            reason = 'remote_not_embedded'
        else:
            reason = 'attachment_not_supplied'
        unchanged = text[cursor:span['char_start']]
        pieces.append(unchanged)
        length += len(unchanged)
        caption = span['caption'].replace('\\', '\\\\').replace('[', r'\[').replace(']', r'\]')
        replacement = f'![{caption}]({path})' if path else f'【缺图 {number}：图片未提供或无法读取，原位置保留】'
        pieces.append(replacement)
        occurrences.append(dict(number=number, status='available' if path else 'unavailable',
            reason=reason, caption=span['caption'][:300], original_range=[span['char_start'], span['char_end']],
            normalized_range=[length, length + len(replacement)], target_sha256=sha(target.encode()),
            target_label='内嵌图片' if target.lower().startswith('data:') else target[:300],
            local_path=path, image_sha256=sha(data) if data is not None else None))
        length += len(replacement)
        cursor = span['char_end']
    pieces.append(text[cursor:])
    content = ''.join(pieces).encode('utf-8')
    # HTML image syntax and unresolved embedded definitions are not silently
    # passed to the model as text. This first version handles CommonMark images.
    if re.search(r'<\s*(?:img|svg)\b', content.decode(), re.I) or b'data:image/' in content.lower():
        raise ContractError('unresolved HTML or embedded image syntax requires technical review before guide')
    report = dict(reference='P30-markdown-images-v1', source_sha256=sha(raw),
        normalized_sha256=sha(content), occurrences=occurrences,
        available_count=sum(o['status'] == 'available' for o in occurrences),
        missing_count=sum(o['status'] == 'unavailable' for o in occurrences))
    return content, assets, report
