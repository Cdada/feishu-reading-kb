"""Pilot control layer for exact-preview decisions.

The live adapter must supply identities from verified platform envelopes
and successful send receipts, never model output or user-supplied JSON. This
module neither sends messages nor authenticates a platform connection.
"""
from dataclasses import dataclass
import json

from .contracts import ContractError
from .publication import Publications
from .registry import now


def _identity(value):
    if not isinstance(value,str) or not value or len(value)>256 or value.strip()!=value or any(ord(c)<32 for c in value):
        raise ContractError('missing or invalid Pilot identity')
    return value


@dataclass(frozen=True)
class PilotScope:
    app_id: str
    chat_id: str
    user_id: str

    def __post_init__(self):
        for value in (self.app_id,self.chat_id,self.user_id):
            _identity(value)


class PilotPreviews:
    COMMANDS = {'确认入库':'confirm','确认当前版本入库':'confirm','补充材料后再入库':'supplement','暂不入库':'defer'}

    def __init__(self,registry,scope):
        self.registry,self.scope = registry,scope
        self.publisher = Publications(registry)

    def _scope(self,app_id,chat_id,user_id):
        if (app_id,chat_id,user_id) != (self.scope.app_id,self.scope.chat_id,self.scope.user_id):
            raise ContractError('Pilot application, chat or user outside configured scope')

    def _candidate(self,db,pid):
        row = db.execute('SELECT * FROM pilot_candidates WHERE publication_id=?',(pid,)).fetchone()
        if row is None:
            raise ContractError('Pilot candidate must be staged before delivery')
        self._scope(row['app_id'],row['chat_id'],row['user_id'])
        return row

    def _current(self,db,pid):
        if not db.execute('SELECT 1 FROM pilot_current_candidates WHERE publication_id=?',(pid,)).fetchone():
            raise ContractError('old preview cannot approve the current candidate')

    def status(self, pid):
        """Report persisted lifecycle facts; an absent receipt is not send failure."""
        row = self.publisher._row(pid)
        with self.registry.transaction() as db:
            candidate = db.execute('SELECT * FROM pilot_candidates WHERE publication_id=?', (pid,)).fetchone()
            if candidate:
                self._scope(candidate['app_id'], candidate['chat_id'], candidate['user_id'])
            current = db.execute('SELECT publication_id FROM pilot_current_candidates WHERE source_asset_id=?',
                                 (row['source_asset_id'],)).fetchone()
            delivery = db.execute('SELECT * FROM pilot_preview_deliveries WHERE publication_id=?', (pid,)).fetchone()
            decision = db.execute('SELECT command FROM pilot_preview_decisions WHERE publication_id=?', (pid,)).fetchone()
            active = db.execute('SELECT publication_id FROM active_publications_v21 WHERE source_asset_id=?',
                                (row['source_asset_id'],)).fetchone()
        state = row['state']
        if state == 'prepared':
            if current and current['publication_id'] != pid:
                state = 'preview_superseded'
            elif decision:
                state = self.COMMANDS[decision['command']]
            elif delivery:
                state = 'awaiting_owner_decision'
            else:
                state = 'preview_ready_pending_delivery' if candidate else 'preview_prepared'
        return dict(state=state, publication_id=pid, publication_state=row['state'],
            delivery_state='VERIFIED' if delivery else 'NO_VERIFIED_RECEIPT',
            owner_decision=self.COMMANDS[decision['command']] if decision else None,
            current_candidate=bool(current and current['publication_id'] == pid),
            active_publication=bool(active and active['publication_id'] == pid))

    def stage(self,pid):
        """Activate a prepared version BEFORE attempting its preview delivery.

        A new candidate invalidates the older preview even if the new send fails
        or its result is unknown. Pending confirmed publication must be resolved
        before replacing it. Already superseded versions cannot be reactivated.
        """
        self.registry.runtime.gate()
        self.publisher._row(pid)
        with self.registry.transaction() as db:
            row = db.execute('SELECT * FROM publications_v21 WHERE publication_id=?',(pid,)).fetchone()
            existing = db.execute('SELECT 1 FROM pilot_candidates WHERE publication_id=?',(pid,)).fetchone()
            if existing:
                candidate = self._candidate(db,pid)
                self._current(db,pid)
                return dict(publication_id=pid,version=candidate['version'])
            if row['state']!='prepared':
                raise ContractError('Pilot requires a prepared publication')
            self.publisher._tree(row,pid+'.candidate')
            current = db.execute('SELECT p.* FROM pilot_current_candidates c JOIN publications_v21 p USING(publication_id) WHERE c.source_asset_id=?',
                                 (row['source_asset_id'],)).fetchone()
            if current and current['state'] in {'confirmed','publishing'}:
                raise ContractError('finish the confirmed publication before replacing its preview')
            version = db.execute('SELECT coalesce(max(c.version),0)+1 FROM pilot_candidates c JOIN publications_v21 p USING(publication_id) WHERE p.source_asset_id=?',
                                 (row['source_asset_id'],)).fetchone()[0]
            db.execute('INSERT INTO pilot_candidates VALUES (?,?,?,?,?,?)',
                       (pid,self.scope.app_id,self.scope.chat_id,self.scope.user_id,version,now()))
            db.execute('INSERT INTO pilot_current_candidates VALUES (?,?) ON CONFLICT(source_asset_id) DO UPDATE SET publication_id=excluded.publication_id',
                       (row['source_asset_id'],pid))
        return dict(publication_id=pid,version=version)

    def bind_delivery(self,pid,*,app_id,chat_id,message_id,pdf_sha256,receipt_ref,outcome,checked_access=None):
        """Record only a checked successful receipt for the exact sent bytes."""
        self.registry.runtime.gate()
        self._scope(app_id,chat_id,self.scope.user_id)
        _identity(message_id)
        _identity(receipt_ref)
        if outcome!='sent':
            raise ContractError('unknown or failed delivery cannot create a preview binding')
        with self.registry.transaction() as db:
            self._candidate(db,pid)
            self._current(db,pid)
            row = db.execute('SELECT * FROM publications_v21 WHERE publication_id=?',(pid,)).fetchone()
            supplied = (app_id,chat_id,message_id,pdf_sha256,receipt_ref)
            old = db.execute('SELECT * FROM pilot_preview_deliveries WHERE publication_id=?',(pid,)).fetchone()
            if old:
                if tuple(old[k] for k in ('app_id','chat_id','message_id','pdf_sha256','receipt_ref'))!=supplied:
                    raise ContractError('preview delivery binding cannot be replaced')
                return
            if row['state']!='prepared' or row['preview_sha256']!=pdf_sha256:
                raise ContractError('delivery must bind the exact prepared PDF')
            self.publisher._tree(row,pid+'.candidate')
            if checked_access is not None:
                checked_access()  # Recheck live window/source after the expensive tree hash.
            if db.execute('SELECT 1 FROM pilot_preview_deliveries WHERE app_id=? AND chat_id=? AND message_id=?',supplied[:3]).fetchone():
                raise ContractError('preview message already belongs to another candidate')
            db.execute('INSERT INTO pilot_preview_deliveries VALUES (?,?,?,?,?,?,?)',(pid,*supplied,now()))

    def decide(self,*,app_id,chat_id,user_id,message_id,reply_to,text,checked_access=None,evidence=None):
        """Persist the user decision and publication confirmation atomically.

        Filesystem publication uses the existing recoverable publisher after the
        transaction. Redelivery of the identical verified message resumes that
        same publication; a reused message identity with different content fails.
        """
        self.registry.runtime.gate()
        self._scope(app_id,chat_id,user_id)
        _identity(message_id)
        _identity(reply_to)
        if text not in self.COMMANDS:
            raise ContractError('reply with an explicit supported decision to this preview')
        with self.registry.transaction() as db:
            delivery = db.execute('SELECT * FROM pilot_preview_deliveries WHERE app_id=? AND chat_id=? AND message_id=?',
                                  (app_id,chat_id,reply_to)).fetchone()
            if delivery is None:
                raise ContractError('reply does not identify a successfully delivered preview')
            pid = delivery['publication_id']
            self._candidate(db,pid)
            prior = db.execute('SELECT * FROM pilot_preview_decisions WHERE app_id=? AND chat_id=? AND message_id=?',
                               (app_id,chat_id,message_id)).fetchone()
            if prior:
                if (prior['publication_id'],prior['user_id'],prior['reply_to'],prior['command'])!=(pid,user_id,reply_to,text):
                    raise ContractError('reused decision message has different binding or content')
                if evidence is not None:
                    saved = db.execute("SELECT payload_json FROM custody_events WHERE dedupe_key=?",
                                       ('pilot-decision:'+pid,)).fetchone()
                    if saved is None or json.loads(saved[0])!=evidence:
                        raise ContractError('verified decision evidence changed')
            else:
                self._current(db,pid)
                if db.execute('SELECT 1 FROM pilot_preview_decisions WHERE publication_id=?',(pid,)).fetchone():
                    raise ContractError('candidate already decided; prepare a new preview to change the decision')
                if self.COMMANDS[text]=='confirm':
                    self.publisher._confirm_in_transaction(db,pid,delivery['pdf_sha256'],user_id,message_id,
                                                          accept_limitations=text=='确认当前版本入库')
                db.execute('INSERT INTO pilot_preview_decisions VALUES (?,?,?,?,?,?,?,?)',
                           (pid,app_id,chat_id,message_id,user_id,reply_to,text,now()))
                if evidence is not None:
                    self.registry.event(db,'pilot_decision_observed',pid,'pilot-decision:'+pid,evidence)
            if checked_access is not None:
                # Confirmation can hash a large candidate and its review. An
                # expired or changed live binding rolls back this transaction.
                checked_access()
        if self.COMMANDS[text]=='confirm':
            path = self.publisher.publish(pid,checked_access=checked_access)
            return dict(status='published',publication_id=pid,path=str(path))
        return dict(status=self.COMMANDS[text],publication_id=pid)
