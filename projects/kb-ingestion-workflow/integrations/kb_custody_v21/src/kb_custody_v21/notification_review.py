"""Read-only assessment of acknowledged notifications; never an unknown resolver.

The caller must bind successful send receipts to immutable request evidence.
An observation cannot change a delivery, create a send, or explain who edited it.
"""
import json
from pathlib import Path

from .contracts import ContractError
from .f01 import PREFIX, _validated_message_content, encoded, sha, validated_message_content
from .runtime import no_symlinks


def acknowledged_notifications(root, hashes, *, app_id, bot_id, chat_id, texts):
    """Load exactly two completed sends, anchored by a caller-approved manifest.

    No search by text or timestamps, and no inference from an absent receipt.
    Even an unrelated unresolved request blocks this bounded follow-up source.
    """
    root = no_symlinks(Path(root))
    keys = ['send:notification-0', 'send:notification-1']
    names = ['journal.local.jsonl'] + [sha(k.encode())+'.response.local' for k in keys]
    if len(texts) != 2 or set(hashes) != set(names):
        raise ContractError('exact two-notification evidence inventory required')
    raw = {n: no_symlinks(root/n).read_bytes() for n in names}
    if any(sha(raw[n]) != hashes[n] for n in names):
        raise ContractError('acknowledged notification evidence changed')
    events = [json.loads(line) for line in raw['journal.local.jsonl'].splitlines()]
    intents = [e['intent'] for e in events if 'intent' in e]
    completed = [e['complete'] for e in events if 'complete' in e]
    if (len(set(intents)) != len(intents) or len(set(completed)) != len(completed)
            or set(intents) != set(completed)):
        raise ContractError('unresolved or duplicate request is not a known-send source')
    previous = events[0].get('approval', {}) if events else {}
    if (previous.get('reference') != 'F04' or previous.get('approved') is not True
            or previous.get('synthetic_only') is not True
            or any(previous.get(k) != v for k, v in
                   dict(app_id=app_id, bot_id=bot_id, chat_id=chat_id, notifications=texts).items())):
        raise ContractError('original approved notification objects differ')
    receipts = []
    for index, key in enumerate(keys):
        attempts = [e for e in events if e.get('intent') == key]
        finished = [e for e in events if e.get('complete') == key]
        payload = raw[sha(key.encode())+'.response.local']
        envelope = json.loads(payload)
        expected_body = dict(receive_id=chat_id, msg_type='text', content=encoded({'text': texts[index]}),
            uuid=sha((previous['starts_at']+'notification-'+str(index)).encode())[:48])
        if (len(attempts) != 1 or attempts[0].get('kind') != 'send'
                or attempts[0].get('method') != 'POST'
                or attempts[0].get('path') != PREFIX+'/im/v1/messages?receive_id_type=chat_id'
                or attempts[0].get('payload_sha256') != sha(encoded(expected_body).encode())
                or len(finished) != 1 or finished[0].get('http_status') != 200
                or finished[0].get('response_sha256') != sha(payload)
                or envelope.get('code') != 0):
            raise ContractError('successful send receipt required; no unknown lookup')
        item = envelope.get('data')
        validated_message_content(item, app_id=app_id, bot_id=bot_id, chat_id=chat_id,
            kind='text', name='notification-'+str(index), text=texts[index])
        bindings = [e['value'] for e in events if e.get('binding') == 'message:notification-'+str(index)]
        if bindings != [{'message_id': item['message_id']}]:
            raise ContractError('send receipt and immutable message binding differ')
        receipts.append(item)
    if receipts[0]['message_id'] == receipts[1]['message_id']:
        raise ContractError('two distinct acknowledged notifications required')
    return receipts


def observe_notification(receipt, items, *, app_id, bot_id, chat_id, text):
    """Compare a known successful send with one exact-ID read response.

    MATCH_WITH_UPDATE_FLAG verifies current content only. It does not verify
    edit history, delivery latency, the person who edited it, or exactly-once.
    Errors must not erase the independently persisted successful send receipt.
    """
    options = dict(app_id=app_id, bot_id=bot_id, chat_id=chat_id,
                   kind='text', name='known-notification', text=text)
    validated_message_content(receipt, **options)
    if not isinstance(items, list) or len(items) != 1:
        raise ContractError('one exact acknowledged notification detail required')
    current = items[0]
    if not isinstance(current, dict) or type(current.get('updated')) is not bool:
        raise ContractError('explicit boolean update observation required')
    _validated_message_content(current, message_id=receipt['message_id'],
                               observe_updated_text=True, **options)
    updated = current.get('updated', False)
    return dict(send_receipt='CONFIRMED', current_content='MATCH',
        status='MATCH_WITH_UPDATE_FLAG' if updated else 'MATCH',
        updated=updated, update_cause='UNDETERMINED' if updated else 'NOT_OBSERVED',
        send_create_time=receipt.get('create_time'), send_update_time=receipt.get('update_time'),
        current_create_time=current.get('create_time'), current_update_time=current.get('update_time'),
        unknown_recovery='NOT_TESTED', edit_history='NOT_VERIFIED')
