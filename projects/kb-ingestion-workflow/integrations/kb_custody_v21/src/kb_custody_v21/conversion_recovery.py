"""Task-bound staging protection and explicit, fenced conversion recovery.

The execution lock starts before claiming a task and is inherited by each
pipeline subprocess. Recovery ends the old task before releasing only its own
leases. Unknown/legacy owners and unrelated dependencies remain protected.
"""
from contextlib import contextmanager
import fcntl
import os

from local_document_conversion_mcp.worker import ConversionWorker

from .contracts import ContractError
from .locking import open_lock
from .registry import now


TERMINAL = {'ready_candidate', 'needs_review', 'failed', 'cancelled'}
RUNNING = {'preparing', 'preprocessing', 'converting', 'quality_checking', 'cancelling'}


class ConversionRecovery:
    def __init__(self, conversion):
        self.conversion = conversion
        self.registry = conversion.registry
        self.runtime = conversion.runtime
        self.ledger = conversion.service.ledger

    @contextmanager
    def lock(self, kind, *, blocking=True):
        if kind not in {'execution', 'admission'}:
            raise ContractError('unknown conversion lock')
        path = self.runtime.checked(self.conversion.settings.ledger_path.parent / (kind + '.lock'))
        fd = open_lock(path)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            yield fd
        finally:
            os.close(fd)

    def register(self, result, inspection):
        """Called before admission unlock, while the submitter still leases bytes.

        If the process dies between ledgers, its submission lease is deliberately
        left unresolved. Do not invent ownership for that older/incomplete task.
        """
        task_id = result['task_id']
        entry = self.registry.entry(inspection['source_relpath'])
        with self.registry.transaction() as db:
            binding = db.execute('SELECT * FROM conversion_bindings WHERE task_id=?', (task_id,)).fetchone()
            if binding:
                if (binding['entry_id'], binding['asset_id'], binding['inspection_id']) != (
                        entry['entry_id'], entry['asset_id'], inspection['inspection_id']):
                    raise ContractError('conversion replay binding conflicts')
                return
            if result['idempotent_replay']:
                raise ContractError('legacy/unbound conversion requires separate reconciliation')
            db.execute('INSERT INTO conversion_bindings VALUES (?,?,?,?,?)',
                       (task_id, entry['entry_id'], entry['asset_id'], inspection['inspection_id'],
                        now() if result['status'] in TERMINAL else None))

    def bound_task(self, task_id):
        rows = self.registry.rows('SELECT * FROM conversion_bindings WHERE task_id=?', (task_id,))
        task = self.ledger.get_task(task_id)
        if not rows or not task:
            raise ContractError('task has no recoverable V2.1 ownership binding')
        binding = rows[0]
        inspection = self.ledger.get_inspection(task['inspection_id'])
        entry = self.registry.entry(binding['entry_id'])
        if (not inspection or inspection['source_kind'] != 'custody_v21'
                or inspection['inspection_id'] != binding['inspection_id']
                or inspection['source_relpath'] != binding['entry_id']
                or entry['asset_id'] != binding['asset_id']
                or inspection['sha256'] != entry['sha256']):
            raise ContractError('conversion task ownership changed')
        return task, binding

    def release_finished(self, task_id):
        """Called with the execution fence held, after durable terminal status."""
        task, binding = self.bound_task(task_id)
        if task['status'] not in TERMINAL:
            raise ContractError('unfinished task cannot release staging protection')
        with self.registry.transaction() as db:
            leases = db.execute('''SELECT l.* FROM asset_leases l JOIN conversion_lease_owners o
                ON o.lease_id=l.lease_id WHERE o.task_id=?''', (task_id,)).fetchall()
            if any(lease['asset_id'] != binding['asset_id'] for lease in leases):
                raise ContractError('task lease asset mismatch')
            stamp = now()
            db.execute('''UPDATE asset_leases SET released_at=? WHERE released_at IS NULL
                AND lease_id IN (SELECT lease_id FROM conversion_lease_owners WHERE task_id=?)''',
                       (stamp, task_id))
            db.execute('UPDATE conversion_bindings SET released_at=COALESCE(released_at,?) WHERE task_id=?',
                       (stamp, task_id))

    def recover(self, task_id, confirmation_ref):
        """End an interrupted owned task; never requeue it or clear all leases.

        The fence excludes claim/execution, including a surviving pipeline
        child. Queued work remains valid and is never cancelled by this API.
        A terminal ledger write precedes lease release, so a crash between the
        two databases can only retain protection, not prematurely remove it.
        """
        if not confirmation_ref:
            raise ContractError('explicit conversion recovery confirmation required')
        try:
            with self.lock('execution', blocking=False), self.lock('admission'):
                task, binding = self.bound_task(task_id)
                previous = task['status']
                if previous == 'queued':
                    raise ContractError('queued task is still a valid dependency')
                if previous not in TERMINAL | RUNNING:
                    raise ContractError('unknown task state; preserve protection')
                if previous in RUNNING:
                    self.ledger.finish_task(task_id, status='failed', updated_at=now(),
                        message_zh='中断任务已受控结束；原件和已有工件保留，重新转换需创建新任务',
                        error_code='CUSTODY_WORKER_INTERRUPTED')
                self.release_finished(task_id)
                with self.registry.transaction() as db:
                    self.registry.event(db, 'conversion_task_reconciled', binding['asset_id'],
                        'conversion-recovery:' + task_id,
                        {'task_id': task_id, 'previous_status': previous,
                         'confirmation_ref': confirmation_ref, 'requeued': False})
                return self.ledger.get_task(task_id)['status']
        except BlockingIOError as exc:
            raise ContractError('conversion worker or pipeline child still holds execution fence') from exc


class CustodyWorker(ConversionWorker):
    def __init__(self, conversion, **kwargs):
        super().__init__(conversion.settings, **kwargs)
        self.recovery = conversion.recovery
        self.active_task = None

    def run_once(self, task_id=None, *, blocking=True):
        # Existing adapter is a single worker. Acquire before claim so recovery
        # can never terminate a task whose old worker is waiting to start.
        with self.recovery.lock('execution', blocking=blocking) as fence:
            with self.recovery.lock('admission'):
                task = self.ledger.claim_next_task(worker_id=self.worker_id, updated_at=now(), task_id=task_id)
            if task is None:
                return False
            _, binding = self.recovery.bound_task(task['task_id'])
            if binding['released_at']:
                raise ContractError('released conversion task cannot execute again')
            self.active_task = task
            self.stage_guard_fds = (fence,)
            try:
                self._execute(task)
                self.recovery.release_finished(task['task_id'])
            finally:
                self.stage_guard_fds = ()
                self.active_task = None
            return True
