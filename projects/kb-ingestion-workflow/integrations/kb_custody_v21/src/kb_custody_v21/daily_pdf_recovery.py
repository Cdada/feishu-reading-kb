"""Operator-only repair after stopping the service; no model or HTTP client."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path

from .contracts import ContractError
from .locking import open_lock
from .mineru_chunks import PdfChunks, PdfOptions, read_json, checked_snapshot
from .mineru_ingestion import checked_mineru_map


class NoConverter:
    def convert(self, *args, **kwargs):
        raise ContractError('repair entry cannot start a new conversion')


@contextmanager
def stopped_service(authority):
    # Fence both a new launcher and surviving owned MinerU children while files
    # are inspected or imported. Never signal a PID from an earlier run.
    descriptors = []
    try:
        for path in (authority.root/'startup.lock', authority.root/'service.lock',
                     authority.root/'mineru/mineru.lock'):
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = open_lock(path)
            descriptors.append(fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ContractError('stop the daily service and wait for MinerU before PDF repair') from error
        yield
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def resume_repaired_pdf(authority, store, registry, message_id, *, operator_ref, first_page=None, raw_output=None):
    with stopped_service(authority):
        authority.check()
        return _resume(authority,store,registry,message_id,operator_ref=operator_ref,first_page=first_page,raw_output=raw_output)


def _resume(authority, store, registry, message_id, *, operator_ref, first_page, raw_output):
    if not isinstance(operator_ref,str) or not operator_ref.strip() or len(operator_ref)>1000:
        raise ContractError('explicit local repair reference required')
    document = store.document(message_id)
    if (document['phase'] != 'held' or document['publication_id'] or document['canonical_id']
            or Path(document['filename']).suffix.lower() != '.pdf'
            or store.rows('SELECT 1 FROM daily_models WHERE source_sha=?',(document['source_sha'],))):
        raise ContractError('repair requires a held PDF with no guide/model/publication')
    entry = registry.entry(document['entry_id'])
    jobs = registry.rows('SELECT task_id FROM pilot_conversion_jobs WHERE entry_id=?',(document['entry_id'],))
    if entry['status'] != 'accepted' or entry['custody_health'] != 'healthy' or not jobs or jobs[0]['task_id'] is not None:
        raise ContractError('repair cannot replace an existing conversion task or damaged original')
    if 'pdf' not in authority.config:
        raise ContractError('PDF execution profile not enabled')
    engine = PdfChunks(authority.runtime.path('conversion-work/pdf-chunks'),PdfOptions(**authority.config['pdf']['options']),
                       NoConverter(),check=authority.check)
    source = authority.runtime.path(entry['relative_path'])
    if (first_page is None) != (raw_output is None):
        raise ContractError('repair must specify both original first page and corrected output')
    if first_page is not None:
        engine.import_repaired_chunk(source,entry['sha256'],first_page,raw_output,operator_ref=operator_ref)
    manifest = read_json(engine.root/entry['sha256']/'manifest.json')
    if any(c['completed'] is None and sum(r['state']=='failed' for r in c['attempts']) >= 2
           for c in manifest['chunks']):
        return dict(state='repair_saved_other_chunks_pending',new_model_calls=0,new_conversion_calls=0)
    if all(c['completed'] is not None for c in manifest['chunks']):
        complete = engine.convert(source,entry['sha256'])
        text = (complete/'source.md').read_text(encoding='utf-8')
        checked_mineru_map(read_json(complete/'source.pages.json'),text,entry['sha256'],complete)
    else:
        # Later, unattempted chunks resume under the normal service; the repair
        # command itself cannot launch a converter or spend a model turn.
        for chunk in manifest['chunks']:
            if chunk['completed']:
                checked_snapshot(engine.root/entry['sha256']/chunk['completed']['directory'],chunk['completed']['files'])
    with store.transaction() as db:
        current = db.execute('SELECT * FROM daily_documents WHERE message_id=?',(message_id,)).fetchone()
        if current['phase'] != 'held' or current['source_sha'] != entry['sha256']:
            raise ContractError('held document changed during local repair')
        if db.execute('SELECT 1 FROM daily_models WHERE source_sha=?',(entry['sha256'],)).fetchone():
            raise ContractError('model reservation appeared during repair')
        db.execute("UPDATE daily_documents SET phase='original_saved',user_action=NULL,reason=NULL,updated=? WHERE message_id=?",(store.now(),message_id))
        store._event(db,'pdf_operator_repair',dict(message_id=message_id,operator_ref=operator_ref,previous_reason=current['reason'],
                                                 source_sha256=entry['sha256'],new_model_calls=0,new_conversion_calls=0))
    return dict(state='ready_to_resume',message_id=message_id,new_model_calls=0,new_conversion_calls=0)
