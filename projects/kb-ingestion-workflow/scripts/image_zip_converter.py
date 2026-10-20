"""One image ZIP -> one ordered article, with local OCR and no partial delivery.

Never extract member paths. Validate the entire directory first, then stream
bounded regular image files to fresh ordinal names in a private work directory.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import unicodedata
import warnings
import zipfile

IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tif', '.tiff', '.heic', '.heif'}
MAX_IMAGES = 200
MAX_MEMBER_BYTES = 40 * 1024**2
MAX_TOTAL_BYTES = 200 * 1024**2
MAX_PIXELS = 40_000_000
MAX_TOTAL_PIXELS = 200_000_000


class ImageZipError(ValueError):
    pass


def ordered_members(archive):
    infos = archive.infolist()
    if len(infos) > 1000:
        raise ImageZipError('压缩包条目过多，请分包（每包最多 200 张图片）')
    images, seen, total = [], set(), 0
    for info in infos:
        name = info.orig_filename
        parts = name.rstrip('/').split('/')
        mode = info.external_attr >> 16
        if (not name or '\\' in name or ':' in name or '\x00' in name or name.startswith('/')
                or any(p in {'', '.', '..'} for p in parts)
                or stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}
                or info.flag_bits & 1):
            raise ImageZipError('压缩包含不安全路径、链接或加密文件，未解压')
        key = unicodedata.normalize('NFC', name).casefold()
        if key in seen:
            raise ImageZipError('压缩包有重名文件，无法可靠判断图片顺序')
        seen.add(key)
        if info.is_dir():
            continue
        # macOS archive packaging metadata, not article content.
        if '__MACOSX' in parts or PurePosixPath(name).name == '.DS_Store' or PurePosixPath(name).name.startswith('._'):
            continue
        if PurePosixPath(name).suffix.lower() not in IMAGE_SUFFIXES:
            raise ImageZipError('目前 ZIP 只支持同一篇文章的图片；请移出文档、程序、其他压缩包等非图片文件')
        total += info.file_size
        if (info.file_size <= 0 or info.file_size > MAX_MEMBER_BYTES or total > MAX_TOTAL_BYTES
                or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                or info.file_size > max(1, info.compress_size) * 1000):
            raise ImageZipError('压缩包大小或压缩比例超出安全范围，请缩小后重试')
        images.append(info)
    if not 1 <= len(images) <= MAX_IMAGES:
        raise ImageZipError('ZIP 需包含 1—200 张图片')
    if len(images) == 1:
        return images
    records = []
    for info in images:
        stem = unicodedata.normalize('NFC', PurePosixPath(info.filename).stem).casefold()
        tokens = re.split(r'([0-9]+)', stem)
        numbers = tokens[1::2]
        if not numbers or any(len(n) > 20 for n in numbers):
            raise ImageZipError('图片顺序不明确，请按同一规则编号，例如 001.jpg、002.jpg、003.jpg')
        records.append((tuple(tokens[::2]), tuple(int(n) for n in numbers), info))
    if len({x[0] for x in records}) != 1 or len({x[1] for x in records}) != len(records):
        raise ImageZipError('图片编号有冲突或混用了多套命名规则，请统一为 001、002、003 等顺序后再打包')
    return [x[2] for x in sorted(records, key=lambda x: x[1])]


def readable_text(markdown):
    text = re.sub(r'!\[[^\]]*\]\([^\n]*?\)', '', markdown)
    text = re.sub(r'<[^>]*>', '', text)
    return ''.join(c for c in text if c.isalnum())


def convert_image_zip(source, *, work_dir, output_dir, run_command, tools_dir, language='ch', timeout=None):
    """Output is published only after every ordered image yielded readable text.

No cloud API; existing pdf2md.sh invokes the locally installed MinerU pipeline.
Original images stay in the ZIP and are additionally linked beside merged text.
"""
    from PIL import Image, ImageOps
    work_dir.mkdir(parents=True, exist_ok=False)
    raw_dir = work_dir / 'originals'; raw_dir.mkdir()
    normalized_dir = work_dir / 'ocr-inputs'; normalized_dir.mkdir()
    stage = work_dir / 'candidate'; stage.mkdir()
    (stage / 'images').mkdir()
    records, sections, total_pixels = [], [], 0
    with zipfile.ZipFile(source) as archive:
        members = ordered_members(archive)
        for index, info in enumerate(members, 1):
            ordinal = f'{index:04d}'
            suffix = PurePosixPath(info.filename).suffix.lower()
            original = raw_dir / (ordinal + suffix)
            digest, copied = hashlib.sha256(), 0
            with archive.open(info) as stream, original.open('xb') as target:
                while chunk := stream.read(1024 * 1024):
                    copied += len(chunk)
                    if copied > min(info.file_size, MAX_MEMBER_BYTES):
                        raise ImageZipError('解压大小与清单不一致，已停止')
                    target.write(chunk); digest.update(chunk)
            if copied != info.file_size:
                raise ImageZipError('图片未完整解压，已停止')
            image_input = original
            if suffix in {'.heic', '.heif'}:
                image_input = work_dir / (ordinal + '-heic.png')
                ok, _ = run_command(['sips', '-s', 'format', 'png', str(original), '--out', str(image_input)], timeout=timeout)
                if not ok:
                    raise ImageZipError(f'第 {index} 张图片格式转换失败，未交付不完整文章')
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(image_input) as image:
                    total_pixels += image.width * image.height
                    if getattr(image, 'n_frames', 1) != 1 or image.width * image.height > MAX_PIXELS:
                        raise ImageZipError(f'第 {index} 张图片过大或含多帧，请先拆成单张图片')
                    if total_pixels > MAX_TOTAL_PIXELS:
                        raise ImageZipError('图片总像素量过大，请分包后处理')
                    image.load()
                    oriented = ImageOps.exif_transpose(image)
                    gray = oriented.convert('L')
                    edge_review = any(sum(v < 160 for v in gray.crop((0,y,gray.width,y+1)).tobytes()) > max(5,gray.width//100)
                                      for y in (0,gray.height-1))
                    # Keep tall screenshots out of MinerU's long-edge downscale
                    # failure mode. This version fails visibly; never shrink silently.
                    if oriented.height / max(1, oriented.width) > 4 or oriented.width / max(1, oriented.height) > 4:
                        raise ImageZipError(f'第 {index} 张是超长图片，请先分成普通页面比例；当前不自动切分，未交付缺字结果')
                    normalized = normalized_dir / (ordinal + '.png')
                    if oriented.mode not in {'RGB', 'L'}:
                        rgba = oriented.convert('RGBA'); canvas = Image.new('RGB', rgba.size, 'white'); canvas.paste(rgba, mask=rgba.getchannel('A'))
                        canvas.save(normalized)
                    else:
                        oriented.save(normalized)
            records.append({'order':index,'member':info.filename,'sha256':digest.hexdigest(), 'edge_review':edge_review})
        (work_dir / 'image-order.json').write_text(json.dumps({'schema_version':'image-zip/v1','ordering':'numeric_filename','images':records},ensure_ascii=False,indent=2),encoding='utf-8')
        ocr_dir = work_dir / 'mineru-output'
        # One batch invocation reuses local models for all pages.
        ok, message = run_command([str(tools_dir / 'pdf2md.sh'), str(normalized_dir), str(ocr_dir), 'pipeline', 'ocr', language], timeout=timeout)
        (work_dir / 'mineru.log').write_text(message, encoding='utf-8')
        if not ok:
            reason = '调用超时' if '超时' in message else '识别失败'
            raise ImageZipError(f'图片批次{reason}，已有中间结果保留，没有交付不完整文章')
        for index, info in enumerate(members, 1):
            ordinal = f'{index:04d}'
            suffix = PurePosixPath(info.filename).suffix.lower()
            original = raw_dir / (ordinal + suffix)
            markdowns = [p for p in ocr_dir.rglob('*.md') if p.stem == ordinal]
            if len(markdowns) != 1:
                raise ImageZipError(f'第 {index} 张未生成唯一正文，请核对本机识别结果')
            md = markdowns[0].read_text(encoding='utf-8')
            if len(readable_text(md)) < 10:
                raise ImageZipError(f'第 {index} 张未识别出足够文字，请核对图片；不会把图片链接当成正文')
            # Keep every OCR-local asset path under this image's own namespace.
            assets = stage / 'images' / ordinal
            assets.mkdir()
            original_dest = assets / ('original' + suffix)
            shutil.copy2(original, original_dest)
            def rewrite(match):
                target = match.group(2)
                if re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*:', target) or target.startswith('#'):
                    return match.group(0)  # no fetch; the source link is untrusted text
                rel = PurePosixPath(target)
                if rel.is_absolute() or '..' in rel.parts or '\\' in target:
                    raise ImageZipError('识别输出的附件路径不安全，已停止')
                asset = markdowns[0].parent.joinpath(*rel.parts)
                if asset.is_symlink() or not asset.is_file():
                    raise ImageZipError('识别正文引用了缺失附件，未交付断链文章')
                destination = assets.joinpath(*rel.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    if destination.read_bytes() != asset.read_bytes():
                        raise ImageZipError('识别输出附件重名，已停止')
                else:
                    shutil.copy2(asset, destination)
                return match.group(1) + f'images/{ordinal}/{target}' + ')'
            md = re.sub(r'(!?\[[^\]]*\]\()([^\n)]+)\)', rewrite, md)
            sections.append(f'## 第 {index} 张\n\n{md.strip()}\n\n[查看原图](images/{ordinal}/{original_dest.name})')
            (work_dir / 'progress.json').write_text(json.dumps({'completed':index,'total':len(members)}),encoding='utf-8')
    (stage / '正文.md').write_text('\n\n'.join(sections)+'\n',encoding='utf-8')
    (work_dir / 'image-order.json').write_text(json.dumps({'schema_version':'image-zip/v1','ordering':'numeric_filename','images':records},ensure_ascii=False,indent=2),encoding='utf-8')
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    if output_dir.exists():
        raise ImageZipError('已有同名转换目录，未覆盖；请通过转换服务建立新版本')
    # Work and candidate directory share the batch filesystem.
    stage.rename(output_dir)
    return len(records)
