"""Brief acknowledgments of validated updates, rendered without an answer model."""

import re
import unicodedata
from decimal import Decimal

from src.generation.response_contract import resolve_response_contract


PREFERENCES = {
    'diet': ('food preference', 'sở thích ăn uống'),
    'pace': ('pace preference', 'nhịp độ du lịch'),
    'interests': ('interests', 'sở thích'),
    'lodging': ('lodging preference', 'sở thích lưu trú'),
    'transport': ('transport preference', 'sở thích đi lại'),
    'language': ('language preference', 'ngôn ngữ trả lời'),
    'home_location': ('home location', 'nơi sinh sống'),
    'accessibility': ('accessibility needs', 'nhu cầu hỗ trợ tiếp cận'),
    'travel_style': ('travel style', 'phong cách du lịch'),
}
TRIP_FIELDS = {
    'destination': (('destination', 'destinations'), 'destination', 'điểm đến'),
    'dates': (('dates', 'start_date', 'end_date'), 'dates', 'ngày đi'),
    'duration': (('duration_days',), 'duration', 'thời lượng'),
    'origin': (('origin',), 'starting point', 'điểm xuất phát'),
    'budget': (('budget', 'budget_amount', 'currency', 'budget_basis'), 'budget', 'ngân sách'),
    'travelers': (('travelers',), 'group size', 'số người'),
    'food': (('cuisines',), 'food requirements', 'yêu cầu ăn uống'),
    'lodging': (('room_type', 'room_rules'), 'lodging requirements', 'yêu cầu lưu trú'),
    'transport': (('transport_constraints', 'transport_mode'), 'transport requirements', 'yêu cầu đi lại'),
    'requirements': (('requirements',), 'requirements', 'yêu cầu'),
    'interests': (('goal',), 'interests', 'sở thích'),
    'weather': (('include_weather',), 'weather preference', 'tùy chọn thời tiết'),
}


def _normalized(text):
    return ''.join(char for char in unicodedata.normalize('NFD', text.casefold())
                   if not unicodedata.combining(char)).replace('đ', 'd')


def _update_only(query):
    # Quoted names/values do not introduce another task. Keep contractions intact.
    text = re.sub(r'"[^"\n]*"|“[^”\n]*”|`[^`\n]*`|(?<!\w)\'[^\'\n]+\'(?!\w)', ' ', query)
    text = _normalized(re.sub(r'\bviết\b', 'write', text, flags=re.I)).strip()
    polite = re.match(r'^(?:please\s+)?(?:can|could|would) you\s+(?:please\s+)?'
                      r'(?:remember|save|forget|update|change|set)\b', text)
    if polite:
        text = text[polite.end():].rstrip().removesuffix('?')
    if re.search(r'(?:^|[.!;\n])\s*(?:is|are|do|does|can|could|would|will|should|have|has)\b', text):
        return False
    if '?' in text or re.search(
            r'\b(?:what|why|where|when|which|how|recommend|suggest|find|search|compare|explain|'
            r'translate|calculate|verify|check|show|tell|book|reserve|write|draft|summarize|rewrite|compose|'
            r'recommendations|suggestions|'
            r'advice|tips|ideas|goi y|de xuat|tim|so sanh|giai thich|cho biet|dich|tinh|'
            r'kiem tra|tom tat|xem|khi nao|o dau|tai sao|bao nhieu|co gi)\b', text):
        return False
    if re.search(r'\b(?:give|make|create|build|design|generate|prepare|plan|schedule)\s+'
                 r'(?:me|us|a|an|the|my|our|day|\d)\b|\b(?:lap|len)\s+(?:lich|ke hoach)\b', text):
        return False
    return True


def _join(labels, vi):
    if len(labels) == 1:
        return labels[0]
    return ', '.join(labels[:-1]) + (' và ' if vi else ' and ') + labels[-1]


def preserve_literal_budget(query, understanding, previous):
    """Recover a single explicit money value in a recognized trip-budget update."""
    from src.memory.conversation import _trip_budget_quotes
    if not _update_only(query):
        return
    facts = _trip_budget_quotes(query)
    if not facts or (previous.budget_amount is None and not any(
            re.search(r'\b(?:budget|ngan sach)\b', fact) for fact in facts)):
        return
    text = ' '.join(facts)
    if re.search(r"\b(?:not|never|don't|khong|dung|chua|around|about|approximately|at least|more than|less than|khoang|tam|it nhat)\b",text):
        return
    currencies = r'vnd|usd|eur|gbp|aud|cad|sgd|jpy|krw|thb'
    amounts = [(match[1],match[2]) for fact in facts for match in re.finditer(
        r'(?<![\d.,])(\d[\d.,]*)\s*('+currencies+r')\b',fact)]
    if len(amounts) != 1:
        return  # Multiple values/currencies need interpretation or clarification.
    number, currency = amounts[0]
    if re.fullmatch(r'\d{1,3}(?:,\d{3})+|\d{1,3}(?:\.\d{3})+',number):
        number = number.replace(',','').replace('.','')
    elif not re.fullmatch(r'\d+(?:\.\d{1,2})?',number):
        return
    amount = Decimal(number)
    if not 0 <= amount <= Decimal('1e12'):
        raise ValueError('Trip budget exceeds the supported amount range.')
    per_person = bool(re.search(r'\b(?:per person|each person|moi nguoi)\b',text))
    total = bool(re.search(r'\b(?:total|tong cong)\b',text))
    if per_person and total:
        return
    understanding.trip_state.budget_amount = int(amount) if amount == int(amount) else float(amount)
    understanding.trip_state.currency = currency.upper()
    understanding.trip_state.budget_basis = ('per_person' if per_person else 'total' if total else
        previous.budget_basis if previous.budget_amount is not None else understanding.trip_state.budget_basis)
    if not (previous.pending_planning_action and previous.pending_clarification):
        understanding.action = 'answer'


def preserve_scoped_requirement(query, understanding, previous, current):
    """Retain a literal trip-only restriction when interpretation drops it."""
    if understanding.preference_changes or not _update_only(query) or len(query) > 300:
        return
    text = _normalized(query).strip()
    if not re.match(r'^(?:for this trip(?: only)?|this time|chi chuyen nay|chuyen nay|lan nay)\b', text):
        return
    if not re.search(r'\b(?:only|no|without|prefer|need|want|must|avoid|chi|khong|uu tien|can|muon)\b', text):
        return
    old, new = previous.model_dump(), current.model_dump()
    if any(old[field] != new[field] for fields, _, _ in TRIP_FIELDS.values() for field in fields):
        return
    if query not in current.requirements and len(current.requirements) < 20:
        current.requirements = [*current.requirements, query]


def _repeated_budget(query, current):
    text = _normalized(query)
    if current.budget_amount is None or not re.search(r'\b(?:budget|ngan sach)\b', text):
        return False
    amounts = []
    for value in re.findall(r'(?<!\w)\d[\d,.]*', query):
        try:
            amounts.append(Decimal(value.rstrip('.,').replace(',', '')))
        except ArithmeticError:
            return False
    currencies = re.findall(r'\b[A-Z]{3}\b', query)
    basis_matches = (not re.search(r'\b(?:per person|moi nguoi)\b', text) or current.budget_basis == 'per_person') and (
        not re.search(r'\b(?:total|tong cong)\b', text) or current.budget_basis == 'total')
    return (len(amounts) == 1 and amounts[0] == Decimal(str(current.budget_amount))
            and all(currency == current.currency for currency in currencies) and basis_matches)


def memory_acknowledgement(query, understanding, previous, current, memory, *, constrained=False):
    """Return a result only for a clear update, never for a question or pending plan.

    Selection is conservative; ambiguous/mixed requests keep the normal pipeline.
    Changes still use ChatService's existing atomic commit before any final return.
    """
    if (constrained or understanding.action in ('create_itinerary', 'revise_itinerary')
            or previous.pending_planning_action and previous.pending_clarification or not _update_only(query)):
        return None
    text = _normalized(query)
    operation = re.search(r'\b(?:remember|save|forget|update|change|set|correct|switch|remove|delete|'
                          r'increase|decrease|raise|lower|'
                          r'ghi nho|luu|quen|xoa|cap nhat|doi|sua|tang|giam)\b', text)
    statement = re.search(r'\b(?:my|our)\s+(?:\w+\s+){0,2}(?:trip|budget|travelers|dates)\b|'
                          r'\b(?:i am|i\x27m|we are) traveling\b|'
                          r'\b(?:this trip|chuyen di|chuyen nay|lan nay|ngan sach|ngay di|so nguoi)\b', text)
    changes = understanding.preference_changes
    if (understanding.action == 'search' and not statement
            and not (changes and all(change.key == 'language' for change in changes))):
        return None
    if not (changes or operation or statement):
        return None
    old, new = previous.model_dump(), current.model_dump()
    groups = [key for key, (fields, _, _) in TRIP_FIELDS.items()
              if any(old[field] != new[field] for field in fields)]
    # An explicitly repeated budget/dates/destination update is an idempotent
    # acknowledgment, not an invitation to generate unsolicited advice.
    repeated = not groups and bool(statement or operation) and _repeated_budget(query, current)
    if not changes and not groups and not repeated:
        return None
    contract = resolve_response_contract(query, memory.model_dump())
    if contract.language is None:
        return None  # Other/bilingual languages remain the normal model's task.
    vi = contract.language == 'vi'
    sentences = []
    preference_parts = []
    for operation_name in ('set', 'delete'):
        labels = [labels[int(vi)] for key, labels in PREFERENCES.items()
                  if any(change.key == key and change.operation == operation_name for change in changes)]
        if labels:
            if len(labels) > 3:
                labels = ['các tùy chọn cá nhân' if vi else 'preferences']
            if vi:
                prefix = 'Đã lưu ' if operation_name == 'set' else 'Đã xóa '
            else:
                prefix = 'Saved your ' if operation_name == 'set' else 'Cleared your saved '
            preference_parts.append(prefix + _join(labels, vi))
    if preference_parts:
        sentences.append('; '.join([preference_parts[0],
            *(part[0].lower()+part[1:] for part in preference_parts[1:])]) + '.')
    if groups:
        labels = ('thông tin' if vi else 'details') if len(groups) > 3 else _join(
            [TRIP_FIELDS[key][2 if vi else 1] for key in groups], vi)
        # Only typed numeric/date values are echoed; arbitrary quotes/URLs never
        # become assistant prose or source links in the acknowledgment.
        detail = ''
        if groups == ['budget'] and current.budget_amount is not None:
            amount = format(Decimal(str(current.budget_amount)), ',f')
            if '.' in amount:
                amount = amount.rstrip('0').rstrip('.')
            currency = f' {current.currency}' if current.currency else ''
            basis = ('mỗi người' if vi else 'per person') if current.budget_basis == 'per_person' else ('tổng cộng' if vi else 'total')
            detail = f': {amount}{currency} ({basis})'
        elif groups == ['dates'] and current.start_date and current.end_date:
            detail = f': {current.start_date} → {current.end_date}'
        sentences.append((f'Đã cập nhật {labels} cho chuyến này' if vi else f'Updated this trip’s {labels}') + detail + '.')
    elif repeated and not changes:
        sentences.append('Thông tin chuyến này không thay đổi.' if vi else 'Your trip details are unchanged.')
    answer = ' '.join(sentences)
    if contract.layout == 'table':
        answer = ('| Cập nhật |' if vi else '| Update |') + '\n| --- |\n| ' + answer + ' |'
    elif contract.layout == 'bullets':
        answer = '- ' + answer
    elif contract.layout == 'numbered_list':
        answer = '1. ' + answer
    return {
        'user_query': query, 'answer': answer, 'answer_format': 'markdown', 'response_mode': 'llm_only',
        'response_routing': {'mode': 'llm_only', 'decision_source': 'constraint'},
        'generation_status': 'memory_acknowledgement', 'pipeline_status': 'complete', 'model': None,
        'retrieval': None, 'context': {'sources': []}, 'weather': None,
        'grounding': {'status': 'not_applicable', 'issues': []},
        'response_contract': {'status': 'rendered', 'requirements': contract.payload(), 'issues': []},
        'memory_update': {'trip_fields': groups, 'preference_changes': [
            {'key': change.key, 'operation': change.operation} for change in changes]},
    }
