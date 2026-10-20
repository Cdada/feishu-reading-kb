"""Bind metered notices to existing custody and observed collection facts.

No general outbox drain, new notification identity, remote recovery or implicit
retry. A live caller still needs the independent approved execution session.
"""
from contextlib import contextmanager
import fcntl
import json
import os

from .contracts import ContractError, LINEAGE_FIELDS, SUCCESS, stable_key
from .locking import open_lock
from .notifications import DeliveryUnknown, OutboxWorker
from .pilot_availability import check_scope, collection_report
from .pilot_notices import PilotExecutionSession, PilotNoticeSender, collection_text
from .publication import Publications
from .registry import encoded
from .staging import IntegrityError


class PilotNoticeWorkflow:
    def __init__(self, session, intake):
        if not isinstance(session, PilotExecutionSession):
            raise ContractError('independent approved Pilot session required')
        check_scope(intake, session.approval)
        self.session, self.intake = session, intake
        self.registry, self.staging = intake.registry, intake.staging

    @contextmanager
    def _serial(self):
        runtime = self.intake.runtime
        runtime.gate()
        fd = open_lock(runtime.checked(runtime.root/'conversion-work/pilot-business-notices.lock'))
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)

    def _custody_item(self, entry_id):
        check_scope(self.intake, self.session.approval)
        job = self.intake._job(entry_id)
        messages = self.registry.rows('SELECT envelope_json FROM pilot_intake_messages WHERE message_key=?',
                                      (job['message_key'],))
        if len(messages) != 1:
            raise ContractError('one persisted Pilot intake envelope required')
        envelope = json.loads(messages[0]['envelope_json'])
        if (envelope['app_id'], envelope['user_id']) != (self.intake.scope.app_id, self.intake.scope.user_id):
            raise ContractError('custody envelope identity changed')
        row = self.registry.entry(entry_id)
        contract = envelope['contracts'][job['resource_index']]
        lineage = {key: contract.get('lineage', {}).get(key) for key in LINEAGE_FIELDS}
        lineage.update(intake_id=row['intake_id'], received_filename=contract['received_filename'])
        intakes = self.registry.rows('SELECT replay_key FROM intakes WHERE intake_id=?', (row['intake_id'],))
        # Compare semantic fields: the persisted envelope sorts JSON keys, while
        # the original contract digest intentionally preserves its input order.
        if (len(intakes) != 1 or intakes[0]['replay_key'] != contract['intake_key']
                or (contract['entry_key'], lineage, contract.get('expected', {}), contract['received_filename']) != (
                    row['replay_key'], json.loads(row['lineage_json']),
                    json.loads(row['expected_json']), row['received_filename'])):
            raise ContractError('custody source differs from the frozen envelope')
        if contract.get('group') is not None:
            raise ContractError('bounded Pilot custody notice requires a single source; preserve group notice')
        items = self.registry.rows("""SELECT * FROM notification_outbox WHERE
            scope_type='single_source' AND scope_id=? AND kind='success' AND intake_id=?""",
            (row['acceptance_id'], row['intake_id']))
        if (row['status'] != 'accepted' or len(items) != 1 or items[0]['payload'] != SUCCESS
                or items[0]['target_chat_id'] != self.intake.scope.chat_id):
            raise ContractError('one accepted original and its exact success notice required')
        return row, items[0]

    def custody(self, entry_id):
        """Only this entry's original success notice, held under the asset lock."""
        with self._serial():
            row, item = self._custody_item(entry_id)
            if item['delivery_state'] != 'pending':
                # Sending/unknown is reported locally; no lookup or lease stealing.
                return dict(item, delivery_state=('delivery_unknown'
                    if item['delivery_state'] == 'sending' else item['delivery_state']))
            with self.staging.input_binding(entry_id, 'pilot_custody_notice') as binding:
                def checked_item():
                    current, notice = self._custody_item(entry_id)
                    if (current['asset_id'], current['sha256'], current['relative_path']) != (
                            row['asset_id'], row['sha256'], row['relative_path']):
                        raise ContractError('custody original binding changed')
                    with self.staging.asset_lock(current, shared=True) as directory:
                        self.staging.verify_locked(current, directory)
                    if self.session.binding().get(binding['path']) != row['sha256']:
                        raise ContractError('custody original must be a frozen approved input')
                    return notice
                checked_item()
                sender = PilotNoticeSender(self.session, 'custody', checked_item)
                try:
                    OutboxWorker(self.registry, sender).drain(1, notification_id=item['notification_id'])
                except BaseException:
                    # A crashed attempt may have reached the provider; don't
                    # release protection just because the Python stack unwound.
                    binding['retain_lease'] = True
                    raise
                finally:
                    try:
                        with self.staging.asset_lock(row, shared=True) as directory:
                            self.staging.verify_locked(row, directory)
                    except IntegrityError:
                        binding['retain_lease'] = True
                        raise
                result = self.registry.rows('SELECT * FROM notification_outbox WHERE notification_id=?',
                                            (item['notification_id'],))[0]
                if result['delivery_state'] in {'sending', 'delivery_unknown'}:
                    binding['retain_lease'] = True
                return result

    def _reports(self, aggregate):
        return [json.loads(r['payload_json']) for r in self.registry.rows(
            "SELECT payload_json FROM custody_events WHERE event_type='pilot_collection_notice' AND aggregate_id=? ORDER BY rowid",
            (aggregate,))]

    def _publication_plan(self, pid):
        publisher = Publications(self.registry)
        row = publisher._row(pid)
        entry, _ = self._custody_item(row['entry_id'])
        decisions = self.registry.rows('SELECT * FROM pilot_preview_decisions WHERE publication_id=?', (pid,))
        confirmations = self.registry.rows('SELECT * FROM publication_confirmations WHERE publication_id=?', (pid,))
        proof = self.registry.rows("SELECT payload_json FROM custody_events WHERE dedupe_key=?", ('pilot-decision:'+pid,))
        active = self.registry.rows('SELECT publication_id FROM active_publications_v21 WHERE source_asset_id=?', (row['source_asset_id'],))
        if (row['state'] != 'published' or len(decisions) != 1 or len(confirmations) != 1 or len(proof) != 1
                or active != [dict(publication_id=pid)]
                or (decisions[0]['app_id'], decisions[0]['chat_id'], decisions[0]['user_id']) != (
                    self.intake.scope.app_id, self.intake.scope.chat_id, self.intake.scope.user_id)
                or json.loads(proof[0]['payload_json'])['reference'] != 'P11-owner-reply-v1'):
            raise ContractError('final notice requires this owner\'s verified published version')
        publisher._tree(row, pid)
        if self.session.binding().get(str(self.intake.runtime.path(entry['relative_path']))) != entry['sha256']:
            raise ContractError('published original is outside the approved source binding')
        nid = stable_key('pilot-publication-notice', pid)
        return row, dict(notification_id=nid, provider_request_key='pilot-pub-'+nid[:40],
            target_chat_id=self.intake.scope.chat_id,
            payload=f"已按您确认的预览将 {encoded(entry['received_filename'])} 保存到本机知识库。\n百度网盘人工备份及下载校验尚未完成；请保留原件。")

    def publication(self, pid):
        """One immutable final notice; notification failure cannot undo publication."""
        with self._serial():
            row, plan = self._publication_plan(pid)
            aggregate = plan['notification_id']
            def events():
                return [json.loads(r['payload_json']) for r in self.registry.rows(
                    "SELECT payload_json FROM custody_events WHERE event_type='pilot_publication_notice' AND aggregate_id=? ORDER BY rowid",
                    (aggregate,))]
            def save(key, payload):
                with self.registry.transaction() as db:
                    self.registry.event(db, 'pilot_publication_notice', aggregate, aggregate+':'+key, payload)
            prior = events()
            attempted = [v for v in prior if v.get('attempt')]
            if attempted:
                return dict(attempted[-1], delivery_state=attempted[-1].get('delivery_state', 'delivery_unknown'))
            plans = [v['plan'] for v in prior if 'plan' in v]
            if plans and plans != [plan]:
                raise ContractError('saved publication notice differs')
            if not plans:
                save('plan', dict(plan=plan))
            with self.staging.input_binding(row['entry_id'], 'pilot_publication_notice') as binding:
                def check():
                    _, current = self._publication_plan(pid)
                    if current != plan or [v['plan'] for v in events() if 'plan' in v] != [plan]:
                        raise ContractError('publication notice changed')
                    return plan
                check()
                self.session.check()
                attempt = aggregate+':send'
                save('intent', dict(attempt=attempt))
                try:
                    mid = PilotNoticeSender(self.session, 'publication', check).send(plan, attempt)
                    result = dict(attempt=attempt, delivery_state='delivered', provider_message_id=mid)
                except DeliveryUnknown:
                    binding['retain_lease'] = True
                    result = dict(attempt=attempt, delivery_state='delivery_unknown')
                except BaseException:
                    binding['retain_lease'] = True
                    raise
                try:
                    save('result', result)
                except BaseException:
                    binding['retain_lease'] = True
                    raise
                return result

    def _report_event(self, aggregate, key, payload):
        with self.registry.transaction() as db:
            self.registry.event(db, 'pilot_collection_notice', aggregate, key, payload)

    def collection(self, scan_id):
        """Freeze one report for a scan. Changed custody needs a new deliberate scan.

        An unfinished intent is unknown forever on this route. A repeated call
        cannot create a new report/key merely because the underlying state moved.
        """
        aggregate = stable_key('pilot-collection', self.session.approval_sha, scan_id)
        with self._serial():
            events = self._reports(aggregate)
            attempted = [e for e in events if e.get('attempt')]
            if attempted:
                return dict(attempted[-1], delivery_state=attempted[-1].get('delivery_state', 'delivery_unknown'))
            report = collection_report(self.session, self.intake, scan_id)
            plans = [e['plan'] for e in events if 'plan' in e]
            plan = dict(notification_id=aggregate, provider_request_key='pilot-rpt-'+aggregate[:40],
                target_chat_id=self.intake.scope.chat_id, payload=collection_text(report), report=report)
            if plans and plans != [plan]:
                raise ContractError('saved collection report changed; keep it and prepare a new bounded scan')
            if not plans:
                self._report_event(aggregate, aggregate+':plan', dict(plan=plan))

            def checked_item():
                saved = [e['plan'] for e in self._reports(aggregate) if 'plan' in e]
                if saved != [plan] or collection_report(self.session, self.intake, scan_id) != report:
                    raise ContractError('collection report changed before receipt confirmation')
                return plan

            checked_item()
            self.session.check()
            attempt = aggregate+':send'
            self._report_event(aggregate, attempt+':intent', dict(attempt=attempt, notice=aggregate))
            try:
                mid = PilotNoticeSender(self.session, 'collection', checked_item).send(plan, attempt)
                result = dict(attempt=attempt, notice=aggregate, delivery_state='delivered', provider_message_id=mid)
            except DeliveryUnknown:
                result = dict(attempt=attempt, notice=aggregate, delivery_state='delivery_unknown')
            self._report_event(aggregate, attempt+':result', result)
            return result
