"""One finite Pilot run, with persisted admission and orderly service notices.

This does not install a listener or keep the machine awake. The bounded caller
holds this controller while doing serial work, then explicitly pauses it.
An unexpected exit leaves a closed/reviewable run, not proof of a sent notice.
"""
from contextlib import contextmanager
import fcntl
import json
import os

from .contracts import ContractError, stable_key
from .locking import open_lock
from .pilot_availability import PilotServiceNotices, check_scope
from .pilot_notices import PilotExecutionSession, PilotNoticeSender
from .pilot_resources import PilotResources


class PilotLifecycle:
    def __init__(self, session, intake, *, resources=None):
        if not isinstance(session, PilotExecutionSession):
            raise ContractError('independent approved Pilot session required')
        check_scope(intake, session.approval)
        self.session, self.intake, self.registry = session, intake, intake.registry
        self.resources = resources if resources is not None else PilotResources(self.registry)
        self.notices = PilotServiceNotices(intake)
        self.run_id = stable_key('pilot-run', session.approval_sha)
        self._held = False
        self._checked_ready = None

    def _events(self):
        return [json.loads(r['payload_json']) for r in self.registry.rows(
            "SELECT payload_json FROM custody_events WHERE event_type='pilot_lifecycle' AND aggregate_id=? ORDER BY rowid",
            (self.run_id,))]

    def status(self):
        events = self._events()
        value = events[-1] if events else dict(state='not_started', admission_open=False)
        # A historical ready record is not evidence that its process is alive.
        return dict(value, admission_open=self._held and value['admission_open'],
                    process_state='owned_here' if self._held else 'not_observed_here', run_id=self.run_id)

    def _state(self, state, admission_open=False, **detail):
        if not self._held:
            raise ContractError('lifecycle ownership required')
        with self.registry.transaction() as db:
            sequence = db.execute("SELECT count(*) FROM custody_events WHERE event_type='pilot_lifecycle' AND aggregate_id=?",
                                  (self.run_id,)).fetchone()[0]
            self.registry.event(db, 'pilot_lifecycle', self.run_id,
                stable_key(self.run_id, sequence), dict(state=state, admission_open=admission_open, **detail))

    @contextmanager
    def running(self):
        if self._held:
            raise ContractError('lifecycle owner cannot enter twice')
        runtime = self.intake.runtime
        runtime.gate()
        fd = open_lock(runtime.checked(runtime.root/'conversion-work/pilot-lifecycle.lock'))
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._held = True
            prior = self._events()
            if prior:
                # Never turn a saved ready/intent into a new run or resend.
                if prior[-1]['state'] not in {'stopped', 'interrupted'}:
                    self._state('interrupted', reason='previous_run_requires_review')
                raise ContractError('this run already started; inspect persisted status without restarting')
            self._state('starting')
            try:
                yield self
            finally:
                if self._events()[-1]['state'] not in {'stopped', 'interrupted'}:
                    # No network from exception cleanup, including expired windows.
                    self._state('interrupted', reason='controller_exit_without_orderly_pause')
        finally:
            self._checked_ready = None
            self._held = False
            os.close(fd)

    def _ready(self):
        self.session.check()
        check_scope(self.intake, self.session.approval)
        self.intake.runtime.gate()
        if self.session.value('isolation_verified') is not True:
            raise ContractError('fixed group isolation must be verified before admission')
        if self.resources.check()['state'] != 'resources_ready':
            raise ContractError('resources do not permit new Pilot work')
        if self._checked_ready is None or self._checked_ready() is not True:
            raise ContractError('bounded executor is not ready to accept the approved material')
        self.session.check()
        return True

    def _checked_phase(self, phase):
        state = self.status()
        if not self._held:
            return False
        if phase == 'ready':
            return state['state'] == 'ready' and state['admission_open'] and self._ready()
        return phase == 'pausing' and state['state'] == 'pausing' and not state['admission_open']

    def _notice(self, phase):
        plan = self.notices.prepare(self.run_id, phase, checked_state=self._checked_phase)

        def authoritative():
            plans = [e['plan'] for e in self.notices._events() if 'plan' in e]
            if not plans or plans[-1] != plan or not self._checked_phase(phase):
                raise ContractError('service phase or authoritative plan changed')
            return plan

        sender = PilotNoticeSender(self.session, 'service', authoritative)
        return self.notices.dispatch(plan['notification_id'], sender,
            checked_access=self.session.check, checked_state=self._checked_phase)

    def start(self, *, checked_ready):
        if not self._held or self.status()['state'] != 'starting' or not callable(checked_ready):
            raise ContractError('new owned run and explicit readiness callback required')
        self._checked_ready = checked_ready
        self._ready()
        self._state('ready', admission_open=True)
        try:
            result = self._notice('ready')
        except (OSError, ContractError):
            self._state('interrupted', reason='readiness_notice_not_completed')
            raise
        if result['delivery_state'] != 'delivered':
            self._state('interrupted', reason='readiness_notice_not_confirmed')
        return result

    def admit(self):
        """Check before each bounded intake/processing step; never nest intake locks."""
        if not self._held or self.status()['state'] != 'ready' or not self.status()['admission_open']:
            raise ContractError('Pilot admission is closed')
        try:
            return self._ready()
        except (OSError, ContractError):
            self._state('paused', reason='admission_checks_failed')
            raise

    def pause(self):
        if not self._held or self.status()['state'] not in {'ready', 'paused'}:
            raise ContractError('only an owned active run can pause')
        # Commit closed admission first. Even notice failure cannot reopen it.
        self._state('pausing')
        result = dict(delivery_state='not_attempted')
        try:
            result = self._notice('pausing')
            return result
        finally:
            self._state('stopped', pause_notice=result['delivery_state'])
