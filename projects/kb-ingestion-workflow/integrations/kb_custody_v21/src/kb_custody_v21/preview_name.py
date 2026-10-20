"""Readable attachment labels; publication identity remains independent."""
from pathlib import Path
import re
import unicodedata

from .contracts import ContractError


def preview_filename(title, version):
    if not isinstance(title, str) or type(version) is not int or not 1 <= version <= 999999999:
        raise ContractError('preview filename requires a title and positive candidate version')
    title = unicodedata.normalize('NFC', title)
    title = ''.join(' ' if c in '/\\:*?"<>|' or unicodedata.category(c) in {'Cc', 'Cf', 'Cs'} else c for c in title)
    title = re.sub(r'\s+', ' ', title).strip(' .')
    if Path(title).suffix.lower() in {'.pdf', '.md', '.webarchive'}:
        title = str(Path(title).with_suffix('')).strip(' .')
    title = title or '未命名资料'
    if len(title) > 48:
        title = title[:47].rstrip() + '…'
    return f'导读_{title}_第{version}版.pdf'
