"""Complete Git source inventory and structure-preserving XML redaction."""
import hashlib
import json
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET


def source_snapshot(worktree):
    worktree = Path(worktree)
    # Include every tracked file (all formats) and pending, nonignored additions.
    # Runtime evidence and credentials must remain ignored, never test sources.
    names = subprocess.check_output(
        ['git', '-C', str(worktree), 'ls-files', '-z', '--cached', '--others', '--exclude-standard']
    ).decode().split('\0')
    files = {}
    for name in sorted(set(names) - {''}):
        path = worktree / name
        if path.is_symlink() or not path.is_file():
            raise ValueError('source inventory contains missing/nonregular file: ' + name)
        files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {'files': files, 'sha256': hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()}


def redact_junit(data, redact):
    # Placeholders such as <DEV_WORKTREE> are text, never XML markup. Redact
    # parsed values then let ElementTree escape attribute/text content.
    root = ET.fromstring(data)
    for element in root.iter():
        element.attrib.update({key: redact(value) for key, value in element.attrib.items()})
        if element.text is not None:
            element.text = redact(element.text)
        if element.tail is not None:
            element.tail = redact(element.tail)
    result = ET.tostring(root, encoding='utf-8', xml_declaration=True)
    ET.fromstring(result)
    return result
