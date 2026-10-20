"""P40 card contracts and durable choices, without network or model access.

The caller must verify physical send/event evidence before passing it here.
Callback values select an existing local binding; they never define its scope,
PDF version or target. Network wiring is kept outside this state component.
"""
import hashlib
import json
import re

from .contracts import ContractError, stable_key
from .registry import encoded
from .relation_cards import RELATION_COMMANDS, HEADERS, checked_fields, commands as relation_commands


REFERENCE = 'P40-card-v1'
COMMANDS = {
    'preview': ('确认入库', '补充材料后再入库', '暂不入库'),
    'images': ('按缺图版本继续', '重新发文件'),
}
COMMANDS.update(RELATION_COMMANDS)


def commands(binding):
    return relation_commands(binding) if binding['purpose'] in RELATION_COMMANDS else COMMANDS[binding['purpose']]


SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_cards(
 card_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, purpose TEXT NOT NULL,
 binding TEXT NOT NULL, title TEXT NOT NULL, state TEXT NOT NULL,
 message_id TEXT UNIQUE, receipt TEXT, created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS daily_cards_document ON daily_cards(document_id,purpose);
CREATE TABLE IF NOT EXISTS daily_card_choices(
 card_id TEXT PRIMARY KEY REFERENCES daily_cards(card_id), command TEXT NOT NULL,
 evidence TEXT NOT NULL, result TEXT, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS daily_card_events(
 event_id TEXT PRIMARY KEY, digest TEXT NOT NULL, card_id TEXT NOT NULL,
 outcome TEXT NOT NULL, created REAL NOT NULL);
"""


def bounded_json(raw, maximum=65536):
    if not isinstance(raw, str) or len(raw.encode('utf-8')) > maximum:
        raise ContractError('bounded card JSON required')
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('duplicate card field')
            value[key] = item
        return value
    def reject_constant(value):
        raise ValueError('nonfinite JSON: '+value)
    try:
        return json.loads(raw, object_pairs_hook=unique, parse_constant=reject_constant)
    except (ValueError, RecursionError) as error:
        raise ContractError('invalid card JSON') from error


def _identity(value, prefix):
    if not isinstance(value, str) or not re.fullmatch(prefix+r'[A-Za-z0-9_-]{1,160}', value):
        raise ContractError('invalid card identity')
    return value


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{64}', value):
        raise ContractError('card binding requires a SHA-256 digest')
    return value


def checked_binding(binding):
    if not isinstance(binding, dict) or binding.get('purpose') not in COMMANDS:
        raise ContractError('supported card purpose required')
    if binding['purpose'] in RELATION_COMMANDS:
        checked_fields(binding)
        _identity(binding['document_id'], 'om_')
        _identity(binding['target_message_id'], 'om_')
        _digest(binding['source_sha256'])
        return dict(binding)
    common = {'purpose', 'document_id', 'source_sha256', 'target_message_id'}
    extra = ({'publication_id', 'pdf_sha256', 'preview_version'}
             if binding['purpose'] == 'preview' else {'extraction_id', 'report_sha256'})
    if set(binding) != common | extra:
        raise ContractError('complete exact material binding required')
    _identity(binding['document_id'], 'om_')
    _identity(binding['target_message_id'], 'om_')
    _digest(binding['source_sha256'])
    if binding['purpose'] == 'preview':
        _identity(binding['publication_id'], 'publication_')
        _digest(binding['pdf_sha256'])
        if type(binding['preview_version']) is not int or not 1 <= binding['preview_version'] <= 999999999:
            raise ContractError('positive preview version required')
    else:
        _identity(binding['extraction_id'], 'extraction_')
        _digest(binding['report_sha256'])
    return dict(binding)


def render_card(card, *, result=None):
    """Use plain text for material titles; only local commands become actions."""
    binding = checked_binding(card['binding'])
    title = card['title']
    if not isinstance(title, str) or not title.strip() or len(title.encode()) > 1024:
        raise ContractError('bounded nonempty card title required')
    if binding['purpose'] in RELATION_COMMANDS:
        detail = binding['details']
    elif binding['purpose'] == 'preview':
        detail = f"导读第 {binding['preview_version']} 版。请先查看本卡片所回复的导读 PDF，再选择。"
    else:
        detail = '请先查看本卡片所回复的缺图说明。继续后仍需单独确认导读入库。'
    elements = [{'tag': 'div', 'text': {'tag': 'plain_text', 'content': title, 'text_size': 'heading-3'}},
                {'tag': 'div', 'text': {'tag': 'plain_text', 'content': detail}}]
    if result is None:
        buttons = []
        for command in commands(binding):
            buttons.append(dict(tag='button', text=dict(tag='plain_text', content=command),
                                type='primary_filled' if command == commands(binding)[0] else 'default',
                                width='fill', behaviors=[dict(type='callback', value=dict(
                                    reference=REFERENCE, card_id=card['card_id'], command=command))]))
        elements.extend(buttons)
    else:
        if not isinstance(result, str) or not result.strip() or len(result.encode()) > 2048:
            raise ContractError('bounded card result required')
        elements.append(dict(tag='div', text=dict(tag='plain_text', content=result)))
    return dict(schema='2.0', config=dict(width_mode='default', update_multi=True, enable_forward=False),
                header=dict(title=dict(tag='plain_text', content=HEADERS.get(binding['purpose'], '确认导读入库' if binding['purpose']=='preview' else '选择缺图处理方式')),
                            template='blue', icon=dict(tag='standard_icon', token='approval_colorful')),
                body=dict(direction='vertical', vertical_spacing='8px', elements=elements))


class CardStore:
    """One choice per immutable card, shared by verified text and button input."""
    def __init__(self, store):
        self.store = store
        with store.connection() as db:
            db.executescript(SCHEMA)

    @staticmethod
    def _card(row):
        if row is None:
            raise ContractError('unknown local card')
        card = dict(row)
        card['binding'] = json.loads(card['binding'])
        card['receipt'] = json.loads(card['receipt']) if card['receipt'] else None
        return card

    def get(self, card_id):
        with self.store.connection() as db:
            return self._card(db.execute('SELECT * FROM daily_cards WHERE card_id=?', (card_id,)).fetchone())

    def prepare(self, binding, title):
        binding = checked_binding(binding)
        # Persisted JSON may reconstruct the same fields in another order.
        # Identity must depend on values, not dictionary insertion order.
        cid = stable_key(REFERENCE, encoded(self.store.policy.scope), encoded(binding))
        render_card(dict(card_id=cid, binding=binding, title=title))
        raw = encoded(binding)
        with self.store.transaction() as db:
            previous = db.execute('SELECT * FROM daily_cards WHERE card_id=?', (cid,)).fetchone()
            if previous:
                if previous['binding'] != raw or previous['title'] != title:
                    raise ContractError('existing card content cannot change')
                return self._card(previous)
            if 'pair_id' in binding:
                # Each pair keeps its own correction/revocation controls.
                for row in db.execute("SELECT card_id,binding FROM daily_cards WHERE document_id=? AND purpose=? AND state IN ('prepared','sent')", (binding['document_id'],binding['purpose'])).fetchall():
                    if json.loads(row['binding']).get('pair_id') == binding['pair_id']:
                        db.execute("UPDATE daily_cards SET state='superseded' WHERE card_id=?",(row['card_id'],))
            else:
                db.execute("UPDATE daily_cards SET state='superseded' WHERE document_id=? AND purpose=? AND state IN ('prepared','sent')",
                           (binding['document_id'], binding['purpose']))
            db.execute('INSERT INTO daily_cards VALUES (?,?,?,?,?,?,NULL,NULL,?)',
                       (cid, binding['document_id'], binding['purpose'], raw, title, 'prepared', self.store.now()))
        return self.get(cid)

    def bind_delivery(self, card_id, message_id, receipt, *, checked_access):
        _identity(message_id, 'om_')
        checked_access()
        if not isinstance(receipt, dict) or not receipt:
            raise ContractError('verified card send receipt required')
        raw = encoded(receipt)
        with self.store.transaction() as db:
            card = self._card(db.execute('SELECT * FROM daily_cards WHERE card_id=?', (card_id,)).fetchone())
            if card['state'] == 'sent' and card['message_id'] == message_id and card['receipt'] == receipt:
                return card
            if card['state'] != 'prepared' or card['message_id'] is not None:
                raise ContractError('card cannot be rebound or reactivated')
            checked_access()
            db.execute("UPDATE daily_cards SET state='sent',message_id=?,receipt=? WHERE card_id=?",
                       (message_id, raw, card_id))
        return self.get(card_id)

    def _choose(self, db, card, command, evidence, *, allow_prepared=False):
        if command not in commands(card['binding']):
            raise ContractError('unsupported explicit card choice')
        old = db.execute('SELECT * FROM daily_card_choices WHERE card_id=?', (card['card_id'],)).fetchone()
        if old:
            return 'duplicate' if old['command'] == command else 'conflict'
        if card['state'] != 'sent' and not (allow_prepared and card['state']=='prepared'):
            return 'stale'
        if not isinstance(evidence, dict) or not evidence:
            raise ContractError('verified decision evidence required')
        db.execute('INSERT INTO daily_card_choices VALUES (?,?,?,NULL,?)',
                   (card['card_id'], command, encoded(evidence), self.store.now()))
        return 'accepted'

    def choose_text(self, card_id, command, evidence, *, current_binding, checked_access):
        """Already verified direct PDF/text replies share the card's decision slot."""
        if command == '确认当前版本入库':
            command = '确认入库'
        checked_access()
        with self.store.transaction() as db:
            card = self._card(db.execute('SELECT * FROM daily_cards WHERE card_id=?', (card_id,)).fetchone())
            if card['binding'] != checked_binding(current_binding):
                return 'stale'
            checked_access()
            return self._choose(db, card, command, evidence, allow_prepared=True)

    def accept_event(self, raw, evidence, *, app_id, current_binding, checked_access):
        """Admit a checked CLI callback, never an arbitrary caller-made event."""
        checked_access()
        event = bounded_json(raw)
        p = self.store.policy
        if not isinstance(event, dict) or app_id != p.app_id:
            raise ContractError('callback application differs')
        event_id = _identity(event.get('event_id'), '')
        if (event.get('type') != 'card.action.trigger' or event.get('operator_id') != p.account_id
                or event.get('chat_id') != p.chat_id or event.get('host') != 'im_message'
                or event.get('action_tag') != 'button'
                or any(event.get(k) not in (None, '') for k in ('form_value', 'input_value', 'option', 'options'))
                or not isinstance(event.get('card_content'), str) or not event['card_content'].strip()):
            raise ContractError('exact owner button event with readable card required')
        value = bounded_json(event.get('action_value'), 4096)
        if not isinstance(value, dict) or set(value) != {'reference','card_id','command'} or value['reference'] != REFERENCE:
            raise ContractError('explicit registered card action required')
        cid = _digest(value['card_id'])
        digest = hashlib.sha256(raw.encode()).hexdigest()
        with self.store.transaction() as db:
            old = db.execute('SELECT * FROM daily_card_events WHERE event_id=?', (event_id,)).fetchone()
            if old:
                if old['digest'] != digest:
                    raise ContractError('event identity reused with different payload')
                return old['outcome']
            card = self._card(db.execute('SELECT * FROM daily_cards WHERE card_id=?', (cid,)).fetchone())
            if event.get('message_id') != card['message_id']:
                raise ContractError('callback does not belong to the delivered card')
            checked_access()
            outcome = ('stale' if card['binding'] != checked_binding(current_binding)
                       else self._choose(db, card, value['command'], evidence))
            db.execute('INSERT INTO daily_card_events VALUES (?,?,?,?,?)',
                       (event_id, digest, cid, outcome, self.store.now()))
        return outcome

    def choice(self, card_id):
        with self.store.connection() as db:
            row = db.execute('SELECT * FROM daily_card_choices WHERE card_id=?', (card_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result['evidence'] = json.loads(result['evidence'])
        result['result'] = json.loads(result['result']) if result['result'] else None
        return result

    def finish(self, card_id, result, *, checked_access):
        """Business completion is explicit; receipt of a click is insufficient."""
        if not isinstance(result, dict) or not result:
            raise ContractError('verified business result required')
        checked_access()
        with self.store.transaction() as db:
            row = db.execute('SELECT result FROM daily_card_choices WHERE card_id=?', (card_id,)).fetchone()
            if row is None:
                raise ContractError('card has no saved owner choice')
            raw = encoded(result)
            if row['result'] is not None and row['result'] != raw:
                raise ContractError('completed card result cannot change')
            checked_access()
            db.execute('UPDATE daily_card_choices SET result=? WHERE card_id=?', (raw, card_id))
            db.execute("UPDATE daily_cards SET state='completed' WHERE card_id=?", (card_id,))
