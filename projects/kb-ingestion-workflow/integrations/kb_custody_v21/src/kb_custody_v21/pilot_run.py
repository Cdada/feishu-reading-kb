"""One explicitly planned material through the existing bounded Pilot adapters.

The caller supplies the approved guide/preview preparation function. This
controller cannot grant model access or approve content on the owner's behalf.
No polling daemon or production subscription is installed.
"""
import json
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from .contracts import ContractError, stable_key, validate_id
from .f05 import file_sha
from .pilot_availability import collection_report
from .pilot_decision import apply_owner_reply, reply_text
from .pilot_delivery import deliver_preview
from .pilot_feishu import read_json
from .pilot_lifecycle import PilotLifecycle
from .pilot_notice_workflow import PilotNoticeWorkflow
from .pilot_pipeline import PilotPipeline
from .pilot_revision import revision_fields
from .pilot_continuation import continuation_fields
from .runtime import no_symlinks
from .s6 import instant
from .staging import IntegrityError


class PilotBoundedRun:
    def __init__(self, session, intake, plan_path, *, prepare_candidate, checked_ready, resources=None):
        self.session, self.intake = session, intake
        self.plan_path = no_symlinks(Path(plan_path).absolute())
        self.plan_sha = file_sha(self.plan_path)
        source = session.binding()
        if source.get(str(self.plan_path)) != self.plan_sha:
            raise ContractError('run plan must be part of the approved frozen inputs')
        self.plan = plan = read_json(self.plan_path, 65536)
        fields = {'reference','sample_path','sample_sha256','max_pages','lookback_seconds'}
        if (set(plan) not in (fields, fields | {'duplicate_selection'}, fields | {'revision'},
                             fields | {'revision','continuation'}, fields | {'prepared_material'},
                             fields | {'revision','prepared_material'})
                or plan['reference'] != 'P11-single-material-v1'
                or type(plan['max_pages']) is not int or not 1 <= plan['max_pages'] <= 12
                or type(plan['lookback_seconds']) is not int or not 1 <= plan['lookback_seconds'] <= 86400
                or not callable(prepare_candidate) or not callable(checked_ready)):
            raise ContractError('exact single-material plan and readiness/preparation adapters required')
        if 'prepared_material' in plan:
            material = plan['prepared_material']
            if not isinstance(material, dict) or set(material) != {'entry_id', 'task_id', 'extraction_id'}:
                raise ContractError('exact existing material, task and expression required')
            for key, kind in (('entry_id', 'entry'), ('task_id', 'task'), ('extraction_id', 'extraction')):
                validate_id(material[key], kind)
        if 'revision' in plan:
            revision = plan['revision']
            if (not isinstance(revision,dict) or set(revision) != {'entry_id','binding'}
                    or not isinstance(revision['entry_id'],str) or not revision['entry_id']):
                raise ContractError('exact existing entry and failed revision binding required')
            revision_fields(revision['binding'])
            if 'prepared_material' in plan and plan['prepared_material']['entry_id'] != revision['entry_id']:
                raise ContractError('prepared revision must retain the same existing entry')
        if 'continuation' in plan:
            continuation_fields(plan['continuation'])
        if 'duplicate_selection' in plan:
            from .pilot_duplicate import validate_selection
            selection = validate_selection(plan['duplicate_selection'])
            if selection['sha256'] != plan['sample_sha256']:
                raise ContractError('duplicate selection belongs to a different planned sample')
        self.sample = no_symlinks(Path(plan['sample_path']).absolute())
        if (str(self.sample) != plan['sample_path'] or self.sample.suffix.lower() not in {'.md','.txt','.pdf','.webarchive'}
                or source.get(str(self.sample)) != plan['sample_sha256'] or not self.sample.is_file()):
            raise ContractError('planned material must have its own exact approved byte binding')
        self.base_source = session.checked_source
        self.prepare_candidate, self.checked_ready = prepare_candidate, checked_ready
        self.pipeline = PilotPipeline(intake, resources=resources)
        self.lifecycle = PilotLifecycle(session, intake, resources=self.pipeline.resources)
        self.notices = PilotNoticeWorkflow(session, intake)
        self.prefix = 'pilot-run:'+self.plan_sha+':'
        self.closed = False
        self.binding_callback = self._source
        session.checked_source = self.binding_callback
        try:
            session.check()
        except BaseException:
            self.close()
            raise

    def _source(self):
        source = dict(self.base_source())
        if (source.get(str(self.plan_path)) != self.plan_sha or file_sha(self.plan_path) != self.plan_sha
                or read_json(self.plan_path, 65536) != self.plan):
            raise ContractError('frozen material plan changed')
        original = self.session.value(self.prefix+'original')
        if original:
            row = self.intake.registry.entry(original['entry_id'])
            expected = dict(entry_id=row['entry_id'], path=str(self.intake.runtime.path(row['relative_path'])),
                sha256=row['sha256'], size_bytes=row['size_bytes'])
            if (original != expected or row['status'] != 'accepted' or row['custody_health'] != 'healthy'
                    or row['sha256'] != self.plan['sample_sha256']
                    or row['size_bytes'] != self.sample.stat().st_size):
                raise ContractError('derived original no longer matches the approved material')
            self.notices._custody_item(row['entry_id'])
            with self.intake.staging.asset_lock(row, shared=True) as directory:
                # This callback also runs inside publication's write transaction.
                # Verify bytes without opening another writer; alert after rollback.
                try:
                    actual = self.intake.staging.file_evidence(directory, row['safe_filename'])
                except OSError as error:
                    raise IntegrityError('bound Pilot original cannot be read') from error
                self.intake.staging.compare(actual, {k: row[k] for k in ('size_bytes','sha256','md5')})
            source[original['path']] = original['sha256']
        return source

    def close(self):
        if not self.closed:
            if self.session.checked_source is not self.binding_callback:
                raise ContractError('run source checker was replaced')
            self.session.checked_source = self.base_source
            self.closed = True

    def check(self):
        if self.closed:
            raise ContractError('run controller closed')
        self.session.check()

    def _scan(self):
        self.check()
        sid = 'scan_'+stable_key(self.prefix, 'intake')
        bound = self.session.value(self.prefix+'interval')
        if not bound:
            end = self.session.clock()
            start = max(end-timedelta(seconds=self.plan['lookback_seconds']),
                        instant(self.session.approval['starts_at'])-timedelta(hours=24))
            bound = dict(start=start.isoformat(), end=end.isoformat())
            self.session.bind(self.prefix+'interval', bound)
        self.session.scan(sid, start=instant(bound['start']), end=instant(bound['end']),
                          max_pages=self.plan['max_pages'])
        return sid

    @contextmanager
    def _integrity_boundary(self):
        try:
            yield
        except IntegrityError:
            original = self.session.value(self.prefix+'original')
            if original:
                self.intake.registry.health(original['entry_id'], 'suspect', 'bound Pilot original verification failed')
            raise

    def run_to_preview(self):
        """Run once after the owner posts the planned file; never guess a duplicate."""
        with self._integrity_boundary():
            return self._run_to_preview()

    def run_to_conversion(self):
        """Accept and convert once, then close intake for explicit local review."""
        with self._integrity_boundary():
            return self._run_to_preview(prepare_guide=False)

    def _run_to_preview(self, *, prepare_guide=True):
        self.check()
        if 'revision' in self.plan or 'prepared_material' in self.plan:
            raise ContractError('existing material plan cannot reopen intake')
        with self.lifecycle.running():
            self.session.verify_isolation()
            ready = self.lifecycle.start(checked_ready=self.checked_ready)
            if ready['delivery_state'] != 'delivered':
                return dict(state='stopped_notice_unknown', notice=ready)
            self.lifecycle.admit()
            sid = self._scan()
            report = collection_report(self.session, self.intake, sid)
            matches = [m for m in report['materials'] if m['filename'] == self.sample.name]
            if len(matches) != 1:
                notice = self.notices.collection(sid)
                if notice['delivery_state'] != 'delivered':
                    return dict(state='stopped_notice_unknown', notice=notice)
                result = dict(state='waiting_planned_material' if not matches else 'ambiguous_planned_material',
                              report=report, accepted=False)
            else:
                mid = matches[0]['message_id']
                self.session.bind(self.prefix+'selected', dict(scan_id=sid, message_id=mid))
                self.lifecycle.admit()
                accepted = self.session.receive_file(sid, mid, self.intake, sample_path=self.sample)
                eid = accepted['receipt']['entry_id']
                row = self.intake.registry.entry(eid)
                self.session.bind(self.prefix+'original', dict(entry_id=eid,
                    path=str(self.intake.runtime.path(row['relative_path'])), sha256=row['sha256'], size_bytes=row['size_bytes']))
                self.check()
                notice = self.notices.custody(eid)
                if notice['delivery_state'] != 'delivered':
                    return dict(state='stopped_notice_unknown', entry_id=eid, notice=notice)
                notice = self.notices.collection(sid)
                if notice['delivery_state'] != 'delivered':
                    return dict(state='stopped_notice_unknown', entry_id=eid, notice=notice)
                self.lifecycle.admit()
                self.pipeline.convert_once(entry_id=eid, duplicate_selection=self.plan.get('duplicate_selection'))
                state = self.intake.status(eid)
                if state['processing_state'] not in {'ready_candidate', 'partial_ready'}:
                    result = dict(state='material_needs_review', entry_id=eid, conversion=state)
                elif not prepare_guide:
                    result = dict(state='converted_awaiting_preparation', entry_id=eid, conversion=state)
                else:
                    self.lifecycle.admit()
                    # Preparation retains its existing qualification, model,
                    # source, lease, image and exact-PDF gates.
                    candidate = self.prepare_candidate(self.pipeline, eid)
                    self.check()
                    if candidate.get('state') in {'paused_resources', 'paused_capacity', 'busy'}:
                        result = dict(candidate, entry_id=eid, pause_notice=self.lifecycle.pause(),
                                      service_admission_open=False)
                        self.session.bind(self.prefix+'result', result)
                        return result
                    pid = candidate.get('publication_id')
                    prepared = self.pipeline.attempt_event(eid, 'prepared')
                    if not prepared or prepared['publication_id'] != pid:
                        raise ContractError('preparation must return this material\'s persisted pipeline candidate')
                    self.session.bind(self.prefix+'candidate', dict(entry_id=eid, publication_id=pid))
                    self.lifecycle.admit()
                    result = deliver_preview(self.session, self.pipeline.previews, pid)
                    result = dict(result, state='awaiting_owner_decision', entry_id=eid)
            result['pause_notice'] = self.lifecycle.pause()
            result['service_admission_open'] = False
            self.session.bind(self.prefix+'result', result)
            return result

    def prepared_to_preview(self):
        """Continue the exact accepted Pilot job after local image/review preparation.

        No intake, conversion, image inference or implicit review is performed.
        An existing failed/unknown text attempt still reaches the pipeline's
        immutable binding checks; this is not a replacement retry grant.
        """
        with self._integrity_boundary():
            self.check()
            material = self.plan.get('prepared_material')
            if 'revision' in self.plan:
                raise ContractError('explicit revision must use the revision action')
            if material is None:
                raise ContractError('explicit prepared material plan required')
            eid = material['entry_id']
            self.notices._custody_item(eid)
            row = self.intake.registry.entry(eid)
            job = self.intake._job(eid)
            if (job['task_id'] != material['task_id'] or job['stop_reason']
                    or row['sha256'] != self.plan['sample_sha256']
                    or row['size_bytes'] != self.sample.stat().st_size):
                raise ContractError('prepared material differs from its accepted original or conversion task')
            self.session.bind(self.prefix+'original',dict(entry_id=eid,
                path=str(self.intake.runtime.path(row['relative_path'])),sha256=row['sha256'],size_bytes=row['size_bytes']))
            self.check()
            self.intake.conversion.publication_extraction(material['extraction_id'], expected_task_id=job['task_id'])
            capacity = self.pipeline.resources.check()
            if capacity['state'] != 'resources_ready':
                return dict(capacity, entry_id=eid, service_admission_open=False)
            if self.checked_ready() is not True:
                raise ContractError('prepared guide dependencies are not ready')
            self.session.verify_isolation()
            candidate = self.prepare_candidate(self.pipeline,eid)
            self.check()
            if candidate.get('state') in {'paused_resources','paused_capacity','busy'}:
                return dict(candidate,entry_id=eid,service_admission_open=False)
            pid = candidate.get('publication_id')
            prepared = self.pipeline.attempt_event(eid,'prepared')
            if not prepared or prepared['publication_id'] != pid:
                raise ContractError("preparation must return this material's persisted pipeline candidate")
            self.session.bind(self.prefix+'candidate',dict(entry_id=eid,publication_id=pid))
            result = dict(deliver_preview(self.session,self.pipeline.previews,pid),
                state='awaiting_owner_decision',entry_id=eid,service_admission_open=False)
            self.session.bind(self.prefix+'result',result)
            return result

    def revise_to_preview(self):
        """Use one existing failed entry; no intake scan, download or conversion.

        Receiving remains closed. The new grant still checks fixed identities,
        original bytes, exact previous failure, candidate and delivery receipts.
        """
        with self._integrity_boundary():
            self.check()
            revision = self.plan.get('revision')
            if not revision:
                raise ContractError('explicit failed revision run plan required')
            eid = revision['entry_id']
            self.notices._custody_item(eid)
            row = self.intake.registry.entry(eid)
            if row['sha256'] != self.plan['sample_sha256'] or row['size_bytes'] != self.sample.stat().st_size:
                raise ContractError('revision original differs from the approved material')
            self.session.bind(self.prefix+'original',dict(entry_id=eid,
                path=str(self.intake.runtime.path(row['relative_path'])),sha256=row['sha256'],size_bytes=row['size_bytes']))
            self.check()
            capacity = self.pipeline.resources.check()
            if capacity['state'] != 'resources_ready':
                return dict(capacity,entry_id=eid,service_admission_open=False)
            if self.checked_ready() is not True:
                raise ContractError('revision dependencies are not ready')
            self.session.verify_isolation()
            candidate = self.prepare_candidate(self.pipeline,eid)
            self.check()
            if candidate.get('state') in {'paused_resources','paused_capacity','busy'}:
                return dict(candidate,entry_id=eid,service_admission_open=False)
            pid = candidate.get('publication_id')
            prepared = self.pipeline.attempt_event(eid,'prepared')
            if not prepared or prepared['publication_id'] != pid or not self.pipeline._event(eid,'revision'):
                raise ContractError('revision must return its persisted pipeline candidate')
            self.session.bind(self.prefix+'candidate',dict(entry_id=eid,publication_id=pid))
            result = dict(deliver_preview(self.session,self.pipeline.previews,pid),
                state='awaiting_owner_decision',entry_id=eid,service_admission_open=False)
            self.session.bind(self.prefix+'result',result)
            return result

    def owner_reply(self, scan_id, message_id):
        """Explicit bounded follow-up, after a separately completed verified scan.

        Only the saved candidate's direct reply is eligible. This does not resume
        intake or fabricate a reply because the owner is away.
        """
        with self._integrity_boundary():
            return self._owner_reply(scan_id, message_id)

    def _owner_reply(self, scan_id, message_id):
        self.check()
        candidate = self.session.value(self.prefix+'candidate')
        if not candidate:
            raise ContractError('this run has no exact preview candidate')
        delivery = self.intake.registry.rows('SELECT message_id FROM pilot_preview_deliveries WHERE publication_id=?',
                                             (candidate['publication_id'],))
        item = self.session.message(scan_id, message_id)
        parent, _ = reply_text(item)
        if len(delivery) != 1 or delivery[0]['message_id'] != parent:
            raise ContractError('reply must address this run\'s exact delivered preview')
        result = apply_owner_reply(self.session, self.pipeline.previews, scan_id, message_id)
        if result['status'] == 'published':
            # Publishing remains true even if the following notification is unknown.
            result['notification'] = self.notices.publication(result['publication_id'])
        return result
