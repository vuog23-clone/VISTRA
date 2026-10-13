"""Repair proposed schedules with current venue evidence; never infer route times."""

from datetime import timedelta

from src.planning.validation import minutes, validate_itinerary
from src.schema.planning import PlanChoice
from src.schema.weather import iso_date


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
    """Try one finite assignment/schedule repair, then apply all normal checks."""
    issues = validate_itinerary(plan, request, notebook, previous=previous, revision_day=revision_day)[0]
    errors = [item for item in issues if item.severity == 'error']
    repairable = {'schedule', 'time_overlap', 'opening_hours', 'diversity', 'complete_information'}
    if not errors or any(item.constraint not in repairable for item in errors):
        return None
    repaired, records = plan.model_copy(deep=True), notebook.records()
    used = {item.entity_id for day in previous.days if day.day != revision_day
            for kind, item in day.choices() if kind in ('restaurant', 'attraction')} if previous and revision_day else set()
    for day in repaired.days:
        if previous and revision_day and day.day != revision_day:
            continue
        activities = []
        for slot, target, earliest, latest, duration in (('breakfast',480,360,660,30),
            ('lunch',720,660,900,60), ('dinner',1080,1020,1380,60)):
            choice = getattr(day, slot)
            if choice is None:
                choice = PlanChoice(evidence_id=next((record.evidence_id for record in records.values()
                    if record.kind == 'restaurant' and record.city == day.city), 'E99R1'))
                setattr(day, slot, choice)
            proposed = minutes(choice.start_time)
            target = min(max(proposed if proposed is not None else target, earliest), latest-duration)
            length = (minutes(choice.end_time) or 0)-(minutes(choice.start_time) or 0)
            activities.append((target, earliest, latest, length if 0 < length <= 120 else duration, 'restaurant', choice))
        for index, choice in enumerate(day.attractions):
            target = minutes(choice.start_time)
            target = target if target is not None else 570 if index == 0 else 840+(index-1)*90
            length = (minutes(choice.end_time) or 0)-(minutes(choice.start_time) or 0)
            activities.append((target,420,1380,length if 0 < length <= 240 else 60,'attraction',choice))
        for choice in day.transportation:
            record = records.get(choice.evidence_id)
            if record is None:
                return None
            target = minutes(choice.start_time)
            length = (minutes(choice.end_time) or 0)-(target or 0)
            duration = max(length if length > 0 else 60, record.duration_minutes or 0)
            activities.append((target if target is not None else 360,0,1439,duration,'transport',choice))
        finish = 0
        for target, earliest, latest, duration, kind, choice in sorted(activities, key=lambda item:item[0]):
            original = records.get(choice.evidence_id)
            if original is None:
                return None
            candidates = [original] if kind == 'transport' or preserve_venues else [original, *[record for record in records.values()
                if record.kind == kind and record.city == day.city and record.entity_id != original.entity_id]]
            for record in candidates:
                if kind != 'transport' and record.entity_id in used:
                    continue
                # A short gap is proposed schedule spacing, not a claim about
                # actual travel duration. Validation continues to flag routes.
                slot = proposed_slot(record, day.date, target, max(earliest,finish+15 if finish else 0), latest, duration)
                if slot is None:
                    continue
                start, finish = slot
                choice.evidence_id, choice.entity_id, choice.name = record.evidence_id, record.entity_id, record.name
                choice.start_time = f'{start//60:02}:{start%60:02}'
                choice.end_time = f'{finish//60:02}:{finish%60:02}'
                if kind != 'transport':
                    used.add(record.entity_id)
                break
            else:
                return None
    final = validate_itinerary(repaired, request, notebook, previous=previous, revision_day=revision_day)[0]
    return repaired if not any(item.severity == 'error' for item in final) else None
