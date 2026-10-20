"""Synthetic daily composition using the real custody/conversion/publication DB."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from kb_custody_v21.daily_backend import DailyBackend
from kb_custody_v21.daily_policy import DailyPolicy
from kb_custody_v21.daily_store import DailyStore
from kb_custody_v21.daily_store import Deferred
from kb_custody_v21.daily_scheduler import MaterialProblem
from kb_custody_v21.f05 import file_sha
from kb_custody_v21.registry import encoded
from kb_custody_v21.preview_name import preview_filename
from pilot_pipeline_fixture import ready
import test_pilot_inbox_guide as fixture


class LocalTransport:
    def __init__(self, authority):
        self.a=authority
        self.calls=[]
        self.results={}

    def _checked_result(self,result):
        for path,digest in result['evidence'].items():
            if file_sha(Path(path))!=digest:
                raise AssertionError('synthetic receipt changed')
        return result

    def request(self,operation,kind,path,**kwargs):
        if operation in self.results:
            return self._checked_result(self.results[operation])
        self.calls.append((operation,kind))
        p=self.a.policy
        root=self.a.root/'synthetic-http'/hashlib.sha256(operation.encode()).hexdigest()
        root.mkdir(parents=True)
        receipt=root/('p001-'+kind+'.response.local')
        if kind=='upload':
            value=dict(code=0,data=dict(file_key='file_uploaded'))
        elif kind=='send':
            body=json.loads(kwargs['body'])
            content=json.loads(body['content'])
            if body['msg_type']=='file':
                content=dict(file_key='file_message',file_name=kwargs['preview']['filename'])
            value=dict(code=0,data=dict(message_id='om_sent'+str(len(self.calls)),chat_id=p.chat_id,
                msg_type=body['msg_type'],deleted=False,updated=False,
                sender=dict(id=p.bot_id,id_type='open_id',sender_type='app'),body=dict(content=encoded(content))))
        elif kind=='download':
            data=Path(kwargs['preview']['pdf_path']).read_bytes()
            receipt.write_bytes(data)
            value=dict(path=str(receipt),sha256=file_sha(receipt),size_bytes=len(data))
        else:
            raise AssertionError('unexpected synthetic operation '+kind)
        if kind!='download':
            receipt.write_text(encoded(value))
        result=dict(value=value,receipt=str(receipt),evidence={str(receipt):file_sha(receipt)})
        self.results[operation]=result
        return result


class DailyBackendTests(unittest.TestCase):
    def setUp(self):
        self.g=fixture.InboxGuideTests()
        self.g.setUp()
        self.addCleanup(self.g.doCleanups)
        self.entry=self.g.accept(convert=False)
        self.registry=self.g.registry
        p=self.g.f.approval
        self.policy=DailyPolicy(p['app_id'],p['chat_id'],p['account_id'],p['bot_id'],4)
        self.root=self.registry.runtime.root/'conversion-work/daily-tests'
        self.root.mkdir()
        self.rule=self.root/'rules.local.json'
        self.rule.write_text('{}')
        self.authority=SimpleNamespace(policy=self.policy,root=self.root,path=self.rule,digest=file_sha(self.rule),
            runtime=self.registry.runtime,config=dict(guide_plan=str(self.g.path),text_node='',text_script='',codex_binary=''))
        self.authority.check=lambda:dict(self.g.f.frozen)
        self.store=DailyStore(self.root/'queue.sqlite3',self.policy)
        self.transport=LocalTransport(self.authority)
        self.backend=DailyBackend(self.authority,self.store,registry=self.registry,transport=self.transport,
                                  resources=ready(self.registry),wire=self.g.wire)
        self.backend.collector.verify_isolation=lambda:None
        raw=self.g.f.message
        self.mid=raw['message_id']
        scan=self.store.scan('chat',initial_start=self.store.now()-60)
        self.store.save_page(scan,[raw],has_more=False)
        self.store.advance(self.mid,'registered','original_saved',entry_id=self.entry)
        self.store.bind_source(self.mid,self.registry.entry(self.entry)['sha256'])

    def advance(self):
        self.backend.advance_heavy(self.store.document(self.mid))

    def preview(self):
        for _ in range(5):
            if self.store.document(self.mid)['phase']=='awaiting_confirmation':
                break
            self.advance()
        return self.store.document(self.mid)

    def test_saved_original_to_guide_to_byte_verified_preview(self):
        document=self.preview()
        self.assertEqual(document['phase'],'awaiting_confirmation')
        self.assertEqual(self.g.wire.calls,1)
        self.assertEqual(self.store.summary()['model_turns'],1)
        self.assertEqual([kind for _,kind in self.transport.calls],['send','upload','send','download'])
        self.assertEqual(len(self.registry.rows('SELECT * FROM pilot_preview_deliveries')),1)
        self.assertEqual(len(self.registry.rows('SELECT * FROM publication_confirmations')),0)
        content=json.loads(document['data']['preview']['sent']['value']['data']['body']['content'])
        self.assertTrue(content['file_name'].startswith('导读_'))
        self.assertTrue(content['file_name'].endswith('_第1版.pdf'))
        self.assertNotIn('publication_',content['file_name'])

    def test_interrupted_legacy_preview_keeps_original_filename(self):
        actual=self.transport.request
        def interrupt(operation,kind,path,**kwargs):
            if kind=='upload':
                legacy=dict(kwargs['preview'],filename='preview-'+kwargs['preview']['publication_id']+'.pdf')
                self.store.operation(operation,kind,dict(preview=legacy))
                raise Deferred('synthetic stopped legacy upload')
            return actual(operation,kind,path,**kwargs)
        self.transport.request=interrupt
        with self.assertRaises(Deferred):self.preview()
        self.transport.request=actual
        document=self.preview()
        name=json.loads(document['data']['preview']['sent']['value']['data']['body']['content'])['file_name']
        self.assertEqual(name,'preview-'+document['publication_id']+'.pdf')
        self.owner_reply(document)
        self.backend.advance_light(document)
        self.assertEqual(self.store.document(self.mid)['phase'],'notified')
        self.assertEqual(self.g.wire.calls,1)

    def test_readable_name_removes_paths_controls_and_bounds_unicode(self):
        self.assertEqual(preview_filename('../合成:论文\u202e\n.pdf',3),'导读_合成 论文_第3版.pdf')
        self.assertEqual(preview_filename('...',1),'导读_未命名资料_第1版.pdf')
        self.assertLessEqual(len(preview_filename('图😀'*300,999999999).encode()),240)
        self.assertTrue(preview_filename('标题'*100,2).endswith('…_第2版.pdf'))

    def owner_reply(self,document,text="确认入库"):
        item=dict(message_id="om_owner_confirm",chat_id=self.policy.chat_id,parent_id=document["preview_message_id"],
            msg_type="text",deleted=False,updated=False,
            sender=dict(id=self.policy.account_id,id_type="open_id",sender_type="user"),body=dict(content=encoded(dict(text=text))))
        proof=self.root/"owner-choice.local.json"
        proof.write_text(encoded(dict(code=0,data=dict(items=[item]))))
        receipt=dict(value=json.loads(proof.read_text()),receipt=str(proof),evidence={str(proof):file_sha(proof)})
        scan=self.store.scan("chat",initial_start=self.store.now()-60)
        self.store.save_page(scan,[item],has_more=False)
        self.backend.collector.message=lambda _: (item,receipt,receipt)

    def test_one_exact_confirmation_publishes_and_notifies(self):
        document=self.preview()
        item=dict(message_id='om_owner_confirm',chat_id=self.policy.chat_id,parent_id=document['preview_message_id'],
            msg_type='text',deleted=False,updated=False,
            sender=dict(id=self.policy.account_id,id_type='open_id',sender_type='user'),body=dict(content=encoded(dict(text='确认入库'))))
        proof=self.root/'owner-reply.local.json'
        proof.write_text(encoded(dict(code=0,data=dict(items=[item]))))
        receipt=dict(value=json.loads(proof.read_text()),receipt=str(proof),evidence={str(proof):file_sha(proof)})
        scan=self.store.scan('chat',initial_start=self.store.now()-60)
        self.store.save_page(scan,[item],has_more=False)
        self.backend.collector.message=lambda _: (item,receipt,receipt)
        self.backend.advance_light(document)
        self.assertEqual(self.store.document(self.mid)['phase'],'notified')
        self.assertEqual(len(self.registry.rows('SELECT * FROM publication_confirmations')),1)
        self.assertEqual(self.pipeline_state(document['publication_id']),'published')
        self.assertEqual(self.g.wire.calls,1)

    def test_unknown_model_is_checked_then_generated_only_once_more(self):
        self.advance()
        self.g.wire.mutate=lambda result:dict(status='unknown')
        with self.assertRaises(Deferred):
            self.advance()
        self.assertEqual(self.store.document(self.mid)['phase'],'model_unknown')
        self.advance()
        self.g.wire.mutate=lambda result:result
        self.advance()
        self.assertEqual(self.store.document(self.mid)['phase'],'guide_ready')
        self.assertEqual(self.g.wire.calls,2)
        self.assertEqual(len(self.registry.rows('SELECT * FROM publications_v21')),1)

    def test_second_unknown_model_never_gets_a_third_generation(self):
        self.advance()
        self.g.wire.mutate=lambda result:dict(status='unknown')
        with self.assertRaises(Deferred):
            self.advance()
        self.advance()
        with self.assertRaises(Deferred):
            self.advance()
        with self.assertRaises(MaterialProblem):
            self.advance()
        self.assertEqual(self.g.wire.calls,2)

    def test_saved_defer_decision_survives_crash_before_recording(self):
        document=self.preview()
        self.owner_reply(document,'暂不入库')
        decide=self.backend._decide
        def interrupt(_):
            raise RuntimeError('synthetic interruption after durable owner choice')
        self.backend._decide=interrupt
        with self.assertRaises(RuntimeError):
            self.backend.advance_light(document)
        self.assertEqual(self.store.document(self.mid)['phase'],'confirmed')
        self.backend._decide=decide
        self.backend.advance_light(self.store.document(self.mid))
        self.assertEqual(self.store.document(self.mid)['phase'],'rejected')
        self.assertEqual(len(self.registry.rows('SELECT * FROM pilot_preview_decisions')),1)
        self.assertEqual(len(self.registry.rows('SELECT * FROM publication_confirmations')),0)

    def test_notification_failure_does_not_repeat_generation_or_publication(self):
        document=self.preview()
        self.owner_reply(document)
        notice=self.backend.notice
        def interrupt(*args,**kwargs):
            raise Deferred('synthetic disconnected notification')
        self.backend.notice=interrupt
        with self.assertRaises(Deferred):
            self.backend.advance_light(document)
        self.assertEqual(self.store.document(self.mid)['phase'],'published')
        self.backend.notice=notice
        self.backend.advance_light(self.store.document(self.mid))
        self.assertEqual(self.store.document(self.mid)['phase'],'notified')
        self.assertEqual(self.g.wire.calls,1)
        self.assertEqual(len(self.registry.rows('SELECT * FROM publication_confirmations')),1)

    def test_damaged_original_recovery_preserves_old_bytes(self):
        from kb_custody_v21.daily_original import restore_original
        row=self.registry.entry(self.entry)
        path=self.registry.runtime.root/row['relative_path']
        download=self.root/'replacement.local.md'
        download.write_bytes(path.read_bytes())
        path.write_text('synthetically damaged')
        restore_original(self.backend.intake.staging,self.entry,download)
        self.assertEqual(file_sha(path),row['sha256'])
        self.assertEqual(len(list(path.parent.glob('.damaged-*'))),1)
        self.assertEqual(self.registry.entry(self.entry)['custody_health'],'healthy')

    def test_wrong_redownload_never_replaces_accepted_original(self):
        from kb_custody_v21.daily_original import restore_original
        from kb_custody_v21.staging import IntegrityError
        row=self.registry.entry(self.entry)
        path=self.registry.runtime.root/row['relative_path']
        download=self.root/'wrong-replacement.local.md'
        download.write_bytes(b'different')
        path.write_bytes(b'damaged')
        with self.assertRaises(IntegrityError):
            restore_original(self.backend.intake.staging,self.entry,download)
        self.assertEqual(path.read_bytes(),b'damaged')

    def test_interrupted_conversion_restarts_from_saved_original(self):
        from unittest.mock import patch
        from kb_custody_v21.conversion_recovery import CustodyWorker
        with patch.object(CustodyWorker,'_execute',side_effect=SystemExit('synthetic power loss')):
            with self.assertRaises(SystemExit):self.advance()
        task=self.backend.intake.status(self.entry)['task_id']
        self.assertEqual(self.backend.intake.status(self.entry)['processing_state'],'needs_recovery')
        self.advance()
        self.assertIsNone(self.backend.intake._job(self.entry)['task_id'])
        self.advance()
        self.assertEqual(self.store.document(self.mid)['phase'],'converted')
        self.assertNotEqual(self.backend.intake._job(self.entry)['task_id'],task)
        self.assertEqual(self.g.wire.calls,0)

    def test_restart_after_conversion_fence_commit_finishes_pointer_update(self):
        from unittest.mock import patch
        from kb_custody_v21.conversion_recovery import CustodyWorker
        with patch.object(CustodyWorker,'_execute',side_effect=SystemExit('synthetic power loss')):
            with self.assertRaises(SystemExit):self.advance()
        recovery=self.backend.intake.conversion.recovery
        recover=recovery.recover
        def interrupt(*args):
            recover(*args)
            raise SystemExit('crash before job pointer update')
        recovery.recover=interrupt
        with self.assertRaises(SystemExit):self.advance()
        recovery.recover=recover
        self.advance()
        self.advance()
        self.assertEqual(self.store.document(self.mid)['phase'],'converted')
        self.assertEqual(self.g.wire.calls,0)

    def test_generated_preview_survives_expired_parent_without_another_model(self):
        self.advance()
        self.advance()
        self.assertEqual(self.store.document(self.mid)['phase'],'guide_ready')
        # Simulate the scheduler crash just before its success phase commit.
        with self.store.transaction() as db:
            db.execute("UPDATE daily_documents SET phase='generating' WHERE message_id=?",(self.mid,))
        clock=self.store.now()
        self.store.clock=lambda:clock+12*3600
        self.advance()
        self.assertEqual(self.store.document(self.mid)['phase'],'guide_ready')
        self.assertEqual(self.g.wire.calls,1)

    def recovery_transport(self):
        from kb_custody_v21.daily_transport import DailyTransport
        self.authority.config['cli_profile']='synthetic'
        receipts=[]
        def exchange(control):
            receipts.append(self.transport.request('recovery-send','send',control['path'],body=control['body']))
        transport=DailyTransport(self.authority,self.store,runner=exchange)
        transport._receipt=lambda *args:receipts[-1]
        self.backend.transport=transport
        return transport

    def test_recovery_notice_retry_reuses_text_after_live_status_changes(self):
        from unittest.mock import patch
        transport=self.recovery_transport()
        self.backend.recover()
        pending=self.store.get('recovery_report_pending')
        def disconnected(**kwargs):
            raise Deferred('temporary_network_failure')
        transport.before_write=disconnected
        with patch('kb_custody_v21.daily_status.user_summary',return_value='合成恢复时状态'):
            with self.assertRaises(Deferred):self.backend.report_status()
        saved=self.store.rows('SELECT * FROM daily_operations WHERE op_key=?',('notice:recovery:'+pending,))[0]
        self.assertEqual(saved['attempts'],0)
        transport.before_write=None
        with patch('kb_custody_v21.daily_status.user_summary',side_effect=AssertionError('must reuse persisted text')):
            self.backend.report_status()
        final=self.store.rows('SELECT * FROM daily_operations WHERE op_key=?',('notice:recovery:'+pending,))[0]
        self.assertEqual(final['payload'],saved['payload'])
        self.assertEqual(final['state'],'complete')
        self.assertEqual(final['attempts'],1)
        self.assertIsNone(self.store.get('recovery_report_pending'))
        self.assertEqual(self.transport.calls,[('recovery-send','send')])
        self.assertEqual(self.g.wire.calls,0)

    def test_recovery_preserves_pending_notice_across_restart(self):
        self.backend.recover()
        pending=self.store.get('recovery_report_pending')
        self.backend.recover()
        self.assertEqual(self.store.get('recovery_report_pending'),pending)
        self.backend.report_status()
        self.assertIsNone(self.store.get('recovery_report_pending'))
        self.backend.recover()
        self.assertNotEqual(self.store.get('recovery_report_pending'),pending)

    def test_completed_recovery_notice_survives_crash_before_pending_clear(self):
        from unittest.mock import patch
        self.recovery_transport()
        self.backend.recover()
        pending=self.store.get('recovery_report_pending')
        put=self.store.put
        def interrupt(key,value):
            if key=='recovery_report_pending' and value is None:
                raise RuntimeError('synthetic crash after confirmed send')
            return put(key,value)
        with patch.object(self.store,'put',side_effect=interrupt):
            with self.assertRaises(RuntimeError):self.backend.report_status()
        self.backend.recover()
        self.assertEqual(self.store.get('recovery_report_pending'),pending)
        self.backend.report_status()
        self.assertIsNone(self.store.get('recovery_report_pending'))
        self.assertEqual(self.transport.calls,[('recovery-send','send')])
        self.assertEqual(self.store.rows('SELECT attempts FROM daily_operations')[0]['attempts'],1)

    def pipeline_state(self,pid):
        return self.backend.pipeline.publisher._row(pid)['state']


if __name__=='__main__':
    unittest.main()
