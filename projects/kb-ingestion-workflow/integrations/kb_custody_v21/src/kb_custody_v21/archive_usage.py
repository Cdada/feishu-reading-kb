"""Local attribution of reserved HTTP attempts, never a provider quota query.

Historical and shared requests remain unattributed. Reservations include failures
and unknown results; these figures are not the provider's billing statement.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

OWNER = ContextVar('archive_request_owner', default=None)
SCHEMA = """
CREATE TABLE IF NOT EXISTS archive_request_owners(
 request_id INTEGER PRIMARY KEY, message_id TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS archive_request_owners_message ON archive_request_owners(message_id);
"""

@contextmanager
def document_usage(message_id):
    token=OWNER.set(message_id)
    try:
        yield
    finally:
        OWNER.reset(token)

def attributed(argument):
    def decorate(function):
        @wraps(function)
        def run(owner, value, *args, **kwargs):
            mid=value if argument=='id' else value[argument]
            with document_usage(mid):
                return function(owner,value,*args,**kwargs)
        return run
    return decorate

def reserve_owner(db, request_id, kind):
    mid=OWNER.get()
    # Identity checks and paginated scans serve the whole service.
    if mid is not None and kind not in {'identity','list'} and db.execute(
            'SELECT 1 FROM daily_documents WHERE message_id=?',(mid,)).fetchone():
        db.execute('INSERT INTO archive_request_owners VALUES (?,?)',(request_id,mid))

def snapshot(db, now, policy):
    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    total=db.execute('SELECT coalesce(sum(count),0) FROM daily_requests WHERE at>?',(now-86400,)).fetchone()[0]
    minute=db.execute('SELECT coalesce(sum(count),0) FROM daily_requests WHERE at>?',(now-60,)).fetchone()[0]
    documents=[]; attributed_day=0
    if 'archive_request_owners' in tables:
        documents=[dict(r) for r in db.execute("""SELECT d.message_id,d.phase,sum(r.count) AS reserved_requests,
            count(r.id) AS attempts,min(r.at) AS first_at,max(r.at) AS last_at
            FROM archive_request_owners o JOIN daily_requests r ON r.id=o.request_id
            JOIN daily_documents d ON d.message_id=o.message_id GROUP BY d.message_id ORDER BY d.message_id""")]
        attributed_day=db.execute("""SELECT coalesce(sum(r.count),0) FROM daily_requests r
            JOIN archive_request_owners o ON o.request_id=r.id WHERE r.at>?""",(now-86400,)).fetchone()[0]
    ceiling=policy['http_per_day']
    return dict(reference='archive-usage-v1',reserved_24h=total,reserved_minute=minute,
        limit_24h=ceiling,limit_minute=policy['http_per_minute'],confirmation_reserve=policy['http_confirmation_reserve'],
        shared_or_historical_24h=total-attributed_day,documents=documents,
        near_limit=total>=ceiling*0.9,over_limit=total>ceiling,
        basis='local_reserved_requests_including_failures_not_provider_usage')

def settle(store):
    with store.transaction() as db:
        value=snapshot(db,store.now(),store._get(db,'policy'))
        for row in value['documents']:
            wait=store._get(db,'defer:'+row['message_id'])
            if row['phase'] in {'notified','published','rejected','duplicate','held','needs_upload','model_unknown','reading_wait'} or wait:
                record=dict(row,paused_reason=wait.get('reason') if wait else None,
                    basis=value['basis'],excludes_shared_or_historical=True)
                key='archive-usage:'+row['message_id']
                if store._get(db,key)!=record:
                    store._put(db,key,record)
