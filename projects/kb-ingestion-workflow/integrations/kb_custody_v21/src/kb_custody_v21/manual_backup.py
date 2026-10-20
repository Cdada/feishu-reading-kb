"""Content-only manual exports. No cloud API, runtime restore or cleanup credit.

The existing append-only custody ledger binds a local trusted manifest outside
the ZIP. Upload reports and each verification/restore attempt remain separate
events; a later failure cannot erase an earlier successful observation.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import zipfile

from .contracts import ContractError, new_id, validate_id
from .conversion import CustodyConversion, durable_new, sha
from .metadata import validate_metadata
from .publication import Publications
from .registry import encoded, now
from .runtime import no_symlinks
from .staging import Staging
from . import safe_publish as fs


MAX_BYTES = 64 * 1024**2
MAX_FILES = 2048
SCHEMA = 'manual-backup/v0.1'
README = '''# 人工内容备份与恢复

请保留本机独立保存的可信清单，再手工上传本 ZIP 到百度网盘。
报告“已上传”只记录本人说明；下载后须用本机可信摘要校验完整 ZIP。
校验成功后，恢复器只向全新的 DEV 内容目录解包并逐文件验证。
请打开原件、提取正文、导读、已确认 PDF 及图片，检查阅读效果。

这是内容副本，不包含运行数据库、凭据、会话或云端定位信息。
不恢复运行服务、不证明云端长期可靠，也不允许删除本机原件。
缺少包外可信清单时停止；不能把 ZIP 内自带摘要当作独立证明。
'''.encode()
PRIVATE_VALUE = re.compile(r'(?:/(?:Users|home)/|[A-Za-z]:\\|\b(?:ou_|oc_|om_|cli_)[0-9a-f]{16,}|'
                           r'\b(?:ghp_|sk-)[A-Za-z0-9_-]{16,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)')
PRIVATE_KEYS = {'access_token', 'refresh_token', 'app_secret', 'secret_key', 'sign_key',
                'password', 'credentials', 'session_id', 'account_id', 'chat_id', 'file_key',
                'message_id', 'open_id', 'tenant_key', 'downloaded_copy_ref'}


def portable_json(value, trail=(), field_pointers=frozenset()):
    """Reject private runtime fields, never silently rewrite a Publication."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in PRIVATE_KEYS and item not in (None, '', [], {}):
                raise ContractError('publication contains private runtime fields; export stopped')
            portable_json(item, trail + (key,), field_pointers)
    elif isinstance(value, list):
        for item in value:
            portable_json(item, trail, field_pointers)
    elif isinstance(value, str):
        # Formal metadata uses JSON pointers for field provenance. They are
        # record locations, not filesystem paths; other absolute paths stop.
        field_pointer = ('field_provenance' in trail and trail[-1:] == ('fields',) and value in field_pointers)
        if PRIVATE_VALUE.search(value) or (value.startswith('/') and not field_pointer) or re.match(r'^[A-Za-z]:[\\/]', value):
            raise ContractError('publication contains private values; export stopped')


@contextmanager
def local_file(path):
    """Read a caller-selected local download with descriptor-relative no-follow."""
    path = no_symlinks(path)
    parent = fs.open_root(path.parent, fs.root_identity(path.parent))
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ContractError('backup must be a regular local file')
            yield stream
    finally:
        os.close(parent)


def read_bounded(stream, size):
    data = stream.read(size + 1)
    if len(data) != size:
        raise ContractError('backup file size changed')
    return data


class ManualBackups:
    def __init__(self, registry):
        self.registry, self.runtime = registry, registry.runtime
        self.root = self.runtime.checked(self.runtime.root / 'cloud-assets/exports/manual-content')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _event(self, bid, kind, payload):
        eid = new_id('manual_event')
        with self.registry.transaction() as db:
            self.registry.event(db, 'manual_backup_' + kind, bid, eid, payload)
        return payload

    def _events(self, bid):
        validate_id(bid, 'manual_backup')
        rows = self.registry.rows("SELECT event_type,payload_json FROM custody_events WHERE aggregate_id=? "
                                  "AND event_type LIKE 'manual_backup_%' ORDER BY rowid", (bid,))
        if not rows or rows[0]['event_type'] != 'manual_backup_created':
            raise ContractError('unknown manual backup batch')
        return [(r['event_type'], json.loads(r['payload_json'])) for r in rows]

    def _trusted(self, bid):
        record = self._events(bid)[0][1]
        path = self.runtime.checked(self.runtime.root / record['local_trusted_manifest_ref'])
        with local_file(path) as stream:
            raw = read_bounded(stream, record['trusted_manifest_size'])
        if sha(raw) != record['trusted_manifest_sha256']:
            raise ContractError('external trusted manifest changed')
        return record, json.loads(raw)

    def _collect(self, publication_ids):
        publications = Publications(self.registry)
        files, roles, scope = {}, {}, []

        def add(name, data, role, refs):
            if name in files or len(files) >= MAX_FILES or sum(map(len, files.values())) + len(data) > MAX_BYTES:
                raise ContractError('manual content package exceeds size/file bound or duplicates a path')
            if name.endswith(('.json', '.md', '.txt')):
                text = data.decode('utf-8')
                if PRIVATE_VALUE.search(text):
                    raise ContractError('content contains excluded private values; export stopped')
                if name.endswith('.json'):
                    value = json.loads(data)
                    pointers = set()
                    if name.endswith('/metadata.json'):
                        # validate_record resolves each pointer against the
                        # actual formal SourceRecord before this exemption.
                        record = validate_metadata(value)['source_record']
                        pointers = {p for v in record['field_provenance'] for p in v['fields']}
                    portable_json(value, field_pointers=pointers)
            files[name], roles[name] = data, dict(role=role, references=refs)

        for pid in publication_ids:
            row = publications._row(pid)
            if row['state'] != 'published':
                raise ContractError('manual export requires an already confirmed published version')
            path = publications._tree(row, pid)
            metadata = validate_metadata(json.loads((path / 'metadata.json').read_bytes()))
            refs = dict(source_id=row['source_id'], asset_id=row['source_asset_id'],
                        extraction_id=metadata['extraction']['extraction_id'], publication_id=pid)
            if metadata['publication']['publication_id'] != pid or metadata['raw_asset_id'] != row['source_asset_id']:
                raise ContractError('publication identities disagree')
            with Staging(self.registry).input_binding(row['entry_id'], 'manual_content_export') as binding:
                entry = self.registry.entry(row['entry_id'])
                try:
                    original = read_bounded(binding['stream'], entry['size_bytes'])
                    if sha(original) != entry['sha256']:
                        raise ContractError('original changed during export')
                except (OSError, ContractError) as exc:
                    self.registry.health(row['entry_id'], 'suspect', 'manual export original read: ' + type(exc).__name__)
                    raise
                original_name = 'originals/' + entry['asset_id'] + '/' + entry['safe_filename']
                add(original_name, original, 'original', refs)
                for name, expected in json.loads(row['files_json']).items():
                    with os.fdopen(self.runtime.read_fd(path / name), 'rb') as stream:
                        data = read_bounded(stream, expected['size_bytes'])
                    if sha(data) != expected['sha256']:
                        raise ContractError('publication bytes changed during export')
                    add('publications/' + pid + '/' + name, data, 'publication', refs)
                # Original images may deliberately be absent from a text-only
                # Publication. Include exactly its declared image asset IDs.
                ancestry = self._ancestry(refs['extraction_id'])
                for item in metadata['assets']:
                    if item['kind'] != 'extracted_image' or item['local_path'] is not None:
                        continue
                    image, data = self._original_image(ancestry, item)
                    add('images/' + item['asset_id'] + '/' + Path(image['relative_path']).name,
                        data, 'original_image', dict(refs, image_asset_id=item['asset_id']))
                scope.append(dict(refs, original_path=original_name,
                                  revision_of=row['revision_of'], extraction_ancestry=[dict(
                                      extraction_id=r['extraction_id'], parent_extraction_id=r.get('parent_extraction_id'),
                                      output_asset_id=m['output_asset_id'], output_sha256=m['output_sha256'])
                                      for r, m, _ in ancestry]))
        index = dict(schema_version='manual-content-index/v0.1', scope=scope,
                     external_references='revision_of/parent identities may refer to versions outside this batch')
        add('CONTENT_INDEX.json', encoded(index).encode(), 'content_index', {})
        add('恢复说明.md', README, 'restore_instructions', {})
        return files, {name: dict(info, **roles[name]) for name, info in fs.manifest(files).items()}, scope

    def _ancestry(self, extraction_id):
        result, seen = [], set()
        while extraction_id:
            if extraction_id in seen or len(seen) >= 16:
                raise ContractError('invalid extraction ancestry')
            seen.add(extraction_id)
            row, mapping, root = CustodyConversion(self.registry).extraction(extraction_id)
            result.append((row, mapping, root))
            extraction_id = row.get('parent_extraction_id')
        return result

    def _original_image(self, ancestry, asset):
        # Re-expressions retain the parent's inventory, but don't copy image
        # bytes. Resolve only through this registered version chain, by ID/SHA.
        for row, _, root in ancestry:
            for image in json.loads(row['inventory_json']):
                if image['asset_id'] != asset['asset_id']:
                    continue
                try:
                    fd = self.runtime.read_fd(root / image['relative_path'])
                except FileNotFoundError:
                    continue
                with os.fdopen(fd, 'rb') as stream:
                    data = read_bounded(stream, asset['size_bytes'])
                if sha(data) != asset['sha256']:
                    raise ContractError('required image bytes changed')
                return image, data
        raise ContractError('required original image is unavailable in registered ancestry')

    @staticmethod
    def _check_content(files, frozen):
        if json.loads(files['CONTENT_INDEX.json'])['scope'] != frozen['scope']:
            raise ContractError('content index scope disagrees with trusted manifest')
        for ref in frozen['scope']:
            prefix = 'publications/' + ref['publication_id'] + '/'
            metadata = validate_metadata(json.loads(files[prefix + 'metadata.json']))
            extraction = json.loads(files[prefix + metadata['extraction']['map_path']])
            original = next(a for a in metadata['assets'] if a['asset_id'] == ref['asset_id'])
            if (metadata['source_record']['source_id'] != ref['source_id'] or
                    metadata['publication']['publication_id'] != ref['publication_id'] or
                    extraction['extraction_id'] != ref['extraction_id'] or
                    extraction['source_asset_id'] != ref['asset_id'] or
                    sha(files[ref['original_path']]) != original['sha256'] or
                    sha(files[prefix + '提取/source.md']) != extraction['output_sha256']):
                raise ContractError('restored content identities disagree')
            for item in metadata['assets']:
                if item['local_path']:
                    data = files[prefix + item['local_path']]
                elif item['kind'] == 'extracted_image':
                    candidates = [name for name in files if name.startswith('images/' + item['asset_id'] + '/')]
                    if len(candidates) != 1:
                        raise ContractError('restored original image reference is unresolved')
                    data = files[candidates[0]]
                else:
                    continue
                if len(data) != item['size_bytes'] or sha(data) != item['sha256']:
                    raise ContractError('restored asset reference bytes disagree')

    def create(self, publication_ids):
        """Each immutable content batch is version 1; changed content gets a new ID."""
        self.runtime.gate()
        if not publication_ids or len(publication_ids) > 20 or len(set(publication_ids)) != len(publication_ids):
            raise ContractError('explicit nonduplicate published scope required (maximum 20)')
        files, manifest, scope = self._collect(publication_ids)
        bid = new_id('manual_backup')
        frozen = dict(schema_version=SCHEMA, backup_batch_id=bid, bundle_version=1,
                      created_at=now(), scope=scope, file_manifest=manifest)
        self._check_content(files, frozen)
        folder = self.runtime.checked(self.root / bid)
        folder.mkdir(mode=0o700, exist_ok=False)
        bundle = folder / 'content.zip'
        # Stored ZIP keeps memory/expansion predictable for material archives.
        fd = os.open(bundle, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            with zipfile.ZipFile(stream, 'w', zipfile.ZIP_STORED) as archive:
                for name, data in {**files, 'MANIFEST.json': encoded(frozen).encode()}.items():
                    archive.writestr(name, data)
            stream.flush(); os.fsync(stream.fileno())
        if self._collect(publication_ids) != (files, manifest, scope):
            raise ContractError('content changed during packaging; uncommitted package retained locally')
        with local_file(bundle) as stream:
            trusted = dict(frozen=frozen, bundle_sha256=hashlib.file_digest(stream, 'sha256').hexdigest(),
                           bundle_size_bytes=os.fstat(stream.fileno()).st_size)
        trusted_raw = encoded(trusted).encode()
        trusted_path = folder / 'trusted.local.json'
        durable_new(trusted_path, trusted_raw)
        record = dict(schema_version=SCHEMA, backup_batch_id=bid, bundle_version=1,
                      bundle_ref=str(bundle.relative_to(self.runtime.root)), bundle_sha256=trusted['bundle_sha256'],
                      local_trusted_manifest_ref=str(trusted_path.relative_to(self.runtime.root)),
                      trusted_manifest_sha256=sha(trusted_raw), trusted_manifest_size=len(trusted_raw),
                      created_at=frozen['created_at'], scope=scope)
        self._event(bid, 'created', record)
        return self.status(bid)

    def status(self, bid):
        events = self._events(bid)
        result = dict(events[0][1], manual_upload_status='pending_manual_upload',
                      verification_status='not_run', restore_test_status='not_run', attempts=[])
        for kind, payload in events[1:]:
            result['attempts'].append(dict(kind=kind, **payload))
            if kind == 'manual_backup_upload_reported':
                result.update(payload, manual_upload_status='user_reported_uploaded')
            elif kind == 'manual_backup_verification':
                result.update(verification_status=payload['status'], latest_verification=payload)
            elif kind == 'manual_backup_restore':
                result.update(restore_test_status=payload['status'], latest_restore=payload)
        return result

    def report_upload(self, bid, *, provider, confirmation_ref):
        self._trusted(bid)
        if provider != 'baidu' or not isinstance(confirmation_ref, str) or not confirmation_ref.strip():
            raise ContractError('explicit user report for the selected Baidu provider required')
        self._event(bid, 'upload_reported', dict(user_reported_provider=provider,
                    user_reported_uploaded_at=now(), confirmation_ref=confirmation_ref,
                    evidence_kind='user_report_only_not_platform_verification'))
        return self.status(bid)

    def _read_copy(self, path, trusted):
        """Validate the external whole-file hash BEFORE opening any ZIP member."""
        with local_file(path) as stream:
            if os.fstat(stream.fileno()).st_size != trusted['bundle_size_bytes'] or os.fstat(stream.fileno()).st_size > MAX_BYTES * 2:
                raise ContractError('downloaded copy size mismatch')
            if hashlib.file_digest(stream, 'sha256').hexdigest() != trusted['bundle_sha256']:
                raise ContractError('downloaded copy differs from external trusted digest')
            stream.seek(0)
            with zipfile.ZipFile(stream) as archive:
                infos = archive.infolist()
                expected = trusted['frozen']['file_manifest']
                if len(infos) > MAX_FILES + 1 or len(infos) != len(expected) + 1:
                    raise ContractError('unexpected archive entries')
                names = [i.filename for i in infos]
                if len(set(names)) != len(names) or set(names) != set(expected) | {'MANIFEST.json'}:
                    raise ContractError('archive entries differ from trusted manifest')
                files, total = {}, 0
                for item in infos:
                    fs.parts(item.filename)
                    mode = item.external_attr >> 16
                    total += item.file_size
                    if (item.orig_filename != item.filename or ':' in item.filename or item.is_dir() or
                            (stat.S_IFMT(mode) not in (0, stat.S_IFREG)) or item.flag_bits & 1 or
                            total > MAX_BYTES * 2 or item.file_size > MAX_BYTES or
                            (item.file_size > 1024**2 and item.file_size > max(item.compress_size, 1) * 100)):
                        raise ContractError('unsafe archive member or expansion')
                    with archive.open(item) as member:
                        data = read_bounded(member, item.file_size)
                    if item.filename == 'MANIFEST.json':
                        if data != encoded(trusted['frozen']).encode():
                            raise ContractError('embedded manifest differs from trusted record')
                    else:
                        wanted = expected[item.filename]
                        if len(data) != wanted['size_bytes'] or sha(data) != wanted['sha256']:
                            raise ContractError('content file differs from trusted manifest')
                        files[item.filename] = data
            # Detect in-place replacement while decompression/read was underway.
            stream.seek(0)
            if hashlib.file_digest(stream, 'sha256').hexdigest() != trusted['bundle_sha256']:
                raise ContractError('download changed during verification')
        fs.manifest(files)
        self._check_content(files, trusted['frozen'])
        files['MANIFEST.json'] = encoded(trusted['frozen']).encode()
        return files

    def verify_download(self, bid, copy_path):
        self._events(bid)
        attempt = dict(verification_id=new_id('manual_verify'), bundle_version=1,
                       downloaded_copy_ref=str(Path(copy_path).absolute()), status='failed')
        try:
            record, trusted = self._trusted(bid)
            original = self.runtime.root / record['bundle_ref']
            if Path(copy_path).resolve() == original.resolve():
                raise ContractError('select a separate downloaded copy, not the original export')
            self._read_copy(copy_path, trusted)
            attempt.update(status='download_verification_passed', verified_at=now(),
                           bundle_sha256=trusted['bundle_sha256'],
                           evidence_kind='caller_supplied_copy_byte_check_not_cloud_provenance')
        except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as exc:
            self._event(bid, 'verification', dict(attempt, failed_at=now(), error_code=type(exc).__name__))
            raise ContractError('download verification failed; preserve copy and trusted record') from exc
        self._event(bid, 'verification', attempt)
        return attempt

    def restore(self, bid, verification_id):
        events = self._events(bid)
        verified = [p for kind, p in events if kind == 'manual_backup_verification' and
                    p['verification_id'] == verification_id and p['status'] == 'download_verification_passed']
        if len(verified) != 1:
            raise ContractError('restore requires this batch version and a successful download verification')
        attempt = dict(restore_id=new_id('manual_restore'), verification_id=verification_id,
                       bundle_version=1, status='failed')
        try:
            _, trusted = self._trusted(bid)
            if verified[0]['bundle_sha256'] != trusted['bundle_sha256']:
                raise ContractError('verified batch changed')
            files = self._read_copy(verified[0]['downloaded_copy_ref'], trusted)
            parent = self.runtime.checked(self.runtime.root / 'test-output/manual-content-restores')
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            target = self.runtime.checked(parent / attempt['restore_id'])
            attempt['target_ref'] = str(target.relative_to(self.runtime.root))
            # Exclusive new directory; partial failures are retained, never
            # overwrite an existing library or remove evidence to retry.
            parent_fd = fs.open_root(parent, fs.root_identity(parent))
            try:
                os.mkdir(target.name, mode=0o700, dir_fd=parent_fd)
                os.fsync(parent_fd)
                target_fd = os.open(target.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
            finally:
                os.close(parent_fd)
            try:
                fs.write_tree(target_fd, files)
                if fs.inspect_tree(target_fd, expected_files=fs.manifest(files)) != fs.manifest(files):
                    raise ContractError('restored bytes changed')
            finally:
                os.close(target_fd)
            attempt.update(status='passed', restored_at=now(), bundle_sha256=trusted['bundle_sha256'],
                           evidence_kind='content_bytes_restored_manual_reading_still_required')
        except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as exc:
            self._event(bid, 'restore', dict(attempt, failed_at=now(), error_code=type(exc).__name__))
            raise ContractError('content restore failed; evidence and any partial target retained') from exc
        self._event(bid, 'restore', attempt)
        return attempt
