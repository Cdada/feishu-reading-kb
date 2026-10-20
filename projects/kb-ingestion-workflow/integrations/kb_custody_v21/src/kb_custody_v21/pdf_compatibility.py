"""Strict PDF reading with one narrow, auditable duplicate-field repair.

Only adjacent identical numeric MediaBox arrays are removed in a derived copy.
Object values are read by pypdf; the byte position comes from its strict error.
Equal-length whitespace preserves all original cross-reference offsets.
"""
import hashlib
import io
import math
from pathlib import Path
import re

from pypdf import PdfReader
from pypdf._utils import read_non_whitespace
from pypdf.errors import PdfReadError
from pypdf.generic import ArrayObject, FloatObject, NumberObject, read_object


REFERENCE = 'pdf-identical-adjacent-mediabox/v1'
WHITESPACE = b' \t\r\n\x00\x0c'
DUPLICATE = re.compile(r'Multiple definitions in dictionary at byte (0x[0-9a-f]+) for key /MediaBox')


def _array_at(data, position):
    stream = io.BytesIO(data)
    stream.seek(position)
    if read_object(stream, None) != '/MediaBox':
        raise PdfReadError('Expected the duplicate MediaBox name')
    read_non_whitespace(stream)
    stream.seek(-1, 1)
    value = read_object(stream, None)
    if (not isinstance(value, ArrayObject) or len(value) != 4
            or any(type(n) not in (NumberObject, FloatObject) or not math.isfinite(float(n)) for n in value)
            or value[0] >= value[2] or value[1] >= value[3]):
        raise PdfReadError('Only finite direct MediaBox rectangles can be repaired')
    return value, stream.tell()


def read_pdf(source):
    original = Path(source).read_bytes()
    data, changes = original, []
    for _ in range(101):
        try:
            reader = PdfReader(io.BytesIO(data), strict=True)
            if not reader.is_encrypted:
                len(reader.pages)  # Resolve the page tree before accepting it.
            audit = None if not changes else dict(reference=REFERENCE,
                source_sha256=hashlib.sha256(original).hexdigest(),
                derived_sha256=hashlib.sha256(data).hexdigest(), changes=changes)
            return reader, audit, data
        except PdfReadError as error:
            match = DUPLICATE.fullmatch(str(error))
            if not match or len(changes) >= 100:
                raise
            end = int(match[1], 16)
            # This is an error-bounded lookup, never a global PDF replacement.
            second = data.rfind(b'/MediaBox', max(0, end-4096), end)
            first = data.rfind(b'/MediaBox', max(0, second-4096), second)
            if first < 0 or second < 0:
                raise
            line = max(data.rfind(b'\n', 0, first), data.rfind(b'\r', 0, first)) + 1
            if data[line:first].strip(WHITESPACE):
                raise  # Do not mistake a commented-out field for the original.
            left, left_end = _array_at(data, first)
            right, right_end = _array_at(data, second)
            if right_end != end or data[left_end:second].strip(WHITESPACE) or left != right:
                raise  # Conflicting values or intervening tokens stay errors.
            changes.append(dict(start=second, end=end, key='/MediaBox', values=[float(n) for n in right]))
            data = data[:second] + b' '*(end-second) + data[end:]
    raise PdfReadError('PDF compatibility correction limit exceeded')
