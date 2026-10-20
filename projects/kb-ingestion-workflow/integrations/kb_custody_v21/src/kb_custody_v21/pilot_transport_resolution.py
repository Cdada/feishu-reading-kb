"""Read-only resolution of one successful inbox download rejected locally.

The old ledger is never amended. Only an explicit new frozen binding can select
this exception; unfinished HTTP, writes, and other stopped operations still fail.
"""
import json
import re
from pathlib import Path

from .contracts import ContractError
from .f01 import PREFIX, identity, sha


def completed_download_resolution(approval, events, http, resolution, root, read):
    fields={'reference','key','sha256','size_bytes'}
    if (not isinstance(resolution,dict) or set(resolution)!=fields
            or resolution['reference']!='P24-complete-download-resolution-v1'
            or approval.get('operation')!='inbox-service'
            or type(resolution['size_bytes']) is not int):
        raise ContractError('explicit complete-download resolution required')
    key=resolution['key']
    if not isinstance(key,str) or not re.fullmatch(r'p[0-9]{3}-download',key):
        raise ContractError('one saved download request key required')
    intents=[e['intent'] for e in events if 'intent' in e]
    completed=[e['complete'] for e in events if 'complete' in e]
    expected_stops=[dict(stopped=key,error_type='ContractError'),dict(stopped='operator_entry',error_type='ContractError')]
    if (not intents or intents[-1]!=key or len(set(intents))!=len(intents)
            or len(set(completed))!=len(completed) or sorted(completed)!=sorted(intents[:-1])
            or [e for e in events if 'stopped' in e]!=expected_stops):
        raise ContractError('only one terminal local download rejection can be resolved')
    op=[e for e in events if e.get('intent')==key][0]
    d=op.get('download',{});policy=approval.get('inbox',{})
    if (op.get('kind')!='download' or op.get('mode')!='read' or 'preview' in op or 'notice' in op
            or set(d)!={'reference','message_id','file_key','filename','max_asset_bytes'}
            or d['reference']!='P19-inbox-download-v1'
            or policy.get('reference')!='P23-service-inbox-v1'
            or d['max_asset_bytes']!=policy.get('max_asset_bytes')
            or not 0<resolution['size_bytes']<=d['max_asset_bytes']
            or Path(d['filename']).suffix.lower() not in policy.get('formats',[])):
        raise ContractError('resolution must retain the original inbox scope')
    mid=identity(d['message_id'],'om_');file_key=identity(d['file_key'],'file_')
    if (op.get('operation')!='inbox-download:'+mid+':'+file_key
            or op.get('path')!=PREFIX+'/im/v1/messages/'+mid+'/resources/'+file_key+'?type=file'):
        raise ContractError('resolution download identity differs')
    def receipt(request_key,kind):
        rows=[e for e in http if e.get('type')=='complete' and e.get('key')==request_key+':'+kind]
        raw=read(root/(request_key+'.response.local'),32*1024*1024)
        if len(rows)!=1 or rows[0].get('http_status')!=200 or rows[0].get('response_sha256')!=sha(raw):
            raise ContractError('resolution requires exact successful physical receipts')
        return raw
    raw=receipt(key,'download');cli=read(root/(key+'.cli.local'),32*1024*1024)
    if raw!=cli or sha(raw)!=resolution['sha256'] or len(raw)!=resolution['size_bytes']:
        raise ContractError('resolved saved download bytes differ')
    details=[e for e in events if str(e.get('operation','')).endswith(':message:'+mid)]
    if len(details)!=1 or details[0]['kind']!='message':
        raise ContractError('one original message receipt required for resolution')
    detail=json.loads(receipt(details[0]['intent'],'message'))
    items=detail.get('data',{}).get('items',[]);message=items[0] if len(items)==1 else {}
    sender=message.get('sender',{})
    if (type(detail.get('code')) is not int or detail['code']!=0 or message.get('message_id')!=mid
            or message.get('chat_id')!=approval['chat_id'] or message.get('msg_type')!='file'
            or message.get('deleted') is not False or message.get('updated',False) is not False
            or sender!={**sender,'id':approval['account_id'],'id_type':'open_id','sender_type':'user'}):
        raise ContractError('resolved original message identity differs')
    content=json.loads(message['body']['content'])
    if content.get('file_key')!=file_key or content.get('file_name')!=d['filename']:
        raise ContractError('resolved original metadata differs')
    claims=[e['value'] for e in events if str(e.get('binding','')).startswith('inbox-claim:')
            and e['value'].get('message_id')==mid]
    if (len(claims)!=1 or claims[0].get('file_key')!=file_key or claims[0].get('filename')!=d['filename']
            or any(str(e.get('binding','')).startswith('inbox-accepted:') and e['value'].get('message_id')==mid for e in events)):
        raise ContractError('one unresolved original claim required')
    return dict(claims[0],path=str(root/(key+'.response.local')),sha256=sha(raw),size_bytes=len(raw))
