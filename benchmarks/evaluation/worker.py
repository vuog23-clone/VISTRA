"""Fresh case process: real models and PostgreSQL, public inputs only."""

from contextlib import ExitStack, redirect_stdout
from datetime import date
import io
import json
from pathlib import Path
import sys
from time import perf_counter
from unittest.mock import patch

from benchmarks.evaluation.protocol import capability, track
from benchmarks.evaluation.providers import SearchReplay, WeatherReplay, PlanningReplay, grounding_context
from benchmarks.measurement.usage import Meter


def owners(case, namespace):
    return {turn['owner_alias']:f"beval_{namespace}_{case['case_id']}_{turn['owner_alias']}" for turn in case['turns']}


def cleanup(case, namespace):
    from src.database.postgres_memory_store import PostgresMemoryStore
    identities = list(owners(case,namespace).values())
    with PostgresMemoryStore() as store, store.connection.transaction():
        store.connection.execute('DELETE FROM vistra_long_term_memories WHERE user_id = ANY(%s)', (identities,))
        store.connection.execute('DELETE FROM vistra_memory_sessions WHERE user_id = ANY(%s)', (identities,))
        remaining = sum(store.connection.execute('SELECT count(*) AS n FROM '+table+' WHERE user_id = ANY(%s)',(identities,)).fetchone()['n']
            for table in ('vistra_long_term_memories','vistra_memory_sessions','vistra_memory_messages'))
    return {'verified_deleted':remaining == 0,'owner_count':len(identities)}


def snapshot(store, case, namespace):
    owner_ids = owners(case,namespace)
    return {'owners':{alias:{'records':[r.model_dump() for r in store.recall(identity)],
        'sessions':{session:store.load_session(identity,session).model_dump() for session in
                    sorted({t['session_alias'] for t in case['turns'] if t['owner_alias']==alias})}}
        for alias,identity in owner_ids.items()}}


def api_case(case):
    from fastapi.testclient import TestClient
    from api.server import create_app
    calls = []
    def forbidden():
        calls.append(True)
        raise AssertionError('Validation case unexpectedly reached backend.')
    key = 'benchmark-only-isolated-validation-key'
    records = []
    with TestClient(create_app(api_key=key,chat_factory=forbidden)) as client:
        for turn in case['turns']:
            override = turn.get('request_overrides',{})
            body = {'user_id':'isolated-validation-owner','session_id':turn['session_alias'],'message':turn['message']}
            body.update(override.get('body',{}))
            auth = override.get('authorization')
            headers = {} if auth == 'missing' else {'X-API-Key':'invalid-key' if auth == 'invalid_test_key' else key}
            response = client.post('/v1/chat',json=body,headers=headers)
            records.append({'turn_id':turn['turn_id'],'http_status':response.status_code,'result':response.json(),
                'body_bytes':len(response.request.content),'backend_calls':len(calls)})
    return records


def execute(payload, output, meter, checkpoint):
    from src.generation.rag_generation import RAGGenerator
    from src.generation.direct_generation import DirectGenerator
    from src.planning.planner import Planner
    from src.memory.conversation import ConversationInterpreter
    from src.service.chat_service import ChatService
    from src.service.chat_pipeline import run_chat_pipeline
    from src.database.postgres_memory_store import PostgresMemoryStore
    from src.router.jev_response import JevResponseRouter
    case, namespace = payload['case'], payload['namespace']
    mode,_ = capability(case)
    if mode == 'api_validation':
        output['turns'] = api_case(case)
        return
    with ExitStack() as resources:
        resources.enter_context(patch('src.service.weather_service.reference_today',return_value=date.fromisoformat(case['reference_date'])))
        def model(cls):
            value = resources.enter_context(cls())
            value.client = meter.wrap(value.client)
            return value
        generator = model(RAGGenerator)
        if mode == 'fixed_evidence_generation':
            for turn in case['turns']:
                record = {'turn_id':turn['turn_id']}
                output['turns'].append(record)
                start = perf_counter()
                try:
                    record['result'] = {'answer':generator.generate(turn['message'],grounding_context(case)),
                        'grounding':generator.last_grounding,'contract':generator.last_contract,'context':generator.last_context}
                except Exception as error:
                    record['error'] = {'type':type(error).__name__,'message':str(error)[:1500]}
                record['seconds'] = perf_counter()-start
                checkpoint()
            return
        direct, planner, interpreter = model(DirectGenerator), model(Planner), model(ConversationInterpreter)
        raw_interpretations, pipeline_calls, search_calls, decisions = [], [], [], []
        class Interpreter:
            def understand(self,*args,**kwargs):
                raw = interpreter.understand(*args,**kwargs)
                raw_interpretations.append(raw.model_dump())
                return raw
        response_router = resources.enter_context(JevResponseRouter())
        class ResponseRouter:
            def route(self,*args,**kwargs):
                raw = response_router.route(*args,**kwargs)
                decisions.append(raw.model_dump())
                return raw
        planning_tools = PlanningReplay(case)
        search = SearchReplay(resources,search_calls)
        def pipeline(query,**options):
            # Observe effective orchestration inputs; do not change interpretation.
            pipeline_calls.append({'query':query,'options':{k:v for k,v in options.items() if not callable(v)}})
            return run_chat_pipeline(query,**options,generator=generator,direct_generator=direct,
                itinerary_planner=planner,planning_tools=planning_tools,search_service=search,
                weather_tool=WeatherReplay(),response_router=ResponseRouter())
        store = resources.enter_context(PostgresMemoryStore())
        service = resources.enter_context(ChatService(store=store,interpreter=Interpreter(),pipeline=pipeline))
        identities = owners(case,namespace)
        from benchmarks.evaluation.protocol import profile
        overrides = profile(case).get('overrides',{})
        if overrides.get('omitted_history') or overrides.get('imported_memory'):
            from src.schema.memory import PreferenceChange,TripState
            imported = overrides.get('imported_memory',{})
            seed_quote = 'vegetarian' if imported else 'Earlier conversation history is omitted.'
            summary = imported.get('summary') or 'Earlier conversation history was omitted; exact older quotes are unavailable.'
            store.commit_turn(identities[case['turns'][0]['owner_alias']],case['turns'][0]['session_alias'],
                expected_version=0,user_message=seed_quote,answer=None,summary=summary,trip_state=TripState(),
                preference_changes=[PreferenceChange(operation='set',key='diet',source_quote=seed_quote)] if imported else [])
            output['public_setup_seed'] = {'provenance':'public scenario profile; excluded from model writes','summary':summary,
                'imported_preference':bool(imported),'omitted_history':bool(overrides.get('omitted_history'))}
        for turn in case['turns']:
            record = {'turn_id':turn['turn_id'],'owner_alias':turn['owner_alias'],'session_alias':turn['session_alias']}
            output['turns'].append(record)
            record['before'] = snapshot(store,case,namespace)
            counts = [len(x) for x in (raw_interpretations,pipeline_calls,search_calls,decisions,planning_tools.calls)]
            start = perf_counter()
            try:
                record['result'] = service.chat(identities[turn['owner_alias']],turn['session_alias'],turn['message'])
            except Exception as error:
                record['error'] = {'type':type(error).__name__,'message':str(error)[:1500],
                    'code':getattr(error,'code',None)}
            record['seconds'] = perf_counter()-start
            record['after'] = snapshot(store,case,namespace)
            for name,values,count in zip(('raw_interpretations','effective_pipeline_inputs','search_calls','response_decisions','tool_calls'),
                (raw_interpretations,pipeline_calls,search_calls,decisions,planning_tools.calls),counts):
                record[name] = values[count:]
            checkpoint()


def main():
    from src.utils.redaction import CredentialRedactor
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stdin.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    payload = json.loads(sys.stdin.read())
    output = {'turns':[],'outcome':'completed'}
    meter, start = Meter(),perf_counter()
    def checkpoint():
        output.update(provider_calls=meter.events,seconds=perf_counter()-start)
        value = json.dumps(CredentialRedactor().data(output),ensure_ascii=False,allow_nan=False)
        path = Path(payload['checkpoint'])
        temp = path.with_suffix('.tmp')
        temp.write_text(value,encoding='utf-8')
        temp.replace(path)
    try:
        with redirect_stdout(io.StringIO()):
            execute(payload,output,meter,checkpoint)
    except Exception as error:
        import traceback
        origin = traceback.extract_tb(error.__traceback__)[-1].filename.replace('\\','/')
        output.update(outcome='harness_error' if '/benchmarks/evaluation/' in origin else 'system_error',
            error={'type':type(error).__name__,'message':str(error)[:1500]})
    finally:
        checkpoint()
    print(json.dumps(CredentialRedactor().data(output),ensure_ascii=False,allow_nan=False))


if __name__ == '__main__':
    main()
