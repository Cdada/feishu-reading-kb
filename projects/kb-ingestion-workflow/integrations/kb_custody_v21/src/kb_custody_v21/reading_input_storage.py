"""Lossless local input storage; model requests remain byte-equivalent.

Shared card/topic text is retained once in existing_files. No live library reads
are needed to restore a snapshot, so a later edit cannot change a saved request.
"""
import hashlib
import json
from .daily_transport import durable_file
from .runtime import no_symlinks
from .reading_contract import require, sha, validate_input

FORMAT='reading-input-storage/v1'

def pack(value):
    source=validate_input(value['source'])
    compact=dict(source)
    for key in ('existing_cards','existing_topics'):
        compact[key]={}
        for path,entry in source[key].items():
            require(value['existing_files'].get(path)==entry['text'],'saved context copies disagree')
            require(hashlib.sha256(entry['text'].encode()).hexdigest()==entry['sha256'],'context digest disagrees')
            compact[key][path]=dict(sha256=entry['sha256'])
    same_excerpt=bool(source['excerpts']) and source['excerpts']==source['original']
    if same_excerpt:compact['excerpts']=''
    return dict(format=FORMAT,source_sha256=sha(source),excerpt_reuses_original=same_excerpt,
                saved=dict(value,source=compact))

def unpack(value):
    if 'format' not in value:
        validate_input(value['source'])
        return value
    require(set(value)=={'format','source_sha256','excerpt_reuses_original','saved'} and value['format']==FORMAT,
            'unsupported saved reading input format')
    require(type(value['excerpt_reuses_original']) is bool,'invalid excerpt storage marker')
    saved=dict(value['saved']);source=dict(saved['source'])
    for key in ('existing_cards','existing_topics'):
        source[key]={}
        for path,entry in saved['source'][key].items():
            require(set(entry)=={'sha256'} and path in saved['existing_files'],'missing retained context')
            text=saved['existing_files'][path]
            require(isinstance(text,str) and hashlib.sha256(text.encode()).hexdigest()==entry['sha256'],
                    'retained context changed')
            source[key][path]=dict(sha256=entry['sha256'],text=text)
    if value['excerpt_reuses_original']:source['excerpts']=source['original']
    validate_input(source)
    require(sha(source)==value['source_sha256'],'restored reading input changed')
    saved['source']=source
    return saved


def save(path, value):
    path=no_symlinks(path)
    if path.exists():
        # A crash may have left a valid old-format snapshot before the queue
        # pointer committed. Reuse its exact bytes and return its original hash.
        require(unpack(json.loads(path.read_bytes()))==value,'saved reading input differs from current plan')
        return
    durable_file(path,pack(value))
