"""Offline quality checks for the authored golden-set candidate. No model calls.

Includes a validator for the JSON Schema keywords used by case.schema.json,
reference/hash checks, bilingual export checks and independent witness arithmetic.
It does not certify natural-language gold labels or run the VISTRA agent.
"""

from collections import Counter
from datetime import date
import hashlib
import json
from math import ceil
from pathlib import Path
import re


ROOT=Path(__file__).resolve().parents[1]/'vistra-v1'/'golden'


def unique_object(pairs):
    result={}
    for key,value in pairs:
        if key in result: raise ValueError('Duplicate JSON key: '+key)
        result[key]=value
    return result


def read(path):
    return json.loads(path.read_text(encoding='utf-8'),object_pairs_hook=unique_object)


def lines(path):
    return [json.loads(line,object_pairs_hook=unique_object)
            for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def contract(value,schema,path='$'):
    """Enforce the explicit authoring contract's supported JSON Schema keywords."""
    def fail(message): raise ValueError(path+': '+message)
    types={'object':lambda v:isinstance(v,dict),'array':lambda v:isinstance(v,list),
           'string':lambda v:isinstance(v,str),'boolean':lambda v:type(v) is bool,
           'integer':lambda v:type(v) is int,'number':lambda v:type(v) in (int,float),
           'null':lambda v:v is None}
    if 'type' in schema:
        choices=schema['type'] if isinstance(schema['type'],list) else [schema['type']]
        if not any(types[t](value) for t in choices): fail('wrong type')
    if 'enum' in schema and value not in schema['enum']: fail('not in enum')
    if 'const' in schema and value!=schema['const']: fail('const mismatch')
    if isinstance(value,dict):
        if not set(schema.get('required',[]))<=set(value): fail('missing required field')
        if len(value)<schema.get('minProperties',0): fail('too few properties')
        if len(value)>schema.get('maxProperties',len(value)): fail('too many properties')
        props=schema.get('properties',{})
        patterns=schema.get('patternProperties',{})
        for key,child in value.items():
            covered=False
            if key in props:
                contract(child,props[key],path+'.'+key); covered=True
            for pattern,rule in patterns.items():
                if re.search(pattern,key): contract(child,rule,path+'.'+key); covered=True
            if not covered and schema.get('additionalProperties') is False: fail('unexpected property '+key)
    if isinstance(value,list):
        if not schema.get('minItems',0)<=len(value)<=schema.get('maxItems',len(value)): fail('array length')
        if schema.get('uniqueItems') and len({json.dumps(v,sort_keys=True) for v in value})!=len(value): fail('duplicate array item')
        if 'items' in schema:
            for index,child in enumerate(value): contract(child,schema['items'],path+f'[{index}]')
        if 'contains' in schema:
            matches=0
            for child in value:
                try: contract(child,schema['contains']); matches+=1
                except ValueError: pass
            if not matches: fail('contains did not match')
    if isinstance(value,str):
        if not schema.get('minLength',0)<=len(value)<=schema.get('maxLength',len(value)): fail('string length')
        if 'pattern' in schema and not re.search(schema['pattern'],value): fail('string pattern')
        if schema.get('format')=='date': date.fromisoformat(value)
    for rule in schema.get('allOf',[]): contract(value,rule,path)
    if 'if' in schema:
        try: contract(value,schema['if'],path)
        except ValueError: condition=False
        else: condition=True
        if condition and 'then' in schema: contract(value,schema['then'],path)


def reference(text):
    filename,_,pointer=text.partition('#')
    target=(ROOT/filename).resolve()
    if not target.is_relative_to(ROOT.resolve()): raise ValueError('Reference escapes dataset root')
    value=read(target)
    for token in pointer.strip('/').split('/') if pointer else []:
        token=token.replace('~1','/').replace('~0','~')
        value=value[int(token)] if isinstance(value,list) else value[token]
    return value


def witness_check(witness,environment):
    records={r['entity_id']:r for r in environment['records']}
    matrix=environment['route_matrix']
    locations={key:index for index,key in enumerate(matrix['locations'])}
    seen=set(); calculated=0
    def minute(text):
        hour,part=map(int,text.split(':')); return hour*60+part
    for index,day in enumerate(witness['days'],1):
        assert day['day']==index
        assert day['date']==date(2026,10,15+index).isoformat()
        previous=None
        for slot in ('breakfast','attraction','lunch','dinner'):
            choice=day[slot]; entity=choice['entity_id']; record=records[entity]
            assert entity not in seen; seen.add(entity)
            assert record['city']==day['city']=='Hanoi'
            assert record['kind']==('attraction' if slot=='attraction' else 'restaurant')
            start,end=minute(choice['start']),minute(choice['end'])
            assert start<end
            assert any(minute(a)<=start and end<=minute(b) for a,b in record['opening_hours']['all_days'])
            if previous:
                duration=matrix['durations'][locations[previous['entity_id']]][locations[entity]]
                assert start-minute(previous['end'])>=ceil(duration/60)
            calculated+=record['cost']*witness['travelers']
            previous=choice
        if day['accommodation']:
            record=records[day['accommodation']]
            assert witness['rooms_per_night']*record['maximum_occupancy']>=witness['travelers']
            assert len(witness['days'])-1>=record['minimum_nights']
            calculated+=record['cost']*witness['rooms_per_night']
    assert calculated==witness['total_cost_vnd']


def main():
    schema=read(ROOT.parent/'case.schema.json')
    manifest=read(ROOT/'manifest.json')
    assert hashlib.sha256((ROOT.parent/'case.schema.json').read_bytes()).hexdigest()==manifest['case_schema_sha256']
    project_root=Path(__file__).resolve().parents[2]
    for rel,expected in manifest['authoring_sources'].items():
        assert hashlib.sha256((project_root/rel).read_bytes()).hexdigest()==expected,rel
    fixtures=read(ROOT/'fixtures/manifest.json')
    for rel,expected in manifest['files'].items():
        assert hashlib.sha256((ROOT/rel).read_bytes()).hexdigest()==expected,rel
    for rel,expected in fixtures['files'].items():
        assert hashlib.sha256((ROOT/rel).read_bytes()).hexdigest()==expected,rel
    fixture_hash=hashlib.sha256((ROOT/'fixtures/manifest.json').read_bytes()).hexdigest()
    assert fixture_hash==manifest['fixture_manifest_sha256']
    cases=[row for path in sorted((ROOT/'private').glob('*.jsonl')) for row in lines(path)]
    assert len(cases)==300 and len({c['family_id'] for c in cases})==300
    assert Counter(c['track'] for c in cases)==dict.fromkeys(('routing','retrieval','grounding','memory','planning','reliability'),50)
    assert Counter(c['split'] for c in cases)=={'development':90,'validation':60,'test':150}
    groups={}
    env=read(ROOT/'fixtures/environment.json')
    entity_ids={r['entity_id'] for r in env['entities'].values()}
    for case in cases:
        contract(case,schema,case['family_id'])
        group=case['leakage_group']
        assert groups.setdefault(group,case['split'])==case['split']
        assert case['status']=='draft' and not case['review']['adjudicated']
        assert case['fixtures']['manifest_sha256']==fixture_hash
        turns={turn['turn_id'] for turn in case['turns']}
        assert len(turns)==len(case['turns'])
        assert set(case['private_expectations']['expected_handling'])==turns
        assertion_ids=set()
        for check in case['private_expectations']['assertions']:
            assert check['id'] not in assertion_ids; assertion_ids.add(check['id'])
            assert check['turn_id'] in turns
            params=check['parameters']
            if 'fixture_fact' in params:
                head,*parts=params['fixture_fact'].split('.')
                value=env['entities'].get(head,env.get(head))
                for part in parts: value=value[part]
                assert value==params['expected_value']
            if 'request_ref' in params: reference(params['request_ref'])
        for key in ('witness_ref','qrels_ref'):
            ref=case['private_expectations'][key]
            if ref: reference(ref)
        for key,ref in case.get('setup',{}).items():
            if ref: reference(ref)
        for turn in case['turns']:
            assert set(turn['messages'])=={'en','vi'}
            assert not turn['messages']['vi'].isascii(),case['family_id']
            assert '@PLAN' not in turn['messages']['en']+turn['messages']['vi']
    qrels=read(ROOT/'fixtures/qrels.json')
    assert len(qrels)==50
    for qrel in qrels.values():
        assert set(qrel['graded']).isdisjoint(qrel['negative'])
        assert set(qrel['graded'])|set(qrel['negative'])<=entity_ids
        assert all(v==2 for v in qrel['graded'].values())
        assert all(v==0 for v in qrel['negative'].values())
    planning=read(ROOT/'fixtures/planning-environment.json')
    witnesses=read(ROOT/'fixtures/planning-witnesses.json')
    for witness in witnesses.values(): witness_check(witness,planning)
    assert witnesses['base3']['total_cost_vnd']==2700000
    for request in read(ROOT/'fixtures/planning-requests.json').values():
        key='solo1' if request['travelers']==1 else 'group4' if request['travelers']==4 else 'base1' if request['duration_days']==1 else 'base3'
        limit=request['budget_amount']*(request['travelers'] if request['budget_basis']=='per_person' else 1)
        assert witnesses[key]['total_cost_vnd']<=limit
    proofs=read(ROOT/'fixtures/contradictions.json')
    assert proofs['budget_floor']['minimum_cost_vnd']>proofs['budget_floor']['budget_vnd']
    assert proofs['occupancy']['guests']>proofs['occupancy']['rooms']*proofs['occupancy']['capacity_per_room']
    assert proofs['minimum_stay']['requested_nights']<proofs['minimum_stay']['minimum_nights']
    assert proofs['time_capacity']['required_minutes']>proofs['time_capacity']['available_minutes']
    public=lines(ROOT/'public/cases.jsonl')
    assert len(public)==600 and len({p['case_id'] for p in public})==600
    by_id={case['family_id']:case for case in cases}
    for row in public:
        assert not {'private_expectations','assertions','expected_handling','review','title','coverage_tags'}&set(row)
        original=by_id[row['family_id']]
        for authored,exported in zip(original['turns'],row['turns']):
            assert exported['message']==authored['messages'][row['language']]
            assert 'messages' not in exported
            assert 'planning-witnesses' not in json.dumps(row['setup'])
    # Mutation check: the authoring contract must reject an unwanted third language.
    altered=json.loads(json.dumps(cases[0]))
    altered['turns'][0]['messages']['third_language']='not allowed'
    try: contract(altered,schema)
    except ValueError: pass
    else: raise AssertionError('Schema failed to reject an undeclared language.')
    report={'status':'passed_structural_reference_witness_checks','families':300,'areas':6,
        'families_per_area':50,'language_cases':600,'user_turn_requests':sum(len(row['turns']) for row in public),
        'languages':['en','vi'],'planning_witnesses_checked':len(witnesses),'contradiction_certificates_checked':len(proofs),
        'source_fact_assertions_checked':sum(check['check']=='claim_support' for case in cases for check in case['private_expectations']['assertions']),
        'retrieval_qrel_sets_checked':len(qrels),'schema_mutation_control_passed':True,
        'dataset_file_hashes':manifest['files'],'agent_benchmark_executed':False,
        'independent_semantic_adjudication':'pending','replay_adapter_implementation':'pending','visual_pixel_labels':'not_included'}
    (ROOT/'validation-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('PASS: 300 families, 50 per area; 600 bilingual cases; schema-contract checks, references, hashes, qrels, source facts, planning witnesses and private/public export separation.')
    print('NOT CERTIFIED: bilingual semantic equivalence, independent adjudication, pixel relevance, replay adapters or VISTRA performance.')


if __name__=='__main__':
    main()
