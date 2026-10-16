"""Reject misleading comparisons and retain unknown/missing-result regressions."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from benchmarks.scripts import compare_catalog_models as comparison


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=comparison.ROOT/'benchmarks/runs/model-comparison',prefix='test-')
        self.root=Path(self.temp.name).resolve()
        self.a,self.b=self.root/'before',self.root/'after'
        for path,model in ((self.a,'gpt-5-nano'),(self.b,'gpt-6-luna')):
            path.mkdir()
            value={k:1 for k in ('workers','deadline_seconds','passes','order_seed')}
            value.update(case_ids=['x'],reference_date='2026-10-15',timezone='Asia/Bangkok',
                setting='same',dependencies={'sdk':'same'},config={'openai_model':model,'budget':4000},
                fingerprints={'application':{'source':'same'},'harness':{'grader':'same'},'catalog':{'cases':'same'}})
            comparison.write(path/'run.json',value)
            (path/'results.jsonl').write_text(json.dumps({'case_id':'x','outcome':'completed'})+'\n',encoding='utf-8')
        self.checks(self.a,[('one','fail')]);self.checks(self.b,[('one','pass')])
        self.override=patch.object(comparison,'RUNS',self.root);self.override.start()

    def tearDown(self):
        self.override.stop()
        assert self.root.is_relative_to((comparison.ROOT/'benchmarks/runs/model-comparison').resolve())
        self.temp.cleanup()

    def checks(self,path,values):
        (path/'checks.jsonl').write_text(''.join(json.dumps({'case_id':'x','check':name,'status':status,
            'actual':status,'expected':True})+'\n' for name,status in values),encoding='utf-8')

    def test_matching_model_only_change_has_paired_checks(self):
        value=comparison.compare(self.a,self.b)
        self.assertEqual(value['improved_scored_checks'],1)
        self.assertIsNone(value['overall_accuracy_percent'])

    def test_missing_check_is_not_a_pass(self):
        self.checks(self.a,[('one','pass')]);self.checks(self.b,[])
        value=comparison.compare(self.a,self.b)
        self.assertEqual(value['passing_checks_now_unscored_or_missing'],1)
        self.assertEqual(value['improved_scored_checks'],0)

    def test_changed_grader_rejected(self):
        value=comparison.read(self.b/'run.json');value['fingerprints']['harness']={'grader':'different'}
        comparison.write(self.b/'run.json',value)
        with self.assertRaises(ValueError):comparison.compare(self.a,self.b)

    def test_changed_non_model_budget_rejected(self):
        value=comparison.read(self.b/'run.json');value['config']['budget']=8000
        comparison.write(self.b/'run.json',value)
        with self.assertRaises(ValueError):comparison.compare(self.a,self.b)

    def test_duplicate_checks_rejected(self):
        self.checks(self.b,[('one','pass'),('one','pass')])
        with self.assertRaises(ValueError):comparison.compare(self.a,self.b)

    def test_changed_expected_answer_rejected(self):
        value=json.loads((self.b/'checks.jsonl').read_text(encoding='utf-8'));value['expected']=False
        (self.b/'checks.jsonl').write_text(json.dumps(value)+'\n',encoding='utf-8')
        with self.assertRaises(ValueError):comparison.compare(self.a,self.b)

    def test_external_interruption_is_explicit_and_not_model_improvement(self):
        (self.a/'results.jsonl').write_text(json.dumps({'case_id':'x','outcome':'interrupted'})+'\n',encoding='utf-8')
        value=comparison.compare(self.a,self.b)
        self.assertEqual(value['externally_interrupted_cases_excluded_from_model_pairing'],['x'])
        self.assertEqual(value['improved_scored_checks'],0)
        self.assertEqual(len(value['transitions']),1)
        self.assertTrue(value['all_planned_cases_retained_in_raw_totals'])
