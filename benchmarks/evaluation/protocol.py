"""Public execution policy. No private expectations are imported by workers."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / 'benchmarks/vistra-v1/golden'
PROTOCOL = 'vistra-catalog-first-pass-v1'


def read(path):
    return json.loads((GOLDEN / path).read_text(encoding='utf-8'))


def public_cases():
    return [json.loads(line) for line in (GOLDEN / 'public/cases.jsonl').read_text(encoding='utf-8').splitlines()]


def profile(case):
    return read('fixtures/scenario-profiles.json')[case['setup']['environment_ref'].split('#/', 1)[1]]


def track(case):
    return {'ROUTE':'routing', 'RETR':'retrieval', 'GROUND':'grounding', 'MEM':'memory',
            'PLAN':'planning', 'FAIL':'reliability'}[case['family_id'].split('-')[1]]


def capability(case):
    """Never silently execute a fault scenario without its injected condition."""
    area, env = track(case), profile(case)
    condition = case['setup']['environment_ref'].split('#/', 1)[1]
    number = int(case['family_id'].rsplit('-', 1)[1])
    if area == 'reliability':
        if 136 <= number <= 144:
            return 'api_validation', None
        if 145 <= number <= 147:
            return 'chat_provider_replay', None
        return 'unsupported', 'Required provider/concurrency/streaming fault adapter is not implemented in this first-pass runner.'
    if area == 'grounding':
        return 'fixed_evidence_generation', None
    if area == 'retrieval':
        return 'unsupported', 'Frozen vectors and photo bytes are absent; metadata lookup cannot measure the declared text/photo retrieval checks.'
    if area == 'memory' and condition in {'memory_truncated_history', 'memory_instruction_in_memory'}:
        return 'chat_provider_replay', None
    if area == 'memory' and condition == 'memory_failed_edit_state':
        return 'unsupported', 'Imported/truncated history or seeded-plan failure setup needs a dedicated adapter.'
    if area == 'planning':
        # The common candidate pool covers ordinary and missing-field profiles.
        # These remaining profiles need extra venues, matrix cells or faults.
        if number not in {115,119,134,135,141,142,150}:
            return 'chat_provider_replay', None
        return 'unsupported', 'Special planning profile has no materialized public evidence transformation in this runner.'
    if env.get('fault') or env.get('overrides') or set(case['setup']) - {'environment_ref'}:
        return 'unsupported', 'Additional declared setup is not supported.'
    if case['surface'] != 'chat_service':
        return 'unsupported', 'Dedicated surface adapter is missing.'
    return 'chat_provider_replay', None
