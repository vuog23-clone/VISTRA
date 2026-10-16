"""Invoke real components with public fixtures; never import private expectations."""

from contextlib import ExitStack
from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import NAMESPACE_URL, uuid5


class MemoryStore:
    """Isolated state adapter, not a PostgreSQL/concurrency performance test."""
    def __init__(self, fail=False):
        from src.schema.memory import SessionSnapshot
        self.snapshot, self.records, self.fail = SessionSnapshot(), [], fail

    def load_session(self, *_args, **_options):
        return self.snapshot.model_copy(deep=True)

    def recall(self, *_args, **_options):
        return deepcopy(self.records)

    def commit_turn(self, _owner, _session, **values):
        from src.database.postgres_memory_store import MemoryConflict
        from src.schema.memory import MemoryMessage, MemoryRecord, SessionSnapshot
        if self.fail or values['expected_version'] != self.snapshot.version:
            raise MemoryConflict('Injected fixture commit conflict.')
        records = deepcopy(self.records)
        for change in values['preference_changes']:
            records = [record for record in records if record.key != change.key]
            if change.operation == 'set':
                records.append(MemoryRecord(kind='preference', key=change.key,
                    content=change.source_quote, source_quote=change.source_quote))
        self.snapshot = SessionSnapshot(version=self.snapshot.version+1, summary=values['summary'],
            trip_state=values['trip_state'], recent_messages=[*self.snapshot.recent_messages,
                MemoryMessage(role='user', content=values['user_message']),
                MemoryMessage(role='assistant', content=values['answer'])])
        self.records = records
        return self.snapshot.version


def _observed(generator, meter):
    generator.client = meter.wrap(generator.client)
    return generator


def route(case, fixtures, meter):
    from src.schema.memory import TripDetails, TurnUnderstanding
    def understand(query, memory):
        return TurnUnderstanding(intent='travel',action=case['input']['interpreted'],standalone_query=query,summary='',
            trip_state=TripDetails(destination='Hanoi',destinations=['Hanoi'],duration_days=3,
                start_date='2026-10-16',end_date='2026-10-20'),preference_changes=[])
    return _route_chat(case,SimpleNamespace(understand=understand))


def _route_chat(case, interpreter):
    from src.service.chat_pipeline import run_chat_pipeline
    from src.service.chat_service import ChatService
    from src.schema.planning import resolve_planning_action
    forbidden = Mock(side_effect=AssertionError('Clarification must precede evidence/planning calls.'))
    direct = SimpleNamespace(model_name='fixture',last_grounding=None,last_contract=None,
        generate=Mock(return_value='An itinerary describes a travel schedule.' if case['language']=='en' else 'Lịch trình mô tả kế hoạch chuyến đi.'))
    observed = []
    class Interpreter:
        def understand(self,query,memory):
            value = interpreter.understand(query,memory)
            observed.append(value)
            return value
    def pipeline(query, **options):
        return run_chat_pipeline(query, **options,
            search_service=SimpleNamespace(search=forbidden),
            planning_tools=SimpleNamespace(run=forbidden),
            itinerary_planner=SimpleNamespace(model_name='fixture',next_action=forbidden,run=forbidden),
            generator=SimpleNamespace(model_name='fixture',generate=forbidden,last_grounding=None,last_context=None,last_vision=None),
            direct_generator=direct,
            response_router=SimpleNamespace(route=lambda *_: {'mode':'llm_only','decision_source':'jev'}))
    store = MemoryStore()
    with ChatService(store=store,interpreter=Interpreter(),pipeline=pipeline) as service:
        result = service.chat('measurement-owner','measurement-session',case['message'],include_weather=False)
    return {'action':resolve_planning_action(case['message'],interpreted=observed[-1].action),
        'branch':'planner' if result.get('planning') else 'direct' if result.get('response_mode')=='llm_only' else 'other',
        'duration':store.snapshot.trip_state.duration_days,'downstream_calls':forbidden.call_count,
        'interpretation':observed[-1].model_dump(),'result':result}


def interpreted_route(case, fixtures, meter):
    from src.memory.conversation import ConversationInterpreter
    with _observed(ConversationInterpreter(), meter) as interpreter:
        return _route_chat(case,interpreter)


def schedule(case, fixtures, meter):
    from src.planning.notebook import Notebook
    from src.planning.orchestrator import TravelOrchestrator
    from src.schema.planning import EvidenceRecord, Itinerary, ToolResult, TripRequest
    from src.schema.routing import RouteLocation, RouteMatrixEvidence
    data = fixtures['schedule']
    book, records = Notebook(), []
    for index, raw in enumerate(data['venues']):
        hours = {'friday': [{'open': '19:00' if raw['kind'] == 'attraction' and case['input']['night_only'] else raw['opens'],
                              'close': '23:00' if raw['kind'] == 'attraction' and case['input']['night_only'] else raw['closes']}]}
        records.append(EvidenceRecord(entity_id=raw['entity_id'], name=raw['name'], kind=raw['kind'],
            city=data['city'], provider='Google metadata', cost=raw['cost'], currency=data['currency'],
            cost_unit='per_person', opening_hours=hours, latitude=21+index/1000, longitude=105))
    for kind, tool in [('restaurant', 'RestaurantSearch'), ('attraction', 'AttractionSearch')]:
        book.write(ToolResult(tool=tool, city=data['city'], status='available',
            records=[record for record in records if record.kind == kind], note='Frozen fictional measurement data.'))
    points = [RouteLocation(**{key: getattr(record, key) for key in
        ('entity_id', 'name', 'city', 'latitude', 'longitude')}) for record in records]
    book.write(ToolResult(tool='DistanceMatrix', city=data['city'], status='available', note='Frozen fictional route matrix.',
        route_matrix=RouteMatrixEvidence(mode='walking', profile='foot-walking', fetched_at='2026-10-16T00:00:00+00:00',
            locations=points, durations_seconds=[[0 if a == b else data['route_minutes']*60 for b in range(4)] for a in range(4)],
            distances_meters=[[0 if a == b else 800 for b in range(4)] for a in range(4)])))
    request = TripRequest(destinations=[data['city']], duration_days=1, start_date=data['date'], end_date=data['date'],
        travelers=data['party_size'], budget_amount=case['input']['budget'], currency=data['currency'], transport_mode='walking')
    draft = Itinerary.model_validate({'days': [{'day': 1, 'city': data['city'], 'date': data['date']}]})
    entries = {entry.tool:ToolResult.model_validate(entry.model_dump(exclude={'entry_id'})) for entry in book.data.entries}
    class Tools:
        def run(self,name,city,**options):
            return entries.get(name,ToolResult(tool=name,city=city,status='no_results',note='No frozen records for this tool.'))
    planner = SimpleNamespace(model_name='scripted-fixture',
        next_action=lambda request,notebook,remaining: {'tool':remaining[0][0],'city':remaining[0][1]} if remaining else {'tool':'Planner'},
        run=lambda *args,**options: draft.model_copy(deep=True))
    from src.utils.utils import load_config
    result = TravelOrchestrator(config=load_config(),planner=planner,tools=Tools()).run(case['message'],request,include_weather=False)
    return {'itinerary':result['itinerary'],'answer':result['answer'],'production_report':result['planning']}


def photos(case, fixtures, meter):
    from frontend.media import build_place_cards
    from src.generation.rag_generation import build_rag_context
    from src.retrieval.category_filters import photo_category_constraints
    from src.schema.models import SearchResult, SearchResponse
    rows = fixtures['photos'][-1:] if case['input']['spoof_only'] else fixtures['photos']
    hits = []
    for raw in rows:
        identity = raw['id']
        point = str(uuid5(NAMESPACE_URL, 'vistra-measurement/'+identity))
        place = dict(place_id=identity, place_name=raw['name'], category=raw['category'],
                     address=raw['city']+', Vietnam', province=raw['city'])
        photo = dict(image_id=point, place_id=raw['photo_owner'], image_url='https://lh3.googleusercontent.com/p/'+identity)
        hits.append(SearchResult(place_id=identity, entity_id='google:'+identity, point_id=point,
            source='image', score=1, name=raw['name'], category=raw['category'], address=place['address'],
            province=raw['city'], payload=place, image=photo, metadata={'place': place, 'image': photo}))
    categories, excluded = photo_category_constraints(case['message'])
    response = SearchResponse(route='hybrid', related_results=hits, requested_categories=categories, excluded_categories=excluded)
    context = build_rag_context(response)
    cards = build_place_cards({'retrieval': asdict(response), 'context': context})
    return {'context': context, 'cards': cards, 'categories': categories, 'excluded': excluded}


def contract(case, fixtures, meter):
    from src.generation.response_contract import resolve_response_contract, response_contract_issues
    vi, kind = case['language'] == 'vi', case['input']['draft']
    if kind == 'bare':
        draft = ('Đóng cửa lúc 17:00. Source: S1' if vi else 'It closes at 17:00. Source: S1')
    elif kind == 'bullets':
        draft = '- Benchmark Museum: 120000 VND [S1]\n- Benchmark Park: 0 VND [S2]'
    else:
        draft = ('| Địa điểm | Giá vé |' if vi else '| Place | Price |') + '\n| --- | --- |\n| Benchmark Museum | 120000 VND [S1] |\n| Benchmark Park | 0 VND [S2] |'
    issues = response_contract_issues(draft, {'sources': fixtures['sources']}, resolve_response_contract(case['message']))
    return {'draft': draft, 'issues': issues}


def answer(case, fixtures, meter):
    from src.generation.rag_generation import RAGGenerator
    from src.utils.utils import load_config
    config = load_config()
    config['generation_vision_enabled'] = False  # Frozen fixtures have no photos.
    with _observed(RAGGenerator(config=config), meter) as generator:
        text = generator.generate(case['message'], {'route': 'text', 'sources': fixtures['sources']})
        return {'answer': text, 'grounding': generator.last_grounding, 'contract': generator.last_contract,
                'context': generator.last_context}


def memory(case, fixtures, meter):
    from src.service.chat_service import ChatService
    from src.schema.memory import PreferenceChange, TripDetails, TurnUnderstanding
    from src.database.postgres_memory_store import MemoryConflict
    scenario = case['input']['scenario']
    store, calls, results = MemoryStore(fail=scenario == 'commit_failure'), [], []
    with ExitStack() as resources:
        if case['profile'] == 'controlled':
            from src.memory.conversation import ConversationInterpreter
            from src.generation.direct_generation import DirectGenerator
            interpreter = resources.enter_context(_observed(ConversationInterpreter(), meter))
            direct = resources.enter_context(_observed(DirectGenerator(), meter))
        else:
            def understand(query, context):
                details = TripDetails.model_validate(context.trip_state.model_dump(exclude={'itinerary'}))
                changes = []
                if scenario == 'budget':
                    details.destination = 'Hanoi'
                    details.budget_amount = 8000000 if context.trip_state.budget_amount is not None else 6000000
                    details.currency = 'VND'
                elif scenario in {'mixed', 'commit_failure'}:
                    changes = [PreferenceChange(key='diet', operation='set', source_quote=query.split('.')[0].split('?')[0])]
                return TurnUnderstanding(intent='memory', action='answer', standalone_query=query,
                    summary='User-provided fixture requirements.', trip_state=details, preference_changes=changes)
            interpreter = SimpleNamespace(understand=understand)
        def pipeline(query, **options):
            calls.append(query)
            text = direct.generate(query, conversation_memory=options['conversation_memory']) if case['profile'] == 'controlled' else '4'
            return {'answer': text, 'pipeline_status': 'complete', 'context': {'sources': []}}
        with ChatService(store=store, interpreter=interpreter, pipeline=pipeline) as service:
            for message in case['message'].split('\n'):
                try:
                    results.append(service.chat('measurement-owner', 'measurement-session', message))
                except MemoryConflict:
                    return {'commit_failed': True, 'state': store.snapshot.model_dump(), 'records': [], 'results': results,
                            'pipeline_calls': len(calls)}
    return {'state': store.snapshot.model_dump(), 'records': [record.model_dump() for record in store.records],
            'results': results, 'pipeline_calls': len(calls)}


def review_failure(case, fixtures, meter):
    from src.generation.rag_generation import RAGGenerator
    from src.utils.utils import load_config
    draft = 'UNVERIFIED 17:00 [S1]' if case['language'] == 'en' else 'CHƯA KIỂM CHỨNG 17:00 [S1]'
    responses = SimpleNamespace(create=Mock(return_value=SimpleNamespace(status='completed', output_text=draft)),
                                parse=Mock(side_effect=RuntimeError('Injected reviewer outage.')))
    config = load_config()
    config['generation_vision_enabled'] = False
    with RAGGenerator(config=config, client=SimpleNamespace(responses=responses)) as generator:
        text = generator.generate(case['message'], {'route': 'text', 'sources': fixtures['sources']})
        return {'answer': text, 'draft_calls': responses.create.call_count, 'review_calls': responses.parse.call_count,
                'grounding': generator.last_grounding}


ADAPTERS = {'route': route, 'interpreted_route': interpreted_route, 'schedule': schedule,
            'photos': photos, 'contract': contract, 'answer': answer, 'memory': memory,
            'review_failure': review_failure}
