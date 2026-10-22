"""Locate the already-owned Feishu attachment without uploading another copy."""
import json
import re
from urllib.parse import parse_qs, urlencode, urlsplit

from .contracts import ContractError
from .f01 import PREFIX


def position(value):
    if type(value) is int and 0<=value<10**20:
        return str(value)
    if isinstance(value,str) and re.fullmatch(r'[0-9]{1,20}',value):
        return value
    return None


def message_link(item, chat_id):
    # Same fields and mobile/desktop query keys as the pinned official CLI's
    # assembleMessageAppLink. Never substitute an opaque message ID for a position.
    if item.get('chat_id')!=chat_id:
        raise ContractError('原件消息不属于当前授权会话。')
    thread=item.get('thread_id')
    thread_pos=position(item.get('thread_message_position'))
    if isinstance(thread,str) and re.fullmatch(r'omt_[A-Za-z0-9_-]+',thread) and thread_pos is not None:
        params=dict(openthreadid=thread,openchatid=chat_id,open_thread_id=thread,
                    open_chat_id=chat_id,thread_position=thread_pos)
        return 'https://applink.feishu.cn/client/thread/open?'+urlencode(params)
    pos=position(item.get('message_position'))
    if pos is not None:
        return 'https://applink.feishu.cn/client/chat/open?'+urlencode(dict(openChatId=chat_id,position=pos))
    # Only an API-provided opaque message link is accepted, never body text.
    raw=item.get('message_app_link')
    if isinstance(raw,str) and len(raw)<=4096 and not any(ord(c)<=32 or ord(c)==127 for c in raw):
        try:
            url=urlsplit(raw);query=parse_qs(url.query,strict_parsing=True)
        except ValueError:
            return None
        if (url.scheme=='https' and url.netloc=='applink.feishu.cn' and not url.fragment
                and url.path=='/client/message/link/open' and set(query)=={'token'}
                and len(query['token'])==1 and query['token'][0]):
            return raw
    return None


def render(backend, sources, job):
    hits=job['result']['hits']
    sources.verify(hits)
    unique={}
    for hit in hits:
        unique.setdefault(hit['material_id'],hit)
    if not 1<=len(unique)<=8:
        raise ContractError('请从最近一次结果中选择 1–8 份原件。')
    policy=backend.authority.policy
    lines=['已入库资料的原件入口：']
    for number,hit in enumerate(unique.values(),1):
        mid=hit['document_id'];document=backend.store.document(mid)
        if document['source_sha']!=hit['source_sha256']:
            raise ContractError('原件保管记录与所选资料不一致。')
        _,observed=backend.collector.observed(mid)
        response=backend.transport.request('qa-original:'+job['id']+':'+mid,'message',
            PREFIX+'/im/v1/messages/'+mid,evidence=observed['evidence'],confirmation=True)
        items=response['value'].get('data',{}).get('items',[])
        if len(items)!=1 or items[0].get('message_id')!=mid:
            raise ContractError('原件消息回执不完整。')
        item=items[0];sender=item.get('sender',{})
        if (item.get('chat_id')!=policy.chat_id
                or (sender.get('id'),sender.get('id_type'),sender.get('sender_type'))!=(policy.account_id,'open_id','user')):
            raise ContractError('原件消息的会话或发送人发生变化。')
        title=f"{number}. {hit['title']}"
        if item.get('deleted') is not False:
            lines.append(title+'\n原飞书消息已不可用；本地原件仍需通过 Codex 取阅。')
            continue
        try:body=json.loads(item.get('body',{}).get('content','{}'))
        except (TypeError,ValueError) as error:raise ContractError('原件消息格式无效。') from error
        resource=document['data']['resource']
        if item.get('msg_type')!='file' or body.get('file_key')!=resource['file_key'] or body.get('file_name')!=resource['filename']:
            raise ContractError('原件消息附件与已入库记录不一致。')
        link=message_link(item,policy.chat_id)
        if link:
            lines.append(title+'\n点开原消息后，可在手机飞书中预览或下载附件：\n'+link)
        else:
            lines.append(title+'\n平台未返回可验证的消息定位信息。请在本群搜索文件名“'+resource['filename']+'”取阅；本次没有生成定位链接。')
    sources.verify(hits)
    lines.append('入口指向当时上传的原件。没有重新上传文件，也没有调用模型。')
    return '\n\n'.join(lines)
