"""Persistent, serial Pilot intake using the existing normalized gateway contract.

This is an offline composition boundary, not a platform authenticator. A live
adapter must verify envelopes and budget downloads separately. No sends, model
calls, cloud archive, automatic duplicate choice or interrupted-task recovery.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil

from .contracts import ContractError, stable_key, validate_id
from .conversion import CustodyConversion
from .conversion_recovery import RUNNING
from .intake import normalized_message_contract
from .locking import open_lock
from .pilot_preview import PilotScope, _identity
from .registry import encoded, now
from .staging import Staging


class PilotIntake:
    def __init__(self, registry, scope: PilotScope, *, max_attachments, max_pending):
        if any(type(v) is not int or v < 1 for v in (max_attachments, max_pending)):
            raise ContractError('explicit positive Pilot queue limits required')
        self.registry, self.scope = registry, scope
        self.runtime = registry.runtime
        self.max_attachments, self.max_pending = max_attachments, max_pending
        self.staging = Staging(registry)
        self.conversion = CustodyConversion(registry)
        with registry.transaction() as db:
            db.execute('INSERT OR IGNORE INTO pilot_intake_scope VALUES (1,?,?,?)',
                       (scope.app_id, scope.chat_id, scope.user_id))
            row = db.execute('SELECT * FROM pilot_intake_scope WHERE singleton=1').fetchone()
            if tuple(row[k] for k in ('app_id', 'chat_id', 'user_id')) != (scope.app_id, scope.chat_id, scope.user_id):
                raise ContractError('Pilot runtime is already bound to another identity scope')

    @contextmanager
    def _serial(self):
        self.runtime.gate()
        fd = open_lock(self.runtime.checked(self.runtime.root / 'conversion-work/pilot-controller.lock'))
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)

    def _contracts(self, message):
        if not isinstance(message, dict) or (
                message.get('appId'), message.get('chatId'), message.get('senderId'), message.get('chatType')) != (
                self.scope.app_id, self.scope.chat_id, self.scope.user_id, 'group'):
            raise ContractError('Pilot requires the configured application, group and person')
        _identity(message.get('messageId'))
        resources = message.get('resources')
        if not isinstance(resources, list) or not 1 <= len(resources) <= self.max_attachments:
            raise ContractError('Pilot attachment count outside configured limit')
        group = message.get('sourceGroup')
        if group is not None:
            if not isinstance(group, dict) or set(group) != {'source_group_id','membership_revision','required_indices'}:
                raise ContractError('invalid explicit Pilot source group')
            indices = group['required_indices']
            if (not isinstance(indices, list) or not indices
                    or any(type(i) is not int or not 0 <= i < len(resources) for i in indices)
                    or len(set(indices)) != len(indices)):
                raise ContractError('invalid required attachment indices')
        contracts = []
        for i, resource in enumerate(resources):
            if not isinstance(resource, dict):
                raise ContractError('invalid attachment')
            _identity(resource.get('fileKey'))
            size = resource.get('sizeBytes')
            if size is not None and (type(size) is not int or not 0 <= size <= self.runtime.config['max_asset_bytes']):
                raise ContractError('Pilot attachment size outside DEV limit')
            membership = None if group is None else dict(source_group_id=group['source_group_id'],
                membership_revision=group['membership_revision'], required=i in group['required_indices'])
            contracts.append(normalized_message_contract(message, i, membership))
        # Freeze only fields with processing meaning. Provider redelivery envelope
        # timestamps/irrelevant text must not create a second job for the same file.
        envelope = encoded(dict(app_id=self.scope.app_id, user_id=self.scope.user_id, contracts=contracts))
        return stable_key('pilot-message', self.scope.app_id, self.scope.chat_id, message['messageId']), envelope, contracts

    def prepare(self, message):
        """Register the complete attachment list before the first download.

        A crash during individual registrations is repairable from the exact
        frozen envelope. Changed order/membership/identity is never a new plan.
        """
        key, envelope, contracts = self._contracts(message)
        with self._serial():
            with self.registry.transaction() as db:
                old = db.execute('SELECT * FROM pilot_intake_messages WHERE message_key=?', (key,)).fetchone()
                if old and old['envelope_json'] != envelope:
                    raise ContractError('Pilot message replay changes frozen attachments')
                if old is None:
                    pending = db.execute('SELECT count(*) FROM pilot_conversion_jobs WHERE task_id IS NULL AND stop_reason IS NULL').fetchone()[0]
                    # Include incomplete preparations so process death cannot bypass admission.
                    pending += sum(max(0, len(json.loads(r['envelope_json'])['contracts']) - r['registered'])
                        for r in db.execute('''SELECT m.envelope_json,count(j.entry_id) AS registered
                          FROM pilot_intake_messages m LEFT JOIN pilot_conversion_jobs j USING(message_key)
                          GROUP BY m.message_key'''))
                    if pending + len(contracts) > self.max_pending:
                        raise ContractError('Pilot queue full; attachments have not been accepted')
                    db.execute('INSERT INTO pilot_intake_messages VALUES (?,?,?)', (key, envelope, now()))
            group = contracts[0].get('group')
            if group:
                self.registry.create_group(group['source_group_id'], group['membership_revision'])
            for i, contract in enumerate(contracts):
                entry = self.registry.register(contract)
                with self.registry.transaction() as db:
                    db.execute('''INSERT OR IGNORE INTO pilot_conversion_jobs
                        (entry_id,message_key,resource_index,request_key) VALUES (?,?,?,?)''',
                        (entry['entry_id'], key, i, 'pilot-' + stable_key(key, i)))
            if group:
                self.registry.seal_group(group['source_group_id'], group['membership_revision'])
            return self.registry.rows('SELECT entry_id FROM pilot_conversion_jobs WHERE message_key=? ORDER BY resource_index', (key,))

    def receive(self, message, index, source_factory):
        entries = self.prepare(message)
        if type(index) is not int or not 0 <= index < len(entries):
            raise ContractError('invalid Pilot resource index')
        # Staging holds the existing asset lock and skips transport on accepted replay.
        return self.staging.ingest(entries[index]['entry_id'], source_factory)

    def _job(self, entry_id):
        rows = self.registry.rows('SELECT * FROM pilot_conversion_jobs WHERE entry_id=?', (entry_id,))
        if not rows:
            raise ContractError('entry is not a Pilot job')
        return rows[0]

    def status(self, entry_id):
        job = self._job(entry_id)
        receipt = self.registry.receipt(entry_id)
        task = self.conversion.service.ledger.get_task(job['task_id']) if job['task_id'] else None
        state = job['stop_reason'] or ('waiting_conversion' if receipt['source_staging_status']=='accepted' else 'waiting_original')
        if task:
            state = 'needs_recovery' if task['status'] in RUNNING else task['status']
        if receipt['custody_health'] != 'healthy' and receipt['source_staging_status']=='accepted':
            state = 'integrity_hold'
        return dict(entry_id=entry_id, original_status=receipt['source_staging_status'],
                    custody_health=receipt['custody_health'], processing_state=state,
                    task_id=job['task_id'], publication_state='NOT_EVALUATED', notification_state='NOT_DISPATCHED_BY_PILOT')

    def work_guarded_once(self, *, resources=None, entry_id=None, duplicate_selection=None):
        """Live composition entry: query actual host resources at admission boundaries.

        Existing work_once remains the offline composition primitive. New live
        executors must use this method; old frozen executors are never rewritten.
        Supplying resources is for controlled offline tests, not inbound messages.
        """
        from .pilot_resources import PilotResources
        gate = resources if resources is not None else PilotResources(
            self.registry, minimum_free_bytes=max(5 * 1024**3, self.conversion.settings.minimum_free_bytes))
        return self.work_once(admission=gate.check, entry_id=entry_id, duplicate_selection=duplicate_selection)

    def work_once(self, *, admission=None, entry_id=None, duplicate_selection=None):
        """Admit and execute at most one known local conversion, with no retries.

        Queue state comes from the existing conversion ledger. Lost submit results
        reuse their fixed idempotency key; missing ownership requires reconciliation.
        The existing worker fence excludes live workers and surviving children.
        """
        if entry_id is not None:
            validate_id(entry_id, 'entry')
            self._job(entry_id)
        if duplicate_selection is not None:
            from .pilot_duplicate import validate_selection
            validate_selection(duplicate_selection)
            if entry_id is None:
                raise ContractError('duplicate selection must target one explicit entry')
        try:
            with self._serial():
                if admission:
                    capacity = admission()
                    if capacity['state'] != 'resources_ready':
                        return capacity
                self.runtime.check_storage()
                if shutil.disk_usage(self.runtime.root).free < self.conversion.settings.minimum_free_bytes:
                    return dict(state='paused_capacity')
                jobs = self.registry.rows('SELECT * FROM pilot_conversion_jobs' +
                    (' WHERE entry_id=?' if entry_id is not None else '') + ' ORDER BY rowid',
                    (entry_id,) if entry_id is not None else ())
                for job in jobs:
                    eid = job['entry_id']
                    entry = self.registry.entry(eid)
                    if entry['status'] != 'accepted' or entry['custody_health'] != 'healthy' or job['stop_reason']:
                        continue
                    # Do not inspect a partial preparation or an incomplete required group.
                    message = self.registry.rows('SELECT envelope_json FROM pilot_intake_messages WHERE message_key=?', (job['message_key'],))[0]
                    count = len(self.registry.rows('SELECT entry_id FROM pilot_conversion_jobs WHERE message_key=?', (job['message_key'],)))
                    if count != len(json.loads(message['envelope_json'])['contracts']):
                        continue
                    memberships = self.registry.rows('''SELECT g.membership_sealed,m.source_group_id,m.membership_revision
                        FROM source_group_memberships m JOIN source_groups g USING(source_group_id,membership_revision)
                        WHERE m.entry_id=?''', (eid,))
                    if any(not g['membership_sealed'] or self.registry.rows('''SELECT 1 FROM source_group_memberships m
                        JOIN staging_receipts r USING(entry_id) JOIN staging_files f ON f.asset_id=r.asset_id
                        WHERE m.source_group_id=? AND m.membership_revision=? AND m.required=1
                        AND (r.status!='accepted' OR f.custody_health!='healthy')''',
                        (g['source_group_id'],g['membership_revision'])) for g in memberships):
                        continue
                    if Path(entry['safe_filename']).suffix.lower() not in {'.md','.txt','.webarchive','.pdf','.zip'}:
                        self._stop(eid, 'custody_only')
                        continue
                    existing = self.conversion.service.ledger.get_task_by_idempotency(
                        requested_by='v21-dev', idempotency_key=job['request_key'])
                    if existing:
                        # This also checks content identity and the durable ownership binding.
                        _, binding = self.conversion.recovery.bound_task(existing['task_id'])
                        if binding['entry_id'] != eid or binding['inspection_id'] != job['inspection_id']:
                            raise ContractError('Pilot conversion ownership conflict')
                        if existing['status'] != 'queued':
                            self._bind(eid, existing['task_id'])
                            continue
                    else:
                        if job['task_id']:
                            raise ContractError('Pilot conversion task disappeared')
                        inspection = self.conversion.service.ledger.get_inspection(job['inspection_id']) if job['inspection_id'] else None
                        # Refresh only local preflight, only after proving no task
                        # exists for the frozen key. Never renew a live API window.
                        if (duplicate_selection is not None or not inspection
                                or datetime.fromisoformat(inspection['expires_at']) <= datetime.now(timezone.utc)):
                            if admission:
                                capacity = admission()
                                if capacity['state'] != 'resources_ready':
                                    return capacity
                            result = self.conversion.inspect(eid)
                            with self.registry.transaction() as db:
                                db.execute('UPDATE pilot_conversion_jobs SET inspection_id=? WHERE entry_id=?', (result['inspection_id'],eid))
                            inspection = self.conversion.service.ledger.get_inspection(result['inspection_id'])
                        action = 'not_applicable'
                        if inspection['duplicate']['requires_confirmation']:
                            if duplicate_selection is None:
                                self._stop(eid, 'duplicate_needs_review')
                                continue
                            from .pilot_duplicate import selected_action
                            action = selected_action(self, entry, inspection, duplicate_selection)
                        elif duplicate_selection is not None:
                            raise ContractError('expected prior conversion is absent; selection cannot be reused')
                        # submit rejects stale inspections and preserves uncertain leases.
                        result = self.conversion.submit(inspection['inspection_id'], job['request_key'], duplicate_action=action)
                        existing = self.conversion.service.ledger.get_task(result['task_id'])
                    self._bind(eid, existing['task_id'])
                    if admission:
                        capacity = admission()
                        if capacity['state'] != 'resources_ready':
                            return dict(capacity, entry_id=eid, task_id=existing['task_id'])
                    self.conversion.worker.run_once(task_id=existing['task_id'], blocking=False)
                    return self.status(eid)
                return dict(state='idle', known_jobs=len(jobs), historical_completeness='NOT_PROVEN')
        except BlockingIOError:
            return dict(state='busy')

    def _bind(self, entry_id, task_id):
        with self.registry.transaction() as db:
            old = db.execute('SELECT task_id FROM pilot_conversion_jobs WHERE entry_id=?', (entry_id,)).fetchone()[0]
            if old and old != task_id:
                raise ContractError('Pilot task binding cannot be replaced')
            db.execute('UPDATE pilot_conversion_jobs SET task_id=? WHERE entry_id=?', (task_id,entry_id))

    def _stop(self, entry_id, reason):
        with self.registry.transaction() as db:
            db.execute('UPDATE pilot_conversion_jobs SET stop_reason=? WHERE entry_id=?', (reason,entry_id))
