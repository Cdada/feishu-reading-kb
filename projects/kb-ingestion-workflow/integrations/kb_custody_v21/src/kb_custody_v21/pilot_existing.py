"""Read V1 registered metadata without initializing or mutating its services.

Matches are review hints, never approval to read a body, reuse a publication or
merge two sources. Unindexed filesystem articles are outside the stated scope.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3
import time
import unicodedata

from .contracts import ContractError
from .runtime import no_symlinks
from .f05 import file_sha


def filename_key(name, *, legacy=False):
    if not isinstance(name, str) or not name.strip() or len(name.encode()) > 1024 or any(x in name for x in '\0/\\'):
        raise ContractError('bounded catalog filename required')
    # Same rule as the installed V1 gateway's workflow lookup: inbound disk
    # names can have an ordinal prefix, user-provided names are not stripped.
    return unicodedata.normalize('NFC', re.sub(r'^0[1-5]-', '', name) if legacy else name).casefold().strip()


@contextmanager
def catalog_reader(path):
    path = no_symlinks(Path(path).absolute())
    if not path.is_file():
        raise ContractError('configured V1 metadata ledger is unavailable')
    db = None
    wal = Path(str(path)+'-wal')
    def evidence():
        return (file_sha(path), file_sha(no_symlinks(wal)) if wal.exists() else None)
    before = evidence()
    live_wal = wal.exists() and wal.stat().st_size > 0
    if live_wal and not no_symlinks(Path(str(path)+'-shm')).is_file():
        raise ContractError('live V1 WAL requires its existing shared-memory sidecar; no source initialization')
    # A regular read-only connection can create WAL/SHM even when no WAL data
    # exists. Only that case uses immutable reading, and source/WAL evidence is
    # rechecked before returning. A nonempty WAL is always read by SQLite.
    uri = path.as_uri() + ('?mode=ro' if live_wal else '?mode=ro&immutable=1')
    deadline = time.monotonic() + 5
    try:
        db = sqlite3.connect(uri, uri=True, timeout=2)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        db.execute('BEGIN')
        yield db
        if evidence() != before:
            raise ContractError('V1 catalog changed during lookup; repeat a fresh read before deciding')
    except sqlite3.Error as error:
        raise ContractError('V1 metadata could not be fully queried; do not assume no match') from error
    finally:
        if db is not None: db.close()


def validate_catalog(catalog):
    if (not isinstance(catalog, dict) or set(catalog) != {'curation_ledger_path','conversion_ledger_path'}
            or any(not isinstance(v,str) or not Path(v).is_absolute() for v in catalog.values())):
        raise ContractError('explicit V1 metadata ledger paths required')
    for value in catalog.values():
        if not no_symlinks(Path(value)).is_file():
            raise ContractError('configured V1 metadata ledger is unavailable')


def lookup_existing(filename, sha256, catalog, *, max_records=5000, max_matches=50):
    """Return bounded metadata and an explicit completeness/scope assessment."""
    if (not isinstance(catalog, dict) or set(catalog) != {'curation_ledger_path','conversion_ledger_path'}
            or any(not isinstance(v,str) or not Path(v).is_absolute() for v in catalog.values())
            or not isinstance(sha256,str) or not re.fullmatch('[a-f0-9]{64}',sha256)
            or type(max_records) is not int or not 1 <= max_records <= 5000
            or type(max_matches) is not int or not 1 <= max_matches <= 50):
        raise ContractError('explicit V1 catalog paths and bounded lookup required')
    validate_catalog(catalog)
    key = filename_key(filename)
    matches = {}; complete = True; received_before = False

    def add(row, origin):
        name = row['filename']; digest = row.get('sha256')
        old_key = filename_key(name,legacy=True)
        same_name = old_key == key
        same_title = Path(old_key).stem == Path(key).stem and Path(old_key).suffix != Path(key).suffix
        if digest is not None and (not isinstance(digest,str) or not re.fullmatch('[a-f0-9]{64}',digest)):
            raise ContractError('V1 source fingerprint is invalid; inspect catalog')
        same_content = digest == sha256
        if not same_name and not same_content and not same_title: return
        relation = ('same_name_same_bytes' if same_name and same_content else 'same_bytes' if same_content
                    else 'possible_same_article_different_format' if same_title
                    else 'same_name_different_bytes' if digest else 'name_only')
        item = dict(source_task_id=row['task_id'], filename=name, relationship=relation,
                    source_sha256=digest, publication_id=row.get('publication_id'),
                    recorded_state=row.get('state'), origin=origin, reusable_verified=False)
        identity = (row['task_id'], row.get('publication_id'), name)
        matches[identity] = item

    with catalog_reader(catalog['curation_ledger_path']) as db:
        # Select metadata columns only; draft_json, body chunks, consent tokens
        # and publication payloads are not part of a lookup.
        rows = db.execute('''SELECT p.source_task_id AS task_id,p.source_filename AS filename,
            p.source_sha256 AS sha256,p.status AS state,p.plan_id,
            (SELECT pu.publication_id FROM publications pu JOIN curation_runs r ON pu.run_id=r.run_id
             WHERE r.plan_id=p.plan_id AND pu.state='committed' AND pu.source_sha256=p.source_sha256
             ORDER BY pu.publication_id LIMIT 1) AS publication_id
            FROM plans p ORDER BY p.updated_at DESC,p.plan_id LIMIT ?''',(max_records+1,)).fetchall()
        complete = complete and len(rows) <= max_records
        for row in rows[:max_records]: add(dict(row),'v1_curation')
        # Preserve V1's original upload-name alias for user-selected converted
        # text, without loading the workflow's full private payload.
        rows = db.execute('''SELECT filename,
            json_extract(payload_json,'$.source_task_id') AS task_id,
            json_extract(payload_json,'$.converted_handoff.task_id') AS handoff_task_id
            FROM workflow_intakes WHERE filename_key=? LIMIT ?''',(key,max_records+1)).fetchall()
        received_before = bool(rows); complete = complete and len(rows) <= max_records
        for row in rows[:max_records]:
            if row['task_id'] and row['task_id'] == row['handoff_task_id']:
                plan = db.execute('''SELECT p.source_task_id AS task_id,p.status AS state,
                    (SELECT pu.publication_id FROM publications pu JOIN curation_runs r ON pu.run_id=r.run_id
                     WHERE r.plan_id=p.plan_id AND pu.state='committed' AND pu.source_sha256=p.source_sha256
                     ORDER BY pu.publication_id LIMIT 1) AS publication_id
                    FROM plans p WHERE p.source_task_id=? ORDER BY p.updated_at DESC LIMIT 1''',(row['task_id'],)).fetchone()
                if plan: add(dict(plan,filename=row['filename'],sha256=None),'v1_original_upload_alias')
    with catalog_reader(catalog['conversion_ledger_path']) as db:
        rows = db.execute('''SELECT t.task_id,t.status AS state,i.filename,i.sha256
            FROM tasks t JOIN inspections i ON i.inspection_id=t.inspection_id
            ORDER BY t.updated_at DESC,t.task_id LIMIT ?''',(max_records+1,)).fetchall()
        complete = complete and len(rows) <= max_records
        for row in rows[:max_records]:
            # Include interrupted or failed conversions as known history too.
            # They must not silently look like a new article with a new budget.
            task = row['task_id']
            if any(k[0] == task for k in matches): continue
            add(dict(row),'v1_conversion')
    values = list(matches.values())
    complete = complete and len(values) <= max_matches
    return dict(scope='v1_registered_metadata', complete=complete, matches=values[:max_matches],
                received_before=received_before, observed_at=datetime.now(timezone.utc).isoformat(),
                requires_review=bool(values) or received_before or not complete,
                unindexed_files_checked=False, bodies_read=False, formal_write=False)
