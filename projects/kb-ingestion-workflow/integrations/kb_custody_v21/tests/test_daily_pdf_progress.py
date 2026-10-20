"""Progress is observational: no extra model, conversion or publication effects."""
import json
from pathlib import Path
import unittest

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.daily_pdf_progress import PdfProgress, render_progress
from kb_custody_v21.daily_scheduler import MaterialProblem
from kb_custody_v21.daily_store import Deferred, PHASES
from kb_custody_v21.daily_transport import DailyTransport
from kb_custody_v21.f05 import file_sha
from kb_custody_v21.mineru_chunks import PdfChunks, PdfOptions, PdfHeld
from kb_custody_v21.registry import encoded
import test_daily_pdf as pdf_fixture
import test_daily_backend as backend_fixture
import test_daily_transport as transport_fixture
import test_mineru_chunks as chunk_fixture


class ProgressTransport(backend_fixture.LocalTransport):
    def request(self,operation,kind,path,**kwargs):
        if kwargs.get('mode')!='update':
            return super().request(operation,kind,path,**kwargs)
        if operation in self.results:
            return self._checked_result(self.results[operation])
        self.calls.append((operation,kind))
        p=self.a.policy
        value=dict(code=0,data=dict(message_id=kwargs['progress']['message_id'],chat_id=p.chat_id,
            msg_type='text',deleted=False,updated=True,sender=dict(id=p.bot_id,id_type='open_id',sender_type='app'),
            body=dict(content=json.loads(kwargs['body'])['content'])))
        path=self.a.root/('progress-'+str(len(self.calls))+'.json')
        path.write_text(encoded(value))
        result=dict(value=value,receipt=str(path),evidence={str(path):file_sha(path)})
        self.results[operation]=result
        return result


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.f=pdf_fixture.DailyPdfTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.b,self.s=self.f.backend,self.f.store
        self.now=self.s.now()+1
        self.s.clock=lambda:self.now
        self.b.authority.config['pdf']['progress']=True
        self.b.transport=ProgressTransport(self.b.authority)
        self.progress=self.b.pdf_progress=PdfProgress(self.b)

    def tick(self):
        self.now+=31
        self.progress.report()

    def snapshot(self,pages,stage='converting'):
        self.progress.record(self.f.mid,dict(source_sha=self.s.document(self.f.mid)['source_sha'],
            total_pages=100,completed_pages=pages,stage=stage,first_page=None,last_page=None))

    def test_every_scheduler_phase_has_a_pdf_progress_label(self):
        for phase in PHASES:
            with self.subTest(phase=phase):
                value=render_progress(dict(phase=phase,filename='synthetic.pdf'),
                    dict(total_pages=8,completed_pages=8,stage='converted'),None,None,self.now)
                self.assertIn('8/8 页',value['text'])
                if phase in ('reading','reading_wait'):
                    self.assertIn('已收到确认',value['text'])
                    self.assertNotIn('已入库',value['text'])

    def test_conversion_counts_only_completed_results_and_does_no_http(self):
        self.tick()
        origin=self.s.get('pdf-display:'+self.f.mid)['origin']['message_id']
        before=len(self.b.transport.calls)
        self.f.advance()
        # The existing custody notice is the only conversion-thread HTTP call.
        self.assertEqual(len(self.b.transport.calls),before+1)
        saved=self.s.get('pdf-progress:'+self.f.mid)
        self.assertEqual((saved['completed_pages'],saved['total_pages']),(7,7))
        self.tick()
        state=self.s.get('pdf-display:'+self.f.mid)
        self.assertEqual(state['origin']['message_id'],origin)
        self.assertIn('7/7 页',state['text'])
        self.assertIn('等待生成导读',state['text'])
        self.assertEqual(self.s.summary()['model_turns'],0)

    def test_one_hundred_pages_coalesce_into_ten_percent_steps_and_survive_restart(self):
        self.snapshot(0); self.tick()
        original=self.s.get('pdf-display:'+self.f.mid)['origin']['message_id']
        for n in range(1,101):
            self.snapshot(n)
            self.progress=PdfProgress(self.b)
            self.tick()
        self.assertEqual(len(self.b.transport.calls),11)
        state=self.s.get('pdf-display:'+self.f.mid)
        self.assertEqual(state['origin']['message_id'],original)
        self.assertIn('100/100 页',state['text'])
        self.assertNotIn('已入库',state['text'])

    def test_pending_update_is_resolved_before_newer_snapshot(self):
        self.snapshot(0); self.tick()
        actual=self.b.transport.request
        pending=[]
        def offline(*args,**kwargs):
            pending.append((args,kwargs))
            raise Deferred('temporary_network_failure')
        self.b.transport.request=offline
        self.snapshot(10); self.tick()
        self.snapshot(90); self.tick()
        self.assertEqual(pending[0],pending[1])
        self.b.transport.request=actual
        self.tick(); self.tick()
        self.assertIn('90/100 页',self.s.get('pdf-display:'+self.f.mid)['text'])

    def test_edit_rejection_does_not_hold_document_or_reserve_model(self):
        self.snapshot(0); self.tick()
        actual=self.b.transport.request
        def expired(*args,**kwargs):
            if kwargs.get('mode')=='update':
                raise MaterialProblem('edit expired')
            return actual(*args,**kwargs)
        self.b.transport.request=expired
        self.snapshot(10); self.tick(); self.tick(); self.tick()
        state=self.s.get('pdf-display:'+self.f.mid)
        self.assertTrue(state['disabled'] and state['fallback_sent'])
        self.assertEqual(self.s.document(self.f.mid)['phase'],'original_saved')
        self.assertEqual(self.s.summary()['model_turns'],0)
        self.assertEqual(sum('unavailable' in op for op,_ in self.b.transport.calls),1)

    def test_historical_terminal_material_gets_no_progress_notice(self):
        self.s.advance(self.f.mid,'original_saved','held',reason='synthetic historical hold')
        self.tick()
        self.assertFalse(self.b.transport.calls)

    def test_disabled_progress_never_warns_after_publication(self):
        self.snapshot(0); self.tick()
        document=self.f.preview()
        self.f.owner_reply(document)
        self.b.advance_light(document)
        display=self.s.get('pdf-display:'+self.f.mid)
        display.update(disabled=True,error='progress_edit_expired')
        self.s.put('pdf-display:'+self.f.mid,display)
        self.tick(); self.tick()
        self.assertEqual(self.s.document(self.f.mid)['phase'],'notified')
        self.assertEqual(sum('unavailable' in op for op,_ in self.b.transport.calls),0)
        self.assertEqual(self.s.get('pdf-display:'+self.f.mid)['fallback_suppressed'],'material_finished')
        self.assertEqual(len(self.f.registry.rows('SELECT * FROM publication_confirmations')),1)

    def test_disabled_pending_material_explains_display_only_failure(self):
        self.snapshot(0); self.tick()
        display=self.s.get('pdf-display:'+self.f.mid)
        display.update(disabled=True,error='progress_edit_expired')
        self.s.put('pdf-display:'+self.f.mid,display)
        self.tick(); self.tick()
        receipt=self.b.transport.results['notice:pdf-progress-unavailable:'+self.f.mid]
        text=json.loads(receipt['value']['data']['body']['content'])['text']
        self.assertIn('不代表资料处理或入库失败',text)
        self.assertEqual(sum('unavailable' in op for op,_ in self.b.transport.calls),1)
        self.assertEqual(self.s.summary()['model_turns'],0)

    def test_throttling_and_resource_pause_remain_separate_from_percentage(self):
        self.snapshot(30); self.tick()
        self.snapshot(80); self.progress.report()
        self.assertEqual(len(self.b.transport.calls),1)
        self.s.put('defer:'+self.f.mid,dict(reason='paused_resources',until=self.now+100))
        self.tick()
        value=self.s.get('pdf-display:'+self.f.mid)
        self.assertEqual(value['stage'],'paused')
        self.assertIn('80/100 页',value['text'])
        self.snapshot(80); self.tick()
        self.assertEqual(self.s.get('pdf-display:'+self.f.mid)['stage'],'converting')

    def test_failure_updates_existing_progress_without_waiting_thirty_seconds(self):
        self.snapshot(0); self.tick()
        origin=self.s.get('pdf-display:'+self.f.mid)['origin']['message_id']
        self.s.advance(self.f.mid,'original_saved','held',reason='synthetic PDF parse failure')
        self.progress.report()
        display=self.s.get('pdf-display:'+self.f.mid)
        self.assertEqual(display['origin']['message_id'],origin)
        self.assertEqual(display['stage'],'held')
        self.assertIn('synthetic PDF parse failure',display['text'])
        self.assertEqual(self.s.summary()['model_turns'],0)

    def test_compatibility_is_visible_in_progress(self):
        self.snapshot(0)
        saved=self.s.get('pdf-progress:'+self.f.mid)
        saved['compatibility']='pdf-identical-adjacent-mediabox/v1'
        self.s.put('pdf-progress:'+self.f.mid,saved)
        self.tick()
        self.assertIn('原件保持不变',self.s.get('pdf-display:'+self.f.mid)['text'])

    def test_forged_source_does_not_change_progress(self):
        with self.assertRaises(ContractError):
            self.progress.record(self.f.mid,dict(source_sha='0'*64,stage='converted'))
        self.assertIsNone(self.s.get('pdf-progress:'+self.f.mid))

    def test_crash_after_first_receipt_recovers_without_second_notice(self):
        self.snapshot(0)
        actual=self.s.put
        def crash(key,value):
            if key=='pdf-display:'+self.f.mid and value.get('origin'):
                raise RuntimeError('synthetic death after send')
            return actual(key,value)
        self.s.put=crash
        with self.assertRaises(RuntimeError):self.tick()
        self.assertEqual(len(self.b.transport.calls),1)
        self.s.put=actual
        self.progress=PdfProgress(self.b)
        self.tick()
        self.assertEqual(len(self.b.transport.calls),1)
        self.assertIsNotNone(self.s.get('pdf-display:'+self.f.mid)['origin'])

    def test_progress_accompanies_one_guide_and_exact_owner_confirmation(self):
        self.tick()
        document=self.f.preview()
        self.tick()
        self.assertIn('请回复对应 PDF',self.s.get('pdf-display:'+self.f.mid)['text'])
        self.assertEqual(self.f.g.wire.calls,1)
        self.assertEqual(len(self.f.registry.rows('SELECT * FROM publication_confirmations')),0)
        self.f.owner_reply(document)
        self.b.advance_light(document)
        self.tick()
        self.assertEqual(self.s.get('pdf-display:'+self.f.mid)['stage'],'notified')
        self.assertEqual(self.f.g.wire.calls,1)
        self.assertEqual(len(self.f.registry.rows('SELECT * FROM publication_confirmations')),1)


class ChunkProgressTests(unittest.TestCase):
    def setUp(self):
        self.f=chunk_fixture.ChunkTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)

    def test_failed_pages_never_advance_and_restart_uses_saved_success(self):
        f=self.f
        f.converter.interrupt=lambda pdf,mode:pdf.stem.startswith('p0006')
        events=[]
        chunks=PdfChunks(f.root/'chunks',PdfOptions(),f.converter)
        with self.assertRaises(Deferred):chunks.convert(f.source,f.digest,progress=events.append)
        self.assertEqual(max(e['completed_pages'] for e in events),5)
        f.converter.interrupt=None
        resumed=[]
        chunks.convert(f.source,f.digest,progress=resumed.append)
        self.assertEqual(resumed[0]['completed_pages'],5)
        self.assertEqual(resumed[-1]['completed_pages'],7)
        self.assertEqual(resumed[-1]['stage'],'converted')
        self.assertEqual(sum(stem=='p0001-0005' for stem,_ in f.converter.calls),1)

    def test_corrupt_completed_chunk_cannot_emit_success_on_restart(self):
        f=self.f
        chunks=PdfChunks(f.root/'chunks',PdfOptions(),f.converter)
        chunks.convert(f.source,f.digest)
        path=next((f.root/'chunks'/f.digest/'p0001-0005').rglob('pages.json'))
        path.write_text('changed')
        events=[]
        with self.assertRaises(ContractError):chunks.convert(f.source,f.digest,progress=events.append)
        self.assertEqual(events,[])


class ProgressRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.f=transport_fixture.TransportTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        f=self.f
        f.a.config['pdf']=dict(progress=True)
        self.origin=f.send()
        self.progress=dict(message_id='om_sent',origin_receipt=self.origin['receipt'],
            initial_text='PDF 处理进度\n0%',previous_text='PDF 处理进度\n0%')
        original=f.exchange
        def exchange(control):
            if control['kind']!='message': return original(control)
            f.calls.append(control)
            root=Path(control['root']); key=control['key']+':message'
            path=root/(control['key']+'.response.local')
            path.write_text(encoded(dict(code=0,data=dict(items=f.items))))
            (root/'http.local.jsonl').write_text(encoded(dict(type='intent',kind='message',key=key))+'\n'+
                encoded(dict(type='complete',kind='message',key=key,http_status=200,response_sha256=file_sha(path)))+'\n')
        f.transport.runner=exchange

    def update(self,operation='progress-test'):
        return self.f.transport.request(operation,'send','/open-apis/im/v1/messages/om_sent',mode='update',
            body=encoded(dict(msg_type='text',content=encoded(dict(text='PDF 处理进度\n10%')))),
            progress=self.progress,evidence=self.origin['evidence'])

    def reject_edit(self,code=230075,valid_hash=True):
        f=self.f
        def rejected(control):
            f.calls.append(control)
            root=Path(control['root']); key=control['key']+':send'
            path=root/(control['key']+'.response.local')
            path.write_text(encoded(dict(code=code,msg='synthetic expired progress')))
            events=[dict(type='intent',kind='send',key=key),dict(type='stop',kind='send',key=key,
                http_status=400,response_sha256=file_sha(path) if valid_hash else '0'*64)]
            (root/'http.local.jsonl').write_text(''.join(encoded(e)+'\n' for e in events))
            raise ContractError('synthetic rejected update')
        f.transport.runner=rejected

    def test_verified_expired_edit_stops_after_one_attempt_without_read_or_retry(self):
        self.reject_edit()
        for _ in range(2):
            with self.assertRaises(MaterialProblem):self.update()
        row=self.f.store.rows('SELECT * FROM daily_operations WHERE op_key=?',('progress-test',))[0]
        self.assertEqual((row['state'],row['attempts'],row['error']),('failed',1,'progress_edit_expired'))
        self.assertEqual([c['mode'] for c in self.f.calls],['send','update'])
        self.assertIsNone(self.f.store.get('network_pause'))

    def test_unverified_rejection_and_other_codes_remain_unknown(self):
        for index,(code,valid_hash) in enumerate([(230075,False),(12345,True)]):
            self.reject_edit(code,valid_hash)
            operation='progress-unverified-'+str(index)
            with self.assertRaises(Deferred):self.update(operation)
            row=self.f.store.rows('SELECT state FROM daily_operations WHERE op_key=?',(operation,))[0]
            self.assertEqual(row['state'],'unknown')

    def test_unknown_completed_update_is_read_without_repeating_put(self):
        f=self.f; f.failure='offline'
        with self.assertRaises(Deferred):self.update()
        f.items=[dict(f.bot(dict(text='PDF 处理进度\n10%')),updated=True)]
        result=self.update()
        self.assertEqual(result['reconciled_from'],'verified_progress_message')
        self.assertEqual([c['mode'] for c in f.calls],['send','update','read'])

    def test_unknown_absent_update_retries_identical_body_after_read(self):
        f=self.f; f.failure='offline'
        with self.assertRaises(Deferred):self.update()
        f.items=[f.bot(dict(text='PDF 处理进度\n0%'))]
        self.update()
        puts=[c for c in f.calls if c['mode']=='update']
        self.assertEqual(len(puts),2)
        self.assertEqual(puts[0]['body'],puts[1]['body'])

    def test_edited_unexpected_content_is_not_overwritten(self):
        f=self.f; f.failure='offline'
        with self.assertRaises(Deferred):self.update()
        f.items=[f.bot(dict(text='unexpected text'))]
        with self.assertRaises(MaterialProblem):self.update()
        self.assertEqual(sum(c['mode']=='update' for c in f.calls),1)

    def test_all_attempts_share_twenty_edit_ceiling(self):
        for index in range(20):
            self.f.now+=10
            self.update('update-'+str(index))
        self.f.now+=10
        with self.assertRaisesRegex(Deferred,'progress_update_limit'):
            self.update('update-21')
        self.assertEqual(sum(c['mode']=='update' for c in self.f.calls),20)

    def test_profile_without_progress_cannot_update(self):
        self.f.a.config['pdf']['progress']=False
        with self.assertRaises(ContractError):self.update()
        self.assertEqual(len(self.f.calls),1)


if __name__=='__main__':unittest.main()
