"""Response-contract diagnostic: historical defects plus live frozen-evidence Q&A.

No retrieval, browser, conversation storage or production database writes occur.
Live generation/review uses configured providers; this is not a quality score.
"""

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys
from zipfile import ZipFile
from time import perf_counter

from benchmarks.scripts.run_samples import grounding_context
from benchmarks.scripts.demo import GOLDEN
from src.generation.direct_generation import DirectGenerator
from src.generation.rag_generation import RAGGenerator
from src.generation.response_contract import resolve_response_contract, response_contract_issues
from src.utils.redaction import CredentialRedactor
from src.utils.utils import PROJECT_ROOT


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if not args.live:
        parser.error('--live is required for real generation/review calls.')
    output = args.output.resolve()
    if not output.is_relative_to((PROJECT_ROOT/'benchmarks'/'runs').resolve()):
        parser.error('Output must be inside benchmarks/runs/.')
    output.mkdir(parents=True, exist_ok=True)
    files = [PROJECT_ROOT/'main.py', PROJECT_ROOT/'config.yaml',
             *sorted((PROJECT_ROOT/'src'/'generation').glob('*.py')),
             *sorted((PROJECT_ROOT/'src'/'prompts').glob('*.py')),
             Path(__file__).resolve()]
    fingerprint = lambda: {str(path.relative_to(PROJECT_ROOT)): sha256(path.read_bytes()).hexdigest() for path in files}
    report = {'scope': __doc__, 'started_at_utc': datetime.now(timezone.utc).isoformat(),
              'code_sha256': fingerprint(), 'historical_checks': [], 'cases': []}
    historical = PROJECT_ROOT/'benchmarks/runs/gpt-5-nano/development-history.zip'
    with ZipFile(historical) as archive:
        historical_text = archive.read('2026-10-15-en-60/results.jsonl').decode('utf-8')
    for line in historical_text.splitlines():
        row = json.loads(line)
        if row['family_id'] not in {'VB1-GROUND-101', 'VB1-GROUND-102', 'VB1-GROUND-140'}:
            continue
        turn = row['turns'][0]
        issues = response_contract_issues(turn['result']['answer'], turn['result']['context'],
                                         resolve_response_contract(turn['message']))
        report['historical_checks'].append({'family_id': row['family_id'], 'message': turn['message'],
            'old_answer': turn['result']['answer'], 'issues': issues, 'blocked': bool(issues)})
    public = {row['case_id']: row for row in map(json.loads, (GOLDEN/'public/cases.jsonl').read_text(encoding='utf-8').splitlines())}
    query = lambda family, language: public[f'VB1-GROUND-{family}-{language}']['turns'][0]['message']
    saved_vi = {'long_term': [{'kind': 'preference', 'key': 'language', 'content': 'Vietnamese',
                             'source_quote': 'Please always reply in Vietnamese.'}]}
    cases = [
        ('closing-time-en', query('101', 'en'), None, False, 'en', None),
        ('monday-closure-en', query('102', 'en'), None, False, 'en', None),
        ('cited-table-en', query('140', 'en'), None, False, 'en', 'table'),
        ('cited-table-vi', query('140', 'vi'), None, False, 'vi', 'table'),
        ('saved-language-vi', query('101', 'en'), saved_vi, False, 'vi', None),
        ('current-language-overrides-memory', 'Answer in English. '+query('101', 'en'), saved_vi, False, 'en', None),
        ('direct-translation', 'Translate "Good morning, thank you for your help." into Vietnamese.', None, True, 'vi', None),
        ('direct-bullets', 'Explain what VISTRA can help with in a bullet list.', None, True, 'en', 'bullets'),
    ]
    redactor = CredentialRedactor()
    for name, message, memory, direct, language, layout in cases:
        print(name+': starting', flush=True)
        started = perf_counter()
        row = {'case': name, 'query': message, 'memory': memory, 'mode': 'direct' if direct else 'frozen_evidence_rag'}
        try:
            with (DirectGenerator() if direct else RAGGenerator()) as generator:
                answer = (generator.generate(message, conversation_memory=memory) if direct else
                          generator.generate(message, grounding_context(), conversation_memory=memory))
                context = {'sources': []} if direct else generator.last_context
                issues = response_contract_issues(answer, context, resolve_response_contract(message, memory), direct=direct)
                requirements_match = generator.last_contract['requirements'] == {'language': language, 'layout': layout}
                row.update(answer=answer, response_contract=generator.last_contract,
                           grounding=generator.last_grounding, context=context, remaining_issues=issues,
                           expected_requirements={'language': language, 'layout': layout}, requirements_match=requirements_match,
                           passed=requirements_match and not issues and generator.last_contract['status'] in {'checked', 'repaired'} and
                                  generator.last_grounding['status'] in {'checked', 'repaired', 'not_applicable'})
        except Exception as error:
            row.update(passed=False, error={'type': type(error).__name__, 'message': str(error)})
        row['elapsed_seconds'] = round(perf_counter()-started, 3)
        report['cases'].append(redactor.data(row))
        (output/'replay.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(name+': '+('passed' if row['passed'] else 'failed'), flush=True)
    report['code_unchanged'] = fingerprint() == report['code_sha256']
    report['finished_at_utc'] = datetime.now(timezone.utc).isoformat()
    report['historical_blocked'] = sum(row['blocked'] for row in report['historical_checks'])
    report['live_passed'] = sum(row['passed'] for row in report['cases'])
    (output/'replay.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({key: report[key] for key in ('historical_blocked', 'live_passed', 'code_unchanged')}), flush=True)


if __name__ == '__main__':
    main()
