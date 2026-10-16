"""Live memory-update diagnostic using real interpretation and isolated PostgreSQL.

Pure updates must not call an answer pipeline. Two ordinary questions use actual
direct generation. No retrieval/planning/weather providers are used. Only a new
random benchmark owner's memory is written and removed at completion.
"""

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import sys
from time import perf_counter
from uuid import uuid4

from src.database.postgres_memory_store import PostgresMemoryStore
from src.generation.direct_generation import DirectGenerator
from src.memory.conversation import ConversationInterpreter
from src.service.chat_service import ChatService
from src.utils.redaction import CredentialRedactor
from src.utils.utils import PROJECT_ROOT


CASES = [
    ('VB1-MEM-121-initial', 'budget', 'My Hanoi trip budget is 6000000 VND.', True),
    ('VB1-MEM-121-correction', 'budget', 'Change it to 8000000 VND total.', True),
    ('VB1-MEM-143-trip', 'trip', 'My trip is to Hanoi, October 16-18.', True),
    ('VB1-MEM-143-new-chat', 'new-chat', 'What trip destination have I chosen in this new chat?', False),
    ('VB1-ROUTE-134', 'preferences', 'Remember that I usually prefer walking.', True),
    ('vietnamese-food-preference', 'preferences', 'Ghi nhớ rằng tôi thích ăn chay.', True),
    ('correct-food-preference', 'preferences', 'Actually, I am vegan. Remember this instead of vegetarian.', True),
    ('forget-food-preference', 'preferences', 'Forget my food preference.', True),
    ('temporary-trip-preference', 'temporary', 'For this trip, vegetarian meals only.', True),
    ('mixed-update-question', 'mixed', 'Remember that I prefer vegetarian meals. What is 2 + 2?', False),
    ('set-language-vi', 'language', 'Remember to reply in Vietnamese.', True),
    ('saved-language-budget', 'budget', 'Change my budget to 9000000 VND total.', True),
    ('current-language-override', 'budget', 'Answer in English. Change my budget to 10000000 VND total.', True),
]


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if not args.live:
        parser.error('--live is required for real model calls and isolated memory writes.')
    output = args.output.resolve()
    if not output.is_relative_to((PROJECT_ROOT/'benchmarks'/'runs').resolve()):
        parser.error('Output must be inside benchmarks/runs/.')
    output.mkdir(parents=True, exist_ok=True)
    files = [PROJECT_ROOT/'config.yaml', PROJECT_ROOT/'main.py',
             *sorted((PROJECT_ROOT/'src').rglob('*.py')), Path(__file__).resolve()]
    fingerprint = lambda: {str(path.relative_to(PROJECT_ROOT)): sha256(path.read_bytes()).hexdigest() for path in files}
    owner = 'benchmark_memory_ack_' + uuid4().hex
    report = {'scope': __doc__, 'started_at_utc': datetime.now(timezone.utc).isoformat(),
              'code_sha256': fingerprint(), 'owner': owner, 'cases': [], 'cleanup': 'pending'}
    redactor = CredentialRedactor()
    try:
        with PostgresMemoryStore() as store, ConversationInterpreter() as interpreter, DirectGenerator() as direct:
            for name, session, query, expected_ack in CASES:
                row = {'case': name, 'session': session, 'query': query, 'expected_acknowledgement': expected_ack}
                started, calls = perf_counter(), []
                interpretations = []
                print(name+': starting', flush=True)
                def pipeline(message, **options):
                    calls.append(message)
                    if expected_ack:
                        raise AssertionError('An update-only turn reached answer generation.')
                    from main import run_pipeline
                    options['response_mode'] = 'llm_only'
                    return run_pipeline(message, direct_generator=direct, **options)
                try:
                    class RecordedInterpreter:
                        def understand(self, message, memory):
                            value = interpreter.understand(message, memory)
                            interpretations.append(value.model_dump())
                            return value
                    with ChatService(store=store, interpreter=RecordedInterpreter(), pipeline=pipeline) as chat:
                        result = chat.chat(owner, session, query)
                    snapshot = store.load_session(owner, session)
                    records = store.recall(owner)
                    is_ack = result.get('generation_status') == 'memory_acknowledgement'
                    checks = {
                        'expected_path': is_ack == expected_ack,
                        'answer_pipeline_calls': len(calls) == (0 if expected_ack else 1),
                        'committed_answer': snapshot.recent_messages[-1].content == result['answer'],
                        'version_matches': snapshot.version == result['chat']['version'],
                    }
                    if expected_ack:
                        checks['brief'] = len(result['answer']) <= 320
                        checks['no_travel_evidence'] = not result.get('retrieval') and not result.get('weather') and not result.get('itinerary')
                    if name == 'VB1-MEM-121-correction':
                        checks['corrected_budget'] = snapshot.trip_state.budget_amount == 8000000 and snapshot.trip_state.currency == 'VND'
                    elif name == 'VB1-MEM-143-new-chat':
                        checks['no_other_session_trip'] = snapshot.trip_state.destination is None and not snapshot.trip_state.destinations
                        checks['no_internal_terminology'] = 'trip_state' not in result['answer']
                    elif name == 'correct-food-preference':
                        checks['corrected_preference'] = any(record.key == 'diet' and 'vegan' in record.content.casefold() for record in records)
                    elif name in ('forget-food-preference', 'temporary-trip-preference'):
                        checks['no_durable_diet'] = not any(record.kind == 'preference' and record.key == 'diet' for record in records)
                        if name == 'temporary-trip-preference':
                            checks['trip_requirement_saved'] = any('vegetarian' in requirement.casefold()
                                for requirement in snapshot.trip_state.requirements)
                    elif name == 'mixed-update-question':
                        checks['question_answered'] = bool(re.search(r'\b(?:4|four)\b', result['answer'], re.I))
                        checks['preference_saved'] = any(record.key == 'diet' for record in records)
                    elif name in ('set-language-vi', 'saved-language-budget'):
                        checks['vietnamese_acknowledgement'] = result['answer'].startswith('Đã ')
                    elif name == 'current-language-override':
                        checks['english_acknowledgement'] = result['answer'].startswith('Updated ')
                    if name.startswith(('VB1-MEM-121', 'VB1-MEM-143')):
                        checks['trip_facts_not_saved_as_preferences'] = not any(record.kind == 'preference' for record in records)
                    row.update(passed=all(checks.values()), checks=checks, answer=result['answer'],
                        generation_status=result.get('generation_status'), pipeline_status=result.get('pipeline_status'),
                        memory_update=result.get('memory_update'), saved_trip=snapshot.trip_state.model_dump(),
                        saved_preferences=[record.model_dump() for record in records if record.kind == 'preference'])
                except Exception as error:
                    row.update(passed=False, error={'type': type(error).__name__, 'message': str(error)})
                row['elapsed_seconds'] = round(perf_counter()-started, 3)
                row['interpretations'] = interpretations
                report['cases'].append(redactor.data(row))
                (output/'replay.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
                print(name+': '+('passed' if row['passed'] else 'failed'), flush=True)
    finally:
        with PostgresMemoryStore() as cleanup:
            cleanup.delete_user_memory(owner)
            remaining = cleanup.connection.execute('''SELECT
                (SELECT COUNT(*) FROM vistra_memory_sessions WHERE user_id=%s) +
                (SELECT COUNT(*) FROM vistra_long_term_memories WHERE user_id=%s) AS remaining''',
                (owner, owner)).fetchone()['remaining']
            report['cleanup'] = 'completed' if remaining == 0 else 'failed'
        report['code_unchanged'] = fingerprint() == report['code_sha256']
        report['finished_at_utc'] = datetime.now(timezone.utc).isoformat()
        report['passed'] = sum(row['passed'] for row in report['cases'])
        (output/'replay.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({key: report[key] for key in ('passed', 'cleanup', 'code_unchanged')}), flush=True)


if __name__ == '__main__':
    main()
