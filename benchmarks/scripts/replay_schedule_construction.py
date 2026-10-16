"""Live planning diagnostic with indexed venues and real directed route estimates.

Uses the real planning/tools stack, not conversation memory. These two fixed
cases are a regression diagnostic, not an independently scored benchmark.
"""

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys
from time import perf_counter

from api.runtime import RetrievalRuntime
from src.planning.orchestrator import TravelOrchestrator, trip_request
from src.planning.planner import Planner
from src.service.search_service import SearchService
from src.utils.redaction import CredentialRedactor
from src.utils.utils import PROJECT_ROOT, load_config


CASES = [
    ('one-day-zero-nights', 'Create a one-day itinerary in Hanoi for two adults on October 17, 2026. '
     'We are already in Hanoi. Include breakfast, lunch, dinner and sightseeing, with no overnight accommodation. '
     'Our total budget is 2,000,000 VND. Use walking route estimates and show unknown details.',
     {'destinations':['Hanoi'], 'origin':'Hanoi', 'duration_days':1, 'start_date':'2026-10-17', 'end_date':'2026-10-17',
      'travelers':2, 'budget_amount':2000000, 'currency':'VND', 'transport_mode':'walking'}),
    ('three-day-qualified-city', 'Create a three-day Hanoi itinerary for two adults from October 17 to October 19, 2026. '
     'We are already in Hanoi. Include breakfast, lunch, dinner and sightseeing each day, plus two nights in a private double room. '
     'We prefer vegetarian food. Our total budget is 6,000,000 VND. Use walking route estimates and identify unknown details.',
     {'destinations':['Hanoi, Vietnam'], 'origin':'Hanoi', 'duration_days':3, 'start_date':'2026-10-17', 'end_date':'2026-10-19',
      'travelers':2, 'budget_amount':6000000, 'currency':'VND', 'transport_mode':'walking',
      'cuisines':['vegetarian'], 'room_type':'private double room'}),
]


class ObservedPlanner(Planner):
    def __init__(self, **options):
        super().__init__(**options)
        self.drafts = []

    def run(self, *args, **options):
        result = super().run(*args, **options)
        self.drafts.append(result.model_dump())
        return result


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not args.live:
        parser.error('--live is required for provider calls.')
    output = args.output.resolve()
    if not output.is_relative_to((PROJECT_ROOT/'benchmarks'/'runs').resolve()):
        parser.error('Output must be inside benchmarks/runs/.')
    output.mkdir(parents=True, exist_ok=True)
    config, redactor = load_config(), CredentialRedactor()
    runtime, results = RetrievalRuntime(), []
    files = [PROJECT_ROOT/'main.py', PROJECT_ROOT/'config.yaml', *sorted((PROJECT_ROOT/'src').rglob('*.py'))]
    metadata = {'scope':'Two fixed live planning diagnostics; no conversation database writes, no gold score.',
        'model':config['openai_model'], 'started_at_utc':datetime.now(timezone.utc).isoformat(),
        'code_sha256':{str(path.relative_to(PROJECT_ROOT)):sha256(path.read_bytes()).hexdigest() for path in files}}
    try:
        for name, query, state in CASES:
            start = perf_counter()
            row = {'case':name, 'query':query}
            print(name+': starting', flush=True)
            def progress(event):
                if event['step'] in ('planning_tool','planning_plan','planning_repair','planning_validate'):
                    print(name+': '+event['title'], flush=True)
            try:
                request = trip_request(query,state)
                with ExitStack() as resources:
                    search = resources.enter_context(SearchService(text_embedder_factory=lambda:runtime.text,
                        image_embedder_factory=lambda:runtime.image, sparse_embedder_factory=lambda:runtime.sparse))
                    planner = resources.enter_context(ObservedPlanner(config=config))
                    result = TravelOrchestrator(config=config, planner=planner).run(query,request,
                        search_service=search, route='text', include_weather=False, on_progress=progress)
                    ids = [choice['entity_id'] for day in (result.get('itinerary') or {}).get('days',[]) for choice in
                           [day.get('breakfast'),day.get('lunch'),day.get('dinner'),*day.get('attractions',[])] if choice]
                    checks = {'returned_itinerary':bool(result.get('itinerary')),
                        'requested_days':len((result.get('itinerary') or {}).get('days',[])) == request.duration_days,
                        'no_validation_errors':not any(i['severity']=='error' for i in result['planning']['issues']),
                        'no_repeated_venues':len(ids)==len(set(ids)),
                        'no_final_night':bool(result.get('itinerary')) and result['itinerary']['days'][-1]['accommodation'] is None,
                        'canonical_city':request.destinations==['Hanoi']}
                    row.update(request=request.model_dump(), proposed_drafts=planner.drafts,
                               result=result, checks=checks, passed=all(checks.values()))
            except Exception as error:
                row.update(passed=False,error={'type':type(error).__name__,'message':str(error)})
            row['elapsed_seconds'] = round(perf_counter()-start,3)
            row = redactor.data(row)
            results.append(row)
            (output/(name+'.json')).write_text(json.dumps(row,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            print(json.dumps({'case':name,'passed':row['passed'],'elapsed_seconds':row['elapsed_seconds']}),flush=True)
    finally:
        runtime.close()
    metadata.update(completed_at_utc=datetime.now(timezone.utc).isoformat(),
                    passed=sum(row['passed'] for row in results), cases=len(results))
    (output/'run.json').write_text(json.dumps(metadata,indent=2)+'\n',encoding='utf-8')
    return 0 if all(row['passed'] for row in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
