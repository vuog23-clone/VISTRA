"""TravelPlanner tool contracts, evidence notebook, itinerary and validation."""

from datetime import date
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.schema.weather import WeatherEvidence, iso_date
from src.schema.routing import RouteMatrixEvidence, ROUTING_SOURCE_URL
from src.security.guardrails import validate_text


ToolName = Literal['GuideSearch', 'FlightSearch', 'DistanceMatrix', 'RestaurantSearch',
    'AttractionSearch', 'AccommodationSearch', 'CitySearch', 'WeatherSearch', 'Planner']
Kind = Literal['guide', 'flight', 'transport', 'restaurant', 'attraction', 'accommodation', 'city']


class PlanningModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class TripRequest(PlanningModel):
    destinations: list[str] = Field(default_factory=list, max_length=3)
    origin: str | None = Field(default=None, max_length=160)
    duration_days: int | None = Field(default=None, ge=1, le=14)
    start_date: str | None = Field(default=None, max_length=10)
    end_date: str | None = Field(default=None, max_length=10)
    travelers: int | None = Field(default=None, ge=1, le=100)
    budget_amount: float | int | None = Field(default=None, ge=0, le=1e12, allow_inf_nan=False)
    currency: str | None = Field(default=None, pattern=r'^[A-Z]{3}$')
    budget_basis: Literal['total', 'per_person'] = 'total'
    cuisines: list[str] = Field(default_factory=list, max_length=8)
    room_type: str | None = Field(default=None, max_length=80)
    room_rules: list[str] = Field(default_factory=list, max_length=8)
    transport_constraints: list[Literal['no_flight', 'no_driving']] = Field(default_factory=list, max_length=2)
    transport_mode: Literal['driving', 'taxi', 'public_transit', 'walking'] | None = None
    requirements: list[str] = Field(default_factory=list, max_length=20)
    preferences: list[str] = Field(default_factory=list, max_length=12)

    @model_validator(mode='after')
    def checked(self):
        for city in self.destinations:
            validate_text(city, 'trip destination', max_chars=160)
        if self.origin:
            validate_text(self.origin, 'trip origin', max_chars=160)
        for values in (self.cuisines, self.room_rules, self.requirements, self.preferences):
            for value in values:
                validate_text(value, 'trip requirement', max_chars=1000, check_injection=False)
        if self.start_date:
            iso_date(self.start_date)
        if self.end_date:
            iso_date(self.end_date)
        return self


class ToolAction(PlanningModel):
    tool: ToolName
    city: str | None = Field(default=None, max_length=160)
    origin: str | None = Field(default=None, max_length=160)
    destination: str | None = Field(default=None, max_length=160)
    departure_date: str | None = Field(default=None, max_length=10)
    mode: Literal['driving', 'taxi', 'public_transit', 'walking'] | None = None


class EvidenceRecord(PlanningModel):
    evidence_id: str = Field(default='', max_length=20)
    entity_id: str = Field(min_length=1, max_length=180)
    kind: Kind
    name: str = Field(min_length=1, max_length=300)
    category: str | None = Field(default=None, max_length=100)
    city: str = Field(min_length=1, max_length=160)
    provider: Literal['Wikivoyage', 'Google metadata', 'TravelPlanner benchmark', 'OpenRouteService']
    url: str | None = Field(default=None, max_length=2048)
    excerpt: str | None = Field(default=None, max_length=1800)
    latitude: float | int | None = Field(default=None, ge=-90, le=90, allow_inf_nan=False)
    longitude: float | int | None = Field(default=None, ge=-180, le=180, allow_inf_nan=False)
    # Exact costs only when a tool provides an explicit unit and currency.
    cost: float | int | None = Field(default=None, ge=0, le=1e12, allow_inf_nan=False)
    currency: str | None = Field(default=None, pattern=r'^[A-Z]{3}$')
    cost_unit: Literal['per_person', 'per_night', 'per_vehicle', 'total'] | None = None
    duration_minutes: int | None = Field(default=None, ge=0, le=10000)
    origin: str | None = Field(default=None, max_length=160)
    destination: str | None = Field(default=None, max_length=160)
    departure_date: str | None = Field(default=None, max_length=10)
    mode: str | None = Field(default=None, max_length=50)
    cuisines: list[str] | None = Field(default=None, max_length=20)
    room_type: str | None = Field(default=None, max_length=80)
    room_rules: list[str] | None = Field(default=None, max_length=20)
    minimum_nights: int | None = Field(default=None, ge=1, le=365)
    maximum_occupancy: int | None = Field(default=None, ge=1, le=1000)
    # Preserve source hours; validated separately to avoid a security-module cycle.
    opening_hours: dict | None = None

    @model_validator(mode='after')
    def checked(self):
        prefix = {'Wikivoyage':'wikivoyage:', 'Google metadata':'google:', 'TravelPlanner benchmark':'benchmark:', 'OpenRouteService':'ors:'}[self.provider]
        if not self.entity_id.startswith(prefix):
            raise ValueError('Evidence identity does not match its provider.')
        for value in (self.name, self.city, self.excerpt, self.origin, self.destination, self.mode, self.room_type):
            if value:
                validate_text(value, 'planning evidence', max_chars=1800, reject_secrets=False)
        if self.departure_date:
            iso_date(self.departure_date)
        if self.provider == 'OpenRouteService':
            if self.kind != 'transport' or self.url != ROUTING_SOURCE_URL or self.mode not in ('walking', 'driving', 'taxi'):
                raise ValueError('Route evidence must use the routing provider and a supported mode.')
        elif self.url:
            from src.security.context import checked_source_url
            self.url = checked_source_url(self.url, 'text' if self.provider == 'Wikivoyage' else 'image')
        if self.opening_hours is not None:
            from src.security.context import Hours
            self.opening_hours = Hours.model_validate(self.opening_hours).model_dump()
        return self


class ToolResult(PlanningModel):
    tool: ToolName
    city: str = Field(max_length=160)
    status: Literal['available', 'partial', 'unavailable', 'no_results', 'not_applicable']
    records: list[EvidenceRecord] = Field(default_factory=list, max_length=20)
    weather: WeatherEvidence | None = None
    route_matrix: RouteMatrixEvidence | None = None
    note: str = Field(max_length=600)

    @model_validator(mode='after')
    def checked(self):
        if self.route_matrix is not None and self.tool != 'DistanceMatrix':
            raise ValueError('Only DistanceMatrix can return routing evidence.')
        expected = {'GuideSearch':'guide','FlightSearch':'flight','DistanceMatrix':'transport',
                    'RestaurantSearch':'restaurant','AttractionSearch':'attraction',
                    'AccommodationSearch':'accommodation','CitySearch':'city'}
        if self.tool in expected and any(record.kind != expected[self.tool] for record in self.records):
            raise ValueError('Tool records have the wrong category.')
        if self.tool == 'WeatherSearch' and self.records:
            raise ValueError('WeatherSearch returns forecast evidence, not venue records.')
        return self


class NotebookEntry(ToolResult):
    entry_id: str = Field(pattern=r'^E[1-9][0-9]?$')


class EvidenceNotebook(PlanningModel):
    entries: list[NotebookEntry] = Field(default_factory=list, max_length=24)

    def records(self):
        return {record.evidence_id: record for entry in self.entries for record in entry.records}


class DraftChoice(PlanningModel):
    evidence_id: str = Field(pattern=r'^E[1-9][0-9]?R[1-9][0-9]?$')
    start_time: str | None = Field(default=None, pattern=r'^([01][0-9]|2[0-3]):[0-5][0-9]$')
    end_time: str | None = Field(default=None, pattern=r'^([01][0-9]|2[0-3]):[0-5][0-9]$')


class PlanChoice(DraftChoice):
    # The application binds these fields from validated current evidence.
    entity_id: str | None = Field(default=None, max_length=180)
    name: str | None = Field(default=None, max_length=300)


class DraftDay(PlanningModel):
    day: int = Field(ge=1, le=14)
    date: str | None = Field(default=None, max_length=10)
    city: str = Field(min_length=1, max_length=160)
    transportation: list[DraftChoice] = Field(default_factory=list, max_length=3)
    breakfast: DraftChoice | None = None
    attractions: list[DraftChoice] = Field(default_factory=list, max_length=5)
    lunch: DraftChoice | None = None
    dinner: DraftChoice | None = None
    accommodation: DraftChoice | None = None


class ItineraryDay(DraftDay):
    transportation: list[PlanChoice] = Field(default_factory=list, max_length=3)
    breakfast: PlanChoice | None = None
    attractions: list[PlanChoice] = Field(default_factory=list, max_length=5)
    lunch: PlanChoice | None = None
    dinner: PlanChoice | None = None
    accommodation: PlanChoice | None = None

    def choices(self):
        return [(kind, item) for kind, items in (
            ('transport', self.transportation), ('restaurant', [self.breakfast, self.lunch, self.dinner]),
            ('attraction', self.attractions), ('accommodation', [self.accommodation])) for item in items if item]


class Itinerary(PlanningModel):
    days: list[ItineraryDay] = Field(min_length=1, max_length=14)


class ValidationIssue(PlanningModel):
    severity: Literal['error', 'unknown']
    constraint: str = Field(max_length=80)
    message: str = Field(max_length=500)
    day: int | None = Field(default=None, ge=1, le=14)


class ClarificationChoice(PlanningModel):
    label: str = Field(min_length=1, max_length=100)
    reply: str = Field(min_length=1, max_length=500)


class PlanningReport(PlanningModel):
    status: Literal['verified', 'partial', 'failed', 'needs_clarification']
    action: Literal['create_itinerary', 'revise_itinerary']
    issues: list[ValidationIssue] = Field(default_factory=list, max_length=80)
    tool_trace: list[str] = Field(default_factory=list, max_length=50)
    repair_attempts: int = Field(default=0, ge=0, le=2)
    known_cost: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    currency: str | None = None
    clarification_options: list[ClarificationChoice] = Field(default_factory=list, max_length=4)


def duration_from_text(text):
    from src.service.weather_service import normalized
    folded = normalized(text)
    words = {'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7,
             'eight': 8, 'nine': 9, 'ten': 10, 'eleven': 11, 'twelve': 12,
             'thirteen': 13, 'fourteen': 14, 'fifteen': 15, 'sixteen': 16,
             'seventeen': 17, 'eighteen': 18, 'nineteen': 19, 'twenty': 20,
             'mot': 1, 'hai': 2, 'ba': 3, 'bon': 4, 'nam': 5,
             'sau': 6, 'bay': 7, 'tam': 8, 'chin': 9, 'muoi': 10}
    for unit, count in [('mot', 1), ('hai', 2), ('ba', 3), ('bon', 4), ('tu', 4),
                        ('lam', 5), ('sau', 6), ('bay', 7), ('tam', 8), ('chin', 9)]:
        words['muoi '+unit] = 10+count
    # Match multiword numbers first, including unsupported lengths so the
    # existing planner limit can reject them rather than reading only a suffix.
    for tens, count in [('hai muoi', 20), ('ba muoi', 30), ('bon muoi', 40),
                        ('nam muoi', 50), ('sau muoi', 60), ('bay muoi', 70),
                        ('tam muoi', 80), ('chin muoi', 90)]:
        words[tens] = count
        for unit, value in [('mot', 1), ('hai', 2), ('ba', 3), ('bon', 4), ('tu', 4),
                            ('lam', 5), ('sau', 6), ('bay', 7), ('tam', 8), ('chin', 9)]:
            words[tens+' '+unit] = count+value
    number = r'\d{1,4}|'+'|'.join(re.escape(word) for word in sorted(words, key=len, reverse=True))
    for match in re.finditer(r'\b('+number+r')[ -]*(?:days?|ngay)\b', folded):
        # "tháng ba ngày 16" is a calendar date, not a three-day stay.
        if re.search(r'\bthang\s*$', folded[:match.start()]):
            continue
        return int(match[1]) if match[1].isdigit() else words[match[1]]
    if re.search(r'\b(?:a|one) week\b|\bmot tuan\b', folded):
        return 7
    # Preserve the existing support for a bare English duration reply.
    if folded in words and folded.isascii() and folded in {'one','two','three','four','five','six','seven','eight','nine','ten'}:
        return words[folded]
    return None


def explicit_trip_dates(query):
    """Recover unambiguous, fully specified calendar ranges, without guessing years."""
    from src.service.weather_service import normalized
    text = normalized(re.sub(r'"[^"\n]*"|“[^”\n]*”|`[^`\n]*`', ' ', query)).replace('’', "'")
    months = {name:index for index,name in enumerate(('january','february','march','april',
        'may','june','july','august','september','october','november','december'),1)}
    months.update({name[:3]:index for name,index in list(months.items())})
    months['sept'] = 9
    month = '(?:'+'|'.join(sorted(months,key=len,reverse=True))+r')\.?'
    found = set()
    def add(match, first, last):
        if not re.search(r"\b(?:not|never|don't|khong|dung)\b[^,;.!?\n]{0,35}$", text[:match.start()]):
            found.add((iso_date(first).isoformat(),iso_date(last).isoformat()))
    for match in re.finditer(r'(?<!\d)(\d{4}-\d{2}-\d{2})\s*(?:to|through|until|den|toi|[-–])\s*(\d{4}-\d{2}-\d{2})(?!\d)',text):
        add(match,match[1],match[2])
    for match in re.finditer(r'\b(?:tu\s+)?(?:ngay\s+)?(\d{1,2})\s*(?:den|toi|[-–])\s*(?:ngay\s+)?(\d{1,2})\s+thang\s+(\d{1,2})\s+(?:nam\s+)?(\d{4})\b',text):
        add(match,f'{int(match[4]):04d}-{int(match[3]):02d}-{int(match[1]):02d}',
                  f'{int(match[4]):04d}-{int(match[3]):02d}-{int(match[2]):02d}')
    for match in re.finditer(r'\b('+month+r')\s+(\d{1,2})(?:st|nd|rd|th)?(?:,\s*(\d{4}))?\s*(?:to|through|until|[-–])\s*(?:('+month+r')\s+)?(\d{1,2})(?:st|nd|rd|th)?(?:,\s*|\s+)(\d{4})\b',text):
        add(match,f'{int(match[3] or match[6]):04d}-{months[match[1].rstrip(".")]:02d}-{int(match[2]):02d}',
                  f'{int(match[6]):04d}-{months[(match[4] or match[1]).rstrip(".")]:02d}-{int(match[5]):02d}')
    return next(iter(found)) if len(found) == 1 else None


def planning_action(query, previous=None):
    action = resolve_planning_action(query, previous=previous)
    return action if action in ('create_itinerary', 'revise_itinerary') else None


def resolve_planning_action(query, *, interpreted=None, previous=None):
    """Reconcile semantic interpretation with explicit, clause-scoped requests.

    Only clear literal requests override the interpreter. Negating a constraint
    ("don't include flights") must not cancel a separate planning request.
    """
    from src.service.weather_service import normalized
    text = normalized(query).replace('\u2019', "'")
    # Inspect individual clauses, not a bag of words spanning the whole message.
    clauses = re.split(r'[,;.!?\n]+|\b(?:but|instead|and|nhung|thay vao do|va)\b', text)
    literal = None
    suppressed = False
    for clause in clauses:
        if not clause.strip():
            continue
        target = r'(?:itinerar(?:y|ies)|(?:travel |trip )?plan|schedule|trip|lich trinh|ke hoach du lich)'
        determiners = r'(?:(?:a|an|the|my|any|this|our|me|new|mot|cho toi)\s+)*'
        # Restrict cancellation to planning itself, not "don't remove lunch"
        # or "don't create a table in the itinerary".
        no_plan = bool(re.search(r"\b(?:do not|don't|dont|never|khong|dung)\s+"
            r'(?:(?:please|need to|want to|muon|can)\s+)*'
            r'(?:plan\b|(?:create|build|design|make|change|revise|update|edit|'
            r'lap|len|tao|sua|doi|want|need)\s+'+determiners+target+r'\b)|'
            r'\b(?:stop|cancel|huy)\s+'+determiners+target+r'\b|'
            r'\bno\s+(?:itinerary|schedule|travel plan)\b', clause))
        discussion = bool(re.match(r"\s*(?:(?:please|can you|could you|hay|vui long)\s+)*"
            r'(?:explain|translate|define|what (?:is|are|does)|how (?:does|do)|'
            r'giai thich|dich|dinh nghia)\b', clause))
        if no_plan or discussion:
            suppressed = True
            if no_plan:
                literal = None
            continue
        if re.search(r"\b(?:do not|don't|dont|never|khong|dung)\b", clause):
            # Negative meal/transport/output constraints are not positive edits.
            continue
        if (re.search(r'\b(?:change|revise|update|edit|replace|remove|swap|adjust|sua|doi)\b.{0,80}'
                       r'\b(?:itinerary|plan|schedule|day \w+|dinner|lunch|breakfast|'
                       r'lich trinh|ngay \w+|bua (?:sang|trua|toi))\b', clause)
                or (previous and re.search(r'\bmake\s+(?:(?:only|just)\s+)?day\s+\w+\b', clause))):
            # A missing saved plan is handled by the existing clarification flow.
            literal = 'revise_itinerary'
        elif re.search(r'\b(?:plan|design|create|build|make)\b.{0,80}'
                     r'\b(?:trip|days?|visit|itinerar(?:y|ies)|schedule)\b|'
                     r'\b(?:lap|len|tao)\b.{0,40}\b(?:lich trinh|ke hoach du lich)\b', clause):
            literal = 'create_itinerary'
        elif re.search(r"\b(?:i want|i would like|i'd like|give me|toi muon)\b.{0,50}"
                       r'\b(?:itinerary|schedule|travel plan|lich trinh)\b', clause):
            literal = 'create_itinerary'
        elif previous and re.fullmatch(r'\s*(?:day|ngay)\s+\d+\s*,?\s*(?:please)?\s*', clause):
            literal = 'revise_itinerary'
    if literal:
        return literal
    if suppressed:
        return interpreted if interpreted in ('answer', 'search') else 'answer'
    if interpreted is not None:
        return interpreted
    # Retain the stateless detector's support for short explicit itinerary requests.
    if re.search(r'\b(?:itinerary|itineraries|travel plan|lich trinh)\b', text):
        return 'create_itinerary'
    return None


def revision_days_from_text(query):
    from src.service.weather_service import normalized
    text = normalized(query)
    names = {'one':1,'two':2,'three':3,'four':4,'five':5,'six':6,'seven':7,
             'eight':8,'nine':9,'ten':10,'eleven':11,'twelve':12,'thirteen':13,'fourteen':14,
             'mot':1,'hai':2,'ba':3,'bon':4,'nam':5,'sau':6,'bay':7,'tam':8,'chin':9,'muoi':10}
    number = r'(?:\d{1,2}|'+'|'.join(names)+r')\b'
    label = r'(?:days?|ngay)'
    targeted = re.search(r'\b(?:change|revise|update|edit|adjust|modify|make|sua|doi)\s+(?:(?:only|just|chi)\s+)?'+label+r'\s*'+number, text)
    edit_clause = text[targeted.start():] if targeted else text
    edit_clause = re.split(r'\b(?:keep|preserve|giu)\b', edit_clause, maxsplit=1)[0]
    values = set()
    def integer(value):
        return int(value) if value.isdigit() else names[value]
    joined = r'(?:\s*(?:,|and|va|&|to|through|den|-)\s*(?:'+label+r'\s*)?'+number+r')*'
    mentions = list(re.finditer(r'\b'+label+r'\s*'+number+joined, edit_clause))
    edits = list(re.finditer(r'\b(?:change|revise|update|edit|adjust|modify|make|move|sua|doi)\s+(?:(?:only|just|chi)\s+)?'+label+r'\s*'+number+joined, edit_clause))
    selected = edits or mentions[:1]
    # Comparison days are references, not edit targets ("make day 2 like day 1").
    if not re.search(r'\b(?:like|similar|match|compare)\b|\bsame as\b', edit_clause):
        selected += list(re.finditer(r'\b(?:and|va)\s+'+label+r'\s*'+number+joined, edit_clause))
    for mention in selected:
        clause = mention.group()
        values.update(integer(value.group()) for value in re.finditer(number, clause))
        for pair in re.finditer('('+number+r')\s*(?:to|through|den|-)\s*(?:'+label+r'\s*)?('+number+')', clause):
            first, last = integer(pair[1]), integer(pair[2])
            values.update(range(min(first, last), max(first, last)+1))
    # Keep invalid day numbers visible so clarification can reject them.
    return values


def revision_scope(revision_day):
    """Internal edits support a single numbered day or a set of numbered days."""
    if revision_day is None:
        return frozenset()
    return frozenset([revision_day]) if type(revision_day) is int else frozenset(revision_day)


def explicit_revision_day(query):
    values = revision_days_from_text(query)
    return next(iter(values)) if len(values) == 1 else None
