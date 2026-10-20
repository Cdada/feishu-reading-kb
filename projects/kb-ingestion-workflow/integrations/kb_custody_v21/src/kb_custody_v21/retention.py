"""Index recovery, manual staging cleanup and independent cache lifecycle.

Destructive operations are limited to disposable DEV test-output runtimes.
A content verification receipt from a mock can never qualify a real original.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from .archive import LEVELS, instant
from .contracts import ContractError, new_id, stable_key
from .conversion import durable_new
from .registry import encoded, now
from .staging import digest_fd, write_all
from .locking import open_lock


def file_sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


class IndexBackup:
    def __init__(self, registry):
        self.registry, self.runtime = registry, registry.runtime

    def create(self):
        bid = new_id('backup')
        path = self.runtime.checked(self.runtime.root / 'cloud-assets/exports' / (bid + '.sqlite3'))
        export = path.with_suffix('.json')
        # sqlite3.backup captures committed WAL pages; never copy the live DB.
        with self.registry.connect() as source, sqlite3.connect(path) as destination:
            source.backup(destination)
        os.chmod(path, 0o600)
        with sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True) as snapshot:
            snapshot.row_factory = sqlite3.Row
            if snapshot.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or snapshot.execute('PRAGMA foreign_key_check').fetchall():
                raise ContractError('backup integrity check failed')
            tables = ('asset_locations', 'assets', 'derived_assets', 'cloud_asset_keys', 'archive_operations', 'asset_references')
            payload = {t: [dict(r) for r in snapshot.execute('SELECT * FROM ' + t)] for t in tables}
        payload.update(format_version='cloud-index-export/v2.1', created_at=now(), backup_id=bid)
        durable_new(export, encoded(payload).encode())
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        revisions = {r['asset_id']: dict(fs_id=r['fs_id'], current_path=r['current_path'],
                      registry_revision=r['registry_revision']) for r in payload['asset_locations']}
        with self.registry.transaction() as db:
            db.execute('INSERT INTO index_backups VALUES (?,?,?,?,?,?,?)', (bid,
                self.runtime.relative(path), file_sha(path), self.runtime.relative(export),
                file_sha(export), now(), encoded(revisions)))
        return bid

    def verify(self, backup_id):
        rows = self.registry.rows('SELECT * FROM index_backups WHERE backup_id=?', (backup_id,))
        if not rows:
            raise ContractError('unknown backup')
        row = rows[0]
        path = self.runtime.checked(self.runtime.path(row['relative_path']))
        export = self.runtime.checked(self.runtime.root / row['export_path'])
        if file_sha(path) != row['sha256'] or file_sha(export) != row['export_sha256']:
            raise ContractError('index backup bytes changed')
        with sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True) as db:
            if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or db.execute('PRAGMA foreign_key_check').fetchall():
                raise ContractError('index backup not recoverable')
        return row

    def restore_to_new_runtime(self, backup_id, target_runtime):
        row = self.verify(backup_id)
        target_runtime.initialize()
        if target_runtime.root == target_runtime.base or target_runtime.db.exists():
            raise ContractError('restore requires a new empty disposable DEV runtime')
        source_path = self.runtime.path(row['relative_path'])
        with sqlite3.connect('file:' + str(source_path) + '?mode=ro', uri=True) as source, sqlite3.connect(target_runtime.db) as target:
            source.backup(target)
        os.chmod(target_runtime.db, 0o600)
        return target_runtime.db


class Retention:
    def __init__(self, archive, days=7, policy_version='retention-default-v1', confirmation_ref='baseline-seven-days'):
        if type(days) is not int or days < 0 or not policy_version or not confirmation_ref:
            raise ContractError('invalid retention configuration')
        self.archive, self.registry, self.runtime = archive, archive.registry, archive.runtime
        self.days, self.version = days, policy_version
        with self.registry.transaction() as db:
            prior = db.execute('SELECT * FROM retention_policies WHERE version=?', (policy_version,)).fetchone()
            if prior and (prior['days'], prior['confirmation_ref']) != (days, confirmation_ref):
                raise ContractError('policy revision is immutable')
            db.execute('INSERT OR IGNORE INTO retention_policies VALUES (?,?,?,?)', (policy_version, days, confirmation_ref, now()))

    def hold(self, asset_id, enabled, confirmation_ref):
        if type(enabled) is not bool or not confirmation_ref:
            raise ContractError('explicit hold change required')
        with self.registry.transaction() as db:
            if db.execute('UPDATE staging_files SET cleanup_hold=? WHERE asset_id=?', (int(enabled), asset_id)).rowcount != 1:
                raise ContractError('only raw staging can be cleaned')
            self.registry.event(db, 'staging_hold_changed', asset_id, new_id('hold'),
                                {'enabled': enabled, 'confirmation_ref': confirmation_ref})

    def eligibility(self, asset_id, at=None):
        at = at or datetime.now(timezone.utc)
        loc, identity = self.archive.location(asset_id), self.archive.identity(asset_id)
        if identity['kind'] != 'raw_upload':
            raise ContractError('derived snapshots are outside staging cleanup')
        receipt = self.registry.receipt(identity['entry_id'])
        file = self.registry.rows('SELECT * FROM staging_files WHERE asset_id=?', (asset_id,))[0]
        clean = self.registry.rows('SELECT * FROM staging_cleanup WHERE asset_id=?', (asset_id,))
        reasons = []
        if clean and clean[0]['state'] == 'cleaned':
            return {'status': 'cleaned', 'asset_id': asset_id, 'local_path': None, 'reasons': []}
        if receipt['source_staging_status'] != 'accepted' or file['custody_health'] != 'healthy':
            reasons.append('custody_not_healthy')
        if loc['availability'] != 'present' or not loc['fs_id'] or not loc['current_path']:
            reasons.append('location_unconfirmed')
        if loc['archive_status'] != 'cloud_archived_verified' or LEVELS[loc['verification_level']] < 2 or loc['integrity_hold']:
            reasons.append('content_not_verified')
        if file['cleanup_hold']:
            reasons.append('cleanup_hold')
        if self.registry.rows('SELECT 1 FROM asset_leases WHERE asset_id=? AND released_at IS NULL', (asset_id,)):
            reasons.append('active_lease')
        if self.registry.rows("SELECT 1 FROM asset_references WHERE asset_id=? AND kind='staging_dependency' AND released_at IS NULL", (asset_id,)):
            reasons.append('task_dependency')
        if self.registry.rows('SELECT 1 FROM conversion_bindings WHERE asset_id=? AND released_at IS NULL', (asset_id,)):
            reasons.append('conversion_dependency')
        deadline = instant(loc['first_content_verified_at']) + timedelta(days=self.days) if loc['first_content_verified_at'] else None
        status = 'blocked' if reasons else 'retain_required' if deadline is None else 'grace_period' if at < deadline else 'cleanup_eligible'
        return dict(asset_id=asset_id, status=status, reasons=reasons, cleanup_not_before=deadline.isoformat() if deadline else None,
            policy_version=self.version, days=self.days, registry_revision=loc['registry_revision'],
            health_revision=file['health_revision'], fs_id=loc['fs_id'], current_path=loc['current_path'],
            sha256=identity['sha256'], size_bytes=identity['size_bytes'], local_path=identity['relative_path'])

    def plan(self, asset_ids):
        if not asset_ids or len(set(asset_ids)) != len(asset_ids):
            raise ContractError('explicit nonduplicate cleanup list required')
        items = [self.eligibility(aid) for aid in asset_ids]
        if any(i['status'] != 'cleanup_eligible' for i in items):
            raise ContractError('all cleanup candidates must currently qualify')
        plan = {'version': 'staging-cleanup-plan/v2.1', 'items': items}
        return {**plan, 'digest': stable_key(plan)}

    def execute_synthetic(self, plan, confirmed_digest, confirmation_ref, session, hook=lambda point: None):
        """Manual test-only deletion. Rechecks under the same lock as intake readers."""
        if self.runtime.root == self.runtime.base or not self.runtime.root.is_relative_to(self.runtime.base / 'test-output'):
            raise ContractError('production/base DEV cleanup remains disabled')
        expected = stable_key({k: v for k, v in plan.items() if k != 'digest'})
        if confirmed_digest != expected or plan['digest'] != expected or not confirmation_ref:
            raise ContractError('exact cleanup list confirmation required')
        results = []
        for item in plan['items']:
            aid = item['asset_id']
            current = self.eligibility(aid)
            if current != item:
                results.append({'asset_id': aid, 'state': 'skipped', 'reason': 'state_changed'})
                continue
            identity = self.archive.identity(aid)
            row = self.registry.entry(identity['entry_id'])
            with self.archive.staging.asset_lock(row) as directory:
                if self.eligibility(aid) != item:
                    results.append({'asset_id': aid, 'state': 'skipped', 'reason': 'state_changed_under_lock'})
                    continue
                # Explicit separate verify operation is part of this manual
                # session's approved scope, not an implicit network privilege.
                verify = self.archive.plan(aid, 'verify')
                session.execute(verify)
                fresh = self.eligibility(aid)
                if fresh['status'] != 'cleanup_eligible' or fresh['fs_id'] != item['fs_id'] or fresh['current_path'] != item['current_path']:
                    results.append({'asset_id': aid, 'state': 'skipped', 'reason': 'remote_changed'})
                    continue
                backup = IndexBackup(self.registry)
                bid = backup.create()
                backup.verify(bid)
                self.archive.staging.compare(self.archive.staging.file_evidence(directory, row['safe_filename']),
                                              {k: row[k] for k in ('size_bytes', 'sha256', 'md5')})
                # Hold SQLite write serialization through unlink. A new task
                # cannot create a dependency/lease between the recheck and unlink.
                with self.registry.transaction() as db:
                    if db.execute('SELECT cleanup_hold FROM staging_files WHERE asset_id=?', (aid,)).fetchone()[0]:
                        raise ContractError('cleanup hold changed')
                    if db.execute('SELECT 1 FROM asset_leases WHERE asset_id=? AND released_at IS NULL', (aid,)).fetchone() or db.execute("SELECT 1 FROM asset_references WHERE asset_id=? AND kind='staging_dependency' AND released_at IS NULL", (aid,)).fetchone():
                        raise ContractError('new dependency/lease blocks cleanup')
                    if db.execute('SELECT 1 FROM conversion_bindings WHERE asset_id=? AND released_at IS NULL', (aid,)).fetchone():
                        raise ContractError('conversion dependency blocks cleanup')
                    db.execute('INSERT INTO staging_cleanup VALUES (?,?,?,?,?)', (aid, 'cleaning', encoded(item), confirmation_ref, now()))
                hook('after_cleanup_intent')
                # Intent survives a crash before/after unlink. The lock blocks
                # readers; Registry transactions serialize any late hold change.
                with self.registry.transaction() as db:
                    hold = db.execute('SELECT cleanup_hold FROM staging_files WHERE asset_id=?', (aid,)).fetchone()[0]
                    loc = db.execute('SELECT registry_revision FROM asset_locations WHERE asset_id=?', (aid,)).fetchone()[0]
                    dependency = db.execute("SELECT 1 FROM asset_references WHERE asset_id=? AND kind='staging_dependency' AND released_at IS NULL", (aid,)).fetchone()
                    lease = db.execute('SELECT 1 FROM asset_leases WHERE asset_id=? AND released_at IS NULL', (aid,)).fetchone()
                    conversion = db.execute('SELECT 1 FROM conversion_bindings WHERE asset_id=? AND released_at IS NULL', (aid,)).fetchone()
                    if hold or dependency or lease or conversion or loc != fresh['registry_revision']:
                        db.execute("DELETE FROM staging_cleanup WHERE asset_id=? AND state='cleaning'", (aid,))
                        raise ContractError('cleanup conditions changed after intent')
                    os.unlink(row['safe_filename'], dir_fd=directory)
                    os.fsync(directory)
                    hook('after_cleanup_unlink')
                    db.execute("UPDATE staging_cleanup SET state='cleaned',updated_at=? WHERE asset_id=?", (now(), aid))
                    self.registry.event(db, 'staging_cleaned', aid, aid + ':cleaned',
                        {'confirmation_ref': confirmation_ref, 'backup_id': bid, 'validation_mode': 'offline_mock'})
                results.append({'asset_id': aid, 'state': 'cleaned'})
        return results

    def reconcile_interrupted(self, asset_id):
        rows = self.registry.rows("SELECT * FROM staging_cleanup WHERE asset_id=? AND state='cleaning'", (asset_id,))
        if not rows:
            return 'no_interruption'
        item = self.archive.identity(asset_id)
        row = self.registry.entry(item['entry_id'])
        with self.archive.staging.asset_lock(row) as directory:
            try:
                self.archive.staging.file_evidence(directory, row['safe_filename'])
            except FileNotFoundError:
                with self.registry.transaction() as db:
                    db.execute("UPDATE staging_cleanup SET state='cleaned',updated_at=? WHERE asset_id=?", (now(), asset_id))
                    self.registry.event(db, 'staging_cleaned', asset_id, asset_id + ':cleaned',
                        {'confirmation_ref': rows[0]['confirmation_ref'], 'recovered': True, 'validation_mode': 'offline_mock'})
                return 'cleaned'
            with self.registry.transaction() as db:
                db.execute("DELETE FROM staging_cleanup WHERE asset_id=? AND state='cleaning'", (asset_id,))
            return 'retained_requires_new_confirmation'


class AssetCache:
    def __init__(self, archive, ttl_hours=24, capacity_bytes=64 * 1024 * 1024):
        if type(ttl_hours) not in (int, float) or ttl_hours <= 0 or type(capacity_bytes) is not int or capacity_bytes < 1:
            raise ContractError('invalid cache policy')
        self.archive, self.registry, self.runtime = archive, archive.registry, archive.runtime
        self.ttl, self.capacity = ttl_hours, capacity_bytes

    @contextmanager
    def lock(self, aid, blocking=True):
        self.archive.identity(aid)
        path = self.runtime.checked(self.runtime.path('cache') / (aid + '.lock'))
        fd = open_lock(path)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            yield fd
        finally:
            os.close(fd)

    def populate(self, asset_id, identity, loc, cache, session):
        # Serialize capacity decisions across assets and processes, only while
        # materializing bytes. Active readers keep their per-asset shared lock.
        budget_lock = open_lock(self.runtime.path('cache/.capacity.lock'))
        try:
            fcntl.flock(budget_lock, fcntl.LOCK_EX)
            self.prune(exclude=asset_id)
            if cache:
                with self.registry.transaction() as db:
                    if db.execute('SELECT 1 FROM cache_leases WHERE asset_id=? AND released_at IS NULL', (asset_id,)).fetchone():
                        raise ContractError('expired cache still has an unresolved lease')
                    self.runtime.checked(self.runtime.path(cache['relative_path'])).unlink(missing_ok=True)
                    db.execute('DELETE FROM asset_cache WHERE asset_id=?', (asset_id,))
                cache = None
            usage = sum(r['size_bytes'] for r in self.registry.rows('SELECT size_bytes FROM asset_cache'))
            if usage + identity['size_bytes'] > self.capacity:
                raise ContractError('cache capacity exhausted; active/nonexpired entries retained')
            oid = self.archive.plan(asset_id, 'verify')
            if oid not in session.allowed:
                raise ContractError('cache retrieval verification outside approved window')
            session.execute(oid)
            with session.adapter.bound_request(session.request):
                session.current = oid
                target = self.runtime.checked(self.runtime.path('cache') / (asset_id + '-' + new_id('cache')))
                adopted = False
                try:
                    with session.adapter.stream(loc['fs_id']) as source, target.open('xb') as output:
                        size, digest = 0, hashlib.sha256()
                        while data := source.read(128 * 1024):
                            size += len(data)
                            if size > identity['size_bytes']:
                                raise ContractError('cache download too large')
                            digest.update(data)
                            output.write(data)
                        output.flush()
                        os.fsync(output.fileno())
                    if size != identity['size_bytes'] or digest.hexdigest() != identity['sha256']:
                        raise ContractError('cache SHA-256 mismatch')
                    with self.registry.transaction() as db:
                        db.execute('INSERT OR REPLACE INTO asset_cache VALUES (?,?,?,?,?)',
                            (asset_id, self.runtime.relative(target), digest.hexdigest(), size,
                             (datetime.now(timezone.utc) + timedelta(hours=self.ttl)).isoformat()))
                    adopted = True
                except (OSError, ContractError):
                    if not adopted and target.exists():
                        target.unlink()  # Only the newly created, unadopted cache artifact.
                    raise
                finally:
                    session.current = None
                return target
        finally:
            os.close(budget_lock)

    @contextmanager
    def acquire(self, asset_id, session, confirmation_ref):
        if not confirmation_ref:
            raise ContractError('manual single-asset retrieval required')
        identity, loc = self.archive.identity(asset_id), self.archive.location(asset_id)
        if not loc['fs_id'] or loc['availability'] != 'present':
            raise ContractError('unique current locator required')
        with self.lock(asset_id) as lock:
            rows = self.registry.rows('SELECT * FROM asset_cache WHERE asset_id=?', (asset_id,))
            cache = rows[0] if rows else None
            if not cache or instant(cache['expires_at']) <= datetime.now(timezone.utc):
                target = self.populate(asset_id, identity, loc, cache, session)
            else:
                target = self.runtime.checked(self.runtime.path(cache['relative_path']))
            fd = self.runtime.read_fd(target)
            lease = new_id('lease')
            try:
                if digest_fd(fd)['sha256'] != identity['sha256']:
                    raise ContractError('cached bytes changed')
                os.lseek(fd, 0, os.SEEK_SET)
                with self.registry.transaction() as db:
                    db.execute('INSERT INTO cache_leases VALUES (?,?,?,NULL)', (lease, asset_id, now()))
                fcntl.flock(lock, fcntl.LOCK_SH)
                with os.fdopen(fd, 'rb', closefd=False) as stream:
                    yield stream
            finally:
                os.close(fd)
                with self.registry.transaction() as db:
                    db.execute('UPDATE cache_leases SET released_at=? WHERE lease_id=?', (now(), lease))

    def prune(self, exclude=None, at=None):
        at = at or datetime.now(timezone.utc)
        removed = []
        for row in self.registry.rows('SELECT * FROM asset_cache'):
            if row['asset_id'] == exclude or instant(row['expires_at']) > at:
                continue
            try:
                with self.lock(row['asset_id'], blocking=False):
                    with self.registry.transaction() as db:
                        current = db.execute('SELECT * FROM asset_cache WHERE asset_id=?', (row['asset_id'],)).fetchone()
                        if not current or instant(current['expires_at']) > at or db.execute('SELECT 1 FROM cache_leases WHERE asset_id=? AND released_at IS NULL', (row['asset_id'],)).fetchone():
                            continue
                        path = self.runtime.checked(self.runtime.path(current['relative_path']))
                        path.unlink(missing_ok=True)
                        db.execute('DELETE FROM asset_cache WHERE asset_id=?', (row['asset_id'],))
                        removed.append(row['asset_id'])
            except BlockingIOError:
                continue  # Active readers hold a shared OS lock, never evicted.
        return removed
