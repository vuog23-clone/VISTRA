"""Independent narrow checks; unimplemented and semantic checks stay unscored."""

import re


def grade(case, private, raw):
    from src.service.weather_service import normalized
    checks = []
    def add(name, actual=None, expected=None, reason=None):
        checks.append({'check':name,'status':'unscored' if reason else 'pass' if actual == expected else 'fail',
                       'actual':actual,'expected':expected,'reason':reason})
    turns = {r['turn_id']:r for r in raw.get('turns',[])}
    for assertion in private['private_expectations']['assertions']:
        prefix = assertion['id']
        if raw.get('outcome') == 'unsupported':
            add(prefix,reason='Required execution adapter is unavailable.')
            continue
        if assertion['grader'] == 'human':
            add(prefix,reason='Independent semantic/bilingual review pending; model self-review is not benchmark gold.')
            continue
        record = turns.get(assertion['turn_id'])
        if not record:
            add(prefix+'.execution',actual=False,expected=True)
            continue
        params,kind = assertion['parameters'],assertion['check']
        result = record.get('result',{})
        if kind == 'decision':
            add(prefix+'.response_mode',result.get('response_mode'),params['response_mode'])
            interpretations = record.get('raw_interpretations',[])
            interpretation = interpretations[-1] if interpretations else {}
            add(prefix+'.raw_intent_agreement',interpretation.get('intent'),params['intent'])
            calls = record.get('effective_pipeline_inputs',[])
            planning_action = calls[-1]['options'].get('planning_action') if calls else None
            if planning_action not in {'create_itinerary','revise_itinerary'}:
                planning_action = None
            effective = ((result.get('planning') or {}).get('action') or planning_action or interpretation.get('action'))
            add(prefix+'.effective_action',effective,params['action'])
            location = result.get('location_detection') or {}
            target = params.get('scope')
            if target == 'ambiguous':
                add(prefix+'.scope',location.get('status'), 'ambiguous')
            elif target == 'unknown_destination':
                add(prefix+'.scope',location.get('validation_status'),'unresolved')
            elif target == 'no_location':
                add(prefix+'.scope',location.get('location'),None)
            else:
                actual = location.get('location')
                if not actual and calls:
                    actual = calls[-1]['options'].get('conversation_memory',{}).get('trip_state',{}).get('destination')
                add(prefix+'.scope',normalized(actual),normalized(target))
            add(prefix+'.behavior',reason='Free-text routing requirement still requires semantic review.')
        elif kind == 'memory_state' and record.get('after'):
            owner, session = record['owner_alias'],record['session_alias']
            state = record['after']['owners'][owner]['sessions'][session]['trip_state']
            expected = params.get('expected_state',{})
            for key,value in expected.get('trip_state',{}).items():
                actual = state.get(key)
                if isinstance(value,str) and key == 'destination':
                    actual,value = normalized(actual),normalized(value)
                add(prefix+'.state.'+key,actual,value)
            records = record['after']['owners'][owner]['records']
            preferences = {r['key']:r for r in records if r['kind']=='preference'}
            for key in expected.get('absent_durable_keys',[]):
                add(prefix+'.absent.'+key,key not in preferences,True)
            for key in expected.get('durable',{}):
                add(prefix+'.stored.'+key,key in preferences,True)
                add(prefix+'.value.'+key,reason='Stored verbatim preference requires bilingual semantic comparison, including negation.')
            if 'durable_write_count' in expected:
                writes = sum(len((t.get('result',{}).get('chat') or {}).get('preference_changes',[])) for t in raw['turns'])
                add(prefix+'.durable_write_count',writes,expected['durable_write_count'])
            if expected.get('exact_contiguous_current_message_quote') or params.get('exact_current_quote_for_writes'):
                matches = []
                for turn in raw['turns']:
                    if not turn.get('after'):
                        continue
                    alias = turn['owner_alias']
                    before = {r['key']:r for r in turn['before']['owners'][alias]['records'] if r['kind']=='preference'}
                    after = {r['key']:r for r in turn['after']['owners'][alias]['records'] if r['kind']=='preference'}
                    message = next(t['message'] for t in case['turns'] if t['turn_id']==turn['turn_id'])
                    matches.extend(r['source_quote'] in message for key,r in after.items() if before.get(key) != r)
                if matches:
                    add(prefix+'.written_quote_exact',all(matches),True)
                else:
                    add(prefix+'.written_quote_exact',reason='No changed durable preference was observed; no non-vacuous write to check.')
            add(prefix+'.remaining_contract',reason='Recall semantics, owner isolation and remaining declared fields need dedicated graders/review.')
        elif kind == 'side_effects' and 'expected_http_status' in params:
            add(prefix+'.http_status',record.get('http_status'),params['expected_http_status'])
            add(prefix+'.backend_calls',record.get('backend_calls'),0)
        elif kind == 'side_effects' and record.get('error',{}).get('type') == 'UnsafeInput':
            add(prefix+'.pre_generation_rejection',len(raw.get('provider_calls',[])),0)
            add(prefix+'.memory_unchanged',record.get('before'),record.get('after'))
            add(prefix+'.remaining_contract',reason='Error transport and content policy still require review.')
        elif kind == 'protected_edits':
            prior = turns.get(params.get('compare_to_turn'),{}).get('result',{}).get('itinerary') or {}
            current = result.get('itinerary') or {}
            def stable(day):
                return {k:[item.get('entity_id') for item in v] if isinstance(v,list) else
                    v.get('entity_id') if isinstance(v,dict) else v for k,v in day.items()}
            for number in params.get('protected_days',[]):
                old = next((d for d in prior.get('days',[]) if d['day']==number),None)
                new = next((d for d in current.get('days',[]) if d['day']==number),None)
                if not old or not new:
                    add(prefix+f'.protected_day_{number}',False,True)
                else:
                    # Stable entity identities and day/date/city, not notebook IDs.
                    add(prefix+f'.protected_day_{number}',stable(new),stable(old))
            add(prefix+'.full_edit_contract',reason='Scoped change correctness and feasibility still require independent review.')
        elif kind == 'plan_constraints':
            plan = result.get('itinerary') or {}
            if case['setup'].get('trip_request_ref'):
                from benchmarks.evaluation.protocol import read
                request = read('fixtures/planning-requests.json')[case['setup']['trip_request_ref'].split('#/',1)[1]]
                days = plan.get('days',[])
                add(prefix+'.day_count',len(days),request['duration_days'])
                if days:
                    from datetime import date,timedelta
                    expected_dates = [(date.fromisoformat(request['start_date'])+timedelta(days=i)).isoformat() for i in range(request['duration_days'])]
                    add(prefix+'.dates',[d.get('date') for d in days],expected_dates)
                    add(prefix+'.meal_slots',all(d.get(slot) for d in days for slot in ('breakfast','lunch','dinner')),True)
                    ids = {r['entity_id'] for r in read('fixtures/planning-environment.json')['records']} | {'google:bench_walking_fare'}
                    choices = [item for d in days for slot in ('breakfast','lunch','dinner','accommodation','attractions','transportation')
                        for item in (d.get(slot,[]) if isinstance(d.get(slot),list) else [d.get(slot)]) if item]
                    add(prefix+'.source_identities',bool(choices) and all(c.get('entity_id') in ids for c in choices),True)
            add(prefix+'.full_feasibility',reason='Independent complete schedule/cost/route grader is not implemented; production validation is diagnostic only.')
        else:
            add(prefix,reason='Dedicated assertion grader is not implemented.')
    # Syntax diagnostics are separate from entailment. No citation means N/A.
    for record in raw.get('turns',[]):
        result = record.get('result',{})
        context = result.get('context') or {}
        citations = re.findall(r'\[(S\d+)\]',result.get('answer') or '')
        if citations:
            allowed = {s['source_id'] for s in context.get('sources',[])}
            add('diagnostic.'+record['turn_id']+'.citation_ids',all(c in allowed for c in citations),True)
    return checks
