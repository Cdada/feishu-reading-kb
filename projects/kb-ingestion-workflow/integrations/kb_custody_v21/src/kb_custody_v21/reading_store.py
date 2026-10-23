"""Reading progress and model intents in the unchanged daily SQLite ledger.

Guide publication and complete reading organization are separate facts. A new
reading call inserts into daily_models, so it consumes the same cumulative cap.
An unresolved intent can be inspected/reconciled, never automatically replayed.
"""
import hashlib
import json
import re

from .contracts import ContractError, stable_key
from .daily_store import Deferred
from .registry import encoded

SCHEMA = """
CREATE TABLE IF NOT EXISTS reading_materials(
 material_id TEXT PRIMARY KEY, document_id TEXT NOT NULL UNIQUE,
 binding TEXT NOT NULL, state TEXT NOT NULL, detail TEXT NOT NULL,
 created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS reading_calls(
 material_id TEXT NOT NULL REFERENCES reading_materials(material_id),
 purpose TEXT NOT NULL, model_key TEXT NOT NULL UNIQUE,
 request_sha256 TEXT NOT NULL, state TEXT NOT NULL, result TEXT,
 PRIMARY KEY(material_id, purpose));
"""
TRANSITIONS = {
    'approved': {'organizing', 'needs_user_input', 'failed'},
    'organizing': {'needs_user_input', 'reviewing', 'failed'},
    'needs_user_input': {'approved', 'failed'},
    'reviewing': {'ready', 'needs_user_input', 'failed'},
    'ready': {'publishing', 'failed'},
    'publishing': {'completed', 'failed'},
    'failed': {'ready'},  # Only inspected, already-reviewed output can resume publishing.
    'completed': set(),
}
BINDING_KEYS = {'document_id', 'source_id', 'extraction_id', 'source_sha256',
                'publication_id', 'preview_sha256', 'preview_version',
                'decision_sha256', 'rules_sha256'}


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def checked_binding(binding):
    if not isinstance(binding, dict) or set(binding) != BINDING_KEYS:
        raise ContractError('complete approved reading material binding required')
    for key, value in binding.items():
        if key == 'preview_version':
            if type(value) is not int or value < 1:
                raise ContractError('positive confirmed preview version required')
        elif key.endswith('_sha256'):
            if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{64}', value):
                raise ContractError('reading material requires immutable SHA-256 bindings')
        elif not isinstance(value, str) or not value or len(value) > 200:
            raise ContractError('stable reading source identity required')
    return dict(binding)


class ReadingStore:
    def __init__(self, daily_store):
        self.store = daily_store
        with self.store.connection() as db:
            db.executescript(SCHEMA)

    @staticmethod
    def _material(row):
        if row is None:
            raise ContractError('unknown reading material')
        result = dict(row)
        for key in ('binding', 'detail'):
            result[key] = json.loads(result[key])
        return result

    def get(self, material_id):
        with self.store.connection() as db:
            return self._material(db.execute('SELECT * FROM reading_materials WHERE material_id=?',
                                             (material_id,)).fetchone())

    def approve(self, binding, *, checked_access):
        binding = checked_binding(binding)
        checked_access()
        key = stable_key('P41-reading-material', binding['document_id'], binding['source_sha256'])
        raw = encoded(binding)
        with self.store.transaction() as db:
            old = db.execute('SELECT * FROM reading_materials WHERE document_id=?',
                             (binding['document_id'],)).fetchone()
            if old:
                if old['binding'] != raw:
                    raise ContractError('confirmed reading source/version/rules cannot change')
                return self._material(old)
            checked_access()
            now = self.store.now()
            db.execute('INSERT INTO reading_materials VALUES (?,?,?,?,?,?,?)',
                       (key, binding['document_id'], raw, 'approved', '{}', now, now))
            self.store._event(db, 'reading_approved', dict(material_id=key, binding=binding))
        return self.get(key)

    def advance(self, material_id, expected, state, *, detail=None, checked_access):
        if state not in TRANSITIONS.get(expected, set()):
            raise ContractError('invalid reading progress transition')
        checked_access()
        with self.store.transaction() as db:
            row = self._material(db.execute('SELECT * FROM reading_materials WHERE material_id=?',
                                             (material_id,)).fetchone())
            if row['state'] != expected:
                raise ContractError('stale reading progress cannot overwrite newer work')
            value = dict(row['detail'], **(detail or {}))
            if state == 'needs_user_input' and not str(value.get('question', '')).strip():
                raise ContractError('reading clarification requires a specific question')
            if state in ('ready', 'publishing', 'completed'):
                if not value.get('review_receipt') or not value.get('bundle_sha256'):
                    raise ContractError('reviewed reading bundle required before completion')
            if state == 'completed':
                if not value.get('publication_receipt') or not value.get('quality_receipt'):
                    raise ContractError('verified publication and quality receipt required')
                value.pop('failure', None)
                value.pop('question', None)
            checked_access()
            db.execute('UPDATE reading_materials SET state=?,detail=?,updated=? WHERE material_id=?',
                       (state, encoded(value), self.store.now(), material_id))
            self.store._event(db, 'reading_progress', dict(material_id=material_id, state=state))
        return self.get(material_id)

    def _purpose_key(self, material_id, purpose):
        if purpose not in ('organize', 'review'):
            raise ContractError('unknown reading model purpose')
        revision = self.get(material_id)['detail'].get('revision', 0)
        return purpose + ('@'+str(revision) if revision else '')

    def answer(self, material_id, answer, *, checked_access):
        if (not isinstance(answer, dict) or set(answer) != {'text', 'message_id', 'receipts'}
                or not isinstance(answer['text'], str) or not answer['text'].strip()
                or len(answer['text']) > 2000 or not answer['message_id'] or not answer['receipts']):
            raise ContractError('verified owner answer required')
        checked_access()
        with self.store.transaction() as db:
            material = self._material(db.execute('SELECT * FROM reading_materials WHERE material_id=?', (material_id,)).fetchone())
            if material['state'] != 'needs_user_input':
                raise ContractError('only an unresolved reading question accepts an answer')
            detail = dict(material['detail'])
            answers = detail.get('answers', [])
            if len(answers) >= 20 or any(a['message_id'] == answer['message_id'] for a in answers):
                raise ContractError('duplicate or excessive reading clarification')
            accepted = dict(answer, question=detail['question'], accepted_at=self.store.now())
            detail.update(answers=[*answers, accepted], revision=detail.get('revision', 0)+1)
            for key in ('question','input_path','input_file_sha256','draft_path','draft_file_sha256'):
                detail.pop(key, None)
            checked_access()
            db.execute("UPDATE reading_materials SET state='approved',detail=?,updated=? WHERE material_id=?",
                (encoded(detail), self.store.now(), material_id))
            self.store._event(db, 'reading_answer', dict(material_id=material_id, revision=detail['revision'], message_id=answer['message_id']))
        return self.get(material_id)

    def call(self, material_id, purpose):
        purpose = self._purpose_key(material_id, purpose)
        with self.store.connection() as db:
            row = db.execute('SELECT * FROM reading_calls WHERE material_id=? AND purpose=?',
                             (material_id, purpose)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result['result'] = json.loads(result['result']) if result['result'] is not None else None
        return result

    def reserve_call(self, material_id, purpose, request_sha256, *, checked_access):
        if (purpose not in ('organize', 'review') or not isinstance(request_sha256, str)
                or not re.fullmatch('[a-f0-9]{64}', request_sha256)):
            raise ContractError('explicit reading call purpose and request digest required')
        purpose_key = self._purpose_key(material_id, purpose)
        checked_access()
        with self.store.transaction() as db:
            material = self._material(db.execute('SELECT * FROM reading_materials WHERE material_id=?',
                                                 (material_id,)).fetchone())
            expected = 'organizing' if purpose == 'organize' else 'reviewing'
            if material['state'] != expected:
                raise ContractError('reading generation is not authorized by current progress')
            if db.execute('SELECT 1 FROM reading_calls WHERE material_id=? AND purpose=?',
                          (material_id, purpose_key)).fetchone():
                raise Deferred('reading_call_already_reserved')
            # Same table/count as the existing guide calls. No per-phase reset.
            if self.store.policy.model_limit_reached(db.execute('SELECT count(*) FROM daily_models').fetchone()[0]):
                raise Deferred('model_limit')
            key = stable_key('P41-reading-model', material_id, purpose_key)
            evidence = dict(purpose='reading_'+purpose, material_id=material_id,
                            original_sha256=material['binding']['source_sha256'], request_sha256=request_sha256)
            checked_access()
            db.execute("INSERT INTO daily_models VALUES (?,1,'started',?,?)",
                       (key, encoded(evidence), self.store.now()))
            db.execute("INSERT INTO reading_calls VALUES (?,?,?,?,'started',NULL)",
                       (material_id, purpose_key, key, request_sha256))
            self.store._event(db, 'reading_model_reserved', evidence)
        return self.call(material_id, purpose)

    def save_call_result(self, material_id, purpose, state, result, *, checked_access):
        if state not in ('validated', 'rejected', 'unknown') or not isinstance(result, dict):
            raise ContractError('explicit reading call outcome required')
        purpose = self._purpose_key(material_id, purpose)
        checked_access()
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM reading_calls WHERE material_id=? AND purpose=?',
                             (material_id, purpose)).fetchone()
            if row is None or row['state'] not in ('started', 'unknown'):
                raise ContractError('reading call is missing or already terminal')
            ledger = db.execute('SELECT * FROM daily_models WHERE source_sha=? AND attempt=1',
                                (row['model_key'],)).fetchone()
            if ledger is None or ledger['state'] not in ('started', 'unknown'):
                raise ContractError('original cumulative model intent differs')
            evidence = dict(json.loads(ledger['evidence']), result=result)
            checked_access()
            db.execute('UPDATE daily_models SET state=?,evidence=?,updated=? WHERE source_sha=? AND attempt=1',
                       (state, encoded(evidence), self.store.now(), row['model_key']))
            db.execute('UPDATE reading_calls SET state=?,result=? WHERE material_id=? AND purpose=?',
                       (state, encoded(result), material_id, purpose))
