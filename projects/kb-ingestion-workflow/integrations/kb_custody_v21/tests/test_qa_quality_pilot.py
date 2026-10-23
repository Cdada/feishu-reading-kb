from copy import deepcopy
from types import SimpleNamespace
import unittest

from kb_custody_v21.contracts import ContractError
from kb_custody_v21.daily_store import Deferred
from kb_custody_v21.qa_answer_contract import INSTRUCTION
from kb_custody_v21.qa_quality import digest
from kb_custody_v21.qa_quality_pilot import run_calibration, run_answer_pilot, verify_controls
from test_qa_quality import fixture, answer, grade, run_config, calibrated_report


def controls():
    values=[]
    for i in range(20):
        a=answer();wrong=i%2==1
        if wrong:a['claims'][0]['text']='The survey is already completed.'
        values.append(dict(id=f'control-{i}',fixture=fixture(f'control-{i}'),answer=a,answer_sha256=digest(a),
            reference_pass=not wrong,reference_critical=wrong,reference_reason='Synthetic independently supplied reference for this test.'))
    return values


class PilotTests(unittest.TestCase):
    def setUp(self):
        self.items=controls();self.progress=[];self.calls=[]
        self.run=dict(run_config(),prompt_sha256=digest(INSTRUCTION))

    def execute(self,**kw):
        self.calls.append(kw)
        g=grade();bad=kw['request']['candidate']['claims'][0]['text'].startswith('The survey is already')
        if bad:
            g['required'][0]['verdict']='missing';g['forbidden'][0]['verdict']='present';g['claims'][0]['verdict']='unsupported'
        return dict(output=kw['validator'](g),provider_model='synthetic-revision',usage=dict(input_tokens=2,output_tokens=1),
            cached=False,new_model_calls=1)

    def run_controls(self,execute=None,**kwargs):
        return run_calibration(self.items,self.run,calls=SimpleNamespace(execute=execute or self.execute),
            grant={},checked_access=lambda:{},judge_adapter=object(),save_progress=self.progress.append,
            reference_provenance='simulated independently reviewed controls',**kwargs)

    def test_twenty_separate_decisions_enable_pilot_only_after_calibration(self):
        r=self.run_controls();self.assertTrue(r['ready_for_answer_pilot']);self.assertEqual(r['new_model_calls'],20)
        self.assertEqual(len(self.calls),20);self.assertFalse(self.progress[1]['ready_for_answer_pilot'])

    def test_critical_miss_stops_immediately_without_replacing_the_control(self):
        def wrong(**kw):
            r=self.execute(**kw)
            if len(self.calls)==2:r['output']=grade()
            return r
        r=self.run_controls(wrong);self.assertEqual(r['state'],'stopped_judge_mismatch')
        self.assertEqual(r['measured'],2);self.assertFalse(r['ready_for_answer_pilot'])

    def test_uncertainty_and_first_four_mismatch_stop_expansion(self):
        def uncertain(**kw):
            r=self.execute(**kw);r['output']['required'][0]['verdict']='uncertain';return r
        self.assertEqual(self.run_controls(uncertain)['measured'],1)

    def test_unknown_call_is_retained_and_never_automatically_retried(self):
        def unknown(**kw):
            self.calls.append(kw);e=Deferred('evaluation_model_unknown_no_resubmit');e.new_model_calls=1;raise e
        r=self.run_controls(unknown);self.assertEqual(r['state'],'stopped_execution')
        self.assertEqual(r['new_model_calls'],1);self.assertEqual(len(self.calls),1)

    def test_missing_tampered_duplicate_heldout_and_wrong_library_controls_fail_before_calls(self):
        for change in ('short','tampered','duplicate','heldout','corpus'):
            values=controls()
            if change=='short':values.pop()
            if change=='tampered':values[0]['answer']['gaps']=['changed']
            if change=='duplicate':values[1]['fixture']=values[0]['fixture']
            if change=='heldout':values[0]['fixture']['split']='heldout'
            if change=='corpus':values[0]['fixture']['corpus_id']='different'
            with self.assertRaises(ContractError):verify_controls(values,self.run)
        self.assertEqual(self.calls,[])

    def test_synthetic_controls_do_not_release_real_quality(self):
        r=self.run_controls(reference_kind='synthetic');self.assertFalse(r['ready_for_answer_pilot'])

    def test_cached_calibration_keeps_zero_new_calls(self):
        def cached(**kw):return dict(self.execute(**kw),new_model_calls=0,cached=True)
        r=self.run_controls(cached);self.assertEqual(r['new_model_calls'],0);self.assertTrue(r['ready_for_answer_pilot'])

    def test_pilot_requires_calibration_and_has_a_four_case_ceiling(self):
        with self.assertRaises(ContractError):run_answer_pilot([fixture()],self.run,calibration_report=None,save_progress=lambda r:None)
        with self.assertRaises(ContractError):run_answer_pilot([fixture(str(i)) for i in range(5)],self.run,
            calibration_report=calibrated_report(),save_progress=lambda r:None)

    def test_pilot_retains_unmeasured_denominator_after_execution_stops(self):
        r=run_answer_pilot([fixture('one'),fixture('two')],self.run,calibration_report=calibrated_report(),save_progress=self.progress.append)
        self.assertEqual(r['summary']['planned'],2);self.assertEqual(r['summary']['states'],{'skipped':1,'not_run':1})
