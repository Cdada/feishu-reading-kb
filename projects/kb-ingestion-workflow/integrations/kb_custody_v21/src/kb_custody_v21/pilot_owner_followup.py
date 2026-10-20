"""A separately approved owner decision for an already delivered exact preview.

The old session is evidence only. Its stopped journal, request counts and PDF
remain untouched; the new session cannot generate, upload or resend a preview.
"""
from datetime import timedelta
import json
from pathlib import Path

from .contracts import ContractError, stable_key
from .f01 import encoded, identity, sha, validated_message_content
from .f05 import file_sha
from .pilot_feishu import read_json, LIMITS
from .pilot_preview import PilotPreviews
from .runtime import no_symlinks
from .pilot_transport_resolution import completed_download_resolution


def previous_preview(binding):
    """Read immutable successful delivery receipts without reviving old consent."""
    required = {'root', 'approval_sha256', 'publication_id', 'message_id', 'pdf_sha256'}
    if not isinstance(binding, dict) or set(binding) not in (required, required | {'resolved_download'}):
        raise ContractError('exact previous preview binding required')
    root = no_symlinks(Path(binding['root']).absolute())
    if str(root) != binding['root'] or root.name != 'p11-requests' or '.dev-runtime' not in root.parts:
        raise ContractError('exact private previous P11 root required')
    identity(binding['message_id'], 'om_')
    files = {}
    def read(path, limit=4*1024*1024):
        path = no_symlinks(path)
        with path.open('rb') as stream: raw = stream.read(limit+1)
        if len(raw) > limit: raise ContractError('previous evidence exceeds bound')
        files[str(path)] = sha(raw)
        return raw
    try:
        approval = json.loads(read(root/'approval.local.json', 65536))
        events = [json.loads(line) for line in read(root/'journal.local.jsonl').splitlines()]
        if (files[str(root/'approval.local.json')] != binding['approval_sha256']
                or approval.get('approved') is not True or approval.get('reference') != 'P11'
                or approval.get('root') != str(root) or events[0] != {'approval': approval}):
            raise ContractError('previous approved journal identity changed')
        intents = [e['intent'] for e in events if 'intent' in e]
        completed = [e['complete'] for e in events if 'complete' in e]
        stops = [e for e in events if 'stopped' in e]
        if ('resolved_download' not in binding and (len(set(intents)) != len(intents) or sorted(intents) != sorted(completed)
                or stops not in ([], [{'stopped':'operator_entry', 'error_type':'ContractError'}]))):
            raise ContractError('unfinished or failed previous transport cannot be followed up')
        manifest_path = no_symlinks(Path(approval['source_manifest']).absolute())
        manifest = json.loads(read(manifest_path))
        executed = json.loads(read(root/'executed-source.local.json'))
        snapshot = manifest['source_snapshot']
        if (files[str(manifest_path)] != approval['source_manifest_sha256'] or manifest != executed
                or not snapshot['files']
                or snapshot['sha256'] != sha(json.dumps(snapshot['files'],sort_keys=True).encode())):
            raise ContractError('previous executed source differs from its approved manifest')
        http = [json.loads(line) for line in read(root/'http.local.jsonl',65536).splitlines()]
        starts, ends = {}, {}
        for e in http:
            key, kind = e.get('key'), e.get('kind')
            if not isinstance(key,str) or kind not in LIMITS:
                raise ContractError('invalid previous physical request')
            if e.get('type') == 'intent' and key not in starts:
                starts[key] = kind
            elif (e.get('type') == 'complete' and key not in ends and starts.get(key) == kind
                    and e.get('http_status') == 200):
                ends[key] = kind
            else: raise ContractError('previous request is unknown or unsuccessful')
        if (starts != ends or len(http)>200 or any(list(starts.values()).count(k)>approval['limits'][k] for k in LIMITS)):
            raise ContractError('previous physical ledger is incomplete or exceeds approval')
        resolution = None
        if 'resolved_download' in binding:
            resolution = completed_download_resolution(approval,events,http,binding['resolved_download'],root,read)
        receipts = {}
        prefix = 'preview:'+binding['publication_id']+':'
        for kind in ('upload','send','download'):
            ops = [e for e in events if e.get('operation') == prefix+kind]
            if len(ops) != 1 or ops[0].get('kind') != kind:
                raise ContractError('one complete previous preview delivery required')
            op = ops[0]; key = op['intent']
            rows = [e for e in http if e.get('type')=='complete' and e.get('key')==key+':'+kind]
            raw = read(root/(key+'.response.local'),32*1024*1024)
            if len(rows)!=1 or rows[0].get('response_sha256')!=sha(raw):
                raise ContractError('previous physical receipt changed')
            if kind == 'download':
                download = op['download']
                if (sha(raw)!=binding['pdf_sha256'] or len(raw)!=download['size_bytes']
                        or download['sha256']!=binding['pdf_sha256'] or download['message_id']!=binding['message_id']):
                    raise ContractError('previous downloaded PDF differs')
                value = dict(sha256=sha(raw),size_bytes=len(raw))
            else:
                value = json.loads(raw)
                if type(value.get('code')) is not int or value['code']!=0:
                    raise ContractError('previous API receipt failed')
            receipts[kind] = value
        sent = receipts['send']['data']
        validated_message_content(sent,app_id=approval['app_id'],bot_id=approval['bot_id'],
            chat_id=approval['chat_id'],kind='file',name='preview-'+binding['publication_id']+'.pdf')
        if sent['message_id'] != binding['message_id']:
            raise ContractError('previous delivered message differs')
        if any(file_sha(Path(p))!=h for p,h in files.items()):
            raise ContractError('previous evidence changed while reading')
        return dict(files=files,receipts=receipts,approval=approval,resolved_download=resolution)
    except (OSError,KeyError,ValueError,TypeError,IndexError) as error:
        raise ContractError('previous preview evidence is incomplete or invalid') from error


def checked_previous(session, previews, binding):
    proof = previous_preview(binding)
    source = session.binding()
    if (str(session.root)==binding['root'] or session.approval_sha==binding['approval_sha256']
            or any(proof['approval'][k]!=session.approval[k] for k in ('app_id','chat_id','account_id','bot_id'))
            or any(source.get(p)!=h for p,h in proof['files'].items())):
        raise ContractError('new approval must freeze the exact previous delivery and same objects')
    row = previews.publisher._row(binding['publication_id'])
    deliveries = previews.registry.rows('SELECT * FROM pilot_preview_deliveries WHERE publication_id=?',
        (binding['publication_id'],))
    if (row['preview_sha256']!=binding['pdf_sha256'] or len(deliveries)!=1
            or deliveries[0]['message_id']!=binding['message_id']
            or deliveries[0]['pdf_sha256']!=binding['pdf_sha256']):
        raise ContractError('bound previous preview differs from the candidate')
    return proof


def owner_followup(session, intake, plan_path):
    """One complete page, one unambiguous direct reply, existing publication."""
    from .pilot_decision import apply_owner_reply, reply_text, observed_reply
    from .pilot_notice_workflow import PilotNoticeWorkflow
    path = no_symlinks(Path(plan_path).absolute())
    if session.binding().get(str(path)) != file_sha(path):
        raise ContractError('owner follow-up plan must be frozen')
    plan = read_json(path,65536)
    if (set(plan)!={'reference','previous_preview','lookback_seconds'}
            or plan['reference'] not in {'P11-owner-followup-v1','P11-owner-followup-v2'}
            or type(plan['lookback_seconds']) is not int or not 1<=plan['lookback_seconds']<=86400
            or session.limits != dict(auth=6,identity=3,list=1,message=1,download=0,upload=0,send=1)):
        raise ContractError('exact owner-only plan and twelve-request budget required')
    binding = plan['previous_preview']
    previews = PilotPreviews(intake.registry,intake.scope)
    checked_previous(session,previews,binding)
    pid = binding['publication_id']
    with previews.registry.transaction() as db:
        previews._candidate(db,pid);previews._current(db,pid)
    row = previews.publisher._row(pid)
    if row['state']!='prepared' or previews.registry.rows('SELECT * FROM pilot_preview_decisions WHERE publication_id=?',(pid,)):
        raise ContractError('follow-up requires an undecided prepared candidate')
    previews.publisher._tree(row,pid+'.candidate')
    entry = intake.registry.entry(row['entry_id'])
    original = intake.runtime.checked(intake.runtime.path(entry['relative_path']))
    if session.binding().get(str(original))!=entry['sha256']:
        raise ContractError('follow-up original must be frozen')
    session.verify_isolation()
    sid = 'scan_'+stable_key('owner-followup',session.approval_sha)
    end = session.clock()
    result = session.scan(sid,start=end-timedelta(seconds=plan['lookback_seconds']),end=end,max_pages=1)
    if result['status']!='COMPLETE_INTERVAL':
        raise ContractError('complete bounded reply interval required')
    observe = plan['reference']=='P11-owner-followup-v2'
    choices = []
    for receipt in result['receipts']:
        for item in read_json(Path(receipt))['data']['items']:
            if item['message_id'] not in result['message_ids'] or item.get('parent_id')!=binding['message_id']:
                continue
            _,text = observed_reply(item,session.approval['bot_id'])[:2] if observe else reply_text(item)
            if text in previews.COMMANDS: choices.append(item)
    if len(choices)!=1:
        raise ContractError('one unambiguous supported direct owner reply required')
    value = apply_owner_reply(session,previews,sid,choices[0]['message_id'],prior_delivery=binding,
        observed=choices[0] if observe else None)
    if value['status']=='published':
        value['notification'] = PilotNoticeWorkflow(session,intake).publication(pid)
    return value
