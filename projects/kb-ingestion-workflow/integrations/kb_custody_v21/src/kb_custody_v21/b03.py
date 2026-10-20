"""One distinct synthetic upload; the original B01 unknown is never retried."""
from datetime import datetime
from contextlib import closing
from pathlib import Path
import sqlite3

from .b01 import BaiduExperiment, prepare_fixture, LIMITS
from .b02 import prior_binding, frozen_files
from .baidu_protocol import json_object
from .contracts import ContractError
from .f01 import encoded, sha
from .f01_flow import TXT
from .runtime import no_symlinks

RUNTIME = '.dev-runtime/kb-v21/test-output/baidu-b03'
APPROVAL = '.dev-runtime/kb-v21/real-validation/baidu-b03-approval.local.json'
RETRY_APPROVAL = '.dev-runtime/kb-v21/real-validation/baidu-b03-r1-approval.local.json'
CONTENT = (b'KB V2.1 B03 synthetic archive verification.\n'
           b'This is a new independent test asset, not a retry of B01.\n'
           b'Expected round trip: preserve these exact UTF-8 bytes.\n')
PURPOSE = 'new_distinct_synthetic_archive_preserve_old_unknown'


def fixture():
    return dict(name='fixture.txt', sha256=sha(CONTENT), size_bytes=len(CONTENT))


def prepare(worktree, *, root=None):
    return prepare_fixture(worktree, root or Path(worktree)/RUNTIME, CONTENT, 'B03')


def observation_files(worktree, bindings):
    if not isinstance(bindings, dict) or set(bindings) != {'approval','journal','consent'}:
        raise ContractError('exact prior B02 observation files required')
    paths = {}
    for key, value in bindings.items():
        if not isinstance(value, dict) or set(value) != {'relative_path','sha256'}:
            raise ContractError('invalid B02 evidence binding')
        relative = Path(value['relative_path'])
        if relative.is_absolute() or '..' in relative.parts or relative.parts[:1] not in (('.dev-runtime',),('tmp',)):
            raise ContractError('prior B02 evidence must remain private DEV data')
        path = no_symlinks(worktree/relative)
        if path.stat().st_size > 512*1024 or sha(path.read_bytes()) != value['sha256']:
            raise ContractError('prior B02 evidence changed or oversized')
        paths[key] = path
    return paths


def predecessor(worktree, approval):
    paths = observation_files(worktree, approval['prior_observation'])
    old = json_object(paths['approval'].read_bytes())
    events = [json_object(line) for line in paths['journal'].read_bytes().splitlines()]
    consent = json_object(paths['consent'].read_bytes())
    if (old.get('reference') != 'B02' or old.get('approved') is not True
            or old.get('source_commit') != approval.get('predecessor_commit')
            or any(old.get(k) != approval.get(k) for k in ('app_id','app_name','account_ref'))
            or len(events) != 9 or events[0].get('approval') != old
            or consent.get('approved_sha256') != sha(paths['approval'].read_bytes())
            or consent.get('source_commit') != old['source_commit']):
        raise ContractError('B02 provenance or original account differs')
    source = dict(commit=old['source_commit'], source_sha256=old['source_snapshot']['sha256'],
                  approval_sha256=sha(paths['approval'].read_bytes()))
    if [e['value'] for e in events if e.get('binding')=='approved_source'] != [source]:
        raise ContractError('B02 execution source is not bound')
    intents = [e for e in events if 'intent' in e]
    receipts = [e for e in events if 'received' in e]
    results = [e['result'] for e in events if 'result' in e]
    if ([e.get('kind') for e in intents] != ['oauth_token','uinfo','list']
            or [e.get('intent') for e in intents] != [1,2,3]
            or [e.get('received') for e in receipts] != [1,2,3]
            or any(e.get('http_status') != 200 for e in receipts)
            or any(e.get('api_errno') != 0 for e in receipts[1:])
            or len(results) != 1):
        raise ContractError('completed bounded B02 observation required')
    result = results[0]
    if (result.get('status') != 'NO_FINAL_FILE_OBSERVED_UNKNOWN_REMAINS'
            or result.get('old_archive_state') != 'unknown' or result.get('unfinished_attempts') != 0
            or result.get('unknown_recovery') != 'NOT_RUN'
            or result.get('physical_attempts') != dict(oauth_token=1,uinfo=1,list=1,filemetas=0,download_meta=0,download=0)):
        raise ContractError('B02 must preserve the protected predecessor unknown')
    times = [events[0]['started']] + [e['at'] for e in intents] + [result['ended_at']]
    parsed = [datetime.fromisoformat(t) for t in times]
    if parsed != sorted(parsed) or not all(datetime.fromisoformat(old['starts_at']) <= t < datetime.fromisoformat(old['expires_at']) for t in parsed):
        raise ContractError('B02 observation falls outside its approved window')
    bound = prior_binding(worktree, old)
    return old, bound


class DistinctArchiveExperiment(BaiduExperiment):
    reference = 'B03'
    content = CONTENT
    first_account = 'must_match_previously_confirmed_uk'

    def __init__(self, registry, approval, **kwargs):
        if approval.get('purpose') != PURPOSE:
            raise ContractError('separate B03 purpose approval required')
        self.old_approval, old = predecessor(registry.runtime.worktree, approval)
        new = approval.get('fixture_binding', {})
        if (sha(CONTENT) == sha(TXT) or new.get('sha256') == old['fixture_binding']['sha256']
                or any(new.get(k) == old['fixture_binding'][k] for k in ('asset_id','entry_id','acceptance_id'))
                or registry.runtime.root == registry.runtime.worktree/self.old_approval['runtime_relative']
                or approval.get('root') == self.old_approval['root']):
            raise ContractError('B03 must use different bytes and identities, never rewrap B01')
        super().__init__(registry, approval, **kwargs)

    def check(self, *, inflight=False):
        observation_files(self.registry.runtime.worktree, self.approval['prior_observation'])
        frozen_files(self.registry.runtime.worktree, self.old_approval['prior_files'])
        return super().check(inflight=inflight)

    def run(self, *, load_credentials, obtain_code):
        # The account was already explicitly confirmed by the operator in B01.
        # The inherited protocol rejects a different live UK before mkdir.
        return super().run(load_credentials=load_credentials, obtain_code=obtain_code,
                           confirm_account=lambda observed: self.approval['account_ref'])


def zero_request_predecessor(worktree, approval):
    """Only the separately approved first retry of a proven pre-HTTP stop."""
    paths = observation_files(worktree, approval.get('retry_of'))
    old = json_object(paths['approval'].read_bytes())
    events = [json_object(line) for line in paths['journal'].read_bytes().splitlines()]
    consent = json_object(paths['consent'].read_bytes())
    if (approval.get('attempt') != 'B03-R1' or old.get('reference') != 'B03'
            or old.get('approved') is not True or 'attempt' in old
            or paths['approval'] != worktree/APPROVAL
            or paths['journal'] != worktree/RUNTIME/'b03-requests/journal.local.jsonl'
            or consent.get('approved_sha256') != sha(paths['approval'].read_bytes())
            or consent.get('source_commit') != old.get('source_commit')):
        raise ContractError('exact original B03 stop and approval provenance required')
    # No new data, identity, target, permissions, budget or window is accepted.
    allowed = {'source_commit','source_snapshot','attempt','retry_of','prior_database'}
    if {k:v for k,v in approval.items() if k not in allowed} != {k:v for k,v in old.items() if k not in allowed}:
        raise ContractError('B03 retry may not expand original scope or window')
    source = dict(commit=old['source_commit'],source_sha256=old['source_snapshot']['sha256'],
                  approval_sha256=sha(paths['approval'].read_bytes()))
    if (len(events) != 4 or events[0].get('approval') != old
            or events[1] != dict(binding='approved_source',value=source)
            or events[2].get('stopped') != 'orchestration' or events[2].get('error_type') != 'ContractError'
            or any('intent' in e or 'received' in e for e in events)):
        raise ContractError('original B03 must have zero durable HTTP intents')
    result = events[3].get('result',{})
    if (result.get('status') != 'STOPPED' or result.get('error_type') != 'ContractError'
            or result.get('physical_attempts') != dict.fromkeys(LIMITS,0)
            or result.get('unfinished_attempts') != 0 or 'archive_result' in result):
        raise ContractError('no retry of a sent, unknown, successful or incomplete experiment')
    proof = approval.get('prior_database',{})
    relative = Path(proof.get('relative_path',''))
    if (set(proof) != {'relative_path','sha256'} or relative.is_absolute() or '..' in relative.parts
            or relative.parts[:1] != ('tmp',)):
        raise ContractError('separate frozen zero-operation database required')
    path = no_symlinks(worktree/relative)
    if path.stat().st_size > 16*1024*1024 or sha(path.read_bytes()) != proof['sha256']:
        raise ContractError('frozen zero-operation database changed')
    for suffix in ('-wal','-journal'):
        sidecar = no_symlinks(str(path)+suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise ContractError('consistent frozen database required')
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1',uri=True)) as db:
        for table in ('archive_operations','archive_attempts','archive_windows','asset_locations'):
            if db.execute('SELECT count(*) FROM '+table).fetchone()[0]:
                raise ContractError('original B03 has archive activity; no startup retry')
        row = db.execute('''SELECT r.entry_id,r.asset_id,r.acceptance_id,a.sha256,a.size_bytes,
            r.status,f.custody_health,f.cleanup_hold FROM staging_receipts r
            JOIN assets a USING(asset_id) JOIN staging_files f USING(asset_id)''').fetchall()
    expected = tuple(old['fixture_binding'][k] for k in ('entry_id','asset_id','acceptance_id','sha256','size_bytes'))
    if row != [expected+('accepted','healthy',1)]:
        raise ContractError('original accepted fixture proof differs')
    return paths


class StartupRetryExperiment(DistinctArchiveExperiment):
    """New persistent attempt; same accepted asset, never delete the old claim."""
    @property
    def request_root(self):
        return self.registry.runtime.root/'b03-r1-requests'

    def __init__(self, registry, approval, **kwargs):
        zero_request_predecessor(registry.runtime.worktree, approval)
        for table in ('archive_operations','archive_attempts','archive_windows','asset_locations'):
            if registry.rows('SELECT 1 FROM '+table+' LIMIT 1'):
                raise ContractError('current asset already has archive activity; no startup retry')
        super().__init__(registry, approval, **kwargs)

    def check(self, *, inflight=False):
        zero_request_predecessor(self.registry.runtime.worktree, self.approval)
        return super().check(inflight=inflight)
