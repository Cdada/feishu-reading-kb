"""Contract parity and opt-in checks for the P42 extension."""
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.daily_authority import DailyAuthority
from kb_custody_v21.daily_card_flow import CardFlow
from kb_custody_v21.daily_cards import render_card
from kb_custody_v21.relation_flow import RelationFlow
import test_daily_authority as authority_fixture
import test_reading_flow as reading_fixture
from test_daily_card_flow import CardTransport


class RelationContractTests(unittest.TestCase):
    def test_python_renderer_matches_shared_physical_guard_fixtures(self):
        path=Path(__file__).parent/'fixtures/p42_cards.json'
        for fixture in json.loads(path.read_text()):
            with self.subTest(purpose=fixture['card']['binding']['purpose'],result=fixture['card']['result']):
                self.assertEqual(render_card(fixture['card'],result=fixture['card']['result'] or None),fixture['rendered'])

    def test_relation_authority_requires_exact_opt_in_with_reading_and_cards(self):
        f=authority_fixture.AuthorityTests();f.setUp();self.addCleanup(f.doCleanups)
        self.assertNotIn('relations',DailyAuthority(f.path,f.save()).config)
        f.value['relations']=dict(reference='P42-relations-v1',enabled=True)
        with self.assertRaisesRegex(ContractError,'P42'):DailyAuthority(f.path,f.save())
        f.value['cards']=dict(reference='P40-cards-v1',enabled=True);f.value['reading']={}
        with patch('kb_custody_v21.reading_profile.ReadingProfile',return_value=SimpleNamespace()):
            self.assertIn('relations',DailyAuthority(f.path,f.save()).config)
        f.value['relations']['enabled']=False
        with self.assertRaisesRegex(ContractError,'P42'):DailyAuthority(f.path,f.save())

    def test_completed_real_reading_pipeline_enqueues_only_after_full_organization(self):
        f=reading_fixture.ReadingFlowTests();f.setUp();self.addCleanup(f.doCleanups)
        f.approve()
        f.f.transport=CardTransport(f.f.transport);f.b.transport=f.f.transport
        f.b.card_flow=CardFlow(f.b);f.b.card_listener=SimpleNamespace(ready=True)
        f.b.relations=RelationFlow(f.b)
        self.assertEqual(f.daily.rows("SELECT * FROM daily_meta WHERE key LIKE 'relation-task:%'"),[])
        f.finish();f.b.relations.report()
        rows=f.daily.rows('SELECT * FROM daily_cards')
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['purpose'],'relation_candidates')
        self.assertEqual(f.daily.summary()['model_turns'],3) # Guide + organize + review; no association call.
        self.assertEqual(f.document()['phase'],'notified')

    def test_enabling_relations_never_backfills_old_completed_intake(self):
        f=reading_fixture.ReadingFlowTests();f.setUp();self.addCleanup(f.doCleanups)
        f.approve();f.finish()
        f.f.transport=CardTransport(f.f.transport);f.b.transport=f.f.transport
        f.b.card_flow=CardFlow(f.b);f.b.card_listener=SimpleNamespace(ready=True)
        f.b.relations=RelationFlow(f.b);f.b.recover();f.b.relations.report()
        self.assertEqual(f.daily.rows("SELECT * FROM daily_meta WHERE key LIKE 'relation-task:%'"),[])
        self.assertEqual(f.daily.rows('SELECT * FROM daily_cards'),[])


if __name__=='__main__':unittest.main()
