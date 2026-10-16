"""Run a reproducible English diagnostic sample: ten families per area.

Uses live providers only with --live. This is an assistant-reviewed diagnostic,
not a certified replay score: retrieval/planning use the current indexed world,
grounding uses frozen synthetic source text, and reliability exercises the real
developer API's validation boundary in-process.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import secrets
import sys
from time import perf_counter

from api.runtime import RetrievalRuntime
from benchmarks.scripts.demo import GOLDEN, load_cases
from src.generation.rag_generation import RAGGenerator
from src.service.chat_service import ChatService
from src.utils.redaction import CredentialRedactor
from src.utils.utils import PROJECT_ROOT, load_config


SELECTION = {
    'routing': [109, 101, 106, 114, 119, 124, 129, 134, 142, 144],
    'retrieval': [101, 103, 106, 115, 117, 118, 119, 121, 133, 136],
    'grounding': [101, 102, 103, 104, 105, 106, 107, 108, 109, 140],
    'memory': [101, 107, 108, 112, 113, 121, 125, 129, 141, 143],
    'planning': [101, 102, 106, 107, 109, 110, 126, 128, 136, 144],
    'reliability': list(range(136, 146)),
}
MODES = {
    'routing': 'live_chat', 'retrieval': 'live_chat', 'memory': 'live_chat', 'planning': 'live_chat',
    'grounding': 'synthetic_grounding_diagnostic', 'reliability': 'in_process_api_boundary',
}


def selected_families(private):
    by_area_number = {(row['track'], int(row['family_id'].rsplit('-', 1)[1])): row for row in private.values()}
    return [by_area_number[(area, numbers[index])] for index in range(10) for area, numbers in SELECTION.items()]


def grounding_context():
    """Supply evidence, never gold assertions or expected answers.

    Source.text only accepts Wikivoyage-prefixed transport IDs. These diagnostic
    text IDs are explicitly synthetic, have no live URL, and quote the unmodified
    frozen entity JSON; they are not represented as real Wikivoyage pages.
    """
    env = json.loads((GOLDEN / 'fixtures/environment.json').read_text(encoding='utf-8'))
    sources = []
    for alias in ('hn_museum', 'hn_park', 'hn_cafe', 'hue_cafe'):
        entity = env['entities'][alias]
        sources.append({'source_id': f'S{len(sources)+1}', 'entity_id': 'wikivoyage:synthetic_fixture_' + alias,
            'source': 'text', 'name': 'Synthetic fixture: ' + entity['name'], 'url': None,
            'excerpts': [{'section': 'Frozen fictional benchmark record', 'text': json.dumps(entity, ensure_ascii=False)}]})
    return {'route': 'text', 'sources': sources}


def compact_result(result):
    keep = ('answer', 'response_mode', 'response_routing', 'pipeline_status', 'generation_status', 'location_detection',
            'grounding', 'generation_vision', 'itinerary', 'planning', 'chat', 'weather', 'elapsed_seconds', 'context', 'notebook')
    compact = {key: result[key] for key in keep if key in result}
    retrieval = result.get('retrieval')
    if retrieval:
        compact['retrieval'] = {key: retrieval.get(key) for key in
            ('route', 'location', 'location_status', 'retrieval_status', 'branch_status', 'retrieval_warnings', 'image_reranking')}
        for branch in ('results', 'related_results'):
            compact['retrieval'][branch] = [{key: hit.get(key) for key in
                ('entity_id', 'name', 'source', 'category', 'province', 'address', 'distance_km', 'url')}
                for hit in retrieval.get(branch, [])]
    return compact


def execute_family(row, public, runtime, api_client, api_key, namespace):
    started = perf_counter()
    family = row['family_id']
    case = public[family + '-en']
    turns = []
    output = {'family_id': family, 'area': row['track'], 'title': row['title'], 'language': 'en',
              'mode': MODES[row['track']], 'planned_turns': len(case['turns']), 'turns': turns,
              'canonical_fixture_replay': False}
    redactor = CredentialRedactor(extra_secrets=[api_key])
    for turn in case['turns']:
        record = {'turn_id': turn['turn_id'], 'owner_alias': turn['owner_alias'], 'session_alias': turn['session_alias'],
                  'message': turn['message']}
        turns.append(record)
        turn_started = perf_counter()
        owner = f"benchmark_{namespace}_{family}_{turn['owner_alias']}"
        session = f"sample_{namespace}_{turn['session_alias']}"
        try:
            if row['track'] == 'grounding':
                context = grounding_context()
                with RAGGenerator() as generator:
                    answer = generator.generate(turn['message'], context)
                    result = {'answer': answer, 'grounding': generator.last_grounding,
                              'context': generator.last_context, 'pipeline_status': 'complete' if
                              generator.last_grounding['status'] in {'checked', 'repaired'} else 'partial'}
                record['result'] = compact_result(result)
            elif row['track'] == 'reliability':
                overrides = turn.get('request_overrides', {})
                body = {'user_id': owner, 'session_id': session,
                        'message': 'Find cafes in Hanoi.' if overrides else turn['message']}
                body.update(overrides.get('body', {}))
                auth = overrides.get('authorization')
                headers = {} if auth == 'missing' else {'X-API-Key': 'invalid-benchmark-key' if auth == 'invalid_test_key' else api_key}
                response = api_client.post('/v1/chat', json=body, headers=headers)
                record['request_summary'] = {'body_bytes': len(response.request.content),
                    'message_characters': len(body['message']), 'authorization': auth or 'valid_test_key',
                    'overridden_fields': list(overrides.get('body', {}))}
                record['http_status'] = response.status_code
                record['result'] = response.json()
                expected = next((assertion['parameters']['expected_http_status'] for assertion in
                    row['private_expectations']['assertions'] if 'expected_http_status' in assertion['parameters']), None)
                record['http_status_matches_gold'] = response.status_code == expected if expected is not None else None
            else:
                with ChatService(pipeline=runtime.pipeline) as service:
                    result = service.chat(owner, session, turn['message'])
                record['result'] = compact_result(result)
        except Exception as error:
            record['error'] = {'type': type(error).__name__, 'message': redactor.text(str(error))[:1500]}
            record['elapsed_seconds'] = round(perf_counter() - turn_started, 3)
            output['stopped_after_failed_turn'] = True
            break
        record['elapsed_seconds'] = round(perf_counter() - turn_started, 3)
    output['elapsed_seconds'] = round(perf_counter() - started, 3)
    output['completed_turns'] = sum('error' not in turn for turn in turns)
    return redactor.data(output)


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--workers', type=int, choices=(1, 2), default=2)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--areas', nargs='+', choices=list(SELECTION), default=list(SELECTION))
    args = parser.parse_args()
    if not args.live:
        parser.error('Use --live to authorize provider calls and isolated benchmark memory writes.')
    output = args.output.resolve()
    allowed = (PROJECT_ROOT / 'benchmarks' / 'runs').resolve()
    if not output.is_relative_to(allowed):
        parser.error('Output must be inside benchmarks/runs/.')
    output.mkdir(parents=True, exist_ok=True)
    public, private, _ = load_cases()
    rows = selected_families(private)
    manifest_path = output / 'run.json'
    code_files = [PROJECT_ROOT / 'main.py', PROJECT_ROOT / 'config.yaml', *sorted((PROJECT_ROOT / 'src').rglob('*.py')),
                  Path(__file__).resolve(), PROJECT_ROOT / 'api/server.py', PROJECT_ROOT / 'api/runtime.py']
    hashes = {path.relative_to(PROJECT_ROOT).as_posix(): sha256(path.read_bytes()).hexdigest() for path in code_files}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        if manifest['code_sha256'] != hashes:
            raise RuntimeError('Code changed since this run began; use a new output directory.')
    else:
        manifest = {'namespace': secrets.token_hex(6), 'language': 'en', 'client_date': '2026-10-15',
            'started_at_utc': datetime.now(timezone.utc).isoformat(), 'provider_model': load_config()['openai_model'],
            'code_sha256': hashes, 'golden_manifest_sha256': sha256((GOLDEN/'manifest.json').read_bytes()).hexdigest(),
            'selection': [{key: row[key] for key in ('family_id', 'title', 'track', 'split')} for row in rows],
            'selection_method': 'Declared diverse diagnostic sample; not random or statistically representative.',
            'modes': MODES, 'canonical_fixture_replay': False,
            'limitations': ['Live data is not the fictional qrels/planning world; exact fixture scores are not applicable.',
                'Grounding is an isolated generation/review diagnostic, not retrieval or conversation replay.',
                'Reliability sample covers API validation and user injection, not provider outage recovery.',
                'No frozen benchmark pass rate or significance claim is produced.',
                'Visa answers follow the user-requested source-qualified behavior; old official-only labels are not treated as release policy.']}
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    result_path = output / 'results.jsonl'
    completed = {json.loads(line)['family_id'] for line in result_path.read_text(encoding='utf-8').splitlines()} if result_path.exists() else set()
    pending = [row for row in rows if row['family_id'] not in completed and row['track'] in args.areas]
    runtime = RetrievalRuntime(cache_size=load_config()['query_embedding_cache_size'])
    api_key = 'benchmark-only-' + secrets.token_hex(24)
    api_service_calls = []
    def chat_factory():
        api_service_calls.append(True)
        return ChatService(pipeline=runtime.pipeline)
    from api.server import create_app
    from fastapi.testclient import TestClient
    try:
        with TestClient(create_app(api_key=api_key, chat_factory=chat_factory)) as api_client, \
             result_path.open('a', encoding='utf-8') as file, ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {executor.submit(execute_family, row, public, runtime, api_client, api_key, manifest['namespace']): row for row in pending}
            for future in as_completed(futures):
                result = future.result()
                file.write(json.dumps(result, ensure_ascii=False, allow_nan=False)+'\n')
                file.flush()
                print(f"{result['family_id']} {result['completed_turns']}/{result['planned_turns']} turns {result['elapsed_seconds']}s", flush=True)
    finally:
        runtime.close()
    manifest['api_chat_factory_calls'] = len(api_service_calls)
    manifest['last_completed_at_utc'] = datetime.now(timezone.utc).isoformat()
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')


if __name__ == '__main__':
    main()
