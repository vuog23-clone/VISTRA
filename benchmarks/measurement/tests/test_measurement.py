from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from subprocess import TimeoutExpired
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from benchmarks.measurement.engine import (HERE, PROTOCOL, compare, digest, invoke, load_run, read,
                                          run, suite_cases, write)
from benchmarks.measurement.graders import check_names, grade
from benchmarks.measurement.metrics import summarize, usage_summary, validate_pricing
from benchmarks.measurement.usage import Meter


def spec(identity='one.en.r1', language='en', repeat=1):
    return {'execution_id':identity,'case_id':'one.'+language,'family_id':'one','area':'example',
            'language':language,'repeat':repeat,'checks':['delivery'],'review_required':False}


def row(item, state='pass', seconds=1):
    return {**item, 'checks':dict.fromkeys(item['checks'],state),'wall_seconds':seconds,'harness_ok':True,
            'observation':{'outcome':'ok','provider_calls':[]}}


def manifest(executions=None):
    return {'protocol':PROTOCOL,'profile':'offline','repeats':1,'settings':{'openai_model':'test'},
            'executions':executions or [spec(),spec('one.vi.r1','vi')], 'compatibility':{'suite':'fixed','profile':'offline'},
            'source_sha256':{'src/example.py':'before'},'served_models':[], 'integrity':True}


class MetricTests(unittest.TestCase):
    def test_missing_execution_is_unknown_and_remains_in_denominator(self):
        data = manifest()
        result = summarize(data,[row(data['executions'][0])])
        self.assertFalse(result['complete'])
        self.assertEqual(result['overall']['automatic_pass_rate'],.5)
        self.assertEqual(result['overall']['checks'],{'pass':1,'fail':0,'unknown':1})
        self.assertEqual(result['bilingual_families']['passed_all_languages_and_repetitions'],0)

    def test_errors_and_timeouts_are_not_dropped(self):
        data = manifest()
        rows = [row(item,'fail',seconds=300) for item in data['executions']]
        rows[0]['observation']['outcome'] = 'timeout'
        rows[1]['observation']['outcome'] = 'system_error'
        result = summarize(data,rows)
        self.assertTrue(result['complete'])
        self.assertEqual(result['overall']['system_errors'],2)
        self.assertEqual(result['overall']['automatic_pass_rate'],0)
        self.assertEqual(result['overall']['worker_wall_p95_seconds'],300)

    def test_p95_uses_nearest_rank_and_includes_failed_attempts(self):
        items = [spec('id'+str(i), 'en' if i%2 else 'vi') for i in range(20)]
        result = summarize(manifest(items),[row(item,'fail' if i == 19 else 'pass',i+1) for i,item in enumerate(items)])
        self.assertEqual(result['overall']['worker_wall_p95_seconds'],19)
        self.assertEqual(result['overall']['worker_wall_median_seconds'],10.5)

    def test_micro_and_macro_have_different_declared_denominators(self):
        data = manifest()
        data['executions'][1]['checks'] = ['a','b','c']
        rows = [row(data['executions'][0]),row(data['executions'][1],'fail')]
        result = summarize(data,rows)['overall']
        self.assertEqual(result['check_micro_pass'],.25)
        self.assertEqual(result['check_macro_pass'],.5)

    def test_duplicate_unexpected_and_missing_check_results_are_rejected(self):
        data = manifest()
        good = row(data['executions'][0])
        for rows in ([good,good],[{**good,'execution_id':'unknown'}],[{**good,'checks':{}}]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                summarize(data,rows)

    def test_pending_human_reviews_cannot_be_reported_as_task_success(self):
        data = manifest()
        for item in data['executions']:
            item['review_required'] = True
        rows = [row(item) for item in data['executions']]
        result = summarize(data,rows)
        self.assertEqual(result['overall']['automatic_pass_rate'],1)
        self.assertIsNone(result['human_review']['task_success_rate'])
        labels = {'reviewer':'test reviewer','rubric_version':'measurement-review-1', 'results':[
            {'execution_id':item['execution_id'],'task_success':'pass','claim_support':'pass','language':'pass'} for item in data['executions']]}
        rows[0]['checks']['delivery'] = 'fail'
        reviewed = summarize(data,rows,reviews=labels)
        self.assertEqual(reviewed['human_review']['task_success_rate'],.5)

    def test_invalid_or_unattributed_reviews_are_rejected(self):
        data = manifest()
        rows = [row(item) for item in data['executions']]
        labels = {'reviewer':'', 'rubric_version':'measurement-review-1','results':[]}
        with self.assertRaises(ValueError):
            summarize(data,rows,reviews=labels)

    def test_missing_pricing_and_missing_usage_are_not_zero(self):
        rows = [{'observation':{'provider_calls':[{'usage':{'input_tokens':100,'output_tokens':50,'cached_input_tokens':0}}]}}]
        result = usage_summary(rows,'controlled')
        self.assertEqual(result['input_tokens'],100)
        self.assertIsNone(result['estimated_cost_usd'])
        rows[0]['observation']['provider_calls'].append({'usage':None})
        result = usage_summary(rows,'controlled')
        self.assertIsNone(result['input_tokens'])
        self.assertEqual(result['cost_status'],'usage_incomplete')

    def test_timeout_loses_usage_and_call_counts_are_explicitly_unknown(self):
        result = usage_summary([{'observation':{'provider_calls':[],'usage_lost_on_timeout':True}}],'controlled')
        self.assertIsNone(result['llm_sdk_calls'])
        self.assertIsNone(result['estimated_cost_usd'])

    def test_supplied_dated_rates_apply_cached_tokens_once(self):
        pricing = {'as_of':'2026-10-16','currency':'USD','source':'Synthetic test-only rates',
            'models':{'test':{'input_per_million':2,'cached_input_per_million':.25,'output_per_million':3}}}
        validate_pricing(pricing)
        rows = [{'observation':{'provider_calls':[{'usage':{'input_tokens':1000,'output_tokens':100,'cached_input_tokens':200}}]}}]
        result = usage_summary(rows,'controlled',pricing,'test')
        self.assertAlmostEqual(result['estimated_cost_usd'],.00195)
        rows[0]['observation']['provider_calls'][0]['usage']['cached_input_tokens'] = None
        self.assertIsNone(usage_summary(rows,'controlled',pricing,'test')['estimated_cost_usd'])
        pricing['models']['test']['input_per_million'] = float('nan')
        with self.assertRaises(ValueError):
            validate_pricing(pricing)

    def test_offline_usage_and_cost_are_not_model_efficiency_measurements(self):
        result = usage_summary([],'offline')
        self.assertFalse(result['usage_applicable'])
        self.assertIsNone(result['estimated_cost_usd'])


class OracleTests(unittest.TestCase):
    def setUp(self):
        self.fixtures = read(HERE/'fixtures.json')
        self.cases = {case['case_id']:case for profile in ('offline','controlled') for case in suite_cases(profile)[2]}

    def observation(self,data):
        return {'outcome':'ok','data':data}

    def test_profiles_are_fixed_bilingual_development_sets(self):
        self.assertEqual(len(suite_cases('offline')[2]),28)
        self.assertEqual(len(suite_cases('controlled')[2]),12)
        for profile in ('offline','controlled'):
            counts = {}
            for case in suite_cases(profile)[2]:
                counts.setdefault(case['id'],set()).add(case['language'])
            self.assertTrue(all(languages == {'en','vi'} for languages in counts.values()))

    def test_every_offline_adapter_is_wired_to_the_current_public_component_api(self):
        from benchmarks.measurement.adapters import ADAPTERS
        from datetime import date
        with patch('socket.socket.connect',side_effect=AssertionError('Offline must not access network')), patch(
                'src.service.weather_service.reference_today',return_value=date(2026,10,16)):
            for case in suite_cases('offline')[2]:
                with self.subTest(case=case['case_id']):
                    public = {key:case[key] for key in ('id','profile','adapter','input','message','language')}
                    result = ADAPTERS[case['adapter']](public,deepcopy(self.fixtures),Meter())
                    checks = grade(case,self.observation(result),self.fixtures)
                    self.assertTrue(all(state=='pass' for state in checks.values()),checks)

    def test_inapplicable_checks_are_excluded_not_free_passes(self):
        self.assertEqual(check_names(self.cases['schedule-budget.en']),['no_invalid_plan'])
        self.assertNotIn('brief_acknowledgment',check_names(self.cases['memory-mixed.en']))
        self.assertNotIn('requested_layout',check_names(self.cases['model-closing.en']))

    def test_provider_error_fails_every_declared_required_check(self):
        case = self.cases['model-table.en']
        result = grade(case,{'outcome':'timeout'},self.fixtures)
        self.assertEqual(result,dict.fromkeys(check_names(case),'fail'))

    def test_source_approval_cannot_override_wrong_facts_or_wrong_format(self):
        case = self.cases['model-table.en']
        data = {'answer':'- Museum: 900000 VND. Source: S1', 'grounding':{'status':'checked'}}
        checks = grade(case,self.observation(data),self.fixtures)
        self.assertEqual(checks['requested_value_present'],'fail')
        self.assertEqual(checks['citation_resolution'],'fail')
        self.assertEqual(checks['requested_layout'],'fail')

    def test_valid_rule_checks_do_not_establish_claim_citation_association(self):
        case = self.cases['model-table.en']
        text = '| Place | Price |\n| --- | --- |\n| Museum | 0 VND [S2] |\n| Park | 120000 VND [S1] |'
        checks = grade(case,self.observation({'answer':text}),self.fixtures)
        self.assertTrue(all(value=='pass' for value in checks.values()))
        self.assertTrue(case['review_required'])  # Human evidence review catches the swapped facts.

    def test_empty_photo_output_cannot_pass_positive_coverage(self):
        case = self.cases['photos-category.en']
        checks = grade(case,self.observation({'context':{'sources':[]},'cards':[]}),self.fixtures)
        self.assertEqual(checks['photo_coverage'],'fail')

    def test_unknown_or_wrong_photo_owner_is_rejected(self):
        case = self.cases['photos-category.en']
        data = {'context':{'sources':[{'entity_id':'google:spoof-beach','photo':{'url':'irrelevant'}}]},'cards':[]}
        self.assertEqual(grade(case,self.observation(data),self.fixtures)['photo_ownership'],'fail')

    def plan(self):
        def choice(identity,start,end):
            return {'entity_id':identity,'start_time':start,'end_time':end}
        return {'days':[{'day':1,'date':'2026-10-16','city':'Hanoi',
            'breakfast':choice('google:measure-meal-a','08:00','09:00'),
            'lunch':choice('google:measure-meal-b','12:00','13:00'),
            'dinner':choice('google:measure-meal-c','18:00','19:00'),
            'attractions':[choice('google:measure-sight','20:00','21:00')],'accommodation':None}]}

    def test_independent_schedule_oracle_accepts_witness(self):
        checks = grade(self.cases['schedule-evening.en'],self.observation({'itinerary':self.plan(),'answer':'08:00 09:00 12:00 13:00 18:00 19:00 20:00 21:00'}),self.fixtures)
        self.assertTrue(all(value=='pass' for value in checks.values()))

    def test_independent_schedule_oracle_detects_hours_gaps_budget_and_lodging(self):
        case = deepcopy(self.cases['schedule-evening.en'])
        plan = self.plan()
        plan['days'][0]['attractions'][0].update(start_time='09:00',end_time='10:00')
        plan['days'][0]['accommodation'] = {'entity_id':'fake-hotel'}
        case['input']['budget'] = 79
        checks = grade(case,self.observation({'itinerary':plan}),self.fixtures)
        self.assertEqual(checks['opening_hours'],'fail')
        self.assertEqual(checks['route_gaps'],'fail')
        self.assertEqual(checks['party_budget'],'fail')
        self.assertEqual(checks['no_final_night'],'fail')

    def test_missing_schedule_times_fail_without_breaking_grader(self):
        plan = self.plan()
        plan['days'][0]['breakfast']['start_time'] = None
        checks = grade(self.cases['schedule-evening.en'],self.observation({'itinerary':plan}),self.fixtures)
        self.assertEqual(checks['opening_hours'],'fail')

    def test_memory_false_save_and_durable_trip_leak_are_detected(self):
        case = self.cases['memory-budget.en']
        data = {'state':{'version':0,'trip_state':{'budget_amount':6000000,'currency':'VND'},'recent_messages':[]},
                'records':[{'key':'travel_style','content':'Trip budget 6000000 VND'}], 'results':[], 'pipeline_calls':1}
        checks = grade(case,self.observation(data),self.fixtures)
        self.assertEqual(checks['saved_state'],'fail')
        self.assertEqual(checks['durable_scope'],'fail')
        self.assertEqual(checks['commit_consistency'],'fail')

    def test_a_correct_limitation_cannot_pass_requested_answer_delivery(self):
        checks = grade(self.cases['model-closing.en'],self.observation({'answer':'I cannot verify an answer.'}),self.fixtures)
        self.assertEqual(checks['requested_value_present'],'fail')


class ProtocolTests(unittest.TestCase):
    def saved_run(self,path,data,rows):
        path.mkdir()
        text = ''.join(json.dumps(item)+'\n' for item in rows)
        (path/'results.jsonl').write_text(text,encoding='utf-8')
        data['results_sha256'] = sha256((path/'results.jsonl').read_bytes()).hexdigest()
        write(path/'manifest.json',data)

    def test_source_changes_are_allowed_but_settings_changes_are_not(self):
        with TemporaryDirectory(dir=HERE/'tests') as temporary:
            old,new = Path(temporary)/'old',Path(temporary)/'new'
            before,after = manifest(),manifest()
            after['source_sha256']['src/example.py'] = 'after'
            self.saved_run(old,before,[row(item) for item in before['executions']])
            rows = [row(item) for item in after['executions']]
            rows[0]['checks']['delivery'] = 'fail'
            self.saved_run(new,after,rows)
            result = compare(old,new)
            self.assertEqual(result['changed_application_files'],['src/example.py'])
            self.assertEqual(result['regressions'][0]['execution_id'],'one.en.r1')
            after['compatibility']['seed'] = 99
            write(new/'manifest.json',after)
            with self.assertRaisesRegex(ValueError,'not comparable'):
                compare(old,new)

    def test_tampered_results_and_incomplete_runs_are_rejected(self):
        with TemporaryDirectory(dir=HERE/'tests') as temporary:
            path = Path(temporary)/'run'
            data = manifest()
            self.saved_run(path,data,[row(data['executions'][0])])
            with self.assertRaisesRegex(ValueError,'Incomplete'):
                compare(path,path)
            (path/'results.jsonl').write_text('',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'changed'):
                load_run(path)

    def test_invalidation_sidecar_prevents_reuse_without_rewriting_results(self):
        with TemporaryDirectory(dir=HERE/'tests') as temporary:
            path = Path(temporary)/'run'
            data = manifest()
            self.saved_run(path,data,[row(item) for item in data['executions']])
            before = (path/'results.jsonl').read_bytes()
            write(path/'INVALIDATED.json',{'reason':'Broken fixture discovered during review.'})
            self.assertFalse(load_run(path)[2]['complete'])
            self.assertEqual((path/'results.jsonl').read_bytes(),before)

    def test_compatibility_rejects_cases_fixtures_settings_harness_and_served_model_drift(self):
        for key in ('suite_sha256','fixtures_sha256','config_sha256','harness_sha256','repeats','execution_order','profile'):
            with self.subTest(key=key), TemporaryDirectory(dir=HERE/'tests') as temporary:
                old,new = Path(temporary)/'old',Path(temporary)/'new'
                before,after = manifest(),manifest()
                after['compatibility'][key] = 'different'
                self.saved_run(old,before,[row(item) for item in before['executions']])
                self.saved_run(new,after,[row(item) for item in after['executions']])
                with self.assertRaisesRegex(ValueError,'not comparable'):
                    compare(old,new)
        with TemporaryDirectory(dir=HERE/'tests') as temporary:
            old,new = Path(temporary)/'old',Path(temporary)/'new'
            before,after = manifest(),manifest()
            after['served_models'] = ['changed-model-version']
            self.saved_run(old,before,[row(item) for item in before['executions']])
            self.saved_run(new,after,[row(item) for item in after['executions']])
            with self.assertRaisesRegex(ValueError,'served_models'):
                compare(old,new)

    def test_legacy_ad_hoc_reports_are_not_accepted_as_a_baseline(self):
        with TemporaryDirectory(dir=HERE/'tests') as temporary:
            path = Path(temporary)
            write(path/'manifest.json',{'passed':12,'cases':13})
            with self.assertRaisesRegex(ValueError,'legacy'):
                load_run(path)

    def test_output_cannot_escape_benchmark_runs_or_overwrite_an_existing_run(self):
        from benchmarks.measurement.engine import output_directory
        with TemporaryDirectory(dir=HERE/'tests') as temporary:
            root = Path(temporary)
            with patch('benchmarks.measurement.engine.ROOT',root):
                with self.assertRaisesRegex(ValueError,'inside'):
                    output_directory(root/'elsewhere')
                output = root/'benchmarks/runs/fixture'
                output_directory(output)
                (output/'existing.txt').write_text('preserve',encoding='utf-8')
                with self.assertRaisesRegex(ValueError,'new empty'):
                    output_directory(output)
                self.assertEqual((output/'existing.txt').read_text(encoding='utf-8'),'preserve')

    def test_controlled_profile_requires_live_before_creating_output(self):
        with self.assertRaisesRegex(ValueError,'require --live'):
            run('unused',profile='controlled')

    def test_worker_deadline_kills_and_drains_process(self):
        process = Mock()
        process.communicate.side_effect = [TimeoutExpired('worker',1),('','')]
        with patch('benchmarks.measurement.engine.subprocess.Popen',return_value=process):
            result,_,valid = invoke({'profile':'controlled'}, {}, '2026-10-16', 1)
        self.assertTrue(valid)
        self.assertEqual(result['outcome'],'timeout')
        self.assertTrue(result['usage_lost_on_timeout'])
        process.kill.assert_called_once()
        self.assertEqual(process.communicate.call_count,2)

    def test_usage_meter_counts_failed_and_successful_logical_calls(self):
        response = SimpleNamespace(status='completed',model='test',usage=SimpleNamespace(
            input_tokens=50,output_tokens=10,input_tokens_details=SimpleNamespace(cached_tokens=20)))
        sdk = SimpleNamespace(responses=SimpleNamespace(create=Mock(return_value=response),parse=Mock(side_effect=RuntimeError('offline'))))
        meter = Meter()
        client = meter.wrap(sdk)
        self.assertIs(client.responses.create(model='test'),response)
        with self.assertRaises(RuntimeError):
            client.responses.parse(model='test')
        self.assertEqual(len(meter.events),2)
        self.assertEqual(meter.events[0]['usage']['cached_input_tokens'],20)
        self.assertIsNone(meter.events[1]['usage'])


if __name__ == '__main__':
    unittest.main()
