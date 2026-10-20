"""Serial local Pilot composition, ending at an unconfirmed preview.

The live caller supplies a separately approved text session. This module cannot
authenticate, send a message, renew a window, approve a PDF or publish content.
"""
import json
from pathlib import Path

from .contracts import ContractError, stable_key
from .conversion import sha
from .f05 import file_sha
from .pilot_preview import PilotPreviews
from .pilot_resources import PilotResources
from .pilot_text import load_validated_guide
from .pilot_revision import digest, failed_receipt, revision_fields, check_new_approval
from .pilot_continuation import blocked_receipt, continuation_fields, check_continuation_approval
from .pilot_images import checked_image_facts, with_image_facts
from .registry import encoded, now


class PilotPipeline:
    def __init__(self, intake, *, resources=None):
        self.intake, self.registry = intake, intake.registry
        self.previews = PilotPreviews(self.registry, intake.scope)
        self.publisher = self.previews.publisher
        self.resources = resources if resources is not None else PilotResources(self.registry,
            minimum_free_bytes=max(5*1024**3, intake.conversion.settings.minimum_free_bytes))

    def convert_once(self, *, entry_id=None, duplicate_selection=None):
        # work_guarded_once owns this same serial lock. Never acquire it twice.
        return self.intake.work_guarded_once(resources=self.resources, entry_id=entry_id,
                                             duplicate_selection=duplicate_selection)

    def _event(self, entry_id, phase):
        rows = self.registry.rows('SELECT payload_json FROM custody_events WHERE dedupe_key=?',
                                  (stable_key('pilot-pipeline',entry_id,phase),))
        return json.loads(rows[0]['payload_json']) if rows else None

    def _save(self, db, entry_id, phase, data):
        self.registry.event(db, 'pilot_pipeline_'+phase, entry_id,
                            stable_key('pilot-pipeline',entry_id,phase), data)

    def attempt_event(self, entry_id, phase):
        continuation = self._event(entry_id, 'continuation')
        revision = self._event(entry_id, 'revision')
        key = ('continuation:'+continuation['attempt_id']+':'+phase if continuation else
               'revision:'+revision['attempt_id']+':'+phase if revision else phase)
        return self._event(entry_id, key)

    def status(self, entry_id):
        status = self.intake.status(entry_id)
        if status['custody_health'] != 'healthy':
            return dict(status,state='integrity_hold')
        prepared = self.attempt_event(entry_id,'prepared')
        if prepared:
            pid = prepared['publication_id']
            return dict(status, **self.previews.status(pid))
        if self.attempt_event(entry_id,'preview_started'):
            return dict(status,state='preview_interrupted_needs_review')
        if self.attempt_event(entry_id,'text_started'):
            return dict(status,state='text_receipt_needs_check')
        return dict(status,state='waiting_guide' if self.attempt_event(entry_id,'plan') else status['processing_state'])

    def prepare(self, entry_id, extraction_id, record, consent, policy, *, font_path,
                text_root, text_approval_sha, checked_source, generate_guide, images=(),
                curation_data=None, reader_context=None, hook=lambda point: None, revision=None, continuation=None,
                daily_attempt=None, checked_attempt=None):
        """Prepare one immutable candidate; repeated calls recover the same one.

        A missing terminal text receipt blocks automatic resubmission. A preview
        interrupted before its SQLite commit requires review of local remnants.
        Completed receipts can be reused without renewing an expired live grant.
        """
        if daily_attempt is not None:
            if (daily_attempt not in (1, 2) or type(daily_attempt) is not int
                    or not callable(checked_attempt) or revision is not None or continuation is not None):
                raise ContractError('explicit fenced daily generation attempt required')
            checked_attempt()
        try:
            with self.intake._serial():
                capacity = self.resources.check()
                if capacity['state'] != 'resources_ready':
                    return capacity
                job = self.intake._job(entry_id)
                if not job['task_id'] or job['stop_reason']:
                    raise ContractError('Pilot conversion is not ready for guide preparation')
                _, mapping, root = self.intake.conversion.publication_extraction(extraction_id, expected_task_id=job['task_id'])
                with self.intake.staging.input_binding(entry_id,'pilot_guide_and_preview'):
                    entry = self.registry.entry(entry_id)
                    if mapping['source_asset_id'] != entry['asset_id']:
                        raise ContractError('Pilot original identity mismatch')
                    body = (root/'source.md').read_bytes()
                    arguments = dict(curation_data=curation_data, reader_context=reader_context)
                    def current_binding(*, old=None):
                        checked_image_facts(self.intake.conversion, extraction_id, images, policy)
                        return sha(encoded(dict(entry_id=entry_id,mapping=mapping,record=record,
                            consent=old['previous_consent'] if old else consent,
                            policy=policy,images=list(images),font_sha256=file_sha(Path(font_path)),
                            text_root=old['previous_text_root'] if old else str(Path(text_root).absolute()),
                            text_approval_sha=old['previous_text_approval_sha256'] if old else text_approval_sha,
                            arguments=arguments)).encode())
                    binding = current_binding()
                    predecessor = self._event(entry_id,'plan')
                    successor = self._event(entry_id,'revision')
                    if revision is None and successor:
                        raise ContractError('explicit revision already exists; old entry cannot resume')
                    resumed = self._event(entry_id,'continuation')
                    if resumed and continuation is None:
                        raise ContractError('explicit continuation already exists; old revision cannot resume')
                    if continuation is not None and revision is None:
                        raise ContractError('continuation requires the original failed revision')
                    prefix = ''
                    if daily_attempt is not None:
                        if predecessor or successor or resumed:
                            raise ContractError('daily attempts cannot replace a legacy pipeline')
                        prefix = 'daily:'+str(daily_attempt)+':'
                        if daily_attempt == 2 and (not self._event(entry_id,'daily:1:text_started')
                                or any(self._event(entry_id,'daily:1:'+phase) for phase in ('preview_started','prepared','staged'))):
                            raise ContractError('daily recovery cannot replace an existing candidate')
                    if revision is not None:
                        revision_fields(revision)
                        prefix = 'revision:'+stable_key(entry_id,revision['previous_plan_sha256'])+':'
                        wanted = dict(attempt_id=prefix.split(':')[1], revision=revision,
                                      binding_sha256=binding)
                        if successor and continuation is None and successor != wanted:
                            raise ContractError('failed revision already bound; no second successor or new budget')

                    revision_prefix = prefix
                    if continuation is not None:
                        continuation_fields(continuation)
                        if (not successor or digest(successor) != continuation['previous_revision_sha256']
                                or successor['revision'] != revision):
                            raise ContractError('continuation must retain the exact previous revision')
                        prefix = 'continuation:'+stable_key(entry_id,continuation['previous_plan_sha256'])+':'
                        continued = dict(attempt_id=prefix.split(':')[1],continuation=continuation,binding_sha256=binding)
                        if resumed and resumed != continued:
                            raise ContractError('continuation already bound; no second continuation or new budget')

                    def event(phase): return self._event(entry_id,prefix+phase)
                    def save(db,phase,data): return self._save(db,entry_id,prefix+phase,data)

                    def check_revision():
                        if revision is None:
                            return
                        if (not predecessor or digest(self._event(entry_id,'plan')) != revision['previous_plan_sha256']
                                or predecessor['binding_sha256'] != current_binding(old=revision)
                                or not self._event(entry_id,'text_started')
                                or any(self._event(entry_id,p) for p in ('preview_started','prepared','staged'))):
                            raise ContractError('previous pipeline plan or failure state changed')
                        receipt = failed_receipt(revision,record,body,mapping,**arguments)
                        if digest(receipt) != revision['previous_receipt_sha256']:
                            raise ContractError('previous rejected evidence fingerprint changed')
                        check_new_approval(text_root,text_approval_sha,revision)
                        if continuation is not None:
                            previous = self._event(entry_id,revision_prefix+'plan')
                            if (not previous or digest(previous) != continuation['previous_plan_sha256']
                                    or previous['binding_sha256'] != current_binding(old=continuation)
                                    or not self._event(entry_id,revision_prefix+'text_started')
                                    or any(self._event(entry_id,revision_prefix+p)
                                           for p in ('preview_started','prepared','staged'))):
                                raise ContractError('previous revision plan or pre-turn state changed')
                            receipt = blocked_receipt(continuation,record,body,mapping,**arguments)
                            if digest(receipt) != continuation['previous_receipt_sha256']:
                                raise ContractError('previous stopped evidence fingerprint changed')
                            check_continuation_approval(text_root,text_approval_sha,continuation)
                        prepared = event('prepared')
                        rows = self.registry.rows('SELECT publication_id FROM publications_v21 WHERE entry_id=?',(entry_id,))
                        allowed = [prepared['publication_id']] if prepared else []
                        if sorted(r['publication_id'] for r in rows) != allowed:
                            raise ContractError('existing candidate or publication cannot enter failed revision')
                        if not self.registry.rows("""SELECT 1 FROM asset_references WHERE asset_id=?
                                AND reference_id=? AND kind='staging_dependency' AND released_at IS NULL""",
                                (entry['asset_id'],'pilot-pipeline:'+entry_id)):
                            raise ContractError('previous original retention dependency is missing')

                    def check_binding():
                        if daily_attempt is not None:
                            checked_attempt()
                        check_revision()
                        if current_binding() != binding:
                            raise ContractError('Pilot inputs changed during preparation')
                    check_revision()
                    if revision is not None and not successor:
                        with self.registry.transaction() as db:
                            self._save(db,entry_id,'revision',wanted)
                            save(db,'plan',dict(binding_sha256=binding,at=now()))
                    if continuation is not None and not resumed:
                        with self.registry.transaction() as db:
                            self._save(db,entry_id,'continuation',continued)
                            save(db,'plan',dict(binding_sha256=binding,at=now()))
                    plan = event('plan')
                    if plan and plan['binding_sha256'] != binding:
                        raise ContractError('Pilot plan changed; retain the existing attempt and request explicit revision')
                    if not plan:
                        with self.registry.transaction() as db:
                            save(db,'plan',dict(binding_sha256=binding,at=now()))
                            # Protect unfinished material even if this process dies.
                            # No timeout/PID/lock-based lease release is introduced.
                            db.execute('INSERT OR IGNORE INTO cloud_asset_keys VALUES (?,?,NULL)',
                                       (entry['asset_id'],entry['asset_id']))
                            statement = ("INSERT OR IGNORE INTO asset_references VALUES (?,?,'staging_dependency',NULL)"
                                         if daily_attempt is not None else
                                         "INSERT INTO asset_references VALUES (?,?,'staging_dependency',NULL)")
                            db.execute(statement, (entry['asset_id'],'pilot-pipeline:'+entry_id))
                    prepared = event('prepared')
                    if not prepared:
                        if event('preview_started'):
                            if daily_attempt is None or self.registry.rows(
                                    'SELECT 1 FROM publications_v21 WHERE entry_id=?',(entry_id,)):
                                raise ContractError('preview interrupted before commit; inspect retained candidate, no automatic rerender')
                            # No publication row means its preparation transaction
                            # never committed. Keep orphan files and rerender from
                            # the same validated guide under the active fence.
                            checked_attempt()
                        if not event('text_started'):
                            with self.registry.transaction() as db:
                                save(db,'text_started',dict(at=now()))
                            hook('before_text')
                            generate_guide(record,body,mapping,consent,**arguments)
                            hook('after_text')
                        result = load_validated_guide(text_root,text_approval_sha,record,body,mapping,consent,
                                                      checked_source=checked_source,**arguments)
                        capacity = self.resources.check()
                        if capacity['state'] != 'resources_ready':
                            return dict(capacity,entry_id=entry_id)
                        # Recheck original and extraction after the model wait.
                        with self.intake.staging.asset_lock(entry,shared=True) as directory:
                            self.intake.staging.verify_locked(entry,directory)
                        self.intake.conversion.publication_extraction(extraction_id, expected_task_id=job['task_id'])
                        check_binding()
                        result = with_image_facts(result, checked_image_facts(self.intake.conversion, extraction_id, images, policy))
                        def saved(db, pid):
                            check_binding()
                            save(db,'prepared',dict(publication_id=pid,binding_sha256=binding))
                        with self.registry.transaction() as db:
                            save(db,'preview_started',dict(at=now()))
                        self.publisher.prepare(entry_id,result[0],mapping,body,result[1],result[2],policy,
                            font_path=font_path,images=images,on_prepared=saved)
                        hook('after_preview_commit')
                        prepared = event('prepared')
                    pid = prepared['publication_id']
                    row = self.publisher._row(pid)
                    if row['entry_id'] != entry_id or row['state'] != 'prepared':
                        raise ContractError('candidate already has a separate lifecycle decision')
                    candidate = self.publisher._tree(row,pid+'.candidate')
                    self.publisher.candidate_review(row,candidate)
                    stage = self.previews.stage(pid)
                    hook('after_stage')
                    with self.registry.transaction() as db:
                        save(db,'staged',dict(publication_id=pid,version=stage['version']))
                    return dict(state='preview_ready_pending_delivery',entry_id=entry_id,**stage,
                        preview_path=str(candidate/'发布记录/已确认入库预览.pdf'),preview_sha256=row['preview_sha256'],
                        delivery_state='NOT_SENT',publication_state='prepared')
        except BlockingIOError:
            return dict(state='busy')
