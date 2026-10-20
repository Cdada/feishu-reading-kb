"""Exact card delivery, readback and recovery through the existing HTTP ledger."""
from datetime import datetime
import json
from urllib.parse import urlencode

from .contracts import ContractError, stable_key
from .daily_cards import bounded_json, render_card, checked_binding, _identity
from .daily_card_readback import card_matches
from .daily_scheduler import MaterialProblem
from .daily_store import Deferred
from .f01 import PREFIX
from .registry import encoded


def owned_card(item, policy, binding, message_id=None):
    if not isinstance(item, dict):
        raise ContractError('card message envelope required')
    sender = item.get('sender', {})
    if (item.get('msg_type') != 'interactive' or item.get('chat_id') != policy.chat_id
            or item.get('deleted') is not False or type(item.get('updated', False)) is not bool
            or item.get('parent_id') != binding['target_message_id']
            or message_id is not None and item.get('message_id') != message_id
            or sender.get('sender_type') != 'app'
            or (sender.get('id_type'), sender.get('id')) not in (
                ('app_id', policy.app_id), ('open_id', policy.bot_id))):
        raise ContractError('card is not the bound bot reply in the approved chat')
    return _identity(item.get('message_id'), 'om_')


def read_card(transport, operation, contract, evidence):
    mid = contract['message_id']
    proof = transport.request(operation, 'message', PREFIX+'/im/v1/messages/'+mid+
        '?card_msg_content_type=user_card_content', card=contract, evidence=evidence, confirmation=True)
    items = proof['value'].get('data', {}).get('items')
    if not isinstance(items, list) or len(items) != 1:
        raise MaterialProblem('无法核对原卡片，请保留原 PDF；卡片操作暂缓。')
    owned_card(items[0], transport.authority.policy, contract['binding'], mid)
    content = bounded_json(items[0].get('body', {}).get('content'))
    return content, proof


def reconcile_card(transport, operation, saved, request):
    contract = request['card']
    desired = bounded_json(json.loads(request['body'])['content'])
    if request['mode'] == 'card_update':
        actual, proof = read_card(transport, 'card-reconcile:'+stable_key(operation, saved['attempts']),
                                  contract, request['evidence'])
        if card_matches(actual, desired):
            result = dict(proof, reconciled_from='verified_card_content')
            transport.store.complete(operation, saved['attempts'], result)
            return result
        if not card_matches(actual, bounded_json(contract['previous_content'])):
            raise MaterialProblem('卡片已被其他更新改变，请核对；已保存的归档决定不受影响。')
        transport.store.resolve_retry(operation, dict(safe_to_retry=True, receipts=[proof['receipt']],
            reason='owned_card_still_has_previous_content; repeat_same_PATCH'))
        return None
    if request['mode'] != 'send':
        raise ContractError('unsupported card recovery')
    start = datetime.fromisoformat(json.loads((transport._root(operation, 1)/'approval.local.json').read_text())['starts_at']).timestamp()
    now = transport.store.now()
    if now-start >= 3000:
        raise MaterialProblem('卡片发送结果需要核查，已停止超时重发；请保留对应导读。')
    key = 'card-reconciliation:'+operation+':'+str(saved['attempts'])
    cursor = transport.store.get(key) or dict(start=max(0, int(start)-1), end=int(now)+1, token=None, receipts=[], matches=[])
    p = transport.authority.policy
    for _ in range(p.pages_per_tick):
        q = dict(container_id_type='chat', container_id=p.chat_id, start_time=str(cursor['start']),
                 end_time=str(cursor['end']), page_size='50', sort_type='ByCreateTimeAsc')
        if cursor['token']:
            q['page_token'] = cursor['token']
        page = transport.request('card-history:'+stable_key(key, cursor['token']), 'list',
                                 PREFIX+'/im/v1/messages?'+urlencode(sorted(q.items())), confirmation=True)
        data = page['value'].get('data', {})
        if not isinstance(data.get('items'), list) or type(data.get('has_more')) is not bool:
            raise ContractError('complete card reconciliation history required')
        for item in data['items']:
            try:
                mid = owned_card(item, p, contract['binding'])
            except ContractError:
                continue
            try:
                created = int(item['create_time'])/1000
            except (ValueError, TypeError, KeyError) as error:
                raise ContractError('card history requires a creation time') from error
            if not cursor['start'] <= created <= cursor['end']:
                raise ContractError('card history item outside requested range')
            actual, proof = read_card(transport, 'card-history-detail:'+stable_key(key, mid),
                dict(contract, message_id=mid, origin_receipt=page['receipt']),
                dict(request['evidence'], **page['evidence']))
            if card_matches(actual, desired) and mid not in [m['value']['data']['message_id'] for m in cursor['matches']]:
                cursor['matches'].append(dict(proof, value=dict(code=0, data=item),
                                               reconciled_from='verified_card_history'))
        cursor['receipts'].append(page)
        if data['has_more']:
            token = data.get('page_token')
            if not isinstance(token, str) or not token or token == cursor['token'] or len(token)>4096:
                raise ContractError('card history cursor did not advance')
            cursor['token'] = token
            transport.store.put(key, cursor)
            continue
        for proof in cursor['receipts']+cursor['matches']:
            transport._checked_result(proof)
        if len(cursor['matches']) > 1:
            raise MaterialProblem('发现多条相同卡片，已停止重发，请核对。')
        if cursor['matches']:
            result = cursor['matches'][0]
            transport.store.complete(operation, saved['attempts'], result)
            return result
        transport.store.resolve_retry(operation, dict(safe_to_retry=True,
            receipts=[r['receipt'] for r in cursor['receipts']],
            reason='complete_history_checked; same_card_uuid_within_one_hour'))
        return None
    raise Deferred('card_reconciliation_pagination')


class CardDelivery:
    def __init__(self, transport, cards):
        self.transport, self.cards = transport, cards

    def send(self, card, target):
        t = self.transport
        t._checked_result(target)
        initial = encoded(render_card(card))
        contract = dict(card_id=card['card_id'], binding=checked_binding(card['binding']), title=card['title'],
                        content=initial, result='', message_id='', origin_receipt='',
                        target_receipt=target['receipt'], previous_content='')
        body = encoded(dict(msg_type='interactive', content=initial, uuid='card-'+card['card_id'][:40]))
        sent = t.request('card-send:'+card['card_id'], 'send',
            PREFIX+'/im/v1/messages/'+card['binding']['target_message_id']+'/reply',
            mode='send', body=body, card=contract, evidence=target['evidence'], confirmation=True)
        mid = owned_card(sent['value'].get('data'), t.authority.policy, card['binding'])
        contract.update(message_id=mid, origin_receipt=sent['receipt'])
        evidence = dict(target['evidence'], **sent['evidence'])
        actual, readback = read_card(t, 'card-readback:'+card['card_id'], contract, evidence)
        if not card_matches(actual, render_card(card)):
            raise ContractError('card readback differs from the exact sent definition')
        def access():
            for proof in (target, sent, readback):
                t._checked_result(proof)
        access()
        return self.cards.bind_delivery(card['card_id'], mid,
            dict(sent=sent, readback=readback, target=target, contract=contract), checked_access=access)

    def update(self, card, result):
        receipt = card['receipt']
        if not receipt:
            raise ContractError('only a delivered card can be updated')
        t = self.transport
        for name in ('sent', 'readback', 'target'):
            t._checked_result(receipt[name])
        content = encoded(render_card(card, result=result))
        contract = dict(receipt['contract'], result=result, content=content,
                        previous_content=encoded(render_card(card)))
        evidence = dict(receipt['sent']['evidence'], **receipt['readback']['evidence'], **receipt['target']['evidence'])
        op = 'card-result:'+stable_key(card['card_id'], result)
        written = t.request(op, 'send', PREFIX+'/im/v1/messages/'+card['message_id'], mode='card_update',
            body=encoded(dict(content=content)), card=contract, evidence=evidence, confirmation=True)
        actual, checked = read_card(t, op+':readback', contract, dict(evidence, **written['evidence']))
        if not card_matches(actual, render_card(card, result=result)):
            raise ContractError('card result update readback differs')
        return checked
