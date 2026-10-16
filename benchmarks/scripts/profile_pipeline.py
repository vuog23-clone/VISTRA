"""Opt-in live developer API profiling; does not change production scheduling.

Run with python -m benchmarks.scripts.profile_pipeline --live from the project root,
or pipe this script into the API container. Uses real providers and cleans up only
its uniquely named test users. Saves aggregate timings, never answers or keys.
"""

import argparse
from collections import defaultdict
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
from statistics import mean
from time import perf_counter
import traceback
from uuid import uuid4


ACTIVE = ContextVar('vistra_profile_stage', default=None)
SAMPLE = None
PATCHES = []


def instrument(target, name, stage):
    original = getattr(target, name)

    @wraps(original)
    def measured(*args, **kwargs):
        label = stage(*args, **kwargs) if callable(stage) else stage
        if SAMPLE is None or label is None or ACTIVE.get() is not None:
            return original(*args, **kwargs)
        token = ACTIVE.set(label)
        start = perf_counter()
        try:
            return original(*args, **kwargs)
        finally:
            SAMPLE['stages'][label] += perf_counter() - start
            SAMPLE['stage_calls'][label] += 1
            ACTIVE.reset(token)

    setattr(target, name, measured)
    PATCHES.append((target, name, original))


def observe(target, name, callback):
    original = getattr(target, name)

    @wraps(original)
    def observed(*args, **kwargs):
        try:
            result = original(*args, **kwargs)
        except Exception as error:
            if SAMPLE is not None:
                SAMPLE.setdefault('errors', []).append({'operation': name, 'type': type(error).__name__,
                    'frames': [{'file': Path(frame.filename).name, 'function': frame.name, 'line': frame.lineno}
                               for frame in traceback.extract_tb(error.__traceback__)[-6:]]})
            raise
        if SAMPLE is not None:
            callback(args[0], result)
        return result

    setattr(target, name, observed)
    PATCHES.append((target, name, original))


def install():
    import src.service.chat_service as chat
    from src.database.postgres_memory_store import PostgresMemoryStore
    from src.memory.conversation import ConversationInterpreter
    from src.router.jev_response import JevResponseRouter
    from src.router.jev import JevRouter
    from src.resolver.jev_location_extractor import JevLocationExtractor
    from src.service.search_service import SearchService
    from src.planning.tools import TravelTools
    import src.planning.planner as planner
    import src.planning.scheduling as scheduling
    import src.planning.orchestrator as orchestrator
    import src.planning.validation as validation
    from src.planning.notebook import Notebook
    from src.generation.direct_generation import DirectGenerator
    import src.generation.rag_generation as rag
    import src.security.context as context
    import main as pipeline

    instrument(chat.ChatService, '_store', 'memory_load')
    for method in ('load_session', 'recall'):
        instrument(PostgresMemoryStore, method, 'memory_load')
    instrument(chat, 'build_memory_context', 'memory_load')
    instrument(PostgresMemoryStore, 'commit_turn', 'memory_save')
    instrument(ConversationInterpreter, 'understand', 'interpretation')
    for target, method in ((JevResponseRouter, 'route'), (JevRouter, 'route'),
                           (JevLocationExtractor, 'extract_location')):
        instrument(target, method, 'routing_location')
    instrument(SearchService, 'search', 'retrieval')

    def tool_stage(_self, name, *_args, **_kwargs):
        return {'GuideSearch': None, 'WeatherSearch': 'weather',
                'DistanceMatrix': 'routes'}.get(name, 'venue_tools')

    instrument(TravelTools, 'run', tool_stage)
    observe(TravelTools, 'run', lambda _self, result: SAMPLE.setdefault('tools', []).append(
        {'tool': result.tool, 'city': result.city, 'status': result.status}))
    instrument(Notebook, 'write', 'notebook')

    def planner_stage(_self, instructions, payload, *_args, **_kwargs):
        if 'remaining_required' in payload:
            return 'tool_selection'
        return 'llm_repair' if payload.get('rejected_draft') or payload.get('validation_errors') else 'generation'

    instrument(planner.Planner, '_parse', planner_stage)
    observe(planner.Planner, 'run', lambda _self, _result: None)
    for target, method in ((DirectGenerator, '_respond'), (rag.RAGGenerator, '_draft')):
        instrument(target, method, lambda *_args, **kwargs:
                   'llm_repair' if kwargs.get('feedback') else 'generation')
    instrument(rag.RAGGenerator, '_grounding_issues', 'source_review')
    observe(rag.RAGGenerator, 'generate', lambda self, _result: SAMPLE.update(
        grounding_status=(self.last_grounding or {}).get('status')))
    instrument(rag, 'prepare_vision_inputs', 'photo_preparation')
    instrument(planner, 'repair_duplicate_choices', 'code_repair')
    instrument(scheduling, 'repair_schedule', 'code_repair')
    for target in (orchestrator, validation):
        instrument(target, 'validate_itinerary', 'validation')
    for target in (rag, context, pipeline):
        if hasattr(target, 'prepare_context'):
            instrument(target, 'prepare_context', 'validation')
        if hasattr(target, 'answer_safety_issues'):
            instrument(target, 'answer_safety_issues', 'validation')
    for target, method in ((orchestrator, 'render_itinerary'),
                           (orchestrator, 'bind_sources'), (rag, 'build_rag_context')):
        instrument(target, method, 'formatting')

    # Count actual SDK requests, including interpretation/grounding retries.
    from openai.resources.responses.responses import Responses
    for method in ('parse', 'create'):
        original = getattr(Responses, method)

        def counted(*args, _original=original, **kwargs):
            if SAMPLE is not None:
                SAMPLE['openai_calls'][ACTIVE.get() or 'other'] += 1
            return _original(*args, **kwargs)

        setattr(Responses, method, counted)
        PATCHES.append((Responses, method, original))


def run(output, repeats):
    global SAMPLE
    from fastapi.testclient import TestClient
    from api.server import create_app
    from src.database.postgres_memory_store import PostgresMemoryStore
    from src.utils.utils import load_config

    install()
    prefix, owners, samples = 'profile-' + uuid4().hex[:16], [], []
    direct = ['Hello! Introduce yourself in one short sentence.',
              'Thanks! What can you help me with? Answer briefly.',
              'Hi VISTRA, greet me in one sentence.']
    qa = ['What cultural sights does the Hanoi guide describe? Show related place photos and cite sources.',
          'What museums and historic places are described in the Hanoi guide? Include related photos and citations.',
          'What does the Hanoi guide say about the Old Quarter and cultural attractions? Include place photos and sources.']
    plan = ('Create a 3-day Hanoi itinerary for two adults from October 14 to October 16, 2026. '
            'We are already in Hanoi, so no flights or intercity transport are needed. '
            'Total budget: 6,000,000 VND for both people, excluding flights. We prefer vegetarian food, '
            'museums, cultural attractions and walking at a relaxed pace. Include breakfast, lunch, '
            'dinner and attractions each day, plus accommodation for two nights in a private double '
            'room near the Old Quarter. Start around 08:00 and finish by 20:00. Include weather '
            'and estimated walking times. Use sourced places and identify unknown prices and availability.')
    jobs = [('qa_cold', qa[0], {})]
    jobs += [('direct', direct[i % len(direct)], {}) for i in range(repeats)]
    jobs += [('qa_warm', qa[i % len(qa)], {}) for i in range(repeats)]
    jobs += [('itinerary_warm', plan + [' Focus on local history.', ' Focus on art and culture.',
                                    ' Focus on historical attractions.'][i % 3],
              {'include_weather': True}) for i in range(repeats)]
    config = load_config()
    report = {'measured_at_utc': datetime.now(timezone.utc).isoformat(),
              'environment': 'Docker developer API, serial in-process HTTP via FastAPI TestClient',
              'model': config['openai_model'], 'retrieval_limit': 20,
              'notes': ['Real providers and indexed data; no mocked responses.',
                        'Cold means new process/model runtime; filesystem cache may already be warm.',
                        'Stage spans are mutually exclusive; retrieval includes parallel branches.',
                        'Other includes uninstrumented setup, cleanup, rendering and API serialization.',
                        'Client network transit, queueing and frontend rendering are excluded.',
                        'Warm samples use varied queries and isolated fresh user sessions.'],
              'samples': samples}
    try:
        with TestClient(create_app()) as client:
            for index, (flow, message, options) in enumerate(jobs):
                user = f'{prefix}-{index}'
                owners.append('api_' + hashlib.sha256(user.encode()).hexdigest())
                SAMPLE = {'flow': flow, 'stages': defaultdict(float),
                          'stage_calls': defaultdict(int), 'openai_calls': defaultdict(int)}
                start = perf_counter()
                try:
                    response = client.post('/v1/chat', headers={'X-API-Key': os.environ['VISTRA_API_KEY']},
                        json={'user_id': user, 'session_id': 'timing', 'message': message,
                              'retrieval_limit': 20, **options})
                    data = response.json()
                    SAMPLE.update(http_status=response.status_code,
                                  pipeline_status=data.get('pipeline_status'),
                                  response_mode=data.get('response_mode'),
                                  reported_pipeline_seconds=data.get('elapsed_seconds'),
                                  planning_status=(data.get('planning') or {}).get('status'),
                                  repair_attempts=(data.get('planning') or {}).get('repair_attempts'),
                                  itinerary_days=len((data.get('itinerary') or {}).get('days', [])))
                except Exception as error:
                    SAMPLE['exception_type'] = type(error).__name__
                SAMPLE['total_seconds'] = perf_counter() - start
                SAMPLE['stages']['other'] = SAMPLE['total_seconds'] - sum(SAMPLE['stages'].values())
                samples.append(SAMPLE)
                print(json.dumps({'sample': index + 1, 'flow': flow,
                                  'seconds': round(SAMPLE['total_seconds'], 3),
                                  'status': SAMPLE.get('pipeline_status'),
                                  'openai_calls': dict(SAMPLE['openai_calls'])}), flush=True)
                SAMPLE = None
                Path(output).write_text(json.dumps(report, indent=2), encoding='utf-8')
    finally:
        SAMPLE = None
        with PostgresMemoryStore() as store:
            for owner in owners:
                store.delete_user_memory(owner)
        for target, method, original in reversed(PATCHES):
            setattr(target, method, original)
    report['averages'] = {}
    for flow in dict.fromkeys(item['flow'] for item in samples):
        group = [item for item in samples if item['flow'] == flow]
        stages = sorted({stage for item in group for stage in item['stages']})
        report['averages'][flow] = {'n': len(group),
            'total_seconds': mean(item['total_seconds'] for item in group),
            'stage_seconds': {stage: mean(item['stages'].get(stage, 0) for item in group) for stage in stages},
            'openai_calls': mean(sum(item['openai_calls'].values()) for item in group)}
    report['outcome_averages'] = {}
    for name, predicate in (('itinerary_with_draft', lambda s: s['itinerary_days'] > 0),
                            ('itinerary_failed', lambda s: s.get('planning_status') == 'failed')):
        group = [s for s in samples if s['flow'] == 'itinerary_warm' and predicate(s)]
        if group:
            report['outcome_averages'][name] = {'n': len(group),
                'total_seconds': mean(item['total_seconds'] for item in group)}
    Path(output).write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({'averages': report['averages'], 'report': output}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Allow real provider requests.')
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--output', default='/tmp/vistra-stage-timings.json')
    args = parser.parse_args()
    if not args.live or not 1 <= args.repeats <= 10:
        parser.error('Supply --live and repeats between 1 and 10.')
    run(args.output, args.repeats)
