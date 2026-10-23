"""New mention -> custody -> preview decision -> reading, entirely synthetic."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from kb_custody_v21.daily_archive import ArchiveMentions, PROFILE
from kb_custody_v21.daily_backend import DailyBackend
from kb_custody_v21.daily_card_events import EventIngress
from kb_custody_v21.daily_policy import DailyPolicy
from kb_custody_v21.daily_store import DailyStore
from kb_custody_v21.f05 import file_sha
from kb_custody_v21.reading_bundle import CARDS, TOPICS, BRIEFS
from kb_custody_v21.reading_flow import ReadingFlow
from kb_custody_v21.reading_profile import ReadingProfile
from kb_custody_v21.registry import encoded
from pilot_pipeline_fixture import ready
from test_daily_backend import LocalTransport
from test_daily_card_flow import CardTransport
import test_pilot_inbox_guide as guide_fixture
from test_pilot_three_formats import profile
from test_reading_flow import SyntheticReadingWire


SOURCE = '# 合成验收：记录与判断\n\n这是独立网页文章的合成测试正文。\n\n相同条件下记录测量结果。样本不足时，只报告观察，不作普遍推论。\n'


class IntakeTransport(CardTransport):
    """Only the external message/download boundary is simulated."""

    def request(self, operation, kind, path, **kwargs):
        if kwargs.get('card') or kind not in ('message', 'download') or kwargs.get('preview'):
            return super().request(operation, kind, path, **kwargs)
        self._checked_result(dict(evidence=kwargs['evidence']))
        if operation in self.results:
            return self._checked_result(self.results[operation])
        self.calls.append((operation, kind))
        receipt = self.a.root / (hashlib.sha256(operation.encode()).hexdigest() + '.local')
        if kind == 'download':
            receipt.write_bytes(SOURCE.encode())
            value = dict(path=str(receipt), sha256=file_sha(receipt), size_bytes=receipt.stat().st_size)
        else:
            mid = path.rsplit('/', 1)[-1].split('?')[0]
            value = dict(code=0, data=dict(items=[deepcopy(self.messages[mid])]))
            receipt.write_text(encoded(value))
        result = dict(value=value, receipt=str(receipt), evidence={str(receipt):file_sha(receipt)})
        self.results[operation] = result
        return result


class ArchiveReadingPipelineTests(unittest.TestCase):
    def setUp(self):
        self.g = guide_fixture.InboxGuideTests()
        self.g.setUp()
        self.addCleanup(self.g.doCleanups)
        profile(self.g)
        self.g.save()
        self.registry = self.g.registry
        scope = self.g.f.approval
        self.policy = DailyPolicy(scope['app_id'], scope['chat_id'], scope['account_id'], scope['bot_id'], 3)
        self.root = self.registry.runtime.root / 'conversion-work/mention-reading-test'
        self.root.mkdir()
        rule = self.root / 'rules.local.json'
        rule.write_text('{}')
        self.authority = SimpleNamespace(policy=self.policy, root=self.root, path=rule, digest=file_sha(rule),
            runtime=self.registry.runtime, config=dict(guide_plan=str(self.g.path), text_node='', text_script='',
                codex_binary='', cards=dict(reference='P40-cards-v1', enabled=True), archive_mentions=PROFILE))
        self.authority.check = lambda: dict(self.g.f.frozen)
        rules = {}
        frozen = {}
        for role in ('archive_rules', 'organize_prompt', 'nlp_skill'):
            path = self.root / (role + '.md')
            path.write_text('Synthetic reading standard. No personal library.')
            frozen[str(path)] = file_sha(path)
            rules[role] = dict(path=str(path), sha256=file_sha(path))
        script = self.root / 'synthetic-reading.mjs'
        script.write_text('// synthetic model boundary')
        frozen[str(script)] = file_sha(script)
        self.authority.reading = ReadingProfile(self.authority.runtime,
            dict(reference='P41-reading-v1', root=str(self.authority.runtime.root/'reading-library'),
                rules=rules, script=str(script), generation=dict(enabled=True, account_sha256='f'*64,
                    wait_seconds=60, minimum_remaining_percent=5)), frozen)
        self.store = DailyStore(self.root/'queue.sqlite3', self.policy)
        self.transport = IntakeTransport(LocalTransport(self.authority))
        self.backend = DailyBackend(self.authority, self.store, registry=self.registry,
            transport=self.transport, resources=ready(self.registry), wire=self.g.wire)
        self.backend.collector.verify_isolation = lambda **kwargs: None
        self.backend.card_listener = SimpleNamespace(ready=True)
        self.reading_wire = SyntheticReadingWire()
        def include_card_and_topic(purpose, value):
            if purpose == 'organize':
                value['cards'] = [dict(title='合成测量边界', kind='method_card', existing_path='',
                    reuse_reason='检查观察与推论的边界。', claim_ids=['c1'])]
                value['topics'] = [dict(title='合成测量', existing_path='')]
            return value
        self.reading_wire.transform = include_card_and_topic
        self.backend.reading_flow = ReadingFlow(self.backend, wire=self.reading_wire)
        self.original = deepcopy(self.g.f.message)
        self.original.update(updated=False)
        self.mid = self.original['message_id']
        self.original['body']['content'] = encoded(dict(file_key='file_synthetic', file_name='合成完整归档.md'))
        self.transport.messages[self.mid] = self.original
        self.session = self.root/'listener.local.json'
        self.session.write_text('{}')

    def document(self):
        return self.store.document(self.mid)

    def mention(self, suffix):
        mid = 'om_mention_' + suffix
        command = dict(message_id=mid, chat_id=self.policy.chat_id, parent_id=self.mid, msg_type='text',
            deleted=False, updated=False, create_time='1000000000',
            sender=dict(id=self.policy.account_id, id_type='open_id', sender_type='user'),
            mentions=[dict(id=self.policy.bot_id, id_type='open_id', key='@_user_1')],
            body=dict(content=encoded(dict(text='@_user_1 归档'))))
        self.transport.messages[mid] = command
        event = dict(type='im.message.receive_v1', event_id='event_'+suffix, timestamp='1000000000',
            operator_id=self.policy.account_id, chat_id=self.policy.chat_id, message_id=mid)
        self.backend.archive_mentions.persist(event, app_id=self.policy.app_id, session_path=self.session)
        self.backend.archive_mentions.drain()
        return event

    def preview(self):
        for _ in range(8):
            if self.document()['phase'] == 'awaiting_confirmation':
                break
            self.backend.advance_heavy(self.document())
        self.assertEqual(self.document()['phase'], 'awaiting_confirmation')
        self.backend.advance_light(self.document())
        return self.backend.card_flow.cards.get(self.store.rows(
            "SELECT card_id FROM daily_cards WHERE purpose='preview'")[0]['card_id'])

    def decide(self, card, command):
        event = dict(type='card.action.trigger', event_id='decision_synthetic', timestamp='1000000000',
            operator_id=self.policy.account_id, chat_id=self.policy.chat_id, host='im_message',
            message_id=card['message_id'], action_tag='button',
            action_value=encoded(dict(reference='P40-card-v1', card_id=card['card_id'], command=command)))
        ingress = EventIngress(self.backend)
        ingress.persist(event, app_id=self.policy.app_id, session_path=self.session)
        ingress.drain()
        self.backend.advance_light(self.document())
        return event

    def library_snapshot(self):
        root = self.authority.reading.root
        return {str(p.relative_to(root)):file_sha(p) for p in root.rglob('*') if p.is_file()}

    def test_new_mention_completes_all_outputs_only_after_preview_confirmation(self):
        self.assertEqual(self.store.rows('SELECT * FROM daily_documents'), [])
        self.assertEqual(self.registry.rows('SELECT * FROM intake_entries'), [])
        self.mention('new')
        self.assertEqual(self.document()['phase'], 'registered')
        self.assertEqual(self.g.wire.calls, 0)
        card = self.preview()
        preview = Path(self.document()['data']['guide_result']['preview_path'])
        self.assertEqual(preview.read_bytes()[:4], b'%PDF')
        preview_sha = file_sha(preview)
        self.assertEqual(self.g.wire.calls, 1)
        self.assertEqual(self.registry.rows('SELECT * FROM publication_confirmations'), [])
        self.assertEqual(self.reading_wire.calls, [])
        self.assertEqual(self.library_snapshot(), {})
        self.mention('waiting')
        self.assertEqual(self.document()['phase'], 'awaiting_confirmation')
        self.assertEqual(self.g.wire.calls, 1)
        event = self.decide(card, '确认入库')
        self.assertEqual(self.document()['phase'], 'reading')
        for _ in range(8):
            if self.document()['phase'] != 'reading':
                break
            self.backend.advance_heavy(self.document())
        self.assertEqual(self.document()['phase'], 'published')
        self.backend.advance_light(self.document())
        self.backend.card_flow.report()
        self.assertEqual(self.document()['phase'], 'notified')
        self.assertEqual(self.store.summary()['model_turns'], 3)
        self.assertEqual([r['purpose'] for r in self.reading_wire.calls], ['organize', 'review'])
        files = self.library_snapshot()
        self.assertTrue(any(p.endswith('/综合读书笔记.md') for p in files))
        self.assertTrue(any(p.startswith(CARDS + '/') for p in files))
        self.assertTrue(any(p.startswith(TOPICS + '/') for p in files))
        self.assertTrue(any(p.startswith(BRIEFS + '/') for p in files))
        entry = self.registry.entry(self.document()['entry_id'])
        self.assertEqual(entry['sha256'], hashlib.sha256(SOURCE.encode()).hexdigest())
        self.assertIn(preview_sha, [digest for path, digest in files.items() if path.endswith('.pdf')])
        # A restarted inbox, historical recovery, duplicate event and fresh @
        # must all retain the same completed archive and three-call ledger.
        self.backend.archive_mentions = ArchiveMentions(self.backend)
        self.backend.archive_mentions.collect()
        self.backend.archive_mentions.drain()
        self.mention('done')
        ingress = EventIngress(self.backend)
        ingress.persist(event, app_id=self.policy.app_id, session_path=self.session)
        ingress.drain()
        self.backend.card_flow.report()
        self.assertEqual(files, self.library_snapshot())
        self.assertEqual(self.store.summary()['model_turns'], 3)
        self.assertEqual(len(self.store.rows('SELECT * FROM daily_documents')), 1)
        self.assertEqual(len(self.registry.rows('SELECT * FROM publication_confirmations')), 1)
        self.assertEqual(len(self.store.rows('SELECT * FROM reading_materials')), 1)

    def test_declining_preview_never_starts_reading_despite_another_mention(self):
        self.mention('new')
        card = self.preview()
        self.decide(card, '暂不入库')
        self.assertEqual(self.document()['phase'], 'rejected')
        self.mention('after_decline')
        self.assertEqual(self.document()['phase'], 'rejected')
        self.assertEqual(self.reading_wire.calls, [])
        self.assertEqual(self.store.summary()['model_turns'], 1)
        self.assertEqual(self.library_snapshot(), {})
        self.assertFalse(any(r['state']=='published' for r in self.registry.rows('SELECT * FROM publications_v21')))


if __name__ == '__main__':
    unittest.main()
