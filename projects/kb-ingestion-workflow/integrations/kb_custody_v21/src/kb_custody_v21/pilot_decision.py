"""Verified owner replies to the exact byte-checked Pilot preview.

Only an existing bounded list/detail read supplies a decision. No latest-item
guess, thread-root fallback, model output, automatic send or inferred approval.
"""
import json
import re
from pathlib import Path

from .contracts import ContractError
from .f01 import encoded, identity, sha
from .f05 import file_sha


def reply_text(item, *, observe_update=False):
    """Parse the raw platform text envelope, refusing edits and ambiguous JSON."""
    if (not isinstance(item,dict) or item.get('msg_type')!='text'
            or item.get('deleted') is not False or type(item.get('updated')) is not bool
            or item['updated'] and not observe_update):
        raise ContractError('text reply without deletion or update flags required')
    parent=identity(item.get('parent_id'),'om_')
    body=item.get('body')
    raw=body.get('content') if isinstance(body,dict) else None
    if not isinstance(raw,str) or len(raw)>4096:
        raise ContractError('bounded raw reply content required')
    def unique(pairs):
        value={}
        for key,item in pairs:
            if key in value: raise ValueError('duplicate key')
            value[key]=item
        return value
    try:
        if len(raw.encode('utf-8'))>4096: raise ValueError('oversized content')
        content=json.loads(raw,object_pairs_hook=unique)
    except (ValueError,UnicodeError,RecursionError) as error:
        raise ContractError('invalid bounded reply JSON') from error
    if not isinstance(content,dict) or set(content)!={'text'} or not isinstance(content['text'],str):
        raise ContractError('one exact text decision required')
    return parent,content['text']


def observed_reply(item, bot_id):
    """Compare current owner intent; an update flag does not prove edit history.

    Only a platform-resolved leading mention of this exact bot is formatting.
    All decision text remains exact. Callers must match this version in the
    independently read detail and preserve both original physical receipts.
    """
    identity(bot_id,'ou_')
    parent,text=reply_text(item,observe_update=True)
    mentions=item.get('mentions',[])
    if not isinstance(mentions,list) or len(mentions)>1:
        raise ContractError('at most one exact leading bot mention required')
    canonical=[]
    if mentions:
        mention=mentions[0]
        if (not isinstance(mention,dict) or mention.get('id')!=bot_id
                or mention.get('id_type')!='open_id' or not isinstance(mention.get('key'),str)
                or not re.fullmatch(r'@_user_[0-9]+',mention['key'])
                or not text.startswith(mention['key']+' ')):
            raise ContractError('mention must resolve to the approved bot and lead the command')
        text=text[len(mention['key'])+1:]
        canonical=[{k:mention[k] for k in ('key','id','id_type')}]
    if '@' in text:
        raise ContractError('unresolved mention in owner command')
    if item['updated']:
        times=[item.get(k) for k in ('create_time','update_time')]
        if (any(not isinstance(t,str) or not re.fullmatch(r'[0-9]{1,16}',t) for t in times)
                or int(times[1])<int(times[0])):
            raise ContractError('updated observation requires valid version timestamps')
    sender=item.get('sender',{})
    version={k:item.get(k) for k in ('message_id','chat_id','parent_id','root_id','msg_type',
        'deleted','updated','create_time','update_time','body')}
    version.update(sender={k:sender.get(k) for k in ('id','id_type','sender_type')},mentions=canonical)
    return parent,text,version


def apply_owner_reply(session,previews,scan_id,message_id,*,prior_delivery=None,observed=None):
    """Persist checked reply provenance with its decision, then resume publication.

    Cached successful reads are reused. Final source/window/receipt checks run
    inside the decision transaction and immediately before publication writes.
    A saved confirmation or publishing intent remains recoverable after a stop.
    """
    session.check()
    a=session.approval
    if (previews.scope.app_id,previews.scope.chat_id,previews.scope.user_id)!=(a['app_id'],a['chat_id'],a['account_id']):
        raise ContractError('decision scope differs from approved objects')
    if session.value('isolation_verified') is not True:
        raise ContractError('verify the fixed group before reading owner decisions')
    prior = None
    if prior_delivery is not None:
        from .pilot_owner_followup import checked_previous
        prior = checked_previous(session,previews,prior_delivery)
    item=session.message(scan_id,message_id)
    observation=None
    if observed is None:
        parent,text=reply_text(item)
    else:
        parent,text,version=observed_reply(item,a['bot_id'])
        if observed_reply(observed,a['bot_id'])[2]!=version:
            raise ContractError('owner reply changed between list and detail')
        observation=dict(version_sha256=sha(encoded(version).encode()),updated=item['updated'],
            update_cause='UNDETERMINED' if item['updated'] else 'NOT_FLAGGED',edit_history='NOT_VERIFIED',
            leading_bot_mention=bool(version['mentions']),normalized_command=text)
    if prior_delivery is not None and parent != prior_delivery['message_id']:
        raise ContractError('reply must address the separately approved previous preview')
    if text not in previews.COMMANDS:
        raise ContractError('reply with an explicit supported decision to this preview')
    rows=previews.registry.rows('SELECT * FROM pilot_preview_deliveries WHERE app_id=? AND chat_id=? AND message_id=?',
        (a['app_id'],a['chat_id'],parent))
    if len(rows)!=1:
        raise ContractError('direct reply must identify a byte-checked preview')
    delivery=rows[0];pid=delivery['publication_id']
    row=previews.publisher._row(pid)
    entry=previews.registry.entry(row['entry_id'])
    original=previews.registry.runtime.checked(previews.registry.runtime.path(entry['relative_path']))
    source=session.binding()
    if source.get(str(original))!=entry['sha256']:
        raise ContractError('decision original must be a frozen approved input')

    # Revalidate the saved physical receipts, not a caller-created preview row.
    frozen=dict(session.value(scan_id+':result')['receipts'])
    if observed is not None:
        matches=[row for p in frozen for row in json.loads(Path(p).read_bytes()).get('data',{}).get('items',[])
                 if row.get('message_id')==message_id]
        if matches!=[observed]:
            raise ContractError('observed reply must match one frozen physical list item')
    def receipt(operation,kind):
        events=[e for e in session.events if e.get('operation')==operation]
        if len(events)!=1 or events[0]['kind']!=kind:
            raise ContractError('exact successful operation provenance required')
        event=events[0]
        value=session._receipt(event['intent'],kind,event.get('download'))
        path=session.root/(event['intent']+'.response.local')
        frozen[str(path)]=file_sha(path)
        return value
    raw=receipt(scan_id+':message:'+message_id,'message')
    if raw.get('data',{}).get('items')!=[item]:
        raise ContractError('reply differs from its physical receipt')
    prefix='preview:'+pid+':'
    if prior is None:
        receipt(prefix+'upload','upload')
        sent=receipt(prefix+'send','send')
        downloaded=receipt(prefix+'download','download')
    else:
        frozen.update(prior['files'])
        sent=prior['receipts']['send']
        downloaded=prior['receipts']['download']
    if (sent.get('data',{}).get('message_id')!=parent
            or downloaded['sha256']!=delivery['pdf_sha256']
            or delivery['receipt_ref']!=downloaded['sha256']
            or row['preview_sha256']!=delivery['pdf_sha256']):
        raise ContractError('reply preview delivery evidence differs')
    evidence=dict(reference=session.reference+'-owner-reply-v1',approval_sha256=session.approval_sha,
        scan_id=scan_id,message_id=message_id,reply_to=parent,pdf_sha256=delivery['pdf_sha256'],
        source_binding_sha256=sha(encoded(source).encode()),
        receipts=frozen)
    if observation is not None:evidence['reply_observation']=observation
    # Shared journals may grow on a later invocation. Freeze their current
    # snapshots for this transaction without changing the saved decision proof.
    frozen=dict(frozen,**{str(session.path):file_sha(session.path),
        str(session.root/'http.local.jsonl'):file_sha(session.root/'http.local.jsonl')})
    def checked_access():
        session.check()
        if session.binding()!=source or any(file_sha(Path(p))!=h for p,h in frozen.items()):
            raise ContractError('decision source or receipt evidence changed')
        session.binding()  # Receipt hashing can also cross the approved deadline.
    checked_access()
    return previews.decide(app_id=a['app_id'],chat_id=a['chat_id'],user_id=a['account_id'],
        message_id=message_id,reply_to=parent,text=text,checked_access=checked_access,evidence=evidence)
