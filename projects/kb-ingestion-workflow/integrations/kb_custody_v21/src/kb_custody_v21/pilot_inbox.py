"""Prospective owner-file custody under an explicit finite parent permission.

No sample file is invented or retroactively added to the approval. Actual bytes
are bound to the original platform message and durable successful HTTP receipt.
This module does not grant model calls, merge old libraries or publish anything.
"""
from contextlib import contextmanager
from datetime import timedelta
import hashlib
import json
from pathlib import Path

from .contracts import ContractError, stable_key
from .f01 import PREFIX, identity
from .f05 import file_sha
from .pilot_feishu import FILE_LIMIT
from .runtime import no_symlinks


class _ReceivedBytes:
    """Hash exactly what custody reads, before its acceptance transaction."""
    def __init__(self, stream):
        self.stream, self.digest, self.size = stream, hashlib.sha256(), 0

    def read(self, count):
        data = self.stream.read(count)
        self.digest.update(data); self.size += len(data)
        return data


def inbox_policy(session):
    a = session.approval
    p = a.get('inbox')
    daily = a.get('operation') == 'inbox-daily'
    service = a.get('operation') == 'inbox-service'
    if (session.reference != 'P11' or a.get('operation') not in {'receive-inbox','inbox-daily','inbox-service'}
            or not isinstance(p, dict) or set(p) != {'reference', 'formats', 'max_asset_bytes', 'max_files'}
            or p['reference'] != ('P23-service-inbox-v1' if service else 'P22-daily-inbox-v1' if daily else 'P19-inbox-v1')
            or not isinstance(p['formats'], list) or not p['formats']
            or any(not isinstance(x, str) or x not in {'.md', '.txt', '.pdf', '.webarchive', '.zip', '.docx', '.xlsx'} for x in p['formats'])
            or len(set(p['formats'])) != len(p['formats'])
            or type(p['max_asset_bytes']) is not int or not 0 < p['max_asset_bytes'] <= FILE_LIMIT
            or type(p['max_files']) is not int or not 1 <= p['max_files'] <= 6
            or daily and p['max_files'] != 1
            or service and p['max_files'] > 3
            or a['limits']['download'] != p['max_files'] * (2 if service else 1) + int(daily)):
        raise ContractError('explicit P19 finite inbox permission required')
    return p


def receive_new_file(session, scan_id, message_id, intake):
    """Receive one native file message; callers must separately control scans.

    A complete one-page result is required. Multiple native messages remain
    separate documents; a post or an inferred attachment group is not admitted.
    """
    s = session; s.check(); p = inbox_policy(s); a = s.approval
    if (intake.scope.app_id, intake.scope.chat_id, intake.scope.user_id) != (a['app_id'], a['chat_id'], a['account_id']):
        raise ContractError('inbox runtime differs from the approved scope')
    if p['max_asset_bytes'] > intake.runtime.config['max_asset_bytes']:
        raise ContractError('inbox file limit exceeds local custody capacity')
    scan = s.value(scan_id+':result')
    if (not scan or scan['status'] != 'COMPLETE_INTERVAL' or len(scan['pages']) != 1
            or scan['interval']['max_pages'] != 1 or message_id not in scan['message_ids']):
        raise ContractError('complete single-page inbox observation required')
    m = s.message(scan_id, message_id)
    if m.get('msg_type') != 'file' or m.get('deleted') is not False or m.get('updated', False) is not False:
        raise ContractError('an available unedited native file message is required')
    raw = m.get('body', {}).get('content')
    if not isinstance(raw, str) or len(raw.encode()) > 65536:
        raise ContractError('bounded native file content required')
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeError) as error:
        raise ContractError('invalid native file JSON') from error
    if not isinstance(body, dict):
        raise ContractError('native file object required')
    name = body.get('file_name')
    if (not isinstance(name, str) or not name or len(name.encode()) > 1024
            or any(ord(x) < 32 or ord(x) == 127 or x in '/\\' for x in name)
            or Path(name).suffix.lower() not in p['formats']):
        raise ContractError('filename or format outside approved inbox scope')
    key = identity(body.get('file_key'), 'file_')
    origins = dict(scan['receipts'])
    origin = [e for e in s.events if e.get('operation') == scan_id+':message:'+message_id]
    if len(origin) != 1:
        raise ContractError('exact message receipt missing')
    receipt = s.root/(origin[0]['intent']+'.response.local')
    origins[str(receipt)] = file_sha(receipt)
    download = dict(reference='P19-inbox-download-v1', message_id=message_id, file_key=key,
                    filename=name, max_asset_bytes=p['max_asset_bytes'])
    envelope = dict(appId=a['app_id'], chatId=a['chat_id'], senderId=a['account_id'], chatType='group',
                    messageId=message_id, resources=[dict(type='file', fileKey=key, fileName=name)])
    # Queue capacity and the exact message relationship are fixed before transport.
    claim_key = 'inbox-claim:'+stable_key(message_id, key)
    old = s.value(claim_key)
    claims = [e for e in s.events if str(e.get('binding', '')).startswith('inbox-claim:')]
    if old is None and len(claims) >= p['max_files']:
        raise ContractError('inbox material count exhausted')
    message_key, _, contracts = intake._contracts(envelope)
    previous = intake.registry.rows('SELECT envelope_json FROM pilot_intake_messages WHERE message_key=?', (message_key,))
    if previous:
        saved = json.loads(previous[0]['envelope_json'])
        prior = saved['contracts']
        if (saved.get('app_id') != a['app_id'] or saved.get('user_id') != a['account_id'] or len(prior) != 1
                or {k:v for k,v in prior[0].items() if k != 'expected'} != contracts[0]):
            raise ContractError('existing message/file relationship differs; retain original records')
        jobs = intake.registry.rows('SELECT entry_id FROM pilot_conversion_jobs WHERE message_key=?', (message_key,))
        if len(jobs) != 1:
            raise ContractError('previous attachment registration is incomplete; inspect before continuation')
        existing = intake.registry.entry(jobs[0]['entry_id'])
        permission = intake.registry.rows('SELECT payload_json FROM custody_events WHERE dedupe_key=?',
            ('pilot-inbox-custody:'+existing['entry_id'],))
        if existing['status'] != 'accepted' and (not permission or
                json.loads(permission[0]['payload_json'])['parent_approval_sha256'] != s.approval_sha):
            raise ContractError('previous incomplete custody cannot acquire another download permission')
        if existing['status'] == 'accepted' and not 0 < existing['size_bytes'] <= p['max_asset_bytes']:
            raise ContractError('previous original exceeds the current inbox scope')
        # Retain the old immutable expected bytes when this exact message was
        # already accepted by the preselected-sample route.
        expected = prior[0].get('expected', {})
        for field, target in (('size_bytes','sizeBytes'),('sha256','sha256')):
            if field in expected: envelope['resources'][0][target] = expected[field]
    entries = intake.prepare(envelope); eid = entries[0]['entry_id']
    claim = dict(entry_id=eid, message_id=message_id, file_key=key, filename=name)
    with intake.registry.transaction() as db:
        custody_key = 'pilot-inbox-custody:'+eid
        if not db.execute('SELECT 1 FROM custody_events WHERE dedupe_key=?', (custody_key,)).fetchone():
            intake.registry.event(db, 'pilot_inbox_custody', eid, custody_key,
                                  dict(parent_approval_sha256=s.approval_sha, **claim))
    s.bind(claim_key, claim)
    observed = None

    def checked_origin():
        s.check()
        if any(file_sha(Path(path)) != digest for path, digest in origins.items()):
            raise ContractError('inbox message evidence changed')

    @contextmanager
    def source():
        nonlocal observed
        observed, _ = s._call('download', PREFIX+'/im/v1/messages/'+message_id+'/resources/'+key+'?type=file',
            operation='inbox-download:'+message_id+':'+key, download=download, frozen_extra=origins)
        path = no_symlinks(Path(observed['path']))
        with path.open('rb') as stream:
            received = _ReceivedBytes(stream)
            yield received
        checked_origin()
        if (received.digest.hexdigest() != observed['sha256'] or received.size != observed['size_bytes']
                or file_sha(path) != observed['sha256'] or path.stat().st_size != observed['size_bytes']):
            raise ContractError('download evidence changed during original custody')

    checked_origin()
    result = intake.staging.ingest(eid, source)
    row = intake.registry.entry(eid)
    checked_origin()
    if observed is not None and (row['sha256'], row['size_bytes']) != (observed['sha256'], observed['size_bytes']):
        intake.registry.health(eid, 'suspect', 'inbox transport/custody mismatch')
        raise ContractError('accepted original differs from transport; acceptance retained')
    binding = dict(claim, sha256=row['sha256'], size_bytes=row['size_bytes'])
    s.bind('inbox-accepted:'+stable_key(message_id, key), binding)
    return dict(receipt=result, status=intake.status(eid), original=binding,
                historical_completeness=scan['status'], permanent_backfill_verified=False)


def receive_inbox(session, intake, plan_path, *, resources=None):
    """Explicit one-scan operator route; model and publication are separate."""
    from .pilot_feishu import read_json
    from .pilot_resources import PilotResources
    s = session; s.check(); p = inbox_policy(s)
    path = no_symlinks(Path(plan_path).absolute())
    if str(path) not in s.binding():
        raise ContractError('approved inbox scan plan required')
    plan = read_json(path, 65536)
    legacy = plan.get('reference') == 'P20-receive-inbox-v2'
    fields = {'reference','lookback_seconds'} | ({'legacy_catalog'} if legacy else set())
    if (set(plan) != fields or plan['reference'] not in {'P19-receive-inbox-v1','P20-receive-inbox-v2'}
            or type(plan['lookback_seconds']) is not int or not 1 <= plan['lookback_seconds'] <= 86400):
        raise ContractError('one finite inbox scan plan required')
    if legacy:
        from .pilot_existing import validate_catalog
        validate_catalog(plan['legacy_catalog'])
    started = 'inbox-run:'+s.approval_sha
    if s.value(started) is not None:
        raise ContractError('inbox entry already consumed; inspect saved originals without restarting')
    s.bind(started, dict(plan_sha256=file_sha(path), at=s.clock().isoformat()))
    gate = resources if resources is not None else PilotResources(intake.registry,
        minimum_free_bytes=max(5 * 1024**3,intake.conversion.settings.minimum_free_bytes))
    capacity = gate.check()
    if capacity['state'] != 'resources_ready':
        return dict(capacity, intake_active=False, accepted=[])
    s.verify_isolation()
    end = s.clock(); scan_id = 'scan_inbox_'+s.approval_sha[:24]
    scan = s.scan(scan_id, start=end-timedelta(seconds=plan['lookback_seconds']), end=end, max_pages=1)
    if scan['status'] != 'COMPLETE_INTERVAL':
        return dict(state='incomplete_scan', intake_active=False, accepted=[], historical_completeness=scan['status'])
    candidates = []
    # Choose only native file messages from the physically recorded page. Text
    # and bot replies do not spend a material slot; unsupported file types stop
    # at the normal explicit validation boundary without downloading them.
    for receipt,digest in scan['receipts'].items():
        if file_sha(Path(receipt)) != digest: raise ContractError('inbox list changed')
        for message in read_json(Path(receipt))['data']['items']:
            if message['message_id'] in scan['message_ids'] and message.get('msg_type') == 'file':
                mid = message['message_id']
                if mid not in candidates: candidates.append(mid)
    if len(candidates) > p['max_files']:
        return dict(state='batch_limit_exceeded', intake_active=False, accepted=[],
                    observed_files=len(candidates), maximum_files=p['max_files'], historical_completeness=scan['status'])
    accepted = []
    for mid in candidates:
        capacity = gate.check()
        if capacity['state'] != 'resources_ready':
            return dict(capacity, intake_active=False, accepted=accepted, remaining_observed=len(candidates)-len(accepted))
        received = receive_new_file(s,scan_id,mid,intake)
        if legacy:
            from .pilot_existing import lookup_existing
            received['existing'] = lookup_existing(received['original']['filename'],received['original']['sha256'],plan['legacy_catalog'])
            received['next_action'] = 'review_existing' if received['existing']['requires_review'] else 'prepare_conversion'
        else:
            received['next_action'] = 'check_existing_catalog'
        accepted.append(received)
    s.check()
    return dict(state='intake_finished', intake_active=False, accepted=accepted,
                historical_completeness=scan['status'], permanent_backfill_verified=False,
                conversion_started=False, model_started=False, published=False)
