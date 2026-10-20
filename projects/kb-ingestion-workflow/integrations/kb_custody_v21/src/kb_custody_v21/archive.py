"""Offline archive plans and individually durable, manually bounded operations."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import fcntl
import json
import os
import re

from .baidu import DETAIL_ENDPOINT, RemoteFailure, RemoteMissing, ResponseValidationError, remote_path
from .contracts import ContractError, new_id, stable_key
from .registry import encoded, now
from .staging import Staging, IntegrityError, digest_fd
from .locking import open_lock

LEVELS = {'none': 0, 'metadata_checked': 1, 'content_md5_verified': 2, 'content_sha256_verified': 3}


def instant(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ContractError('timezone required')
    return result


class Archive:
    def __init__(self, registry, adapter):
        self.registry, self.runtime, self.adapter = registry, registry.runtime, adapter
        self.staging = Staging(registry)

    def register_raw(self, entry_id):
        row = self.registry.entry(entry_id)
        with self.staging.input_binding(entry_id, 'archive_registration'):
            with self.registry.transaction() as db:
                db.execute('INSERT OR IGNORE INTO cloud_asset_keys VALUES (?,?,NULL)', (row['asset_id'], row['asset_id']))
                self._location(db, row['asset_id'])
        return row['asset_id']

    def register_extraction(self, extraction_id):
        from .conversion import CustodyConversion
        row, mapping, root = CustodyConversion(self.registry).extraction(extraction_id)
        if 'parent_extraction_id' in row:
            # Expression versions keep originals in their immutable parent;
            # the rewritten local text is retained inside its Publication.
            return self.register_extraction(row['parent_extraction_id'])
        inventory = json.loads(row['inventory_json'])
        entries = [dict(asset_id=row['output_asset_id'], relative_path='source.md',
                        sha256=mapping['output_sha256'], size_bytes=(root / 'source.md').stat().st_size,
                        kind='extracted_text')]
        entries += [{**i, 'kind': 'extracted_image'} for i in inventory if i['role'] == 'extracted_original']
        with self.registry.transaction() as db:
            for item in entries:
                path = root / item['relative_path']
                fd = self.runtime.read_fd(path)
                try:
                    evidence = digest_fd(fd)
                finally:
                    os.close(fd)
                if evidence['sha256'] != item['sha256'] or evidence['size_bytes'] != item['size_bytes']:
                    raise ContractError('derived original identity changed')
                values = (item['asset_id'], row['source_asset_id'], extraction_id, item['kind'],
                          path.name, self.runtime.relative(path), evidence['size_bytes'],
                          evidence['sha256'], evidence['md5'], now())
                db.execute('INSERT OR IGNORE INTO derived_assets VALUES (?,?,?,?,?,?,?,?,?,?)', values)
                db.execute('INSERT OR IGNORE INTO cloud_asset_keys VALUES (?,NULL,?)', (item['asset_id'], item['asset_id']))
                self._location(db, item['asset_id'])
                for publication in db.execute('SELECT publication_id FROM publications_v21 WHERE source_asset_id=?', (row['source_asset_id'],)).fetchall():
                    db.execute("INSERT OR IGNORE INTO asset_references VALUES (?,?,'publication',NULL)", (item['asset_id'], publication[0]))
        return [item['asset_id'] for item in entries]

    def _location(self, db, asset_id):
        old = db.execute('SELECT * FROM asset_locations WHERE asset_id=?', (asset_id,)).fetchone()
        if old and (old['account_ref'], old['root_path']) != (self.adapter.account_ref, self.adapter.root):
            raise ContractError('account/root binding immutable in this registry namespace')
        db.execute('INSERT OR IGNORE INTO asset_locations(asset_id,provider,account_ref,root_path,updated_at) VALUES (?,?,?,?,?)',
                   (asset_id, 'baidu_netdisk', self.adapter.account_ref, self.adapter.root, now()))

    def identity(self, asset_id):
        rows = self.registry.rows('SELECT * FROM archive_asset_identity WHERE asset_id=?', (asset_id,))
        if len(rows) != 1:
            raise ContractError('unique managed asset required')
        return rows[0]

    def location(self, asset_id):
        rows = self.registry.rows('SELECT * FROM asset_locations WHERE asset_id=?', (asset_id,))
        if not rows:
            raise ContractError('unregistered cloud asset')
        return rows[0]

    @contextmanager
    def binding(self, asset_id, purpose):
        item = self.identity(asset_id)
        if item['entry_id']:
            with self.staging.input_binding(item['entry_id'], purpose) as binding:
                yield binding['stream']
            return
        # Derived bytes are immutable and are never staging-cleanup targets.
        lease = new_id('lease')
        fd = self.runtime.read_fd(self.runtime.path(item['relative_path']))
        try:
            if digest_fd(fd) != {k: item[k] for k in ('size_bytes', 'sha256', 'md5')}:
                raise ContractError('derived asset changed')
            os.lseek(fd, 0, os.SEEK_SET)
            with self.registry.transaction() as db:
                db.execute('INSERT INTO derived_leases VALUES (?,?,?,?,NULL)', (lease, asset_id, purpose, now()))
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                yield stream
        finally:
            os.close(fd)
            with self.registry.transaction() as db:
                db.execute('UPDATE derived_leases SET released_at=? WHERE lease_id=?', (now(), lease))

    def plan(self, asset_id, operation='upload', target=None, batch_id=None, dependencies=()):
        if operation not in ('upload', 'verify', 'recover', 'rename', 'mkdir'):
            raise ContractError('unsupported archive operation')
        identity, location = self.identity(asset_id), self.location(asset_id)
        target = target or location['current_path'] or (
            location['planned_path'] if operation == 'recover' else None
        ) or self.adapter.root + '/' + asset_id + '-' + identity['filename']
        remote_path(target, self.adapter.root)
        if operation == 'upload' and location['fs_id']:
            raise ContractError('existing location: verify/recover instead of re-upload')
        if operation == 'upload' and self.registry.rows("SELECT 1 FROM archive_operations WHERE json_extract(plan_json,'$.asset_id')=? AND json_extract(plan_json,'$.operation')='upload' AND state IN ('running','unknown')", (asset_id,)):
            raise ContractError('unknown prior upload must be recovered before planning any new upload')
        if operation in ('verify', 'rename') and not location['fs_id']:
            raise ContractError('operation requires an existing locator')
        if operation == 'recover' and not location['planned_path'] and not location['fs_id']:
            raise ContractError('no prior upload intent to recover')
        data = dict(version='archive-plan/v2.1', asset_id=asset_id, operation=operation, target=target,
            account_ref=self.adapter.account_ref, root=self.adapter.root, revision=location['registry_revision'],
            old_fs_id=location['fs_id'], old_path=location['current_path'],
            expected={k: identity[k] for k in ('size_bytes', 'sha256', 'md5')}, dependencies=list(dependencies))
        for dep in dependencies:
            if not self.registry.rows('SELECT 1 FROM archive_operations WHERE operation_id=?', (dep,)):
                raise ContractError('unknown operation dependency')
        # A replay of exactly the same plan returns the original operation;
        # unknown/running results cannot be avoided by requesting a new batch.
        oid = 'operation_' + stable_key(data)
        with self.registry.transaction() as db:
            db.execute('INSERT OR IGNORE INTO archive_operations(operation_id,batch_id,plan_json) VALUES (?,?,?)',
                       (oid, batch_id or new_id('batch'), encoded(data)))
        return oid

    def operation(self, operation_id):
        rows = self.registry.rows('SELECT * FROM archive_operations WHERE operation_id=?', (operation_id,))
        if not rows:
            raise ContractError('unknown operation')
        return rows[0]

    def reference(self, asset_id, reference_id, kind='publication', release=False):
        if kind not in ('publication', 'staging_dependency') or not reference_id:
            raise ContractError('invalid reference')
        with self.registry.transaction() as db:
            db.execute('INSERT OR IGNORE INTO asset_references VALUES (?,?,?,NULL)', (asset_id, reference_id, kind))
            if release:
                db.execute('UPDATE asset_references SET released_at=? WHERE asset_id=? AND reference_id=? AND kind=?',
                           (now(), asset_id, reference_id, kind))

    def session(self, confirmation_ref, operation_ids, expires_at, max_calls, *, starts_at=None, physical_limits=None):
        return ArchiveSession(self, confirmation_ref, operation_ids, expires_at, max_calls,
                              starts_at=starts_at, physical_limits=physical_limits)


class ArchiveSession:
    def __init__(self, archive, confirmation_ref, operation_ids, expires_at, max_calls, *, starts_at=None, physical_limits=None):
        if not confirmation_ref or not operation_ids or type(max_calls) is not int or max_calls < 1:
            raise ContractError('specific confirmation, operations, and physical call budget required')
        self.archive, self.registry, self.adapter = archive, archive.registry, archive.adapter
        self.ref, self.allowed, self.expires, self.max_calls = confirmation_ref, set(operation_ids), instant(expires_at), max_calls
        self.starts = instant(starts_at) if starts_at is not None else None
        if self.starts is not None and self.starts >= self.expires:
            raise ContractError('window start must precede expiry')
        if physical_limits is not None and (not isinstance(physical_limits, dict) or not physical_limits
                or any(not isinstance(k, str) or not re.fullmatch('[a-z][a-z0-9_]{0,63}', k)
                       or type(v) is not int or not 0 <= v <= max_calls for k, v in physical_limits.items())
                or sum(physical_limits.values()) < max_calls):
            raise ContractError('explicit physical method limits must cover the total budget')
        self.policy = encoded(dict(starts_at=starts_at, physical_limits=physical_limits))
        self.policy_key = 'archive-window-policy:' + stable_key(confirmation_ref)
        self.calls, self.current, self.stopped = 0, None, False
        with self.registry.transaction() as db:
            scope = encoded(sorted(self.allowed))
            previous = db.execute('SELECT * FROM archive_windows WHERE confirmation_ref=?', (self.ref,)).fetchone()
            if previous and (previous['scope_json'], previous['expires_at'], previous['max_calls']) != (scope, expires_at, max_calls):
                raise ContractError('window scope/budget immutable; requires new explicit authorization')
            old_policy = db.execute('SELECT payload_json FROM custody_events WHERE dedupe_key=?', (self.policy_key,)).fetchone()
            if old_policy and old_policy[0] != self.policy:
                raise ContractError('window start/method limits immutable; requires new explicit authorization')
            if previous and not old_policy and previous['used_calls'] and (starts_at is not None or physical_limits is not None):
                raise ContractError('cannot retrofit new limits to an already used legacy window')
            db.execute('INSERT OR IGNORE INTO archive_windows(confirmation_ref,scope_json,expires_at,max_calls) VALUES (?,?,?,?)',
                       (self.ref, scope, expires_at, max_calls))
            self.registry.event(db, 'archive_window_policy', self.ref, self.policy_key, json.loads(self.policy))
        self.check()

    def check(self):
        current = datetime.now(timezone.utc)
        if self.stopped or current >= self.expires:
            raise ContractError('manual window expired/stopped')
        if self.starts is not None and current < self.starts:
            raise ContractError('manual window has not started')
        if self.registry.rows('SELECT stopped FROM archive_windows WHERE confirmation_ref=?', (self.ref,))[0]['stopped']:
            raise ContractError('manual window stopped')

    def request(self, method, payload):
        transport = self.adapter.transport
        if getattr(transport, 'physical_request_composition', False) is True:
            return transport.compose(method, payload, self.physical_request)
        return self.physical_request(method, lambda: transport.request(method, payload))

    def physical_request(self, method, perform):
        # Compound protocol operations must reserve each HTTP attempt separately.
        # Persist only the operation label, never OAuth/query/body credentials.
        self.check()
        if self.current is None:
            raise ContractError('current archive operation required for physical request')
        attempt = new_id('attempt')
        with self.registry.transaction() as db:
            # Recheck time after acquiring SQLite's transaction lock. A caller
            # may have waited for another process beyond the approved window.
            current = datetime.now(timezone.utc)
            if current >= self.expires or (self.starts is not None and current < self.starts):
                raise ContractError('manual window outside approved time')
            limits = json.loads(self.policy)['physical_limits']
            if limits is not None:
                used = db.execute("SELECT count(*) FROM custody_events WHERE event_type='archive_http_reserved' AND aggregate_id=? AND json_extract(payload_json,'$.method')=?",
                                  (self.ref, method)).fetchone()[0]
                if method not in limits or used >= limits[method]:
                    raise ContractError('physical method budget exhausted or not authorized')
            if db.execute('UPDATE archive_windows SET used_calls=used_calls+1 WHERE confirmation_ref=? AND used_calls<max_calls AND stopped=0', (self.ref,)).rowcount != 1:
                raise ContractError('physical request budget exhausted')
            db.execute('INSERT INTO archive_attempts VALUES (?,?,?,?,NULL,?)',
                       (attempt, self.current, method, now(), 'started'))
            self.registry.event(db, 'archive_http_reserved', self.ref, attempt + ':reserved',
                                {'attempt_id': attempt, 'method': method})
        self.calls += 1
        try:
            result = perform()
        except (OSError, ContractError, IntegrityError) as exc:
            self.stopped = True
            # A ContractError alone says nothing about receiving a response.
            # Only the executor's recorded receipt can supply that distinction.
            outcome = ('integrity_error' if isinstance(exc, IntegrityError) else
                       'response_rejected' if isinstance(exc, ResponseValidationError) else 'unknown')
            with self.registry.transaction() as db:
                db.execute("UPDATE archive_attempts SET finished_at=?,outcome=? WHERE attempt_id=?",
                           (now(), outcome, attempt))
                db.execute('UPDATE archive_windows SET stopped=1 WHERE confirmation_ref=?', (self.ref,))
            raise
        with self.registry.transaction() as db:
            db.execute("UPDATE archive_attempts SET finished_at=?,outcome='response' WHERE attempt_id=?", (now(), attempt))
        return result

    def execute(self, operation_id):
        plan = json.loads(self.archive.operation(operation_id)['plan_json'])
        path = self.archive.runtime.checked(self.archive.runtime.root / 'cloud-assets' / (plan['asset_id'] + '.archive.lock'))
        fd = open_lock(path)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.adapter.bound_request(self.request):
                return self._execute(operation_id)
        finally:
            os.close(fd)

    def _execute(self, operation_id):
        self.check()
        if operation_id not in self.allowed:
            raise ContractError('operation outside confirmed scope')
        operation = self.archive.operation(operation_id)
        if operation['state'] == 'succeeded':
            return json.loads(operation['result_json'])
        if operation['state'] != 'planned':
            raise ContractError('uncertain/failed operation requires explicit recovery; no blind retry')
        plan = json.loads(operation['plan_json'])
        for dep in plan['dependencies']:
            if self.archive.operation(dep)['state'] != 'succeeded':
                raise ContractError('operation dependency incomplete')
        aid, kind = plan['asset_id'], plan['operation']
        if (plan['account_ref'], plan['root']) != (self.adapter.account_ref, self.adapter.root):
            raise ContractError('plan account/root mismatch')
        if plan['expected'] != {k: self.archive.identity(aid)[k] for k in ('size_bytes', 'sha256', 'md5')}:
            raise ContractError('asset identity changed')
        # A conditional claim prevents two windows sending the same operation.
        with self.registry.transaction() as db:
            loc = db.execute('SELECT * FROM asset_locations WHERE asset_id=?', (aid,)).fetchone()
            # Plans may predate a crash. Recheck the asset-wide intent inside
            # the same transaction as claiming this operation, before any I/O.
            if kind == 'upload':
                unresolved = db.execute("""SELECT 1 FROM archive_operations
                    WHERE json_extract(plan_json,'$.asset_id')=?
                    AND json_extract(plan_json,'$.operation')='upload'
                    AND state IN ('running','unknown')""", (aid,)).fetchone()
                if unresolved:
                    raise ContractError('unresolved upload must be recovered before executing another plan')
                if loc['fs_id']:
                    raise ContractError('existing location: verify/recover instead of re-upload')
            if loc['registry_revision'] != plan['revision']:
                raise ContractError('stale index plan: refresh/replan')
            if db.execute("UPDATE archive_operations SET state='running',started_at=? WHERE operation_id=? AND state='planned'",
                          (now(), operation_id)).rowcount != 1:
                raise ContractError('operation already claimed')
            if kind == 'upload':
                # Attribute the attempted upload before network I/O, including
                # unknown failures or process death before final verification.
                db.execute("UPDATE asset_locations SET planned_path=?,archive_status='uploading',validation_mode=? WHERE asset_id=?",
                           (plan['target'], self.adapter.validation_mode, aid))
        self.current = operation_id
        try:
            pre = self.adapter.refresh(plan['target'])
            if kind in ('upload', 'mkdir', 'rename') and pre.get('target_exists') is not False:
                raise RemoteFailure('target conflict or unknown existence')
            if kind == 'upload':
                if pre['free_bytes'] < plan['expected']['size_bytes']:
                    raise RemoteFailure('insufficient capacity')
                with self.archive.binding(aid, 'archive_upload') as stream:
                    created = self.adapter.upload(stream, self.archive.identity(aid), plan['target'])
                item = self.adapter.detail(created['fs_id'])
                if item['path'] != plan['target']:
                    raise RemoteFailure('actual path differs from upload intent')
            elif kind == 'rename':
                before = self.adapter.detail(plan['old_fs_id'])
                if before['path'] != plan['old_path']:
                    raise RemoteFailure('locator changed: replan required')
                item = self.adapter.rename(plan['old_fs_id'], plan['old_path'], plan['target'])
                if item['path'] != plan['target']:
                    raise RemoteFailure('move not completed at target')
            elif kind == 'recover':
                loc = self.archive.location(aid)
                item = None
                if loc['fs_id']:
                    try:
                        item = self.adapter.detail(loc['fs_id'])
                    except RemoteMissing:
                        pass  # Explicit read-only fallback, operation records every attempt.
                if item is None:
                    candidates = self.adapter.candidates(loc, plan['target'])
                    if len(candidates) != 1:
                        raise RemoteFailure('recovery has no unique candidate; writes stopped')
                    item = candidates[0]
            elif kind == 'mkdir':
                result = self.adapter.mkdir(plan['target'])
                self._finish(operation_id, result)
                return result
            else:
                item = self.adapter.detail(plan['old_fs_id'])
            result = self.verify(aid, item, force_sha=kind == 'recover' or (kind == 'verify' and item['path'] != plan['old_path']))
            self._finish(operation_id, result)
            return result
        except (OSError, ContractError, IntegrityError) as exc:
            self.stopped = True
            uncertain = kind in ('upload', 'rename', 'mkdir') or isinstance(exc, OSError)
            with self.registry.transaction() as db:
                if isinstance(exc, IntegrityError):
                    # Durable write intent is conservative evidence of possible
                    # remote effects, even when no reliable reply reached us.
                    # Read-only preflight failure does not imply an upload.
                    uncertain = db.execute("""SELECT 1 FROM archive_attempts
                        WHERE operation_id=? AND method IN
                        ('precreate','slice','create','mkdir','rename') LIMIT 1""",
                        (operation_id,)).fetchone() is not None
                db.execute('UPDATE archive_operations SET state=?,finished_at=?,error_code=? WHERE operation_id=?',
                    ('unknown' if uncertain else 'failed', now(), type(exc).__name__, operation_id))
                db.execute("UPDATE asset_locations SET availability='unknown',integrity_hold=1,updated_at=?,registry_revision=registry_revision+1 WHERE asset_id=?", (now(), aid))
                db.execute('UPDATE archive_windows SET stopped=1 WHERE confirmation_ref=?', (self.ref,))
            raise
        finally:
            self.current = None

    def _finish(self, operation_id, result):
        with self.registry.transaction() as db:
            db.execute("UPDATE archive_operations SET state='succeeded',finished_at=?,result_json=? WHERE operation_id=?",
                       (now(), encoded(result), operation_id))
            self.registry.event(db, 'archive_operation_completed', operation_id, operation_id + ':completed',
                                {'confirmation_ref': self.ref, 'validation_mode': self.adapter.validation_mode})

    def verify(self, aid, item, force_sha=False):
        expected = self.archive.identity(aid)
        capability = self.adapter.md5_evidence or {}
        qualified = (capability.get('endpoint') == DETAIL_ENDPOINT and capability.get('kind') == 'content_md5'
                     and self.adapter.validation_mode == 'offline_mock'
                     and capability.get('validation_mode') == 'offline_mock' and bool(capability.get('evidence_ref')))
        size_ok, md5_ok = item['size'] == expected['size_bytes'], item['md5'] == expected['md5']
        semantic = 'content_md5' if qualified else 'unknown'
        level, hold = 'metadata_checked', 0
        if size_ok and md5_ok and qualified and not force_sha:
            level = 'content_md5_verified'
        else:
            size, digest = 0, hashlib.sha256()
            with self.adapter.stream(item['fs_id']) as stream:
                while data := stream.read(128 * 1024):
                    size += len(data)
                    if size > expected['size_bytes']:
                        raise RemoteFailure('download exceeds expected size')
                    digest.update(data)
            if size != expected['size_bytes'] or digest.hexdigest() != expected['sha256']:
                raise RemoteFailure('streamed content does not match original')
            # Re-read after stream. Persistent contradiction remains a hold even
            # though the independently streamed bytes passed SHA-256.
            refreshed = self.adapter.detail(item['fs_id'])
            if refreshed['path'] != item['path']:
                raise RemoteFailure('locator changed during content verification')
            item = refreshed
            level = 'content_sha256_verified'
            hold = int(item['size'] != size or (qualified and item['md5'] != expected['md5']))
        timestamp = now()
        with self.registry.transaction() as db:
            db.execute('''UPDATE asset_locations SET fs_id=?,path_snapshot=coalesce(path_snapshot,?),current_path=?,
                remote_size_bytes=?,cloud_md5=?,cloud_md5_kind=?,cloud_md5_endpoint=?,semantic_evidence=?,
                archive_status=?,availability='present',verification_level=?,archived_at=coalesce(archived_at,?),
                last_verified_at=?,last_seen_at=?,first_content_verified_at=coalesce(first_content_verified_at,?),
                integrity_hold=?,registry_revision=registry_revision+1,updated_at=? WHERE asset_id=?''',
                (item['fs_id'], item['path'], item['path'], item['size'], item['md5'], semantic, DETAIL_ENDPOINT,
                 capability.get('evidence_ref'), 'cloud_archived_unverified' if hold else 'cloud_archived_verified',
                 level, timestamp, timestamp, timestamp, None if hold else timestamp, hold, timestamp, aid))
        return {'asset_id': aid, 'fs_id': item['fs_id'], 'verification_level': level,
                'integrity_hold': bool(hold), 'validation_mode': self.adapter.validation_mode}
