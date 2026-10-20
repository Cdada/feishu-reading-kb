#!/usr/bin/env python3
"""Package one already converted Markdown article for the finite V2.1 inbox.

Run explicitly in Codex after PDF conversion. This command has no model,
network, iCloud publication or knowledge-base write side effect.
"""
import argparse
from io import BytesIO
from pathlib import Path, PurePosixPath
import tempfile
from urllib.parse import unquote, urlsplit
import zipfile

from markdown_bundle import (IMAGE_EXTENSIONS, MAX_BODY_BYTES, MAX_MEMBER_BYTES,
                             MAX_TOTAL_BYTES, MarkdownBundleError,
                             _links, convert_markdown_bundle)


def checked_file(path, maximum):
    path = Path(path).absolute()
    if any(parent.is_symlink() for parent in (path, *path.parents)) or not path.is_file():
        raise MarkdownBundleError('只接受普通文件，不能通过链接读取其他目录')
    with path.open('rb') as stream:
        raw = stream.read(maximum+1)
    if len(raw) > maximum:
        raise MarkdownBundleError('文件超过资料包大小上限')
    return raw


def pack_markdown(markdown, output, *, original_pdf=None, sidecars=()):
    markdown, output = Path(markdown).absolute(), Path(output).absolute()
    if markdown.suffix.lower() != '.md' or output.suffix.lower() != '.zip':
        raise MarkdownBundleError('请选择 MD 正文和 ZIP 输出文件')
    if output.exists():
        raise MarkdownBundleError('输出文件已存在，未覆盖')
    content = checked_file(markdown, MAX_BODY_BYTES)
    text = content.decode('utf-8-sig')
    members = {markdown.name: content}
    for target, image in _links(text):
        url = urlsplit(target)
        if not image and (url.scheme in {'http', 'https', 'mailto'} or target.startswith('#')):
            continue
        relative = PurePosixPath(unquote(url.path))
        if (url.scheme or url.netloc or url.query or image and url.fragment
                or relative.is_absolute() or '..' in relative.parts
                or any(c in str(relative) for c in '\\:\x00\r\n')
                or relative.suffix.lower() not in IMAGE_EXTENSIONS | {'.md', '.json', '.pdf'}):
            raise MarkdownBundleError('正文附件必须使用安全的本地相对路径；请先补齐外部图片')
        name = relative.as_posix()
        if name not in members:
            members[name] = checked_file(markdown.parent.joinpath(*relative.parts), MAX_MEMBER_BYTES)
        if sum(map(len, members.values())) > MAX_TOTAL_BYTES:
            raise MarkdownBundleError('资料包总内容超过 64 MiB')
    extras = [('source/original.pdf', original_pdf)] if original_pdf else []
    extras += [('source/'+Path(path).name, path) for path in sidecars]
    for name, path in extras:
        if name in members:
            raise MarkdownBundleError('辅助文件与正文附件重名，未覆盖')
        expected = '.pdf' if name == 'source/original.pdf' else '.json'
        if Path(path).suffix.lower() != expected:
            raise MarkdownBundleError('辅助文件仅接受原 PDF 和明确选择的 JSON')
        members[name] = checked_file(path, MAX_MEMBER_BYTES)
        if sum(map(len, members.values())) > MAX_TOTAL_BYTES:
            raise MarkdownBundleError('资料包总内容超过 64 MiB')
    stream = BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, raw in members.items():
            archive.writestr(name, raw)
    package = stream.getvalue()
    if len(package) > 32*1024**2:
        raise MarkdownBundleError('ZIP 超过首版单份接收的 32 MiB 上限')
    # Use the same importer as the actual worker; a package is delivered only
    # after its links, images and archive members have passed that contract.
    with tempfile.TemporaryDirectory(prefix='kb-markdown-package-') as folder:
        check = Path(folder)/'input.zip'
        check.write_bytes(package)
        convert_markdown_bundle(check, Path(folder)/'validated')
    if any(p.is_symlink() for p in output.parents):
        raise MarkdownBundleError('输出目录不能通过链接跳转')
    with output.open('xb') as target:
        target.write(package)
    return dict(output=str(output), files=len(members), size_bytes=len(package))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--markdown', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--original-pdf', type=Path)
    parser.add_argument('--sidecar', action='append', default=[], type=Path)
    args = parser.parse_args()
    try:
        result = pack_markdown(args.markdown, args.output, original_pdf=args.original_pdf, sidecars=args.sidecar)
    except (MarkdownBundleError, UnicodeError, OSError) as error:
        parser.exit(1, '未生成资料包：'+str(error)+'\n')
    print('资料包检查通过：'+result['output'])
    print(f"共 {result['files']} 个文件，{result['size_bytes']} 字节；尚未发送或入库。")


if __name__ == '__main__':
    main()
