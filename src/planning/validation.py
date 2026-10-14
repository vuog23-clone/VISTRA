"""Deterministic itinerary checks; unknown facts cannot become verified constraints."""

from datetime import timedelta
from math import ceil

from src.schema.planning import Itinerary, ValidationIssue, revision_scope
from src.schema.weather import iso_date
from src.resolver.location_resolver import LocationResolver
from src.service.routing_service import route_for_pair


def minutes(value):
    import re
    return int(value[:2])*60 + int(value[3:]) if isinstance(value, str) and re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d|24:00', value) else None


def validate_itinerary(plan, request, notebook, *, previous=None, revision_day=None):
    plan = Itinerary.model_validate(plan)
    records, issues, seen = notebook.records(), [], set()
    costs, unknown_cost, lodgings, known_cuisines = [], False, {}, set()
    def issue(severity, constraint, message, day=None):
        value = ValidationIssue(severity=severity, constraint=constraint, message=message, day=day)
        if value not in issues:
            issues.append(value)
    if [day.day for day in plan.days] != list(range(1, (request.duration_days or 0)+1)):
        issue('error', 'duration', 'The plan must have exactly the requested consecutive days.')
    allowed_cities = {LocationResolver._normalize(city) for city in request.destinations}
    scheduled_cities = {LocationResolver._normalize(day.city) for day in plan.days}
    if allowed_cities - scheduled_cities:
        issue('error', 'city_coverage', 'The itinerary omits a requested destination.')
    cuisines_unknown = False
    for day in plan.days:
        restaurants_available = sum(record.kind == 'restaurant' and record.city == day.city for record in records.values())
        attractions_available = sum(record.kind == 'attraction' and record.city == day.city for record in records.values())
        days_in_city = sum(item.city == day.city for item in plan.days)
        if LocationResolver._normalize(day.city) not in allowed_cities:
            issue('error', 'city', 'The scheduled city is outside the requested destinations.', day.day)
        expected = (iso_date(request.start_date)+timedelta(days=day.day-1)).isoformat() if request.start_date else None
        if day.date != expected:
            issue('error', 'dates', 'Each day must match its requested date, or stay undated when dates are unknown.', day.day)
        for slot in ('breakfast', 'lunch', 'dinner'):
            if getattr(day, slot) is None:
                unknown_cost = True
                cuisines_unknown = True
                issue('error' if restaurants_available >= 3*days_in_city else 'unknown',
                      'complete_information', f'{slot.title()} has no verified restaurant selection.', day.day)
        if not day.attractions:
            issue('error' if attractions_available >= days_in_city else 'unknown',
                  'complete_information', 'No sourced attraction is scheduled.', day.day)
        if day.day < (request.duration_days or 0) and day.accommodation is None:
            unknown_cost = True
            issue('unknown', 'accommodation', 'Overnight accommodation is not supplied.', day.day)
        scheduled = []
        for kind, choice in day.choices():
            record = records.get(choice.evidence_id)
            if record is None:
                issue('error', 'source_identity', f'{choice.evidence_id} is absent from the current notebook.', day.day)
                continue
            if record.kind not in ({'flight', 'transport'} if kind == 'transport' else {kind}):
                issue('error', 'source_kind', f'{choice.evidence_id} is a {record.kind}, but this slot requires {kind}.', day.day)
            if kind != 'transport' and LocationResolver._normalize(record.city) != LocationResolver._normalize(day.city):
                issue('error', 'within_city', 'A venue is outside the scheduled city.', day.day)
            if choice.name not in (None, record.name) or choice.entity_id not in (None, record.entity_id):
                issue('error', 'source_identity', 'A choice changes the identity or name of its source record.', day.day)
            if kind in ('restaurant', 'attraction'):
                if record.entity_id in seen:
                    issue('error', 'diversity', f'{choice.evidence_id} ({record.name}) repeats an earlier restaurant or attraction.', day.day)
                seen.add(record.entity_id)
            start, end = minutes(choice.start_time), minutes(choice.end_time)
            if kind != 'accommodation':
                if start is None or end is None:
                    issue('error' if kind in ('attraction', 'restaurant') else 'unknown',
                          'schedule', 'An activity has no proposed start/end time.', day.day)
                elif end <= start:
                    issue('error', 'schedule', 'Activity end times must follow their start times.', day.day)
                else:
                    scheduled.append((start, end, choice.evidence_id, kind))
            if kind in ('restaurant', 'attraction') and day.date:
                hours = record.opening_hours
                weekday = iso_date(day.date).strftime('%A').lower()
                windows = hours.get(weekday) if hours else None
                yesterday = (iso_date(day.date)-timedelta(days=1)).strftime('%A').lower()
                previous_windows = hours.get(yesterday) if hours else None
                def fits_window(window, previous_day=False):
                    opened, closed = minutes(window.get('open')), minutes(window.get('close'))
                    if opened is None or closed is None or start is None or end is None:
                        return False
                    if previous_day:
                        return closed < opened and end <= closed
                    if opened == closed:
                        return True  # The saved schedule represents a full day.
                    return opened <= start and end <= (closed if closed > opened else 1440)
                fits_previous = any(fits_window(window, True) for window in previous_windows or [])
                if windows is None:
                    if not fits_previous:
                        issue('unknown', 'opening_hours', 'Opening hours for a scheduled venue are unknown.', day.day)
                elif windows == []:
                    if not fits_previous:
                        issue('error', 'opening_hours', 'A venue is closed in its saved schedule on this date.', day.day)
                elif start is not None and end is not None:
                    valid = [window for window in windows if minutes(window.get('open')) is not None and minutes(window.get('close')) is not None]
                    try:
                        fits = fits_previous or any(fits_window(w) for w in valid)
                        if len(valid) != len(windows):
                            issue('unknown', 'opening_hours', 'Some saved opening windows are incomplete.', day.day)
                        elif not fits:
                            issue('error', 'opening_hours', 'A visit falls outside its saved opening hours.', day.day)
                    except (ValueError, TypeError):
                        issue('unknown', 'opening_hours', 'Saved opening hours cannot be interpreted.', day.day)
            if kind == 'restaurant':
                if record.cuisines is None:
                    cuisines_unknown = True
                known_cuisines.update(value.casefold() for value in record.cuisines or [])
            if kind == 'accommodation':
                lodgings.setdefault(record.entity_id, []).append(day.day)
                if request.room_type:
                    if record.room_type is None:
                        issue('unknown', 'room_type', 'The selected accommodation has no room-type evidence.', day.day)
                    elif record.room_type.casefold() != request.room_type.casefold():
                        issue('error', 'room_type', 'The room type conflicts with the request.', day.day)
                for rule in request.room_rules:
                    if record.room_rules is None:
                        issue('unknown', 'room_rules', 'Accommodation rules are not available.', day.day)
                    elif rule.casefold() not in {value.casefold() for value in record.room_rules}:
                        issue('error', 'room_rules', 'The requested room rule is not supported by this accommodation.', day.day)
                if record.minimum_nights is None or record.maximum_occupancy is None:
                    issue('unknown', 'accommodation', 'Minimum stay or occupancy restrictions are unknown.', day.day)
            if kind == 'transport':
                if any(value and LocationResolver._normalize(value) not in
                       allowed_cities | ({LocationResolver._normalize(request.origin)} if request.origin else set())
                       for value in (record.origin, record.destination)):
                    issue('error', 'transport_route', 'A selected route leaves the requested trip cities.', day.day)
                if record.mode == 'flight' and 'no_flight' in request.transport_constraints:
                    issue('error', 'transport', 'The itinerary uses a flight despite the no-flight constraint.', day.day)
                if record.mode in ('driving', 'self-driving') and 'no_driving' in request.transport_constraints:
                    issue('error', 'transport', 'The itinerary uses self-driving despite the no-driving constraint.', day.day)
                if record.departure_date and record.departure_date != day.date:
                    issue('error', 'transport', 'A selected departure occurs on a different date.', day.day)
                if record.duration_minutes is None:
                    issue('unknown', 'transport_time', 'A selected connection has no verified duration.', day.day)
                elif start is not None and end is not None and end-start < record.duration_minutes:
                    issue('error', 'transport_time', 'The allotted travel time is shorter than the route duration.', day.day)
            if record.cost is None or not record.currency or not record.cost_unit:
                unknown_cost = True
            else:
                multiplier = 1
                if record.cost_unit == 'per_person':
                    multiplier = request.travelers
                elif record.cost_unit == 'per_night':
                    multiplier = ceil(request.travelers / record.maximum_occupancy) if request.travelers and record.maximum_occupancy else None
                if multiplier is None:
                    unknown_cost = True
                else:
                    costs.append((record.currency, record.cost*multiplier))
        scheduled.sort()
        for before, after in zip(scheduled, scheduled[1:]):
            if before[1] > after[0]:
                issue('error', 'time_overlap', f'{before[2]} ({before[0]}–{before[1]} minutes) overlaps {after[2]} ({after[0]}–{after[1]} minutes).', day.day)
            elif before[3] != 'transport' and after[3] != 'transport':
                route = route_for_pair(notebook, records[before[2]], records[after[2]], request)
                if route is None:
                    issue('unknown', 'transport_time', 'Travel time between venues has not been verified.', day.day)
                elif after[0] - before[1] < ceil(route[2]/60):
                    issue('error', 'transport_time',
                          f'{before[2]} to {after[2]} needs about {ceil(route[2]/60)} minutes by {route[1].mode}; the proposed gap is too short.', day.day)
        if day.day > 1 and day.city != plan.days[day.day-2].city and not day.transportation:
            issue('unknown', 'city_route', 'An intercity move has no supported transportation.', day.day)
        if day.day == 1 and request.origin and request.origin != day.city and not day.transportation:
            issue('unknown', 'city_route', 'The journey from the departure city is not supplied.', day.day)
        if day.day == len(plan.days) and request.origin and request.origin != day.city:
            if not any(records.get(item.evidence_id) and records[item.evidence_id].destination == request.origin for item in day.transportation):
                issue('unknown', 'city_route', 'The return journey to the departure city is not supplied.', day.day)
    for entity, nights in lodgings.items():
        record = next(record for record in records.values() if record.entity_id == entity)
        if record.minimum_nights:
            runs, current = [], 0
            previous_day = None
            for night in sorted(nights):
                if previous_day is not None and night != previous_day+1:
                    runs.append(current)
                    current = 0
                current += 1
                previous_day = night
            runs.append(current)
            if any(count < record.minimum_nights for count in runs):
                issue('error', 'minimum_nights', 'A lodging stay violates its minimum consecutive nights.')
    for cuisine in request.cuisines:
        if cuisine.casefold() not in known_cuisines:
            issue('unknown' if cuisines_unknown else 'error', 'cuisine', 'The requested cuisine is not confirmed by selected restaurants.')
    currencies = {currency for currency, _ in costs}
    currency = next(iter(currencies)) if len(currencies) == 1 else request.currency
    total = sum(cost for unit, cost in costs if unit == currency) if currency and costs else None
    if unknown_cost or len(currencies) > 1:
        issue('unknown', 'budget', 'Some prices, cost units, traveler counts or currency conversions are missing; total cost cannot be verified.')
    if request.budget_amount is not None:
        limit = request.budget_amount * (request.travelers or 0) if request.budget_basis == 'per_person' else request.budget_amount
        if not request.currency or (request.budget_basis == 'per_person' and not request.travelers):
            issue('unknown', 'budget', 'The budget currency or traveler count is unspecified.')
        elif currency == request.currency and total is not None and total > limit:
            issue('error', 'budget', 'Even the known costs exceed the requested budget.')
        elif currency != request.currency:
            issue('unknown', 'budget', 'The evidence currency does not match the budget currency.')
    if request.requirements:
        issue('unknown', 'additional_requirements', 'Free-text requirements without structured evidence still need confirmation.')
    scope = revision_scope(revision_day)
    if previous and scope:
        if len(previous.days) != len(plan.days):
            issue('error', 'revision_scope', 'A day-specific edit cannot add or remove other days.')
        for old, new in zip(previous.days, plan.days):
            if old.day not in scope:
                def signature(day):
                    return day.city, day.date, [(kind, item.entity_id, item.start_time, item.end_time) for kind, item in day.choices()]
                # Bind current IDs so new notebook entry numbers don't count as edits.
                for _, item in new.choices():
                    record = records.get(item.evidence_id)
                    if record:
                        item.entity_id = record.entity_id
                if signature(old) != signature(new):
                    issue('error', 'revision_scope', 'An unrequested day was changed.', old.day)
    # Long drafts can have many unknown facts. Never truncate a later error
    # behind those warnings and accidentally deliver a rejected itinerary.
    issues.sort(key=lambda item: item.severity != 'error')
    return issues[:80], float(total) if total is not None else None, currency


def bind_sources(plan, notebook):
    records = notebook.records()
    for day in plan.days:
        for _, choice in day.choices():
            record = records[choice.evidence_id]
            choice.entity_id, choice.name = record.entity_id, record.name
    return plan
