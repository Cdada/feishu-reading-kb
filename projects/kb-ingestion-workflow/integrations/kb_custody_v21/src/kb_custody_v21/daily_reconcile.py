"""Resolve unknown writes from real receipts without inventing a POST receipt."""
from datetime import datetime
import json
from urllib.parse import urlencode

from .contracts import ContractError, stable_key
from .daily_scheduler import MaterialProblem
from .daily_store import Deferred
from .f01 import PREFIX, validated_message_content
from .f05 import file_sha
from .pilot_feishu import read_json


def reconcile_write(transport, operation, saved, request):
    store=transport.store
    root=transport._root(operation,saved['attempts'])
    approval=root/'approval.local.json'
    if not approval.exists():
        raise ContractError('unknown write has no immutable child authorization')
    transport.authority.check()
    if request.get('card'):
        from .daily_card_io import reconcile_card
        return reconcile_card(transport,operation,saved,request)
    if request['mode']=='update':
        from .daily_pdf_progress import reconcile_progress
        return reconcile_progress(transport, operation, saved, request)
    if request['mode']=='upload':
        # Uploading the same PDF may leave an unused remote file, but cannot
        # send a chat message or publish a document. Never reuse an unknown key.
        plan=request['preview']
        if not plan or file_sha(plan['pdf_path'])!=plan['sha256']:
            raise ContractError('unknown upload PDF changed')
        store.resolve_retry(operation,dict(safe_to_retry=True,receipts=[str(approval)],
            reason='upload_has_no_chat_or_publication_effect; unknown_file_key_is_not_reused'))
        return None
    if request['mode']!='send':
        raise ContractError('unsupported unknown write kind')
    first=read_json(transport._root(operation,1)/'approval.local.json')
    started=datetime.fromisoformat(first['starts_at']).timestamp()
    now=store.now()
    if now-started>transport.authority.policy.history_seconds-2:
        raise MaterialProblem('发送记录已超出自动补查范围，需要由 Codex 核查。')
    cursor_key='send-reconciliation:'+operation+':'+str(saved['attempts'])
    cursor=store.get(cursor_key)
    if cursor is None:
        cursor=dict(start=max(0,int(started)-1),end=int(now)+1,token=None,matches=[],receipts=[])
        store.put(cursor_key,cursor)
    body=json.loads(request['body'])
    content=json.loads(body['content'])
    p=transport.authority.policy
    for _ in range(p.pages_per_tick):
        params=dict(container_id_type='chat',container_id=p.chat_id,start_time=str(cursor['start']),
            end_time=str(cursor['end']),page_size='50',sort_type='ByCreateTimeAsc')
        if cursor['token']:
            params['page_token']=cursor['token']
        key='reconcile:'+stable_key(operation,saved['attempts'],cursor['start'],cursor['end'],cursor['token'])
        receipt=transport.request(key,'list',PREFIX+'/im/v1/messages?'+urlencode(sorted(params.items())),confirmation=True)
        data=receipt['value'].get('data',{})
        items,more=data.get('items'),data.get('has_more')
        if not isinstance(items,list) or type(more) is not bool:
            raise ContractError('complete send-reconciliation page required')
        for item in items:
            try:
                validated_message_content(item,app_id=p.app_id,bot_id=p.bot_id,chat_id=p.chat_id,
                    kind=body['msg_type'],name=request['preview']['filename'] if request['preview'] else '',
                    text=content.get('text'))
            except ContractError:
                continue
            try:
                created=int(item['create_time'])/1000
            except (KeyError,TypeError,ValueError):
                raise ContractError('reconciled send requires platform creation time')
            if not cursor['start']<=created<=cursor['end']:
                raise ContractError('reconciled message outside requested range')
            if item['message_id'] not in [m['value']['data']['message_id'] for m in cursor['matches']]:
                cursor['matches'].append(dict(value=dict(code=0,data=item),receipt=receipt['receipt'],
                    evidence=receipt['evidence'],reconciled_from='verified_chat_history'))
        cursor['receipts'].append(receipt)
        if more:
            token=data.get('page_token')
            if not isinstance(token,str) or not token or token==cursor['token'] or len(token)>4096:
                raise ContractError('reconciliation page did not advance')
            cursor['token']=token
            store.put(cursor_key,cursor)
            continue
        for proof in cursor['receipts']:
            transport._checked_result(proof)
        matches=cursor['matches']
        if len(matches)>1:
            raise MaterialProblem('发现多条可能相同的发送结果，需要由 Codex 核查。')
        if matches:
            result=matches[0]
            transport._checked_result(result)
            store.complete(operation,saved['attempts'],result)
            return result
        # uuid deduplication has a documented one-hour bound. Leave headroom
        # for the next physical child; never extend it on restart/retry.
        if now-started>=3000:
            raise MaterialProblem('尚未找到发送结果，已超出去重重发的安全时间，请由 Codex 核查。')
        store.resolve_retry(operation,dict(safe_to_retry=True,
            receipts=[r['receipt'] for r in cursor['receipts']],reason='complete_history_checked; same_uuid_within_one_hour'))
        return None
    raise Deferred('send_reconciliation_pagination')
