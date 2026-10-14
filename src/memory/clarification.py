"""Resolve brief stay-length answers without making users repeat their trip."""

from datetime import timedelta
import re

from src.schema.memory import TripDetails
from src.schema.weather import iso_date
from src.security.guardrails import UnsafeInput
from src.service.weather_service import normalized
from src.utils.language import query_language


def resolve_stay_reply(understanding, query, memory):
    previous = memory.trip_state
    action = previous.pending_planning_action
    if not previous.pending_clarification or action not in ('create_itinerary', 'revise_itinerary'):
        return understanding, None
    text = normalized(query).strip().rstrip('.!').strip()
    if '?' in text or re.search(r'\b(?:price|cost|weather|bao nhieu|gia)\b', text):
        return understanding, None
    words = {'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7,
             'eight': 8, 'nine': 9, 'ten': 10, 'eleven': 11, 'twelve': 12, 'thirteen': 13,
             'fourteen': 14, 'mot': 1, 'hai': 2, 'ba': 3, 'bon': 4, 'nam': 5,
             'sau': 6, 'bay': 7, 'tam': 8, 'chin': 9, 'muoi': 10}
    count = r'(\d{1,3}|'+'|'.join(words)+r')'
    match = re.fullmatch(r'(?:(?:actually|yes|ok|okay|use|make it|stay|dung|vang)\s*[,:-]?\s*)?'
        +count+r'\s*[ -]?(days?|nights?|ngay|dem)(?:\s+(?:at|in|near|around|o|tai)\s+[^?]+)?', text)
    if match and not re.search(r'\b(?:but|not|khong|nhung|and|va)\b', text):
        amount = int(match[1]) if match[1].isdigit() else words[match[1]]
        days = amount + 1 if match[2] in ('night', 'nights', 'dem') else amount
    elif text in ('keep the dates', 'use the dates', 'keep my dates', 'giu ngay', 'giu ngay da chon'):
        if not previous.start_date or not previous.end_date:
            return understanding, None
        days = (iso_date(previous.end_date)-iso_date(previous.start_date)).days+1
    else:
        return understanding, None
    if not 1 <= days <= 14:
        raise UnsafeInput('planning_duration', 'I can plan up to 14 days at a time. Please choose a shorter stay or split the trip.')
    # Retain known details if the interpretation dropped fields on a short reply.
    data = {key: value for key, value in previous.model_dump().items() if key in TripDetails.model_fields}
    data.update({key: value for key, value in understanding.trip_state.model_dump().items()
                 if value is not None and value != [] and value != ''})
    data['duration_days'] = days
    if previous.start_date:
        data['start_date'] = previous.start_date
        data['end_date'] = (iso_date(previous.start_date)+timedelta(days=days-1)).isoformat()
        data['dates'] = f"{data['start_date']} to {data['end_date']}"
    else:
        data['start_date'] = data['end_date'] = None
    resolved = understanding.model_copy(update={'intent': 'travel', 'action': action,
        'trip_state': TripDetails.model_validate(data)})
    vi = query_language(query) == 'vi'
    stay = f'{days} ngày, {days-1} đêm' if vi else f'{days} days and {days-1} nights'
    dates = f" ({data['start_date']} → {data['end_date']})" if data['start_date'] else ''
    acknowledgement = (f'Đã rõ — mình sẽ dùng {stay}{dates} và giữ các thông tin khác của chuyến đi.' if vi else
                       f'Got it — {stay}{dates}. I’ll keep your other trip details.')
    return resolved, acknowledgement
