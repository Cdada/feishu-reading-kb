"""Stable publication evidence for frozen operator inputs. No transport or approval."""
from pathlib import Path
import json
from kb_custody_v21.contracts import ContractError
from kb_custody_v21.f05 import file_sha
from kb_custody_v21.publication import Publications
from kb_custody_v21.runtime import no_symlinks

def reject_moving_inputs(runtime, files):
    root=runtime.data_root/'conversion-work/publications'
    for name in files:
        path=Path(name)
        relative=path.relative_to(root) if path.is_relative_to(root) else None
        if relative and relative.parts and relative.parts[0].endswith('.candidate'):
            raise ContractError('freeze a stable publication evidence copy, not a live candidate path')

def stable_publication_inputs(registry,pid,destination):
    publisher=Publications(registry);row=publisher._row(pid)
    if row['state'] not in {'prepared','published'}:
        raise ContractError('stable input preparation requires an intact prepared or published version')
    source=publisher._tree(row,pid+'.candidate' if row['state']=='prepared' else pid)
    destination=no_symlinks(Path(destination).absolute())
    if destination.is_relative_to(publisher.root):
        raise ContractError('evidence copies must stay outside the live publication directory')
    destination.mkdir(parents=True,exist_ok=False)
    expected=json.loads(row['files_json']);frozen={}
    for name in expected:
        original=source/name;target=destination/'files'/name
        target.parent.mkdir(parents=True,exist_ok=True)
        with target.open('xb') as stream:stream.write(original.read_bytes())
        if file_sha(target)!=file_sha(original):raise ContractError('publication copy changed')
        frozen[str(target)]=file_sha(target)
    publisher._tree(row,source.name)
    manifest=destination/'manifest.local.json'
    with manifest.open('x') as stream:
        json.dump(dict(publication_id=pid,preview_sha256=row['preview_sha256'],publication_manifest_sha256=row['manifest_sha256'],files=frozen),stream,ensure_ascii=False,indent=2)
    frozen[str(manifest)]=file_sha(manifest)
    reject_moving_inputs(registry.runtime,frozen)
    return frozen
