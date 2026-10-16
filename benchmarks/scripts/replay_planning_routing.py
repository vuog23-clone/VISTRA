"""Replay the date-conflict regression with the configured live interpreter.

No database writes or place/weather retrieval are allowed: both inputs must stop
at planning clarification. The store and downstream providers are test doubles.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from unittest.mock import Mock

from src.memory.conversation import ConversationInterpreter
from src.schema.memory import SessionSnapshot
from src.service.chat_pipeline import run_chat_pipeline
from src.service.chat_service import ChatService
from src.utils.redaction import CredentialRedactor
from src.utils.utils import PROJECT_ROOT


def replay(query):
    observed = []
    forbidden = Mock(side_effect=AssertionError('Clarification must precede downstream providers.'))
    store = Mock()
    store.load_session.return_value = SessionSnapshot()
    store.recall.return_value = []
    store.commit_turn.return_value = 1
    with ConversationInterpreter() as live:
        class Interpreter:
            def understand(self, message, memory):
                parsed = live.understand(message, memory)
                observed.append(parsed.model_dump())
                return parsed

        def pipeline(message, **options):
            return run_chat_pipeline(message, **options,
                planning_tools=Mock(run=forbidden), itinerary_planner=Mock(run=forbidden, next_action=forbidden),
                search_service=Mock(search=forbidden),
                generator=Mock(generate=forbidden, last_grounding=None, last_vision=None),
                response_router=Mock(route=forbidden),
                direct_generator=Mock(generate=forbidden, last_grounding=None))

        with ChatService(store=store, interpreter=Interpreter(), pipeline=pipeline) as service:
            result = service.chat('routing-replay', 'date-conflict', query)
        state = store.commit_turn.call_args.kwargs['trip_state']
        checks = {
            'planning_clarification': result['generation_status'] == 'planning_clarification',
            'needs_clarification': result['pipeline_status'] == 'needs_clarification',
            'create_action': (result.get('planning') or {}).get('action') == 'create_itinerary',
            'both_choices': [x['reply'] for x in (result.get('planning') or {}).get('clarification_options', [])] == ['5 days', '3 days'],
            'conflicting_dates_preserved': state.start_date == '2026-10-16' and state.end_date == '2026-10-20',
            'duration_preserved': state.duration_days == 3,
            'no_downstream_calls': forbidden.call_count == 0,
        }
        return {'message': query, 'interpreter': observed, 'result': result,
                'saved_trip_state': state.model_dump(), 'checks': checks, 'passed': all(checks.values())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not args.live:
        parser.error('--live is required for the configured interpreter provider call.')
    output = args.output.resolve()
    if not output.is_relative_to((PROJECT_ROOT / 'benchmarks' / 'runs').resolve()):
        parser.error('Output must be inside benchmarks/runs/.')
    output.parent.mkdir(parents=True, exist_ok=True)
    cases = []
    for message in ('Plan three days in Hanoi from October 16 to October 20, 2026.',
                    'Plan three days in Hanoi from October 16 to October 20, 2026, but do not include flights.'):
        try:
            cases.append(replay(message))
        except Exception as error:
            cases.append({'message': message, 'passed': False,
                          'error': {'type': type(error).__name__, 'message': str(error)}})
    report = CredentialRedactor().data({'executed_at_utc': datetime.now(timezone.utc).isoformat(),
        'scope': 'Live interpreter and real routing/clarification, mocked store and downstream providers; not a planning quality score.',
        'cases': cases, 'passed': all(case['passed'] for case in cases)})
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({'passed': report['passed'], 'cases': len(cases), 'output': str(output)}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
