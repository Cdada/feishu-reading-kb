"""DEV-only atomic publication, exact confirmed PDF and explicit revision identity."""
from copy import deepcopy
import json
import os
from pathlib import Path

from .contracts import ContractError, new_id, validate_id
from .conversion import sha, CustodyConversion
from .content import validate_record, validate_evidence
from .metadata import asset, render_metadata
from .preview import render_preview
from .registry import now, encoded
from .staging import Staging
from .material_review import MaterialReviews
from . import safe_publish as fs


class Publications:
    def __init__(self, registry):
        self.registry, self.runtime = registry, registry.runtime
        self.root = self.runtime.checked(self.runtime.path('conversion-work/publications'))
        self.runtime.ensure_directory('conversion-work/publications')

    def _row(self, pid):
        validate_id(pid,'publication')
        rows = self.registry.rows('SELECT * FROM publications_v21 WHERE publication_id=?',(pid,))
        if not rows: raise ContractError('unknown V2.1 publication')
        return rows[0]

    def _tree(self, row, name):
        path = self.runtime.checked(self.root / name)
        fd = fs.open_root(path,fs.root_identity(path))
        try:
            expected = json.loads(row['files_json'])
            if fs.inspect_tree(fd,expected_files=expected) != expected:
                raise ContractError('publication candidate changed; prepare and confirm a new version')
        finally:
            os.close(fd)
        return path

    def prepare(self, entry_id, record, mapping, body, curation, guide, policy, *, font_path, revision_of=None, images=(),
                on_prepared=None):
        self.runtime.gate()
        validate_record(record)
        entry = self.registry.entry(entry_id)
        with Staging(self.registry).input_binding(entry_id,'publication_prepare'):
            _, stored_mapping, extracted_root = CustodyConversion(self.registry).publication_extraction(mapping['extraction_id'])
            if stored_mapping != mapping or (extracted_root / 'source.md').read_bytes() != body:
                raise ContractError('publication requires the registered immutable extraction/expression')
            if mapping['source_asset_id'] != entry['asset_id'] or mapping['output_sha256'] != sha(body):
                raise ContractError('publication source identity mismatch')
            generation = curation['guide_generation']
            if curation['source_id'] != record['source_id'] or curation['extraction_id'] != mapping['extraction_id'] or generation['guide_sha256'] != sha(guide.encode()):
                raise ContractError('guide changed or points to an old extraction; rebind evidence and review')
            for item in curation['evidence']:
                validate_evidence(item,record['source_id'],mapping,body.decode(),generation['coverage'])
            if policy['exception_check']['status'] != 'passed': raise ContractError('unresolved image policy decision')
            active = self.registry.rows('SELECT * FROM active_publications_v21 WHERE source_asset_id=?',(entry['asset_id'],))
            if active and revision_of != active[0]['publication_id']:
                raise ContractError('same original already published: explicit revision_of required')
            if revision_of and (not active or active[0]['publication_id'] != revision_of):
                raise ContractError('revision must reference the current publication')
            pid, confirmation = new_id('publication'),new_id('confirmation')
            metadata_assets = [dict(asset_id=entry['asset_id'],kind='raw_upload',media_type='application/octet-stream',
                captured_filename=entry['received_filename'],uploaded_filename=entry['received_filename'],size_bytes=entry['size_bytes'],
                sha256=entry['sha256'],md5=entry['md5'],local_path=None,derived_from=[],extraction_id=None,immutable=True),
                asset(mapping['output_asset_id'],'extracted_text','source.md',body,local_path='提取/source.md',
                      derived_from=[entry['asset_id']],extraction_id=mapping['extraction_id'])]
            public_policy = deepcopy(policy)
            files = {'提取/source.md':body,'提取/extraction-map.json':encoded(mapping).encode(),
                     'curation.json':encoded(curation).encode(),'导读.md':guide.encode()}
            from .image_availability import require_image_choice
            image_check = require_image_choice(CustodyConversion(self.registry), mapping['extraction_id'])
            if image_check:
                if not any(c['code'] == 'missing_images_accepted' for c in record['evidence_scope']['explicit_constraints']):
                    raise ContractError('missing-image disclosure must appear in the guide record')
                files['发布记录/缺图处理决定.json'] = encoded(image_check['decision']).encode()
                files['提取/图片完整性检查.json'] = encoded(image_check['report']).encode()
            review = MaterialReviews(self.registry).checked(mapping['extraction_id'])
            if review:
                files['发布记录/材料复核.json'] = encoded(review).encode()
            observations = policy['execution_result'].get('visual_observations',[])
            if curation.get('visual_observations',[]) != observations:
                raise ContractError('curation and policy visual reading facts disagree')
            from .visual_reading import render_image_notes
            notes = render_image_notes(observations)
            if notes is not None: files['提取/image-notes.md'] = notes
            from .image_links import image_spans
            originals = {i['asset_id']:i for i in images if i['role']=='extracted_original'}
            required = {i['original_asset_id'] for i in policy['per_asset']}
            if not required <= set(originals): raise ContractError('missing managed original image assets')
            for aid, item in originals.items():
                image_data = self.runtime.checked(item['path']).read_bytes()
                if sha(image_data) != item['sha256'] or len(image_data) != item['size_bytes']:
                    raise ContractError('original image identity changed')
                metadata_assets.append(asset(aid,'extracted_image',Path(item.get('original_target',item['path'])).name,image_data,
                    derived_from=[entry['asset_id']],extraction_id=mapping['extraction_id']))
            for output in policy['execution_result']['per_asset_results']:
                data = self.runtime.checked(output['path']).read_bytes()
                if sha(data) != output['sha256'] or len(data) != output['size_bytes']:
                    raise ContractError('image representation changed')
                relative = '提取/images/' + Path(output['path']).name
                files[relative] = data
                existing = next((a for a in metadata_assets if a['asset_id']==output['asset_id']),None)
                if existing:
                    existing['local_path'] = relative
                else:
                    metadata_assets.append(asset(output['asset_id'],'image_representation',Path(output['path']).name,data,
                        local_path=relative,derived_from=[output['original_asset_id']],extraction_id=mapping['extraction_id']))
            for span in image_spans(body.decode()):
                if '提取/' + span['original_path'] not in files:
                    raise ContractError('broken or unbound local image link')
            for item in public_policy['execution_result']['per_asset_results']:
                item['path'] = '提取/images/' + Path(item['path']).name
            pub = dict(publication_id=pid,state='published',guide_path='导读.md',curation_path='curation.json',
                       preview_path='发布记录/已确认入库预览.pdf',confirmation_event_id=confirmation,generated_at=now(),revision_of=revision_of)
            metadata = render_metadata(record,mapping,public_policy,metadata_assets,pub,
                classification=dict(source_family='document',content_nature='unknown'),privacy=dict(sensitivity='private',brief_visibility='private'))
            files['metadata.json'] = encoded(metadata).encode()
            pdf = render_preview(record,guide,generation['coverage_statement'],font_path=font_path,
                                 limitations=MaterialReviews.limitations(review),
                                 image_notes=render_image_notes(observations,human_labels=True).decode() if notes else None)
            files[pub['preview_path']] = pdf
            manifest = dict(schema_version='kb-manifest/v2.1',publication_id=pid,files=fs.manifest(files))
            files['.kb-manifest.json'] = encoded(manifest).encode()
            candidate = self.runtime.checked(self.root / (pid+'.candidate'))
            candidate.mkdir(exist_ok=False)
            fd = fs.open_root(candidate,fs.root_identity(candidate))
            try: fs.write_tree(fd,files)
            finally: os.close(fd)
            with self.registry.transaction() as db:
                db.execute('INSERT INTO publications_v21 VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (pid,entry_id,entry['asset_id'],record['source_id'],'prepared',sha(pdf),sha(files['.kb-manifest.json']),
                     encoded(fs.manifest(files)),confirmation,revision_of))
                # The local controller records this identity in the SAME commit.
                # A crash after commit cannot create an unbound second preview.
                if on_prepared is not None:
                    on_prepared(db, pid)
        return dict(publication_id=pid,preview_sha256=sha(pdf),preview_path=str(candidate/pub['preview_path']),
                    manifest_sha256=sha(files['.kb-manifest.json']))

    def confirm(self,pid,pdf_sha256,actor,confirmation_ref):
        with self.registry.transaction() as db:
            self._confirm_in_transaction(db,pid,pdf_sha256,actor,confirmation_ref)

    def candidate_review(self, row, path, *, db=None):
        mapping = json.loads((path / '提取/extraction-map.json').read_bytes())
        review_path = path / '发布记录/材料复核.json'
        frozen = json.loads(review_path.read_bytes()) if review_path.exists() else None
        current = MaterialReviews(self.registry).checked(mapping['extraction_id'], db=db)
        if current != frozen or (current and current['qualification'] != 'qualified'):
            raise ContractError('candidate material review changed; prepare and confirm a new preview')
        # Recheck conversion qualification even if this candidate predates a review.
        CustodyConversion(self.registry).publication_extraction(mapping['extraction_id'])
        return current

    def _confirm_in_transaction(self,db,pid,pdf_sha256,actor,confirmation_ref,*,accept_limitations=False):
        """Share the transaction with a durable, externally bound user decision."""
        validate_id(pid,'publication')
        row = db.execute('SELECT * FROM publications_v21 WHERE publication_id=?',(pid,)).fetchone()
        if row is None or row['state'] != 'prepared' or row['preview_sha256'] != pdf_sha256 or not actor or not confirmation_ref:
            raise ContractError('confirmation must bind the exact displayed PDF and actor')
        path = self._tree(row,pid+'.candidate')
        review = self.candidate_review(row, path, db=db)
        limitations = MaterialReviews.limitations(review)
        if bool(limitations) != accept_limitations:
            raise ContractError('exact material limitations need an explicit current-version decision')
        if limitations:
            self.registry.event(db,'material_limitations_accepted',pid,'material-choice:'+pid,
                dict(review_id=review['review_id'],review_sha256=sha(encoded(review).encode()),
                     pdf_sha256=pdf_sha256,actor=actor,confirmation_ref=confirmation_ref,
                     limitations=limitations,choice='accepted_current_version'))
        db.execute('INSERT INTO publication_confirmations VALUES (?,?,?,?,?,?)',
                   (row['confirmation_event_id'],pid,pdf_sha256,actor,confirmation_ref,now()))
        db.execute("UPDATE publications_v21 SET state='confirmed' WHERE publication_id=? AND state='prepared'",(pid,))

    def publish(self,pid,*,checked_access=None):
        def check():
            if checked_access is not None: checked_access()
        check()
        row = self._row(pid)
        if row['state']=='published':
            final = self._tree(row,pid)
            check()
            return final
        if row['state'] not in {'confirmed','publishing'}: raise ContractError('exact PDF confirmation required')
        with Staging(self.registry).input_binding(row['entry_id'],'publication_commit'):
            with self.registry.transaction() as db:
                location = pid if (self.root / pid).exists() else pid+'.candidate'
                self.candidate_review(row, self._tree(row,location), db=db)
                active = db.execute('SELECT publication_id FROM active_publications_v21 WHERE source_asset_id=?',(row['source_asset_id'],)).fetchone()
                if active and active[0] not in (row['revision_of'],pid): raise ContractError('active publication changed')
                check()
                db.execute("UPDATE publications_v21 SET state='publishing' WHERE publication_id=?",(pid,))
                # Write intent precedes filesystem mutation; a restart verifies
                # the exact destination tree and completes this same identity.
            if not (self.root/pid).exists():
                self._tree(row,pid+'.candidate')
                fd = fs.open_root(self.root,fs.root_identity(self.root))
                try:
                    check()
                    fs.rename_exclusive(fd,pid+'.candidate',fd,pid)
                finally: os.close(fd)
            final = self._tree(row,pid)
            with self.registry.transaction() as db:
                self.candidate_review(row, final, db=db)
                active = db.execute('SELECT publication_id FROM active_publications_v21 WHERE source_asset_id=?',(row['source_asset_id'],)).fetchone()
                if active and active[0] not in (row['revision_of'],pid):
                    raise ContractError('another publication committed while this candidate was publishing; review required')
                check()
                db.execute("UPDATE publications_v21 SET state='published' WHERE publication_id=?",(pid,))
                db.execute('INSERT INTO active_publications_v21 VALUES (?,?) ON CONFLICT(source_asset_id) DO UPDATE SET publication_id=excluded.publication_id',
                           (row['source_asset_id'],pid))
                db.execute('INSERT OR IGNORE INTO cloud_asset_keys VALUES (?,?,NULL)', (row['source_asset_id'],row['source_asset_id']))
                db.execute("INSERT OR IGNORE INTO asset_references VALUES (?,?,'publication',NULL)", (row['source_asset_id'],pid))
                self.registry.event(db,'publication_published',pid,'publication:'+pid,{'manifest_sha256':row['manifest_sha256']})
            return final

    def withdraw(self,pid,confirmation_ref):
        row = self._row(pid)
        if not confirmation_ref or row['state']!='published': raise ContractError('withdrawal requires explicit current publication confirmation')
        self._tree(row,pid)
        with self.registry.transaction() as db:
            db.execute("UPDATE publications_v21 SET state='withdrawn' WHERE publication_id=?",(pid,))
            db.execute('DELETE FROM active_publications_v21 WHERE publication_id=?',(pid,))
            self.registry.event(db,'publication_withdrawn',pid,'withdraw:'+pid,{'confirmation_ref':confirmation_ref})

    def restore(self,pid,confirmation_ref):
        row = self._row(pid)
        if not confirmation_ref or row['state']!='withdrawn': raise ContractError('restore requires explicit withdrawn publication confirmation')
        self._tree(row,pid)
        with self.registry.transaction() as db:
            if db.execute('SELECT 1 FROM active_publications_v21 WHERE source_asset_id=?',(row['source_asset_id'],)).fetchone():
                raise ContractError('another revision is active')
            db.execute('INSERT INTO active_publications_v21 VALUES (?,?)',(row['source_asset_id'],pid))
            db.execute("UPDATE publications_v21 SET state='published' WHERE publication_id=?",(pid,))
            self.registry.event(db,'publication_restored',pid,'restore:'+pid,{'confirmation_ref':confirmation_ref})
