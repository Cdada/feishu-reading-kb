"""Bounded, offline import of one Markdown document with packaged assets.

PDF/JSON sidecars are retained, never parsed as new model instructions or
trusted page mappings. No MinerU invocation is needed for already parsed text.
"""
from html.parser import HTMLParser
from io import BytesIO
from pathlib import PurePosixPath
import stat
import unicodedata
from urllib.parse import unquote, urlsplit
import warnings
import zipfile

from markdown_it import MarkdownIt
from PIL import Image

IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.tiff', '.bmp'}
MAX_MEMBERS = 512
MAX_BODY_BYTES = 1024 * 1024
MAX_MEMBER_BYTES = 32 * 1024**2
MAX_TOTAL_BYTES = 64 * 1024**2


class MarkdownBundleError(ValueError):
    pass


def _members(archive):
    infos = archive.infolist()
    if len(infos) > MAX_MEMBERS:
        raise MarkdownBundleError('压缩包条目超过 512 个，请缩小后重试')
    members, seen, total = {}, set(), 0
    for info in infos:
        name = info.orig_filename
        parts = name.rstrip('/').split('/')
        mode = stat.S_IFMT(info.external_attr >> 16)
        if (not name or name.startswith('/') or any(c in name for c in '\\:\x00\r\n')
                or any(p in {'', '.', '..'} for p in parts)
                or mode not in {0, stat.S_IFREG, stat.S_IFDIR} or info.flag_bits & 1):
            raise MarkdownBundleError('压缩包含不安全路径、链接或加密文件，未解压')
        key = unicodedata.normalize('NFC', name.rstrip('/')).casefold()
        if key in seen:
            raise MarkdownBundleError('压缩包含重名文件，无法可靠对应正文与附件')
        seen.add(key)
        if info.is_dir() or '__MACOSX' in parts or parts[-1] == '.DS_Store' or parts[-1].startswith('._'):
            continue
        path = PurePosixPath(name)
        if path.suffix.lower() not in IMAGE_EXTENSIONS | {'.md', '.pdf', '.json'}:
            raise MarkdownBundleError('资料包仅支持一个 MD 正文、图片以及 PDF/JSON 辅助文件')
        total += info.file_size
        if (info.file_size > MAX_MEMBER_BYTES or total > MAX_TOTAL_BYTES
                or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                or info.file_size > max(1, info.compress_size) * 1000):
            raise MarkdownBundleError('压缩包大小或压缩比例超出范围，请缩小后重试')
        members[path] = info
    mains = [p for p in members if p.suffix.lower() == '.md']
    if len(mains) != 1:
        raise MarkdownBundleError('每个资料包必须有且只有一个 MD 正文；请按文章分别打包')
    main = mains[0]
    if members[main].file_size > MAX_BODY_BYTES:
        raise MarkdownBundleError('完整 Markdown 正文超过 1 MiB，请分篇处理')
    # One optional wrapping folder is harmless. Everything else must be below
    # the Markdown parent so downstream readers cannot lose sibling assets.
    if any(not p.is_relative_to(main.parent) for p in members):
        raise MarkdownBundleError('请把附件目录与正文放在同一个资料文件夹内')
    files = set(members)
    if any(parent in files for p in files for parent in p.parents):
        raise MarkdownBundleError('压缩包的文件与目录路径发生冲突')
    return main, members


def _read(archive, info, maximum):
    with archive.open(info) as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum or len(raw) != info.file_size:
        raise MarkdownBundleError('解压内容与清单不一致，未交付不完整资料')
    return raw


def _links(text):
    """Use CommonMark, including reference links; do not parse code examples."""
    targets = []
    class HTMLImages(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag in {'img', 'picture', 'source', 'svg', 'video', 'audio', 'iframe', 'object'}:
                raise MarkdownBundleError('正文含未支持的 HTML 媒体，请先转为 Markdown 图片链接')
    parser = MarkdownIt('commonmark')
    # Parse disallowed URL schemes as links so our validator rejects them
    # explicitly instead of silently treating the syntax as ordinary text.
    parser.validateLink = lambda value: True
    for token in parser.parse(text):
        for child in [token, *(token.children or [])]:
            if child.type == 'image':
                targets.append((child.attrGet('src'), True))
            elif child.type == 'link_open':
                targets.append((child.attrGet('href'), False))
            elif child.type in {'html_inline', 'html_block'}:
                HTMLImages().feed(child.content)
    return targets


def _validate_links(text, main, members):
    for target, image in _links(text):
        url = urlsplit(target)
        if not image and (url.scheme in {'https', 'http', 'mailto'} or target.startswith('#')):
            continue  # Keep ordinary citations as text, never fetch them.
        name = unquote(url.path)
        relative = PurePosixPath(name)
        if (url.scheme or url.netloc or url.query or image and url.fragment
                or not name or relative.is_absolute() or '..' in relative.parts
                or any(c in name for c in '\\:\x00\r\n')):
            raise MarkdownBundleError('正文存在外部图片或不安全的附件链接，请将图片完整打包')
        path = main.parent / relative
        if path not in members:
            raise MarkdownBundleError(f'正文引用的附件未包含在资料包中：{relative}')
        if image and path.suffix.lower() not in IMAGE_EXTENSIONS:
            raise MarkdownBundleError('正文图片链接没有指向受支持的图片文件')


def convert_markdown_bundle(source, output):
    """Validate every member before creating a fresh candidate directory."""
    if output.exists():
        raise MarkdownBundleError('已有同名转换目录，未覆盖')
    try:
        with zipfile.ZipFile(source) as archive:
            main, members = _members(archive)
            body = _read(archive, members[main], MAX_BODY_BYTES)
            text = body.decode('utf-8-sig')
            if not text.strip():
                raise MarkdownBundleError('Markdown 正文为空')
            _validate_links(text, main, members)
            # Total retained bytes are bounded above before allocating buffers.
            content, pixels = {}, 0
            for path, info in members.items():
                raw = body if path == main else _read(archive, info, MAX_MEMBER_BYTES)
                if path.suffix.lower() in IMAGE_EXTENSIONS:
                    with warnings.catch_warnings():
                        warnings.simplefilter('error', Image.DecompressionBombWarning)
                        with Image.open(BytesIO(raw)) as picture:
                            pixels += picture.width * picture.height
                            if (getattr(picture, 'n_frames', 1) != 1
                                    or picture.width * picture.height > 40_000_000 or pixels > 200_000_000):
                                raise MarkdownBundleError('图片过大或包含多帧，请缩小或拆分后处理')
                            picture.verify()
                content[path.relative_to(main.parent)] = raw
    except MarkdownBundleError:
        raise
    except (zipfile.BadZipFile, UnicodeError, OSError, ValueError,
            Image.DecompressionBombWarning, Image.DecompressionBombError) as error:
        raise MarkdownBundleError('资料包或图片无法完整读取，请检查文件后重试') from error
    output.mkdir(parents=True, exist_ok=False)
    for relative, raw in content.items():
        target = output.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('xb') as stream:
            stream.write(raw)
    return output / main.name
