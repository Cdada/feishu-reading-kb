"""Durable offline regression tasks; single worker, explicit failure, resumable cases."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import sqlite3

from .contracts import ContractError
from .daily_store import Deferred
from .locking import open_lock
from .runtime import no_symlinks
from .qa_quality import digest, require, text


class RegressionStore:
    def __init__(self, path):
        self.path = no_symlinks(Path(path).absolute())
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.connection() as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            require(not tables or 'qa_regression_tasks' in tables, 'refuse unrelated regression database')
            db.executescript('''CREATE TABLE IF NOT EXISTS qa_regression_tasks(
                key TEXT PRIMARY KEY,plan TEXT NOT NULL,state TEXT NOT NULL,reason TEXT);
                CREATE TABLE IF NOT EXISTS qa_regression_results(
                task_key TEXT NOT NULL,case_id TEXT NOT NULL,result TEXT NOT NULL,
                PRIMARY KEY(task_key,case_id));
                CREATE TABLE IF NOT EXISTS qa_regression_notifications(
                task_key TEXT PRIMARY KEY,summary_sha256 TEXT NOT NULL);''')
        self.path.chmod(0o600)

    @contextmanager
    def connection(self):
        for suffix in ('', '-journal', '-wal', '-shm'):
            no_symlinks(str(self.path)+suffix)
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA synchronous=FULL')
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, plan):
        require(isinstance(plan, dict) and plan.get('version') == 'qa-regression-plan-v1', 'regression plan required')
        require(plan.get('enabled') is True and plan.get('state') == 'ready', 'disabled or not-due plan cannot run')
        require(plan.get('automatic_model_execution') is False and plan.get('service_start') is False,
            'task cannot start model or services automatically')
        content = {k: v for k, v in plan.items() if k not in ('task_key', 'enabled', 'state', 'new_model_calls', 'service_start', 'schedules_installed')}
        require(plan.get('task_key') == digest(content), 'regression plan changed')
        raw = json.dumps(plan, ensure_ascii=False, sort_keys=True)
        with self.connection() as db:
            old = db.execute('SELECT plan FROM qa_regression_tasks WHERE key=?', (plan['task_key'],)).fetchone()
            require(old is None or old['plan'] == raw, 'task identity reused with a different plan')
            db.execute("INSERT OR IGNORE INTO qa_regression_tasks VALUES (?,?,'pending',NULL)", (plan['task_key'], raw))
        return plan['task_key']

    def get(self, key):
        with self.connection() as db:
            row = db.execute('SELECT * FROM qa_regression_tasks WHERE key=?', (key,)).fetchone()
            require(row is not None, 'unknown regression task')
            results = [json.loads(r[0]) for r in db.execute('SELECT result FROM qa_regression_results WHERE task_key=? ORDER BY case_id', (key,))]
        return dict(row, plan=json.loads(row['plan']), results=results)

    def _state(self, key, state, reason=None):
        with self.connection() as db:
            db.execute('UPDATE qa_regression_tasks SET state=?,reason=? WHERE key=?', (state, reason, key))

    def run(self, key, evaluator, *, retry_failed=False):
        """evaluator executes ONE offline case and returns an explicit status.

        Model evaluation is separate; this queue has no model adapter or grant.
        A crashed process releases the OS lock and already saved cases are reused.
        """
        require(callable(evaluator) and type(retry_failed) is bool, 'explicit offline evaluator required')
        fd = open_lock(no_symlinks(str(self.path)+'.lock'))
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise Deferred('regression_worker_busy') from error
            task = self.get(key)
            if task['state'] in ('completed', 'partial'):
                return task
            if task['state'] == 'failed' and not retry_failed:
                raise Deferred('regression_failed_requires_inspection')
            self._state(key, 'running')
            done = {r['id'] for r in task['results']}
            try:
                for case_id in task['plan']['offline_case_ids']:
                    if case_id in done:
                        continue
                    if case_id in task['plan']['stale_fixture_ids']:
                        result = dict(id=case_id, status='needs_review', reason='source_changed_rebuild_fixture', model_calls=0)
                    else:
                        result = evaluator(case_id)
                    require(isinstance(result, dict) and result.get('id') == case_id
                        and result.get('status') in ('passed', 'failed', 'needs_review', 'skipped')
                        and type(result.get('model_calls')) is int and result['model_calls'] == 0,
                        'offline result must be explicit and make zero model calls')
                    with self.connection() as db:
                        db.execute('INSERT INTO qa_regression_results VALUES (?,?,?)', (key, case_id, json.dumps(result, ensure_ascii=False, allow_nan=False)))
            except (OSError, Deferred) as error:
                self._state(key, 'deferred', type(error).__name__)
                return self.get(key)
            except Exception as error:
                # Preserve diagnostics and re-raise programmer/contract failures, never hide them.
                self._state(key, 'failed', type(error).__name__)
                raise
            results = self.get(key)['results']
            require(len(results) == len(task['plan']['offline_case_ids']), 'task results incomplete')
            self._state(key, 'partial' if any(r['status'] in ('needs_review', 'skipped') for r in results) else 'completed')
            return self.get(key)
        finally:
            os.close(fd)

    def notification(self, key):
        """Return a changed actionable event for an authorized caller; sends nothing."""
        task = self.get(key)
        payload = dict(task_key=key, state=task['state'], reason=task['reason'],
            results=[dict(id=r['id'], status=r['status']) for r in task['results']])
        if task['state'] not in ('completed', 'partial', 'failed', 'deferred'):
            return None
        fingerprint = digest(payload)
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT summary_sha256 FROM qa_regression_notifications WHERE task_key=?', (key,)).fetchone()
            if previous and previous[0] == fingerprint:
                return None
            db.execute('INSERT INTO qa_regression_notifications VALUES (?,?) ON CONFLICT(task_key) DO UPDATE SET summary_sha256=excluded.summary_sha256',
                (key, fingerprint))
        return payload
