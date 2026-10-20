"""Connect durable card choices to the existing image and preview decisions."""
import hashlib
import json
from pathlib import Path

from .contracts import ContractError, stable_key
from .daily_cards import CardStore, COMMANDS
from .daily_card_io import CardDelivery
from .daily_scheduler import MaterialProblem
from .daily_store import Deferred
from .f05 import file_sha
from .image_availability import availability, COMMANDS as IMAGE_COMMANDS
from .pilot_decision import observed_reply
from .registry import encoded


class CardFlow:
    def __init__(self, backend):
        self.backend = backend
        self.store = backend.store
        self.cards = CardStore(self.store)
        self.delivery = CardDelivery(backend.transport, self.cards)

    def material(self, document, purpose=None):
        b = self.backend
        b._check_material(document)
        purpose = purpose or ('images' if document['phase']=='awaiting_images' else 'preview')
        common = dict(purpose=purpose, document_id=document['message_id'], source_sha256=document['source_sha'])
        if purpose == 'images':
            notice = document['data']['image_notice']
            current = availability(b.intake.conversion, document['data']['extraction_id'])
            if current is None or current['binding'] != document['data']['image_check']['binding']:
                raise ContractError('card image report changed')
            target = notice['receipt']
            binding = dict(common, target_message_id=notice['message_id'],
                extraction_id=document['data']['extraction_id'],
                report_sha256=hashlib.sha256(encoded(current['binding']).encode()).hexdigest())
        else:
            status = b.pipeline.previews.status(document['publication_id'])
            if not status['current_candidate']:
                raise ContractError('card preview was superseded')
            row = b.pipeline.publisher._row(document['publication_id'])
            candidates = b.registry.rows('SELECT version FROM pilot_candidates WHERE publication_id=?', (document['publication_id'],))
            if len(candidates) != 1:
                raise ContractError('one staged preview required')
            for proof in document['data']['preview'].values():
                b.transport._checked_result(proof)
            target = document['data']['preview']['sent']
            binding = dict(common, target_message_id=document['preview_message_id'],
                publication_id=document['publication_id'], pdf_sha256=row['preview_sha256'],
                preview_version=candidates[0]['version'])
        b.transport._checked_result(target)
        return binding, target

    def receive(self, raw, proof):
        from .daily_cards import bounded_json
        event = bounded_json(raw)
        value = bounded_json(event.get('action_value'), 4096)
        card = self.cards.get(value.get('card_id'))
        document = self.store.document(card['document_id'])
        if card['purpose'].startswith('relation_'):
            binding, target = self.backend.relations.current(card)
        else:
            binding, target = self.material(document, card['purpose'])
        def access():
            self.backend.check_authorization()
            if any(file_sha(Path(p)) != h for p,h in proof['receipts'].items()):
                raise ContractError('durable listener evidence changed')
            for receipt in (card['receipt'] or {}).values():
                if isinstance(receipt, dict) and 'evidence' in receipt:
                    self.backend.transport._checked_result(receipt)
            self.backend.transport._checked_result(target)
        evidence = dict(proof, reference='P40-owner-card-v1', rules_sha256=self.backend.authority.digest,
            event_id=event['event_id'], card_id=card['card_id'], card_message_id=card['message_id'],
            message_id='card-event:'+event['event_id'], reply_to=binding['target_message_id'], command=value['command'])
        outcome = self.cards.accept_event(raw, evidence, app_id=self.backend.authority.policy.app_id,
            current_binding=binding, checked_access=access)
        if outcome == 'stale' and card['purpose'].startswith('relation_'):
            self.backend.relations.stale(card)
        return outcome

    def advance(self, document):
        binding, target = self.material(document)
        card = self.cards.prepare(binding, document['filename'])
        choices = []
        parents = {binding['target_message_id']}
        if card['message_id']:
            parents.add(card['message_id'])
        for parent in parents:
            for item in self.backend._replies(parent):
                try:
                    command = observed_reply(item,self.backend.authority.policy.bot_id)[1]
                except ContractError:
                    continue
                command = '确认入库' if command=='确认当前版本入库' else command
                if command in COMMANDS[card['purpose']]:
                    choices.append((item, command))
        saved = self.cards.choice(card['card_id'])
        if len({v[1] for v in choices}) > 1 and saved is None:
            raise MaterialProblem('收到不同确认回复，请核对这一份导读或缺图说明。')
        for observed, command in choices:
            parsed, detail, listed = self.backend._verified_reply(observed)
            evidence = dict(reference='P40-owner-text-v1', rules_sha256=self.backend.authority.digest,
                message_id=observed['message_id'], reply_to=binding['target_message_id'],
                platform_parent=parsed[0], command=command, card_id=card['card_id'],
                receipts=dict(detail['evidence'], **listed['evidence'], **target['evidence']))
            def access():
                self.backend._check_material(document)
                for proof in (detail, listed, target):
                    self.backend.transport._checked_result(proof)
            outcome = self.cards.choose_text(card['card_id'], command, evidence,
                current_binding=binding, checked_access=access)
            if outcome == 'conflict':
                self.store.put('card-conflict:'+card['card_id'], dict(command=command, at=self.store.now()))
        choice = self.cards.choice(card['card_id'])
        if choice is None:
            if card['state'] == 'prepared':
                listener = getattr(self.backend, 'card_listener', None)
                if listener is None or not listener.ready:
                    raise Deferred('card_listener_not_ready')
                self.delivery.send(card, target)
            return
        evidence = choice['evidence']
        if any(file_sha(Path(p))!=h for p,h in evidence['receipts'].items()):
            raise ContractError('saved card choice evidence changed')
        if card['purpose'] == 'preview':
            self.store.advance(document['message_id'], 'awaiting_confirmation', 'confirmed', user_action=None,
                data=dict(decision=dict(evidence, command=choice['command']), card_id=card['card_id']))
            return
        current = availability(self.backend.intake.conversion, document['data']['extraction_id'])
        decision = dict(binding=current['binding'], choice=IMAGE_COMMANDS[choice['command']], evidence=evidence)
        with self.backend.registry.transaction() as db:
            old = db.execute('SELECT payload_json FROM custody_events WHERE dedupe_key=?', (current['key'],)).fetchone()
            if old and json.loads(old[0]) != decision:
                raise ContractError('saved image card decision cannot be replaced')
            if not old:
                self.backend.registry.event(db, 'owner_image_availability_choice', current['binding']['source_asset_id'], current['key'], decision)
        phase = 'converted' if decision['choice']=='continue_without_missing' else 'needs_upload'
        self.store.advance(document['message_id'], 'awaiting_images', phase, user_action=None,
            data=dict(image_decision=decision, image_card_id=card['card_id']))
        self.cards.finish(card['card_id'], dict(status=phase), checked_access=self.backend.check_authorization)

    def report(self):
        # Include terminal documents: display failure must not repeat business.
        rows = self.store.rows("SELECT c.card_id,c.document_id,c.purpose FROM daily_cards c JOIN daily_card_choices q USING(card_id) WHERE c.purpose IN ('preview','images') ORDER BY c.created")
        labels = {'published':'已确认入库。', 'notified':'已确认入库。', 'rejected':'已按你的选择暂不入库。',
                  'converted':'已选择按缺图版本继续；导读生成后仍需确认入库。', 'needs_upload':'已记录，请重新发送含图原件。'}
        for row in rows:
            card = self.cards.get(row['card_id'])
            choice = self.cards.choice(row['card_id'])
            document = self.store.document(row['document_id'])
            if choice['result'] is None:
                phase = document['phase']
                # Recover a crash between the image state change and finish.
                if row['purpose']=='images':
                    if document['data'].get('image_card_id') != row['card_id']:
                        continue
                    phase = 'converted' if document['data']['image_decision']['choice']=='continue_without_missing' else 'needs_upload'
                elif document['data'].get('card_id') != row['card_id'] or phase not in ('published','notified','rejected'):
                    continue
                result = dict(status='published' if phase=='notified' else phase)
                self.cards.finish(row['card_id'], result, checked_access=self.backend.check_authorization)
                choice = self.cards.choice(row['card_id'])
                card = self.cards.get(row['card_id'])
            key = 'card-display:'+row['card_id']
            display = self.store.get(key) or {}
            if display.get('status') in ('verified', 'fallback_verified') or not card['receipt']:
                continue
            wait = self.store.get('card-display-wait:'+row['card_id']) or {}
            if wait.get('until') and wait['until'] > self.store.now():
                continue
            text = labels[choice['result']['status']]
            if document['data'].get('reading_completed') and choice['result']['status']=='published':
                text = '已完成独立测试库的完整整理，原件、笔记及适用的卡片和简报已保存。'
            if choice['command']=='补充材料后再入库':
                text = '已记录，等待补充材料后再入库。'
            if display.get('status') == 'fallback_pending':
                self._fallback(row['card_id'], text, display['reason'])
                return
            try:
                proof = self.delivery.update(card, text)
                self.store.put(key, dict(status='verified', proof=proof))
            except MaterialProblem as error:
                self._fallback(row['card_id'], text, error.reason)
            except Deferred as error:
                self.store.put('card-display-wait:'+row['card_id'], dict(reason=error.reason, until=error.until))
                if error.reason in ('card_update_limit','attempts_exhausted'):
                    self._fallback(row['card_id'], text, error.reason)
            return

    def _fallback(self, card_id, text, reason):
        key = 'card-display:'+card_id
        # Save pending before the send, so interruption retries the same notice
        # through its existing reconciliation rather than losing the result.
        self.store.put(key, dict(status='fallback_pending', reason=reason))
        proof = self.backend.notice('card-result-fallback:'+card_id,
            text+' 卡片显示未能更新，已保存的决定和结果不受影响。', confirmation=True)
        self.store.put(key, dict(status='fallback_verified', reason=reason, proof=proof))
