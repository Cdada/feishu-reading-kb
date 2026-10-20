"""Independent collection/confirmation and one serial heavy worker.

The adapter is the only component with platform/model capabilities. It must
reconcile persisted material facts on each step before changing scheduler state.
The scheduler never converts a transport timeout into another model request.
"""
from concurrent.futures import ThreadPoolExecutor
import threading
import time

from .contracts import ContractError
from .daily_store import Deferred
from .storage_layout import StorageUnavailable


HEAVY_PHASES = ('registered', 'original_saved', 'converted', 'generating',
                'model_unknown', 'guide_ready', 'preview_ready', 'send_unknown', 'reading')
LIGHT_PHASES = ('awaiting_images', 'awaiting_confirmation', 'confirmed', 'published', 'reading_wait')


class MaterialProblem(Exception):
    """Expected, isolated document failure with an actionable user explanation."""
    def __init__(self, reason, action='contact_codex'):
        super().__init__(reason)
        self.reason, self.action = reason, action


class DailyScheduler:
    def __init__(self, store, adapter, *, stop=None, monotonic=time.monotonic, external_stop=None):
        self.store, self.adapter = store, adapter
        self.stop = stop if stop is not None else threading.Event()
        self.monotonic = monotonic
        self.external_stop=external_stop
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='kb-daily-material')
        self.future = None
        self.last_poll = None
        self.closed = False

    def _heartbeat(self, state, **details):
        self.store.put('heartbeat', dict(at=self.store.now(), state=state, **details))

    def _isolated(self, document, method):
        try:
            method(document)
        except StorageUnavailable as error:
            self.store.put('storage_wait', dict(reason=error.reason, message=str(error), at=self.store.now()))
            self.store.put('defer:'+document['message_id'], dict(until=self.store.now()+self.store.policy.poll_seconds, reason='storage_unavailable'))
        except Deferred as error:
            # Durable due times prevent an exhausted oldest item from starving
            # later work. The rest of the service continues collecting replies.
            self.store.put('defer:'+document['message_id'], dict(
                until=error.until or self.store.now()+self.store.policy.poll_seconds,
                reason=error.reason))
        except MaterialProblem as error:
            fresh = self.store.document(document['message_id'])
            if fresh['phase'] in ('published', 'notified'):
                # A notice failure is never allowed to unpublish a document.
                self.store.put('defer:'+document['message_id'], dict(
                    until=self.store.now()+self.store.policy.poll_seconds, reason=error.reason))
            else:
                self.store.advance(fresh['message_id'], fresh['phase'], 'held',
                                   user_action=error.action, reason=error.reason)

    def _documents(self, phases):
        placeholders = ','.join('?' for _ in phases)
        rows = self.store.rows('SELECT message_id FROM daily_documents WHERE phase IN ('+placeholders+') ORDER BY created,message_id', phases)
        for row in rows:
            deferred = self.store.get('defer:'+row['message_id'])
            if deferred and deferred['until'] > self.store.now():
                continue
            yield self.store.document(row['message_id'])

    def _heavy_step(self, document):
        self.adapter.check_authorization()
        self._isolated(document, self.adapter.advance_heavy)

    def report_status(self):
        """One bounded report, also usable immediately before a trial stops."""
        self.adapter.check_authorization()
        try:
            self.adapter.report_status()
        except Deferred as error:
            self.store.put('report_wait', dict(reason=error.reason, until=error.until))
            return False
        except StorageUnavailable as error:
            self.store.put('storage_wait', dict(reason=error.reason, message=str(error), at=self.store.now()))
            self.store.put('report_wait', dict(reason='storage_unavailable'))
            return False
        except MaterialProblem as error:
            self.store.put('report_wait', dict(reason=error.reason, action=error.action))
            return False
        self.store.put('report_wait', None)
        return True

    def tick(self):
        if self.closed:
            raise ContractError('daily scheduler is closed')
        self.adapter.check_authorization()
        if self.future is not None and self.future.done():
            # Unexpected code/configuration failures are observable and stop the
            # supervisor; they are not misclassified as retryable file errors.
            self.future.result()
            self.future = None
        if self.stop.is_set() or self.store.get('stop_requested') is True or self.external_stop is not None and self.external_stop():
            self.stop.set()
            self._heartbeat('stopping', heavy_active=self.future is not None)
            return False
        current = self.monotonic()
        if self.last_poll is None or current-self.last_poll >= self.store.policy.poll_seconds:
            try:
                self.adapter.collect()
            except Deferred as error:
                self.store.put('collection_wait', dict(reason=error.reason, until=error.until))
            else:
                self.store.put('collection_wait', None)
            self.report_status()
            self.last_poll = self.monotonic()
        # A slow generation runs on its own worker. Exact owner decisions and
        # local publication use separate short registry transactions here.
        for document in self._documents(LIGHT_PHASES):
            if self.stop.is_set():
                break
            self._isolated(document, self.adapter.advance_light)
        if self.future is None and not self.stop.is_set():
            document = next(self._documents(HEAVY_PHASES), None)
            if document is not None:
                self.future = self.pool.submit(self._heavy_step, document)
        self._heartbeat('running', heavy_active=self.future is not None,
                        collection_wait=self.store.get('collection_wait'))
        return True

    def run(self):
        try:
            self.adapter.recover()
            while self.tick():
                self.stop.wait(1)
        except BaseException as error:
            self._heartbeat('interrupted', error_type=type(error).__name__, detail=str(error))
            raise
        finally:
            self.close()

    def close(self):
        if self.closed:
            return
        self.stop.set()
        # Request bounded subprocess cancellation from the adapter; it records
        # uncertainty if a remote effect cannot be proved absent.
        self.adapter.request_stop()
        try:
            self.pool.shutdown(wait=True, cancel_futures=True)
            if self.future is not None and not self.future.cancelled():
                self.future.result()
        finally:
            close = getattr(self.adapter, 'close', None)
            try:
                if close is not None:
                    close()
            except BaseException as error:
                self._heartbeat('interrupted', error_type=type(error).__name__, detail=str(error))
                raise
            else:
                previous = self.store.get('heartbeat') or {}
                if previous.get('state') != 'interrupted':
                    self._heartbeat('stopped', heavy_active=False)
            finally:
                self.closed = True
