"""Cost reported usage at verified rates; missing usage is never zero spend."""

from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path


PRICING_PATH=Path(__file__).parent/'model_comparison/pricing.json'


def pricing():
    return json.loads(PRICING_PATH.read_text(encoding='utf-8'))


def estimate(model,usage,*,service_tier=None):
    rate=pricing()['models'][model]
    if usage is None:
        return {'estimated_usd':None,'reason':'Usage is unavailable.'}
    if service_tier not in (None,'default','auto'):
        return {'estimated_usd':None,'reason':'No verified rate for returned service tier '+str(service_tier)}
    details=usage.get('input_tokens_details') or {}
    counts={'input':usage.get('input_tokens'),'output':usage.get('output_tokens'),
        'cached':details.get('cached_tokens',usage.get('cached_input_tokens')),
        'written':details.get('cache_write_tokens',None if 'cache_write' in rate else 0)}
    if any(value is None for value in counts.values()):
        return {'estimated_usd':None,'reason':'Required cache/usage breakdown is unavailable.','tokens':counts}
    if any(type(value) is not int or value<0 for value in counts.values()) or counts['input']<counts['cached']+counts['written']:
        raise ValueError('Invalid provider token accounting.')
    if counts['written'] and 'cache_write' not in rate:
        return {'estimated_usd':None,'reason':'Cache writes have no verified price for this model.'}
    ordinary=counts['input']-counts['cached']-counts['written']
    long=counts['input']>rate.get('long_context_threshold',10**12)
    input_multiplier=Decimal(str(rate.get('long_input_multiplier',1) if long else 1))
    output_multiplier=Decimal(str(rate.get('long_output_multiplier',1) if long else 1))
    input_cost=(Decimal(ordinary)*Decimal(str(rate['input']))+
        Decimal(counts['cached'])*Decimal(str(rate['cached_input']))+
        Decimal(counts['written'])*Decimal(str(rate.get('cache_write',0))))*input_multiplier/Decimal(1000000)
    output_cost=Decimal(counts['output'])*Decimal(str(rate['output']))*output_multiplier/Decimal(1000000)
    return {'estimated_usd':float(input_cost+output_cost),'input_usd':float(input_cost),
        'output_usd':float(output_cost),'tokens':counts,'long_context_rate':long,
        'standard_tier_assumed':service_tier in (None,'auto')}


def catalog_cost(path):
    records=[json.loads(line) for line in (path/'results.jsonl').read_text(encoding='utf-8').splitlines()]
    model=json.loads((path/'run.json').read_text(encoding='utf-8'))['model']
    sdk=[event for record in records for event in record.get('provider_calls',[])]
    full=[json.loads(line) for file in (path/'transport-usage').glob('*.jsonl')
        for line in file.read_text(encoding='utf-8').splitlines()]
    if full:
        unique={event['response_id']:event for event in full}
        if None in unique:
            raise ValueError('Full usage audit lacks a response ID for deduplication.')
        events=list(unique.values())
    else:
        events=sdk
    estimates=[estimate(model,event.get('usage'),service_tier=event.get('service_tier')) for event in events]
    unknown=sum(e['estimated_usd'] is None for e in estimates)+max(0,len(sdk)-len(events))
    interrupted=[record['case_id'] for record in records if record['outcome'] in ('interrupted','timeout')]
    subtotal=sum(Decimal(str(e['estimated_usd'])) for e in estimates if e['estimated_usd'] is not None)
    prices=pricing()
    def breakdown(group,key):
        values=[(event['usage'].get(group) or {}).get(key) for event in events] if full else []
        return sum(values) if values and all(type(value) is int and value>=0 for value in values) else None
    return {'model':model,'currency':'USD','as_of':prices['as_of'],
        'price_source':prices['sources'][model],'cache_accounting_source':prices['sources']['cache_accounting'],
        'pricing_sha256':sha256(PRICING_PATH.read_bytes()).hexdigest(),
        'estimated_known_usage_subtotal_usd':float(subtotal),
        'estimated_total_usd':None if unknown or interrupted else float(subtotal),
        'sdk_calls_observed':len(sdk),'priceable_returned_usage_events':sum(e['estimated_usd'] is not None for e in estimates),
        'unpriceable_or_missing_usage_events':unknown,'interrupted_cases_with_possible_unreported_calls':interrupted,
        'reported_input_tokens':sum((e.get('usage') or {}).get('input_tokens') or 0 for e in sdk),
        'reported_output_tokens':sum((e.get('usage') or {}).get('output_tokens') or 0 for e in sdk),
        'reported_cached_input_tokens':sum((e.get('usage') or {}).get('cached_input_tokens') or 0 for e in sdk),
        'reported_cache_write_tokens':breakdown('input_tokens_details','cache_write_tokens'),
        'reported_reasoning_tokens':breakdown('output_tokens_details','reasoning_tokens'),
        'standard_tier_assumption_events':sum(bool(e.get('standard_tier_assumed')) for e in estimates),
        'cost_per_independently_reviewed_success_usd':None,
        'preflight_included':False,'invoice_verified':False,
        'limitations':['Standard-rate estimate, not the provider invoice; tier assumptions are recorded.',
            'Jev, local GPU/CPU, SDK-internal retries and unreported calls are not priced.',
            'Cached and cache-write tokens are subsets of input; reasoning tokens are already part of output.']}
