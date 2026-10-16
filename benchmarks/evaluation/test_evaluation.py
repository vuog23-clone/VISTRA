"""Check coverage accounting, fixture materialization and negative graders."""

from copy import deepcopy
import unittest

from benchmarks.evaluation.protocol import public_cases,capability,track,GOLDEN
from benchmarks.evaluation.providers import grounding_context,PlanningReplay
from benchmarks.evaluation.grading import grade


class EvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import json
        cls.cases = {c['case_id']:c for c in public_cases()}
        cls.private = {r['family_id']:r for p in (GOLDEN/'private').glob('*.jsonl')
            for r in map(json.loads,p.read_text(encoding='utf-8').splitlines())}

    def test_complete_catalog_and_explicit_capabilities(self):
        self.assertEqual(len(self.cases),600)
        self.assertEqual(sum(len(c['turns']) for c in self.cases.values()),716)
        for case in self.cases.values():
            mode,reason = capability(case)
            self.assertEqual(bool(reason),mode=='unsupported')
        self.assertEqual(sum(capability(c)[0]!='unsupported' for c in self.cases.values()),408)

    def test_no_visual_ranking_claims(self):
        for case in self.cases.values():
            if track(case)=='retrieval':
                self.assertEqual(capability(case)[0],'unsupported')

    def test_fault_not_silently_ignored(self):
        self.assertEqual(capability(self.cases['VB1-FAIL-101-en'])[0],'unsupported')
        self.assertEqual(capability(self.cases['VB1-FAIL-136-en'])[0],'api_validation')

    def test_grounding_all_contexts_validate(self):
        from src.security.context import prepare_context
        for case in self.cases.values():
            if track(case)=='grounding':
                context = prepare_context(grounding_context(case))
                self.assertTrue(context['sources'])
                self.assertLessEqual(len(context['sources']),10)

    def test_grounding_removal_is_real(self):
        import json
        cases = [c for c in self.cases.values() if c['setup']['environment_ref'].endswith('/grounding_pure_abstention')]
        self.assertEqual(len(cases),2)
        for case in cases:
            text = json.dumps(grounding_context(case))
            self.assertNotIn('adult_ticket_vnd',text)

    def test_directed_matrix_kept_unchanged(self):
        from benchmarks.evaluation.protocol import read
        raw = read('fixtures/planning-environment.json')['route_matrix']
        result = PlanningReplay().run('DistanceMatrix','Hanoi')
        self.assertEqual(result.route_matrix.durations_seconds,raw['durations'])
        self.assertEqual(result.route_matrix.distances_meters,raw['distances_meters'])
        self.assertEqual(PlanningReplay().run('RestaurantSearch','Hue').status,'unavailable')

    def test_bad_routing_cannot_pass(self):
        case = self.cases['VB1-ROUTE-101-en']
        row = {'outcome':'completed','turns':[{'turn_id':'t1','raw_interpretations':[{'intent':'travel','action':'search'}],
                'result':{'response_mode':'retrieval'}}]}
        checks = grade(case,self.private[case['family_id']],row)
        self.assertEqual(sum(c['status']=='fail' for c in checks),3)
        self.assertTrue(any(c['status']=='unscored' for c in checks))

    def test_wrong_http_and_backend_call_both_fail(self):
        case = self.cases['VB1-FAIL-136-en']
        checks = grade(case,self.private[case['family_id']],{'outcome':'completed',
            'turns':[{'turn_id':'t1','http_status':200,'backend_calls':1}]})
        self.assertEqual(sum(c['status']=='fail' for c in checks),2)

    def test_unsupported_never_passes(self):
        case = self.cases['VB1-PLAN-150-en']
        self.assertTrue(all(c['status']=='unscored' for c in grade(case,self.private[case['family_id']],{'outcome':'unsupported'})))

    def test_no_private_labels_in_public_payload(self):
        for case in self.cases.values():
            self.assertFalse(set(case)&{'private_expectations','review','witness_ref','qrels_ref'})

    def test_nonplanning_sentinel_is_not_an_action(self):
        case = self.cases['VB1-ROUTE-101-en']
        row = {'outcome':'completed','turns':[{'turn_id':'t1','raw_interpretations':[{'intent':'other','action':'answer'}],
                'effective_pipeline_inputs':[{'options':{'planning_action':'none'}}],
                'result':{'response_mode':'llm_only'}}]}
        self.assertFalse(any(c['status']=='fail' for c in grade(case,self.private[case['family_id']],row)))

    def test_missing_fields_and_closure_are_applied(self):
        from benchmarks.evaluation.protocol import profile
        for case in self.cases.values():
            condition = profile(case).get('condition')
            if track(case)=='planning' and condition in {'unknown_prices','unknown_hours','unknown_rooms','closed_candidate'}:
                kind = 'AccommodationSearch' if condition=='unknown_rooms' else 'AttractionSearch'
                result = PlanningReplay(case).run(kind,'Hanoi')
                for r in result.records:
                    if condition=='unknown_prices':
                        self.assertIsNone(r.cost)
                    elif condition=='unknown_hours':
                        self.assertIsNone(r.opening_hours)
                    elif condition=='unknown_rooms':
                        self.assertIsNone(r.maximum_occupancy)
                    elif r.entity_id=='google:bench_attraction_01':
                        self.assertEqual(r.opening_hours['friday'],[])


if __name__=='__main__':
    unittest.main()
