"""Repair proposed schedules with current venue evidence; never infer route times."""

from datetime import timedelta
from math import ceil

from src.planning.validation import choice_cost, dietary_conflict, minutes, validate_itinerary
from src.schema.planning import PlanChoice, revision_scope
from src.schema.weather import iso_date
from src.service.routing_service import route_for_pair


def opening_intervals(record, date):
    if not date or record.opening_hours is None:
        return [(0, 1440)]
    current = iso_date(date)
    hours = record.opening_hours
    windows = hours.get(current.strftime('%A').lower())
    if windows is None:
        return [(0, 1440)]
    intervals = []
    for window in windows:
        start, end = minutes(window.get('open')), minutes(window.get('close'))
        if start is None or end is None:
            return [(0, 1440)]
        intervals.append((start, end if end > start else 1440))
        if start == end:
            intervals[-1] = (0, 1440)
    yesterday = (current-timedelta(days=1)).strftime('%A').lower()
    for window in hours.get(yesterday) or []:
        start, end = minutes(window.get('open')), minutes(window.get('close'))
        if start is not None and end is not None and end < start:
            intervals.append((0, end))
    return sorted(intervals)


def proposed_slot(record, date, target, earliest, latest, duration):
    for preferred in (max(target, earliest), earliest):
        for opened, closed in opening_intervals(record, date):
            start = max(preferred, opened)
            if start+duration <= min(closed, latest):
                return start, start+duration
    return None


def repair_schedule(plan, request, notebook, *, previous=None, revision_day=None, preserve_venues=False):
    """Compatibility entry point for bounded repair of an already rejected draft."""
    issues = validate_itinerary(plan, request, notebook, previous=previous, revision_day=revision_day)[0]
    if not any(item.severity == 'error' for item in issues):
        return None
    return build_schedule(plan, request, notebook, previous=previous, revision_day=revision_day,
                          preserve_venues=preserve_venues)


def build_schedule(plan, request, notebook, *, previous=None, revision_day=None,
                   preserve_venues=False, max_nodes=12000, diagnostics=None):
    """Assign sourced venues and proposed times with finite backtracking.

    A result always passes the normal validator. Unknown hours, prices and routes
    stay unknown there; exhaustion is not proof that the trip is impossible.
    Protected edit days are never changed. No provider calls happen here.
    """
    if type(max_nodes) is not int or not 1 <= max_nodes <= 50000:
        raise ValueError('Schedule search budget must be from 1 to 50000 nodes.')
    if diagnostics is not None:
        diagnostics.update(status='rejected_draft', nodes=0, max_nodes=max_nodes)
    errors = [item for item in validate_itinerary(plan, request, notebook,
        previous=previous, revision_day=revision_day)[0] if item.severity == 'error']
    repairable = {'schedule', 'time_overlap', 'opening_hours', 'diversity', 'complete_information',
                  'transport_time', 'accommodation', 'minimum_nights', 'room_type', 'room_rules', 'cuisine', 'budget'}
    if any(item.constraint not in repairable for item in errors):
        return None
    from src.planning.planner import planning_candidates
    repaired, records = plan.model_copy(deep=True), notebook.records()
    scope = revision_scope(revision_day)
    protected = {day.day for day in repaired.days if previous and scope and day.day not in scope}
    used = {records[item.evidence_id].entity_id for day in repaired.days if day.day in protected
            for kind, item in day.choices() if kind in ('restaurant', 'attraction') and item.evidence_id in records}
    # Cache directed estimates during the bounded search; missing cells remain None.
    route_cache = {}
    def route(before, after):
        key = before.evidence_id, after.evidence_id
        if key not in route_cache:
            route_cache[key] = route_for_pair(notebook, before, after, request)
        return route_cache[key]
    pools = {(city, kind): planning_candidates(notebook, request, kind, city=city)
             for city in {day.city for day in repaired.days} for kind in ('restaurant', 'attraction', 'accommodation')}
    def compatible(record, kind):
        if kind == 'restaurant':
            return not dietary_conflict(record, request)
        if kind == 'accommodation':
            if request.room_type and record.room_type and record.room_type.casefold() != request.room_type.casefold():
                return False
            if record.room_rules is not None and any(rule.casefold() not in
                    {value.casefold() for value in record.room_rules} for rule in request.room_rules):
                return False
        return True
    slots = []
    for day_index, day in enumerate(repaired.days):
        if day.day in protected:
            continue
        if day.day == request.duration_days:
            day.accommodation = None
        activities = []
        for slot, target, earliest, latest, duration in (('breakfast',480,360,660,30),
            ('lunch',720,660,900,60), ('dinner',1080,1020,1380,60)):
            choice = getattr(day, slot)
            proposed = minutes(choice.start_time) if choice else None
            target = min(max(proposed if proposed is not None else target, earliest), latest-duration)
            length = (minutes(choice.end_time) or 0)-(proposed or 0) if choice else 0
            activities.append((slot, None, target, earliest, latest, length if 0 < length <= 120 else duration, 'restaurant', choice))
        if not day.attractions and pools[(day.city, 'attraction')] and not preserve_venues:
            day.attractions = [PlanChoice(evidence_id=pools[(day.city, 'attraction')][0].evidence_id)]
        for index, choice in enumerate(day.attractions):
            target = minutes(choice.start_time)
            target = target if target is not None else 570 if index == 0 else 840+(index-1)*90
            length = (minutes(choice.end_time) or 0)-(minutes(choice.start_time) or 0)
            duration = length if 0 < length <= 240 else 60
            openings = [max(420,opened) for candidate in pools[(day.city,'attraction')]
                for opened,closed in opening_intervals(candidate,day.date)
                if max(420,opened)+max(duration,candidate.duration_minutes or 0) <= min(closed,1380)]
            # An evening-only visit belongs after meals, not before lunch with
            # every later activity pushed outside its own allowed window.
            target = max(target,min(openings)) if openings else target
            activities.append(('attractions',index,target,420,1380,duration,'attraction',choice))
        for index, choice in enumerate(day.transportation):
            record = records.get(choice.evidence_id)
            if record is None:
                return None
            target = minutes(choice.start_time)
            length = (minutes(choice.end_time) or 0)-(target or 0)
            duration = max(length if length > 0 else 60, record.duration_minutes or 0)
            activities.append(('transportation',index,target if target is not None else 360,0,1439,duration,'transport',choice))
        ordered = sorted(activities,key=lambda item:(item[2],item[6] != 'attraction'))
        next_start = None
        for index in range(len(ordered)-1,-1,-1):
            item = list(ordered[index])
            if next_start is not None:
                item[4] = min(item[4],next_start-15)
            next_start = item[4]-item[5]
            ordered[index] = tuple(item)
        slots.extend((day_index, *item) for item in ordered)
        if day.day < request.duration_days:
            slots.append((day_index,'accommodation',None,0,0,1440,0,'accommodation',day.accommodation))

    costs = {}
    for day in repaired.days:
        if day.day in protected:
            for _, choice in day.choices():
                record = records[choice.evidence_id]
                cost = choice_cost(record, request)
                if cost is not None:
                    costs[record.currency] = costs.get(record.currency, 0)+cost
    limit = request.budget_amount
    if request.budget_basis == 'per_person':
        limit = limit*request.travelers if limit is not None and request.travelers else None
    visited = 0
    solution = None
    # A cheap matching check reserves scarce future venues. Without it, a DFS
    # can spend its whole budget permuting Friday choices needed on Saturday.
    future = {}
    for position, (day_index, attribute, index, target, earliest, latest, duration, kind, original_choice) in enumerate(slots):
        if kind not in ('restaurant','attraction'):
            continue
        day = repaired.days[day_index]
        city_days = sum(item.city == day.city for item in repaired.days)
        pool = pools[(day.city,kind)]
        if len({record.entity_id for record in pool}) < (3 if kind == 'restaurant' else 1)*city_days:
            continue  # Missing inventory is reported as partial by validation.
        if preserve_venues:
            pool = [records[original_choice.evidence_id]] if original_choice else []
        future[position] = {record.entity_id for record in pool if compatible(record, kind)
            and proposed_slot(record, day.date, earliest, earliest, min(latest,1439),
                              max(duration,record.duration_minutes or 0)) is not None}

    def can_finish(position):
        domains = [values-used for slot,values in future.items() if slot >= position]
        if any(not values for values in domains):
            return False
        matched = {}
        def augment(index, seen):
            for entity in sorted(domains[index]):
                if entity in seen:
                    continue
                seen.add(entity)
                if entity not in matched or augment(matched[entity], seen):
                    matched[entity] = index
                    return True
            return False
        domains.sort(key=len)
        return all(augment(index,set()) for index in range(len(domains)))
    def assign(day, attribute, index, value):
        if index is None:
            setattr(day, attribute, value)
        else:
            getattr(day, attribute)[index] = value

    def search(position, last_day=None, finish=0, before=None, before_kind=None):
        nonlocal visited, solution
        if visited >= max_nodes:
            return False
        if position == len(slots):
            candidate = repaired.model_copy(deep=True)
            for day in candidate.days:
                day.attractions = [choice for choice in day.attractions if choice is not None]
            final = validate_itinerary(candidate, request, notebook, previous=previous, revision_day=revision_day)[0]
            if not any(item.severity == 'error' for item in final):
                solution = candidate
                return True
            return False
        if not can_finish(position):
            return False
        day_index, attribute, index, target, earliest, latest, duration, kind, original_choice = slots[position]
        day = repaired.days[day_index]
        if last_day != day_index:
            finish, before, before_kind = 0, None, None
        original = records.get(original_choice.evidence_id) if original_choice else None
        pool = [original] if kind == 'transport' or preserve_venues else pools[(day.city, kind)]
        def stay_fits(record):
            if kind != 'accommodation' or not record.minimum_nights:
                return True
            a = b = day_index
            while a > 0 and repaired.days[a-1].city == day.city:
                a -= 1
            while b+1 < len(repaired.days)-1 and repaired.days[b+1].city == day.city:
                b += 1
            return record.minimum_nights <= b-a+1
        candidates = [record for record in pool if record is not None and compatible(record, kind) and stay_fits(record)]
        from src.retrieval.category_filters import metadata_photo_category
        city_days = sum(item.city == day.city for item in repaired.days)
        food_count = sum(metadata_photo_category({'category':record.category}) != 'cafe'
                         for record in pools[(day.city,'restaurant')])
        def ranking(record):
            cost = choice_cost(record, request)
            matching = {value.casefold() for value in request.cuisines}.intersection(
                value.casefold() for value in record.cuisines or [])
            cafe = metadata_photo_category({'category':record.category}) == 'cafe'
            meal_penalty = (not cafe if attribute == 'breakfast' and food_count <= 2*city_days else cafe) if kind == 'restaurant' else False
            return (meal_penalty,
                    record.evidence_id != (original.evidence_id if original else None),
                    -len(matching), not route(record, record) if kind in ('restaurant','attraction') else False,
                    cost is None, cost or 0, record.evidence_id)
        candidates.sort(key=ranking)
        seen = set()
        for record in candidates:
            visited += 1
            if visited > max_nodes:
                return False
            if record.entity_id in seen or kind in ('restaurant','attraction') and record.entity_id in used:
                continue
            seen.add(record.entity_id)
            if original and route(original, original) and kind in ('restaurant','attraction') and not route(record, record):
                continue  # Do not erase known route failures using uncovered alternatives.
            gap = 15  # Suggested spacing only; the validator still flags unknown routes.
            if before and before_kind != 'transport' and kind not in ('transport','accommodation'):
                estimate = route(before, record)
                if estimate:
                    gap = max(gap, ceil(estimate[2]/60))
                elif original and route(before, original):
                    continue
            cost = choice_cost(record, request)
            if cost is not None and record.currency == request.currency and limit is not None:
                if costs.get(record.currency, 0)+cost > limit:
                    continue
            alternatives = [(None, None)] if kind == 'accommodation' else []
            if kind != 'accommodation':
                length = max(duration, record.duration_minutes or 0)
                for preferred in (target, earliest):
                    slot = proposed_slot(record, day.date, preferred,
                        max(earliest, finish+gap if finish else 0), min(latest,1439), length)
                    if slot is not None and slot not in alternatives:
                        alternatives.append(slot)
            for start, end in alternatives:
                choice = PlanChoice(evidence_id=record.evidence_id, entity_id=record.entity_id, name=record.name,
                    start_time=f'{start//60:02}:{start%60:02}' if start is not None else None,
                    end_time=f'{end//60:02}:{end%60:02}' if end is not None else None)
                assign(day, attribute, index, choice)
                if kind in ('restaurant','attraction'):
                    used.add(record.entity_id)
                if cost is not None:
                    costs[record.currency] = costs.get(record.currency,0)+cost
                if search(position+1, day_index, finish if end is None else end,
                          before if kind == 'accommodation' else record,
                          before_kind if kind == 'accommodation' else kind):
                    return True
                if kind in ('restaurant','attraction'):
                    used.remove(record.entity_id)
                if cost is not None:
                    costs[record.currency] -= cost
        assign(day, attribute, index, original_choice)
        city_days = sum(item.city == day.city for item in repaired.days)
        # Missing evidence may produce a partial plan, never duplicated venues.
        shortage = kind in ('restaurant','attraction') and len({r.entity_id for r in pools[(day.city,kind)]}) < (3 if kind == 'restaurant' else 1)*city_days
        if (index is None or kind == 'attraction') and (not candidates or shortage) and not preserve_venues and kind != 'transport':
            assign(day, attribute, index, None)
            if search(position+1, day_index, finish, before, before_kind):
                return True
        assign(day, attribute, index, original_choice)
        return False

    found = search(0)
    if diagnostics is not None:
        diagnostics.update(status='found' if found else 'budget_exhausted' if visited >= max_nodes else 'no_assignment',
                           nodes=min(visited,max_nodes))
    return solution
