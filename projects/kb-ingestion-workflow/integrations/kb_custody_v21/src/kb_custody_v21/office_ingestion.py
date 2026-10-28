"""Adapt immutable office/text conversion to the existing custody worker."""
import csv
import json
from pathlib import Path

from .contracts import ContractError
from .conversion import durable_new, sha
from .office_documents import DocumentProblem, extract_document
from .registry import encoded


def read_original(conversion, entry):
    path = conversion.runtime.checked(conversion.runtime.path(entry['relative_path']))
    with path.open('rb') as stream:
        raw = stream.read(32*1024**2+1)
    if len(raw) != entry['size_bytes'] or sha(raw) != entry['sha256']:
        raise ContractError('accepted office original changed or exceeds byte bound')
    return raw


def stage_document(conversion, entry, batch):
    try:
        result = extract_document(read_original(conversion, entry), Path(entry['safe_filename']).suffix.lower())
    except DocumentProblem as error:
        with conversion.registry.transaction() as db:
            conversion.registry.event(db, 'office_conversion_blocked', entry['entry_id'],
                'office-conversion:'+entry['entry_id']+':'+sha(str(error).encode()), dict(message=str(error)))
        raise
    manifest = batch/'_batch_manifest.csv'
    with manifest.open(encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f); fields, rows = reader.fieldnames, list(reader)
    if len(rows) != 1:
        raise ContractError('one accepted office original required')
    root = conversion.runtime.checked(batch/'04_知识库输入文件/custody-document')
    root.mkdir(parents=True, exist_ok=False)
    content = result.text.encode()
    durable_new(root/'source.md', content)
    durable_new(root/'candidate.md', '# 完整资料预检查\n\n'.encode()+content)
    durable_new(root/'document-map.json', encoded(result.mapping(entry['sha256'])).encode())
    for target, data in result.assets.items():
        path = conversion.runtime.checked(root/target)
        path.parent.mkdir(exist_ok=True)
        durable_new(path, data)
    rows[0].update(processing_status='processed', kb_folder=root.relative_to(batch).as_posix(),
        primary_input=(root/'candidate.md').relative_to(batch).as_posix(),
        auxiliary_input=(root/'source.md').relative_to(batch).as_posix(),
        group_role='', classification='混合型', quality_status='', conversion_method='custody_office_document_v1',
        notes='原件完整保留，正文未截断；结构位置不代表物理页码。转换完成不代表已批准导读或入库。')
    with manifest.open('w', encoding='utf-8-sig', newline='') as f:
        writer=csv.DictWriter(f, fieldnames=fields); writer.writeheader(); writer.writerows(rows)
    return True


def checked_document_map(conversion, entry, source, text):
    saved = json.loads((source.parent/'document-map.json').read_bytes())
    # Deterministically reproduce from leased accepted bytes. Neither a sidecar
    # nor a reader-supplied coordinate is trusted independently of the original.
    result = extract_document(read_original(conversion, entry), Path(entry['safe_filename']).suffix.lower())
    if saved != result.mapping(entry['sha256']) or result.text != text:
        raise ContractError('office structure map differs from accepted original')
    for target, data in result.assets.items():
        if conversion.runtime.checked(source.parent/target).read_bytes() != data:
            raise ContractError('office extracted image changed')
    return dict(extractor='custody-office-documents', extractor_version=sha(Path(__file__).with_name('office_documents.py').read_bytes()),
                locator_precision='document_structure', document_structure=saved,
                issues=['Structure locations are not printed pages; see document_structure.details for format boundaries.'])
