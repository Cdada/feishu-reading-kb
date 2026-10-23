"""Mention, history and restart share one queue; no network/model access."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.daily_archive import ArchiveMentions, PROFILE, archive_command
from kb_custody_v21.daily_card_events import CardListener
from kb_custody_v21.daily_cards import CardStore, render_card
from kb_custody_v21.daily_scheduler import DailyScheduler
from kb_custody_v21.daily_store import DailyStore, Deferred
from kb_custody_v21.f05 import file_sha
from kb_custody_v21.registry import encoded
from test_daily_store import message, policy


def command(mid='cmd', parent='om_file', text='@_user_1 归档'):
    v = message(mid, kind='text', text=text)
    v.update(parent_id=parent, mentions=[dict(id='ou_bot',id_type='open_id',key='@_user_1')])
    return v


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()
        self.now=1000000.
        self.p=policy()
        self.store=DailyStore(self.root/'queue.sqlite3',self.p,clock=lambda:self.now)
        self.a=SimpleNamespace(policy=self.p, root=self.root, config=dict(archive_mentions=PROFILE, pdf={}))
        self.calls=[]; self.cache={}; self.items={'om_cmd':command(),'om_file':message('file')}
        self.backend=SimpleNamespace(authority=self.a, store=self.store, transport=self,
            check_authorization=lambda:None, stop=threading.Event(), notice=Mock(),
            card_flow=SimpleNamespace(cards=CardStore(self.store)))
        self.flow=ArchiveMentions(self.backend)
        self.backend.archive_mentions=self.flow
        self.session=self.root/'listener.local.json'; self.session.write_text('{}')
        self.defer=None

    @property
    def authority(self): return self.a

    def proof(self, key, value):
        path=self.root/(hashlib.sha256(key.encode()).hexdigest()+'.json')
        path.write_text(encoded(value))
        return dict(value=value,receipt=str(path),evidence={str(path):file_sha(path)})

    def _checked_result(self, value):
        for name,digest in value['evidence'].items():
            if file_sha(name)!=digest: raise ContractError('evidence changed')
        return value

    def request(self, operation, kind, path, **kw):
        self._checked_result(dict(evidence=kw['evidence']))
        if self.defer:
            error=self.defer; self.defer=None; raise error
        if operation in self.cache: return self.cache[operation]
        self.calls.append((operation,kind,path,kw))
        if kw.get('mode')=='send':
            card=kw['card']; mid='om_card'+str(len(self.calls))
            v=message(mid.removeprefix('om_'),kind='interactive')
            v.update(parent_id=card['binding']['target_message_id'],sender=dict(id='ou_bot',id_type='open_id',sender_type='app'),body=dict(content=card['content']))
            self.items[mid]=v
            value=dict(code=0,data=v)
        else:
            mid=path.rsplit('/',1)[-1].split('?')[0]
            value=dict(code=0,data=dict(items=[copy.deepcopy(self.items[mid])]))
        result=self.proof(operation,value); self.cache[operation]=result
        return result

    def persist(self, mid='om_cmd', eid='event_one'):
        event=dict(type='im.message.receive_v1',event_id=eid,timestamp='1000000000',operator_id='ou_owner',chat_id='oc_synthetic',message_id=mid)
        self.flow.persist(event,app_id='cli_synthetic',session_path=self.session)

    def row(self, mid='om_cmd'):
        return self.store.rows('SELECT * FROM daily_archive_inbox WHERE message_id=?',(mid,))[0]

    def test_event_admits_exact_file_and_sends_native_status_card(self):
        self.persist(); self.flow.drain()
        self.assertEqual(self.store.document('om_file')['phase'],'registered')
        self.assertEqual(self.row()['state'],'processed')
        self.assertEqual([x[1] for x in self.calls],['message','message','send','message'])
        card=self.backend.card_flow.cards.get(self.row()['card_id'])
        self.assertEqual(card['binding']['target_message_id'],'om_cmd')
        self.assertIn('仍需你确认入库',card['binding']['details'])
        self.assertFalse(any(x['tag']=='button' for x in render_card(card)['body']['elements']))
        self.assertNotIn('root_id',card['binding'])

    def test_mention_alone_is_supported_but_free_text_is_not_inferred(self):
        for text in ('@_user_1',' @_user_1\n归档 ','@_user_1 归档'):
            self.assertTrue(archive_command(command(text=text),self.p))
        for text in ('@机器人 归档','@_user_1归档','@_user_1 确认入库','归档 @_user_1','@_user_1 帮我看看'):
            self.assertFalse(archive_command(command(text=text),self.p))

    def test_foreign_edited_or_ambiguous_commands_are_ignored(self):
        cases=[dict(chat_id='oc_other'),dict(updated=True),dict(deleted=True),dict(mentions=[]),
            dict(sender=dict(id='ou_other',id_type='open_id',sender_type='user')),
            dict(mentions=[dict(id='ou_other',id_type='open_id',key='@_user_1')]),
            dict(mentions=command()['mentions']*2)]
        for change in cases:
            self.assertFalse(archive_command(dict(command(),**change),self.p))
        self.persist(); self.items['om_cmd']['deleted']=True; self.flow.drain()
        self.assertEqual(self.row()['state'],'ignored')
        self.assertFalse(self.store.rows('SELECT * FROM daily_documents'))

    def test_missing_parent_never_uses_root_or_latest_attachment(self):
        self.items['om_cmd'].update(parent_id='',root_id='om_file')
        self.persist(); self.flow.drain()
        self.assertFalse(self.store.rows('SELECT * FROM daily_documents'))
        self.assertIn('请直接回复',self.row()['result'])
        self.assertEqual(len([x for x in self.calls if x[2].endswith('/om_file')]),0)

    def test_reply_to_foreign_or_nonfile_is_rejected_with_help(self):
        self.items['om_file']['sender']['id']='ou_other'
        self.persist(); self.flow.drain()
        self.assertFalse(self.store.rows('SELECT * FROM daily_documents'))
        self.assertIn('不是可归档文件',self.row()['result'])

    def test_replay_history_and_restart_do_not_duplicate_work_or_card(self):
        self.persist(); self.flow.drain(); calls=len(self.calls)
        self.flow=ArchiveMentions(self.backend)
        self.persist(); self.persist(eid='event_redelivery'); self.flow.collect(); self.flow.drain()
        self.assertEqual(len(self.calls),calls)
        self.assertEqual(len(self.store.rows('SELECT * FROM daily_documents')),1)
        self.assertEqual(len(self.store.rows('SELECT * FROM daily_cards')),1)

    def test_history_without_live_event_is_recovered(self):
        item=self.items['om_cmd']; proof=self.proof('history',dict(code=0,data=dict(items=[item],has_more=False)))
        self.flow._observe(item,proof)
        self.flow.collect(); self.flow.drain()
        self.assertEqual(self.store.document('om_file')['phase'],'registered')
        self.assertEqual(self.row()['state'],'processed')

    def test_duplicate_command_for_published_file_never_restarts_material(self):
        self.persist(); self.flow.drain()
        with self.store.transaction() as db:
            db.execute("UPDATE daily_documents SET phase='notified' WHERE message_id='om_file'")
        self.items['om_again']=command('again')
        self.persist('om_again','event_two'); self.flow.drain()
        self.assertEqual(self.store.document('om_file')['phase'],'notified')
        self.assertIn('已入库',self.row('om_again')['result'])

    def test_existing_wait_states_are_reported_without_rearming_them(self):
        self.persist(); self.flow.drain()
        cases={'needs_upload':'需要补传原件','confirmed':'无需重复确认',
               'model_unknown':'结果尚待核查','send_unknown':'结果尚待核查'}
        for phase,expected in cases.items():
            with self.subTest(phase=phase):
                with self.store.transaction() as db:
                    db.execute('UPDATE daily_documents SET phase=? WHERE message_id=?',(phase,'om_file'))
                mid='om_'+phase
                self.items[mid]=command(phase)
                self.persist(mid,'event_'+phase); self.flow.drain()
                self.assertEqual(self.store.document('om_file')['phase'],phase)
                self.assertIn(expected,self.row(mid)['result'])

    def test_full_queue_preserves_request_and_resumes_after_space_is_available(self):
        with self.store.transaction() as db:
            for n in range(self.p.max_pending): self.store._observe(db,message(n))
        self.persist(); self.flow.drain()
        self.assertEqual(self.row()['state'],'pending')
        self.assertEqual(self.row()['result'],'queue_full')
        self.backend.notice.assert_called_once()
        with self.store.transaction() as db: db.execute("UPDATE daily_documents SET phase='notified'")
        self.now+=self.p.poll_seconds
        self.flow.drain()
        self.assertEqual(self.row()['state'],'processed')

    def test_transient_read_waits_and_changed_receipt_is_not_accepted(self):
        self.persist(); self.defer=Deferred('temporary_network_failure',self.now+5)
        self.flow.drain(); self.assertEqual(self.row()['state'],'pending')
        self.flow.drain(); self.assertFalse(self.calls)
        self.session.write_text('changed')
        self.now+=6
        with self.assertRaises(ContractError): self.flow.drain()

    def test_unreadable_command_is_visible_in_local_status(self):
        from kb_custody_v21.daily_scheduler import MaterialProblem
        from kb_custody_v21.daily_status import local_status, user_summary
        self.persist(); self.defer=MaterialProblem('synthetic missing message')
        self.flow.drain()
        self.assertEqual(self.row()['state'],'held')
        value=local_status(self.root)
        self.assertEqual(value['summary']['archive_mentions']['held'],1)
        self.assertIn('@归档请求需核查：1',user_summary(value))

    def test_listener_persists_before_ack_without_network(self):
        listener=CardListener(self.backend); listener.session_path=self.session; listener._ready.set()
        event=dict(type='im.message.receive_v1',event_id='event_one',timestamp='1000000000',operator_id='ou_owner',chat_id='oc_synthetic',message_id='om_cmd')
        listener._handle(dict(type='event',app_id='cli_synthetic',event=event))
        self.assertEqual(self.row()['state'],'pending'); self.assertFalse(self.calls)
        with self.assertRaises(ContractError):
            self.flow.persist(dict(event,chat_id='oc_other'),app_id='cli_synthetic',session_path=self.session)

    def test_event_is_processed_between_polls_while_heavy_worker_is_busy(self):
        from test_daily_scheduler import Adapter
        a=Adapter(self.store)
        a.process_events=self.flow.drain
        s=DailyScheduler(self.store,a,monotonic=lambda:0)
        self.addCleanup(s.close)
        s.tick(); self.assertTrue(a.heavy_started.wait(1))
        self.persist(); s.tick()
        self.assertEqual(a.collected,1)
        self.assertEqual(self.row()['state'],'processed')
        self.assertEqual(self.store.document('om_file')['phase'],'registered')
        self.assertEqual(a.maximum,1)


if __name__=='__main__': unittest.main()
