"""Declared denominators and efficiency summaries; no weighted quality score."""

from collections import Counter, defaultdict
from math import ceil, isfinite
from statistics import median


def percentile(values, percentile_value):
    return sorted(values)[max(0, ceil(len(values)*percentile_value)-1)] if values else None


def ratio(passed, total):
    return passed/total if total else None


def usage_summary(rows, profile, pricing=None, configured_model=None):
    events = [event for row in rows for event in row.get('observation', {}).get('provider_calls', [])]
    lost = any(row.get('observation', {}).get('usage_lost_on_timeout') for row in rows)
    result = {'llm_sdk_calls': None if lost else len(events), 'known_llm_sdk_calls': len(events), 'usage_applicable': profile == 'controlled',
              'calls_with_usage': sum(event.get('usage') is not None for event in events),
              'input_tokens': None, 'output_tokens': None, 'cached_input_tokens': None,
              'estimated_cost_usd': None, 'cost_status': 'not_applicable' if profile == 'offline' else 'pricing_not_supplied'}
    if profile == 'offline':
        return result
    complete = bool(events) and not lost and all(event.get('usage') is not None for event in events)
    if complete and any(any(type(event['usage'].get(key)) is not int or event['usage'][key] < 0
                           for key in ('input_tokens','output_tokens')) for event in events):
        result['cost_status'] = 'usage_invalid'
        return result
    if complete:
        for name in ('input_tokens','output_tokens','cached_input_tokens'):
            values = [event['usage'].get(name) for event in events]
            if all(type(value) is int and value >= 0 for value in values):
                result[name] = sum(values)
    if not complete:
        result['cost_status'] = 'usage_incomplete'
        return result
    if pricing:
        total = 0
        for event in events:
            rate = pricing['models'].get(event.get('served_model')) or pricing['models'].get(configured_model)
            usage = event['usage']
            if rate is None:
                result['cost_status'] = 'model_rate_missing'
                return result
            cached = usage.get('cached_input_tokens')
            if type(cached) is not int or not 0 <= cached <= usage['input_tokens']:
                result['cost_status'] = 'cached_usage_incomplete'
                return result
            total += ((usage['input_tokens']-cached)*rate['input_per_million'] +
                      cached*rate['cached_input_per_million'] + usage['output_tokens']*rate['output_per_million'])/1000000
        result.update(estimated_cost_usd=total, cost_status='estimated_from_reported_usage')
    return result


def validate_pricing(pricing):
    from datetime import date
    if pricing is None:
        return
    date.fromisoformat(pricing['as_of'])
    if pricing.get('currency') != 'USD' or not pricing.get('source') or not pricing.get('models'):
        raise ValueError('Pricing needs an as_of date, USD currency, source and explicit per-model rates.')
    for rate in pricing['models'].values():
        if set(rate) != {'input_per_million','cached_input_per_million','output_per_million'} or any(
                type(value) not in (int,float) or not isfinite(value) or value < 0 for value in rate.values()):
            raise ValueError('Pricing rates must be finite, nonnegative numbers per million tokens.')


def summarize(manifest, rows, pricing=None, reviews=None):
    index = {row['execution_id']: row for row in rows}
    expected = manifest['executions']
    if len(index) != len(rows) or set(index)-{row['execution_id'] for row in expected}:
        raise ValueError('Duplicate or unexpected measurement results.')
    groups = defaultdict(list)
    enriched = []
    for spec in expected:
        row = index.get(spec['execution_id'])
        if row is not None and set(row['checks']) != set(spec['checks']):
            raise ValueError('Result check coverage differs from the declared denominator.')
        checks = row['checks'] if row else dict.fromkeys(spec['checks'], 'unknown')
        if any(value not in {'pass','fail','unknown'} for value in checks.values()):
            raise ValueError('Invalid check state.')
        item = {**spec, 'checks': checks, 'passed': bool(checks) and all(value == 'pass' for value in checks.values()),
                'row': row}
        enriched.append(item)
        groups[spec['area']].append(item)

    def aggregate(items):
        states = Counter(value for item in items for value in item['checks'].values())
        durations = [item['row']['wall_seconds'] for item in items if item['row'] is not None]
        passes = sum(item['passed'] for item in items)
        data = {'planned_executions': len(items), 'recorded_executions': sum(item['row'] is not None for item in items),
                'automatic_passed': passes, 'automatic_pass_rate': ratio(passes, len(items)),
                'checks': {key: states[key] for key in ('pass','fail','unknown')},
                'check_micro_pass': ratio(states['pass'], sum(states.values())),
                'check_macro_pass': sum(sum(value == 'pass' for value in item['checks'].values())/len(item['checks']) for item in items)/len(items),
                'worker_wall_median_seconds': median(durations) if durations else None,
                'worker_wall_p95_seconds': percentile(durations, .95), 'latency_samples': len(durations),
                'system_errors': sum(item['row'] is not None and item['row']['observation']['outcome'] != 'ok' for item in items)}
        data['usage'] = usage_summary([item['row'] for item in items if item['row']], manifest['profile'], pricing, manifest['settings']['openai_model'])
        # Efficiency per automatic pass is labeled as such, never task-success cost.
        cost = data['usage']['estimated_cost_usd']
        data['cost_per_automatic_pass_usd'] = cost/passes if cost is not None and passes else None
        return data

    languages = {language: aggregate([item for item in enriched if item['language'] == language]) for language in ('en','vi')}
    family_groups = defaultdict(list)
    for item in enriched:
        family_groups[item['family_id']].append(item)
    both = sum(all(item['passed'] for item in items) for items in family_groups.values())
    repeats = {str(repeat): aggregate([item for item in enriched if item['repeat'] == repeat]) for repeat in range(1,manifest['repeats']+1)}
    review_specs = [item for item in enriched if item['review_required']]
    review_index = {} if reviews is None else {row['execution_id']: row for row in reviews['results']}
    if reviews is not None:
        if len(review_index) != len(reviews['results']) or set(review_index)-{item['execution_id'] for item in review_specs}:
            raise ValueError('Duplicate or ineligible human review IDs.')
        if not reviews.get('reviewer') or reviews.get('rubric_version') != 'measurement-review-1':
            raise ValueError('Human reviews require reviewer attribution and the pinned rubric version.')
        for review in review_index.values():
            if any(review.get(key) not in {'pass','fail','unknown'} for key in ('task_success','claim_support','language')):
                raise ValueError('Human review labels must be pass, fail or unknown.')
    reviewed = sum(all(review_index.get(item['execution_id'], {}).get(key, 'unknown') != 'unknown'
        for key in ('task_success','claim_support','language')) for item in review_specs)
    human_passes = sum(item['passed'] and all(review_index.get(item['execution_id'], {}).get(key) == 'pass'
        for key in ('task_success','claim_support','language')) for item in review_specs)
    reviewed_usage = usage_summary([item['row'] for item in review_specs if item['row']],manifest['profile'],pricing,manifest['settings']['openai_model'])
    reviewed_cost = reviewed_usage['estimated_cost_usd']
    return {'complete': len(index) == len(expected) and all(row.get('harness_ok', False) for row in rows),
        'scope': 'automatic development checks, not a whole-system quality score',
        'overall': aggregate(enriched), 'areas': {key: aggregate(items) for key, items in sorted(groups.items())},
        'languages': languages, 'repetitions': repeats,
        'bilingual_families': {'passed_all_languages_and_repetitions': both, 'families': len(family_groups), 'rate': ratio(both,len(family_groups))},
        'human_review': {'eligible': len(review_specs), 'fully_reviewed': reviewed, 'pending': len(review_specs)-reviewed,
            'task_success_passed': human_passes, 'task_success_rate': ratio(human_passes,len(review_specs)) if review_specs and reviewed == len(review_specs) else None,
            'cost_per_success_usd': reviewed_cost/human_passes if reviewed == len(review_specs) and human_passes and reviewed_cost is not None else None}}
