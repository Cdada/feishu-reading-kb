"""One-shot evaluation calls charged to the existing cumulative daily ledger.

No model client is constructed here. The caller supplies a narrow authorized
adapter and a fresh access check; offline CLI commands never open this ledger.
"""
from copy import deepcopy
import json
import math

from .contracts import ContractError, stable_key
from .daily_store import Deferred
from .registry import encoded
from .qa_quality import digest, require, sha, text

SCHEMA = '''
CREATE TABLE IF NOT EXISTS qa_eval_grants(id TEXT PRIMARY KEY,digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS qa_eval_calls(
 call_key TEXT PRIMARY KEY,grant_id TEXT NOT NULL,run_id TEXT NOT NULL,case_id TEXT NOT NULL,
 purpose TEXT NOT NULL,model_key TEXT NOT NULL UNIQUE,request_sha256 TEXT NOT NULL,
 model_fingerprint TEXT NOT NULL,state TEXT NOT NULL,result TEXT,created REAL NOT NULL);
'''


class EvaluationCalls:
    def __init__(self, daily):
        self.daily = daily
        with daily.connection() as db:
            db.executescript(SCHEMA)

    def get(self, run_id, case_id, purpose):
        key = stable_key('qa-evaluation', run_id, case_id, purpose)
        rows = self.daily.rows('SELECT * FROM qa_eval_calls WHERE call_key=?', (key,))
        if not rows:
            return None
        result = rows[0]
        result['result'] = json.loads(result['result']) if result['result'] is not None else None
        return result

    def _grant(self, grant, checked_access, model_fingerprint):
        fields = {'id', 'approved', 'ledger_path', 'scope_sha256', 'policy_digest', 'starts_at', 'expires_at',
            'max_calls', 'max_request_bytes', 'max_response_bytes', 'model_fingerprints', 'billing'}
        require(isinstance(grant, dict) and set(grant) == fields and text(grant['id']), 'explicit evaluation grant required')
        require(grant['approved'] is True and grant['billing'] == 'subscription_only'
            and grant['ledger_path'] == str(self.daily.path)
            and grant['scope_sha256'] == digest(self.daily.policy.scope)
            and grant['policy_digest'] == self.daily.policy.digest, 'grant must bind the existing ledger and policy')
        for key in ('starts_at', 'expires_at'):
            require(type(grant[key]) in (int, float) and math.isfinite(grant[key]), 'finite grant times required')
        for key, high in (('max_calls', 10000), ('max_request_bytes', 4*1024*1024), ('max_response_bytes', 4*1024*1024)):
            require(type(grant[key]) is int and 0 <= grant[key] <= high, 'bounded evaluation grant required')
        require(isinstance(grant['model_fingerprints'], list) and all(map(sha, grant['model_fingerprints']))
            and model_fingerprint in grant['model_fingerprints'], 'model not included in evaluation grant')
        if not grant['starts_at'] <= self.daily.now() < grant['expires_at']:
            raise Deferred('evaluation_grant_not_current')
        proof = checked_access()
        require(isinstance(proof, dict) and proof and all(text(k) and sha(v) for k, v in proof.items()),
            'fresh authority evidence required')
        return proof

    def _reserve(self, run_id, case_id, purpose, fingerprint, request, grant, checked_access):
        require(all(map(text, (run_id, case_id))) and purpose in ('answer', 'judge'), 'explicit run, case and purpose required')
        require(sha(fingerprint), 'model fingerprint required')
        self._grant(grant, checked_access, fingerprint)
        raw = encoded(request)
        request_hash = digest(request)
        if len(raw.encode()) > grant['max_request_bytes']:
            raise Deferred('evaluation_input_too_large_no_truncation')
        key = stable_key('qa-evaluation', run_id, case_id, purpose)
        model_key = stable_key('qa-evaluation-model', key)
        with self.daily.transaction() as db:
            self._grant(grant, checked_access, fingerprint)
            old = self.get(run_id, case_id, purpose)
            if old:
                require(old['request_sha256'] == request_hash and old['model_fingerprint'] == fingerprint,
                    'evaluation request changed; use an explicit new run')
                if old['state'] == 'validated':
                    return old, False
                raise Deferred('evaluation_call_unresolved_or_rejected_no_resubmit')
            saved = db.execute('SELECT digest FROM qa_eval_grants WHERE id=?', (grant['id'],)).fetchone()
            require(saved is None or saved[0] == digest(grant), 'evaluation grant id cannot change scope or allowance')
            if db.execute('SELECT count(*) FROM qa_eval_calls WHERE grant_id=?', (grant['id'],)).fetchone()[0] >= grant['max_calls']:
                raise Deferred('evaluation_grant_limit')
            if db.execute('SELECT count(*) FROM daily_models').fetchone()[0] >= self.daily.policy.model_turn_limit:
                raise Deferred('model_limit')
            db.execute('INSERT OR IGNORE INTO qa_eval_grants VALUES (?,?)', (grant['id'], digest(grant)))
            proof = dict(purpose='qa_evaluation_'+purpose, run_id=run_id, case_id=case_id,
                grant_id=grant['id'], request_sha256=request_hash, model_fingerprint=fingerprint)
            db.execute("INSERT INTO daily_models VALUES (?,1,'started',?,?)", (model_key, encoded(proof), self.daily.now()))
            db.execute("INSERT INTO qa_eval_calls VALUES (?,?,?,?,?,?,?,?,'started',NULL,?)",
                (key, grant['id'], run_id, case_id, purpose, model_key, request_hash, fingerprint, self.daily.now()))
            self.daily._event(db, 'qa_evaluation_reserved', proof)
        return self.get(run_id, case_id, purpose), True

    def _finish(self, call, state, result):
        require(state in ('validated', 'rejected', 'unknown'), 'explicit call outcome required')
        with self.daily.transaction() as db:
            current = db.execute('SELECT state FROM qa_eval_calls WHERE call_key=?', (call['call_key'],)).fetchone()
            require(current is not None and current[0] == 'started', 'call already finished')
            db.execute('UPDATE qa_eval_calls SET state=?,result=? WHERE call_key=?', (state, encoded(result), call['call_key']))
            evidence = dict(purpose='qa_evaluation_'+call['purpose'], run_id=call['run_id'],
                case_id=call['case_id'], request_sha256=call['request_sha256'], result=result)
            db.execute('UPDATE daily_models SET state=?,evidence=?,updated=? WHERE source_sha=? AND attempt=1',
                (state, encoded(evidence), self.daily.now(), call['model_key']))

    def execute(self, *, run_id, case_id, purpose, model_fingerprint, request, grant, checked_access, adapter, validator):
        """Adapter returns output, provider_model and actual/unknown token counts.

        The adapter must perform exactly one business call and must not retry.
        Unexpected programmer errors leave a durable started intent for inspection.
        """
        require(callable(adapter) and callable(validator) and callable(checked_access), 'explicit adapter and authority required')
        grant, request = deepcopy(grant), deepcopy(request)
        call, created = self._reserve(run_id, case_id, purpose, model_fingerprint, request, grant, checked_access)
        if not created:
            saved = call['result']
            require(saved['request_sha256'] == call['request_sha256']
                and saved.get('output_sha256') == digest(saved['output']), 'saved evaluation output changed')
            validator(saved['output'])
            return dict(call['result'], cached=True, new_model_calls=0)
        start = self.daily.now()
        try:
            self._grant(grant, checked_access, model_fingerprint)
            response = adapter(deepcopy(request), lambda: self._grant(grant, checked_access, model_fingerprint))
        except (OSError, TimeoutError, Deferred, ContractError) as error:
            self._finish(call, 'unknown', dict(reason=type(error).__name__))
            failure = Deferred('evaluation_model_unknown_no_resubmit')
            failure.new_model_calls = 1
            raise failure from error
        try:
            raw = encoded(response)
            require(len(raw.encode()) <= grant['max_response_bytes'], 'evaluation response too large')
            require(isinstance(response, dict) and set(response) == {'output', 'provider_model', 'usage'}
                and text(response['provider_model']) and isinstance(response['usage'], dict)
                and set(response['usage']) == {'input_tokens', 'output_tokens'}, 'model receipt contract required')
            require(all(v is None or type(v) is int and v >= 0 for v in response['usage'].values()), 'actual or unknown usage required')
            digest(response)  # Reject non-finite/non-JSON output before persisting a success.
            output = validator(response['output'])
        except (ContractError, ValueError, TypeError, KeyError) as error:
            self._finish(call, 'rejected', dict(reason=type(error).__name__))
            failure = ContractError('evaluation response rejected; no automatic retry')
            failure.new_model_calls = 1
            raise failure from error
        result = dict(output=output, provider_model=response['provider_model'], usage=response['usage'],
            output_sha256=digest(output),
            request_sha256=call['request_sha256'], response_sha256=digest(response),
            seconds=self.daily.now()-start)
        self._finish(call, 'validated', result)
        return dict(result, cached=False, new_model_calls=1)
