"""Independent finite-rule oracles. No production validators or model approvals."""

import re
import unicodedata


CHECKS = {
    'route': ['planning_action','execution_path'], 'interpreted_route': ['planning_action','execution_path','literal_duration'],
    'schedule': ['delivery_handling', 'required_slots', 'evidence_identity', 'opening_hours', 'route_gaps', 'party_budget', 'no_final_night','rendered_times'],
    'photos': ['photo_coverage', 'photo_ownership', 'category_scope'],
    'contract': ['contract_decision'],
    'answer': ['requested_value_present', 'requested_layout', 'citation_resolution', 'no_obvious_language_drift'],
    'memory': ['saved_state', 'durable_scope', 'pipeline_calls', 'commit_consistency', 'brief_acknowledgment', 'additional_question'],
    'review_failure': ['unchecked_draft_withheld', 'outage_disclosed', 'bounded_calls'],
}


def check_names(case):
    adapter, expected = case['adapter'], case['expected']
    if adapter == 'schedule' and not expected['delivery']:
        return ['no_invalid_plan']
    if adapter == 'memory':
        if expected.get('commit_failed'):
            return ['no_commit_on_failure', 'no_success_acknowledgment']
        return ['saved_state', 'durable_scope', 'pipeline_calls', 'commit_consistency'] + (
            ['brief_acknowledgment'] if expected['brief'] else []) + (['additional_question'] if expected.get('answer_four') else [])
    if adapter == 'answer' and not expected['table']:
        return [name for name in CHECKS[adapter] if name != 'requested_layout']
    return CHECKS[adapter]


def fold(text):
    return ''.join(char for char in unicodedata.normalize('NFD', text.casefold())
                   if not unicodedata.combining(char)).replace('đ', 'd')


def table(text):
    text = re.sub(r'```[\s\S]*?```|~~~[\s\S]*?~~~', '', text)
    lines = text.splitlines()
    for index in range(1, len(lines)-1):
        cells = [re.split(r'(?<!\\)\|', line.strip().strip('|')) for line in lines[index-1:index+2]]
        if '|' in lines[index] and len(cells[0]) >= 2 and len({len(row) for row in cells}) == 1 and all(
                re.fullmatch(r'\s*:?-{3,}:?\s*', cell) for cell in cells[1]):
            return True
    return False


def grade(case, observation, fixtures):
    names = check_names(case)
    if observation.get('outcome') != 'ok':
        return {name: 'fail' for name in names}
    actual, expected, adapter = observation['data'], case['expected'], case['adapter']
    if adapter in {'route', 'interpreted_route'}:
        checks = {'planning_action': actual.get('action') == expected['action']}
        checks['execution_path'] = actual.get('branch') == ('planner' if expected['action']=='create_itinerary' else 'direct') and actual.get('downstream_calls') == 0
        if adapter == 'interpreted_route':
            checks['literal_duration'] = actual.get('duration') == expected['duration']
    elif adapter == 'contract':
        checks = {'contract_decision': bool(actual['issues']) == expected['blocked']}
    elif adapter == 'schedule':
        checks = _schedule(case, actual, fixtures)
    elif adapter == 'photos':
        photos = {raw['id']: raw for raw in fixtures['photos']}
        sources = actual['context']['sources']
        context_ids = {row['entity_id'] for row in sources if row.get('photo')}
        gallery_ids = {'google:'+row['place_id'] for row in actual['cards'] if row.get('photo_url')}
        exposed = context_ids | gallery_ids
        scope_ids = {row['entity_id'] for row in sources} | {'google:'+row['place_id'] for row in actual['cards']}
        checks = {'photo_coverage': context_ids == gallery_ids == set(expected['entities']),
            'photo_ownership': all(photos.get(identity.removeprefix('google:'),{}).get('photo_owner') == identity.removeprefix('google:') for identity in exposed),
            'category_scope': all(photos.get(identity.removeprefix('google:'),{}).get('category') == 'Beach' for identity in scope_ids)}
    elif adapter == 'memory':
        checks = _memory(case, actual)
    elif adapter == 'answer':
        text = actual['answer']
        normalized = fold(text)
        value = expected.get('fact') in text if 'fact' in expected else (
            '120000' in re.sub(r'[, .](?=\d{3}\b)', '', text) and bool(re.search(r'\b0\s*VND\b|\bfree\b|mien phi', normalized, re.I)))
        refs = re.findall(r'\b[SW]\d+\b', text, re.I)
        bracketed = re.findall(r'\[([SW]\d+)\]', text)
        en = len(re.findall(r'\b(?:the|is|are|has|with|from|your|you|this|these|and|for)\b', normalized))
        vi = len(re.findall(r'\b(?:toi|minh|ban|cua|cac|khong|nguon|gia|ve|dong|vao|mien|phi)\b', normalized))
        checks = {'requested_value_present': bool(value),
            'requested_layout': not expected['table'] or table(text),
            'citation_resolution': bool(bracketed) and len(refs) == len(bracketed) and set(bracketed) <= {'S1','S2'} and 'S1' in bracketed,
            'no_obvious_language_drift': not (vi >= 4 and vi > en*2) if case['language'] == 'en' else not (en >= 4 and en > vi*2)}
        if not expected['table']:
            checks.pop('requested_layout')
    else:
        text = actual['answer']
        checks = {'unchecked_draft_withheld': '17:00' not in text and 'UNVERIFIED' not in text and 'CHƯA KIỂM CHỨNG' not in text,
            'outage_disclosed': bool(re.search(r'verif|kiem chung', fold(text))),
            'bounded_calls': actual['draft_calls'] == actual['review_calls'] == 1}
    if set(checks) != set(names) or any(type(value) is not bool for value in checks.values()):
        raise ValueError('Grader omitted a declared check or returned an invalid verdict.')
    return {name: 'pass' if checks[name] else 'fail' for name in names}


def _memory(case, actual):
    expected, state = case['expected'], actual['state']
    if expected.get('commit_failed'):
        return {'no_commit_on_failure': actual.get('commit_failed') is True and state['version'] == 0 and not actual['records'],
                'no_success_acknowledgment': not actual['results']}
    results = actual['results']
    snapshot = state['trip_state']
    saved = True
    if 'budget' in expected:
        saved = snapshot['budget_amount'] == expected['budget'] and snapshot['currency'] == 'VND'
    if expected.get('temporary'):
        restriction = fold(' '.join([*snapshot['requirements'], *snapshot['cuisines']]))
        saved = saved and ('vegetarian' in restriction or 'an chay' in restriction)
    diet = any(record['key'] == 'diet' for record in actual['records'])
    if expected['durable_diet']:
        saved = saved and any(record['key'] == 'diet' and re.search(r'vegetarian|an chay', fold(record['content'])) for record in actual['records'])
    scope = diet == expected['durable_diet'] and all(record['key'] == 'diet' for record in actual['records'])
    brief = all(len(row['answer']) <= 240 and not re.search(
        r'weather|forecast|itinerary|recommend|suggest|https?://|trip_state|du bao|lich trinh|goi y', fold(row['answer'])) for row in results)
    checks = {'saved_state': bool(saved), 'durable_scope': scope, 'pipeline_calls': actual['pipeline_calls'] == expected['pipeline_calls'],
        'commit_consistency': len(results) == len(case['message'].split('\n')) == state['version'] and bool(results) and
            state['recent_messages'][-1]['content'] == results[-1]['answer'],
        'brief_acknowledgment': bool(results) and brief,
        'additional_question': bool(results) and bool(re.search(r'\b(?:4|four|bon)\b', fold(results[-1]['answer'])))}
    return {name: checks[name] for name in check_names(case)}


def _schedule(case, actual, fixtures):
    plan = actual['itinerary']
    if not case['expected']['delivery']:
        # All four records cost 10/person; required 3 meals + 1 attraction = 80.
        if sum(row['cost'] for row in fixtures['schedule']['venues'])*fixtures['schedule']['party_size'] <= case['input']['budget']:
            raise ValueError('The infeasible fixture has no valid budget contradiction.')
        return {'no_invalid_plan': plan is None}
    checks = dict.fromkeys(CHECKS['schedule'], False)
    checks['delivery_handling'] = plan is not None
    if not plan:
        return checks
    data = fixtures['schedule']
    records = {row['entity_id']: row for row in data['venues']}
    days = plan.get('days', [])
    if len(days) != 1:
        return checks
    day = days[0]
    meals = [day.get(key) for key in ('breakfast','lunch','dinner')]
    sights = day.get('attractions', [])
    choices = [choice for choice in [*meals, *sights] if choice]
    checks['required_slots'] = all(meals) and len(sights) >= 1 and day['date'] == data['date'] and day['city'] == data['city']
    ids = [choice['entity_id'] for choice in choices]
    checks['evidence_identity'] = len(ids) == len(set(ids)) and all(identity in records for identity in ids) and all(
        records[choice['entity_id']]['kind'] == 'restaurant' for choice in meals if choice) and all(
        records[choice['entity_id']]['kind'] == 'attraction' for choice in sights)
    checks['no_final_night'] = day.get('accommodation') is None
    checks['rendered_times'] = bool(choices) and all(choice.get('start_time') and choice.get('end_time') and
        choice['start_time'] in actual.get('answer','') and choice['end_time'] in actual.get('answer','') for choice in choices)
    if not checks['evidence_identity']:
        return checks
    def minutes(value):
        hour, minute = map(int, value.split(':'))
        return hour*60+minute
    events = []
    for choice in choices:
        if not all(isinstance(choice.get(key), str) and re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', choice[key])
                   for key in ('start_time','end_time')):
            return checks
        start, end = minutes(choice['start_time']), minutes(choice['end_time'])
        raw = records[choice['entity_id']]
        opening, closing = (19*60, 23*60) if raw['kind'] == 'attraction' and case['input']['night_only'] else (
            minutes(raw['opens']), minutes(raw['closes']))
        events.append((start, end, opening <= start < end <= closing))
    events.sort()
    checks['opening_hours'] = bool(events) and all(row[2] for row in events)
    checks['route_gaps'] = all(right[0]-left[1] >= data['route_minutes'] for left, right in zip(events, events[1:]))
    checks['party_budget'] = sum(records[identity]['cost'] for identity in ids)*data['party_size'] <= case['input']['budget']
    return checks
