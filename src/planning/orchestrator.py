"""TravelPlanner-style tool → NotebookWrite → Planner execution with bounded repair."""

from contextlib import ExitStack
from datetime import timedelta
import re

from src.schema.planning import (TripRequest, ToolAction, ToolResult, Itinerary, PlanningReport,
    ValidationIssue, ClarificationChoice, duration_from_text, explicit_trip_dates, revision_days_from_text, revision_scope)
from src.schema.weather import iso_date
from src.planning.notebook import Notebook
from src.planning.tools import TravelTools
from src.planning.validation import validate_itinerary, bind_sources
from src.utils.language import query_language
from src.utils.redaction import CredentialRedactor


def trip_request(query, state, *, location=None, start=None, end=None, preferences=()):
    fields = TripRequest.model_fields
    data = {key: value for key, value in state.items() if key in fields}
    data['destinations'] = state.get('destinations') or ([location or state.get('destination')] if location or state.get('destination') else [])
    if location:
        data['destinations'] = [location]
    from src.resolver.location_resolver import LocationResolver
    from src.utils.utils import load_config
    config = load_config()
    data['destinations'] = list(dict.fromkeys(LocationResolver.configured_name(city, config) for city in data['destinations']))
    if data.get('origin'):
        data['origin'] = LocationResolver.configured_name(data['origin'], config)
    data['preferences'] = list(preferences)[:12]
    explicit_duration = duration_from_text(query)
    if explicit_duration and explicit_duration > 14:
        from src.security.guardrails import UnsafeInput
        raise UnsafeInput('planning_duration', 'The itinerary planner supports up to 14 days per trip. Please choose a shorter trip or split it into parts.')
    data['duration_days'] = explicit_duration or data.get('duration_days') or duration_from_text(str(state.get('dates') or ''))
    if start or end:
        data.update(start_date=start, end_date=end)
    elif literal_dates := explicit_trip_dates(query):
        data.update(start_date=literal_dates[0], end_date=literal_dates[1])
    if data.get('start_date') and not data.get('end_date') and data.get('duration_days'):
        data['end_date'] = (iso_date(data['start_date'])+timedelta(days=data['duration_days']-1)).isoformat()
    if data.get('start_date') and data.get('end_date') and not data.get('duration_days'):
        count = (iso_date(data['end_date'])-iso_date(data['start_date'])).days+1
        if count > 14:
            from src.security.guardrails import UnsafeInput
            raise UnsafeInput('planning_duration', 'The itinerary planner supports up to 14 days per trip. Please split this date range into shorter trips.')
        data['duration_days'] = count if count > 0 else None
    return TripRequest.model_validate(data)


def clarification(request, previous, action, revision_day, query, *, countries=()):
    vi = query_language(query) == 'vi'
    from src.resolver.location_resolver import LocationResolver
    if any(LocationResolver._normalize(city) in countries for city in request.destinations):
        return ('Bạn muốn ghé những thành phố nào? Chưa có công cụ tự chọn thành phố cho cả quốc gia.'
                if vi else 'Which destination cities should I include? Automatic city selection for an entire country is not connected yet.')
    if action == 'revise_itinerary' and previous is None:
        return 'Mình chưa thấy lịch trình trong cuộc trò chuyện này. Bạn muốn đi đâu và ở lại bao lâu để mình tạo một lịch trình?' if vi else 'I don’t see a saved itinerary in this chat yet. Let’s start one — where would you like to go, and how long will you stay?'
    if request.start_date and request.end_date and iso_date(request.start_date) > iso_date(request.end_date):
        return 'Ngày về đang sớm hơn ngày đi. Bạn muốn dùng ngày đi và ngày về nào?' if vi else 'Your return date is earlier than your arrival date. What arrival and return dates should I use?'
    if not request.destinations or not request.duration_days:
        if not request.destinations and not request.duration_days:
            return 'Bạn muốn đi đâu và ở lại bao lâu? Chỉ cần trả lời như “Phú Quốc, 4 đêm”.' if vi else 'Where would you like to go, and how long will you stay? Something like “Phu Quoc, 4 nights” is enough.'
        return ('Bạn muốn đi đâu?' if vi else 'Where would you like to go?') if not request.destinations else (
            'Bạn sẽ ở lại bao lâu? Bạn có thể trả lời bằng số ngày hoặc số đêm.' if vi else 'How long will you stay? You can reply with days or nights — I’ve kept your other details.')
    if request.start_date and request.end_date and (iso_date(request.end_date)-iso_date(request.start_date)).days+1 != request.duration_days:
        start, end = iso_date(request.start_date), iso_date(request.end_date)
        days = (end-start).days+1
        shorter_end = (start+timedelta(days=request.duration_days-1)).isoformat()
        return (f'{request.start_date} đến {request.end_date} là {days} ngày, {days-1} đêm. '
                f'Bạn muốn giữ các ngày này hay đi {request.duration_days} ngày, đến {shorter_end}? '
                'Chỉ cần trả lời bằng số ngày hoặc số đêm; mình đã giữ các thông tin khác.' if vi else
                f'{request.start_date} to {request.end_date} is {days} days and {days-1} nights. '
                f'Would you like to keep those dates, or make it {request.duration_days} days, ending {shorter_end}? '
                'Just reply with days or nights — I’ve kept your other trip details.')
    if previous and any(day < 1 or day > len(previous.days) for day in revision_scope(revision_day)):
        return 'Lịch trình chưa có ngày này. Bạn muốn sửa ngày nào?' if vi else 'The saved itinerary does not have that day. Which day should I revise?'
    return None


def render_itinerary(plan, report, notebook, query, request=None):
    vi = query_language(query) == 'vi'
    def label(en, vn):
        return vn if vi else en
    def literal(value):
        return re.sub(r'([\\`*_{}\[\]<>#!|])', r'\\\1', ' '.join(str(value).split()))
    blocks = [label('## Your itinerary', '## Lịch trình của bạn'),
        label('Draft: missing evidence and unverified constraints are listed below.' if report.status == 'partial' else
              'Checks passed against the supplied tool evidence; indexed details and forecasts may change.',
              'Bản nháp: thông tin thiếu và điều kiện chưa xác minh được liệt kê bên dưới.' if report.status == 'partial' else
              'Đã kiểm tra theo dữ liệu công cụ; thông tin đã lưu và dự báo có thể thay đổi.')]
    blocks.append(label('Activity times and visit lengths are suggestions; known opening hours and travel estimates are checked before display.',
                        'Giờ tham quan và thời lượng là đề xuất; giờ mở cửa đã biết và thời gian di chuyển ước tính được kiểm tra trước khi hiển thị.'))
    records = notebook.records()
    for day in plan.days:
        blocks.append(f"### {label('Day', 'Ngày')} {day.day} — {literal(day.city)}" + (f' · {day.date}' if day.date else ''))
        for heading, choices in ((label('Transport', 'Di chuyển'), day.transportation),
            (label('Breakfast', 'Bữa sáng'), [day.breakfast]), (label('Attractions', 'Tham quan'), day.attractions),
            (label('Lunch', 'Bữa trưa'), [day.lunch]), (label('Dinner', 'Bữa tối'), [day.dinner]),
            (label('Accommodation', 'Chỗ ở'), [day.accommodation])):
            selected = [item for item in choices if item]
            if not selected:
                if heading == label('Accommodation', 'Chỗ ở') and day.day == len(plan.days):
                    continue
                blocks.append(f"- **{heading}:** " + label('Not selected; remains unverified.', 'Chưa chọn; chưa được xác minh.'))
            for choice in selected:
                record = records[choice.evidence_id]
                schedule = f" ({label('suggested', 'dự kiến')} {choice.start_time}–{choice.end_time})" if choice.start_time and choice.end_time else ''
                if record.provider == 'OpenRouteService':
                    schedule += f" · ~{record.duration_minutes} {label('min', 'phút')} · {record.mode} (OpenRouteService {label('estimate', 'ước tính')})"
                blocks.append(f"- **{heading}:** {literal(record.name)}{schedule} [{record.evidence_id}]")
        if request:
            from math import ceil
            from src.planning.validation import minutes
            from src.service.routing_service import route_for_pair
            timed = sorted((minutes(choice.start_time), minutes(choice.end_time), choice.evidence_id)
                           for kind, choice in day.choices() if kind in ('restaurant', 'attraction')
                           and choice.start_time and choice.end_time)
            estimates = []
            for before, after in zip(timed, timed[1:]):
                route = route_for_pair(notebook, records[before[2]], records[after[2]], request)
                if route:
                    source_id, matrix, seconds, meters = route
                    estimates.append(f"- {literal(records[before[2]].name)} → {literal(records[after[2]].name)}: "
                                     f"~{ceil(seconds/60)} {label('min', 'phút')} · {meters/1000:.1f} km · {matrix.mode} [{source_id}]")
            if estimates:
                blocks.append(label('**Estimated travel between stops**', '**Ước tính di chuyển giữa các điểm**'))
                blocks.extend(estimates)
    matrices = [entry for entry in notebook.entries if entry.route_matrix]
    if matrices:
        blocks.append(label('Route times are OpenRouteService estimates, without live traffic or fares.',
                            'Thời gian di chuyển là ước tính OpenRouteService, không có giao thông trực tiếp hay giá cước.'))
        blocks.extend(f'OpenRouteService · {entry.route_matrix.mode} · {entry.route_matrix.fetched_at} [{entry.entry_id}]'
                      for entry in matrices)
    for entry in notebook.entries:
        if entry.weather:
            weather = entry.weather
            blocks.append(f"### {label('Weather', 'Thời tiết')} — {literal(entry.city)}")
            blocks.append(literal(weather.note))
            for day in weather.days:
                parts = [day.date, day.condition]
                if day.temperature_min_c is not None and day.temperature_max_c is not None:
                    parts.append(f'{day.temperature_min_c}–{day.temperature_max_c} °C')
                if day.precipitation_probability_pct is not None:
                    parts.append(f"{label('rain probability', 'khả năng mưa')} {day.precipitation_probability_pct}%")
                blocks.append('- ' + literal(' · '.join(parts)) + ' [W1]')
            if weather.days:
                blocks.append(f'Open-Meteo · {literal(weather.timezone)} · {literal(weather.fetched_at)} [W1]')
    if report.known_cost is not None:
        blocks.append(label('Known cost subtotal, not the full trip cost: ', 'Tổng chi phí đã biết, chưa phải toàn bộ chuyến đi: ')
                      + f'{report.known_cost:g} {report.currency or ""}')
    warnings = list(dict.fromkeys(issue.message for issue in report.issues if issue.severity == 'unknown'))
    if warnings:
        blocks.append(label('### Still unverified', '### Chưa được xác minh'))
        blocks.extend('- ' + literal(message) for message in warnings[:12])
    if any(record.provider == 'TravelPlanner benchmark' for record in records.values()):
        blocks.append(label('Uses static TravelPlanner benchmark records, not current travel availability.',
                            'Sử dụng dữ liệu benchmark TravelPlanner, không phải tình trạng dịch vụ hiện tại.'))
    return CredentialRedactor().text('\n\n'.join(blocks))


class TravelOrchestrator:
    def __init__(self, *, config, planner=None, tools=None):
        self.config, self.planner, self.tools = config, planner, tools

    def run(self, query, request, *, previous=None, action='create_itinerary', revision_day=None,
            search_service=None, weather_tool=None, limit=20, route=None, include_weather=None, on_progress=None):
        if action == 'revise_itinerary':
            days = revision_days_from_text(query)
            if days:
                revision_day = next(iter(days)) if len(days) == 1 else frozenset(days)
        notebook, trace = Notebook(city_count=max(1, len(request.destinations)),
                                   duration_days=request.duration_days or 1), []
        def report(step, title, status='running', **data):
            if on_progress:
                on_progress({'step': step, 'title': title, 'status': status, **data})
        report('planning_details', 'Check trip requirements')
        from src.utils.utils import read_json, resolve_path
        from src.resolver.location_resolver import LocationResolver
        country_path = self.config.get('country_scopes_path')
        country_data = read_json(resolve_path(country_path)) if country_path else {}
        countries = {LocationResolver._normalize(name) for country, data in country_data.items()
                     for name in (country, data['country_code'], *data.get('aliases', []))}
        question = clarification(request, previous, action, revision_day, query, countries=countries)
        if question:
            options = []
            if (request.start_date and request.end_date and request.duration_days and request.destinations
                    and not any(LocationResolver._normalize(city) in countries for city in request.destinations)
                    and not (action == 'revise_itinerary' and previous is None)):
                count = (iso_date(request.end_date)-iso_date(request.start_date)).days+1
                if 1 <= count <= 14 and count != request.duration_days:
                    vi = query_language(query) == 'vi'
                    options = [ClarificationChoice(label=f'{count} ngày · {count-1} đêm' if vi else f'{count} days · {count-1} nights', reply=f'{count} ngày' if vi else f'{count} days'),
                        ClarificationChoice(label=f'{request.duration_days} ngày · {request.duration_days-1} đêm' if vi else f'{request.duration_days} days · {request.duration_days-1} nights',
                                            reply=f'{request.duration_days} ngày' if vi else f'{request.duration_days} days')]
            pending_action = 'create_itinerary' if action == 'revise_itinerary' and previous is None else action
            return {'answer': question, 'pipeline_status': 'needs_clarification', 'generation_status': 'planning_clarification',
                    'context': None, 'retrieval': None, 'itinerary': None,
                    'planning': PlanningReport(status='needs_clarification', action=pending_action,
                                               clarification_options=options).model_dump()}
        report('planning_details', 'Trip requirements are ready', 'done')
        with ExitStack() as resources:
            tools = self.tools or TravelTools(request, query, resources=resources, config=self.config,
                search_service=search_service, weather_tool=weather_tool, limit=limit, route=route,
                include_weather=include_weather, on_progress=on_progress, previous=previous)
            planner = self.planner
            if planner is None:
                from src.planning.planner import Planner
                planner = resources.enter_context(Planner(config=self.config))
            required = [(tool, city) for city in request.destinations for tool in
                        ('GuideSearch', 'AttractionSearch', 'RestaurantSearch', 'AccommodationSearch', 'DistanceMatrix')]
            if include_weather is not False and self.config.get('weather_enabled', True):
                required.extend(('WeatherSearch', city) for city in request.destinations)
            if request.origin and 'no_flight' not in request.transport_constraints and request.transport_mode not in ('driving', 'taxi', 'walking'):
                origin = LocationResolver._normalize(request.origin)
                if origin != LocationResolver._normalize(request.destinations[0]):
                    required.append(('FlightSearch', request.destinations[0]))
                if origin != LocationResolver._normalize(request.destinations[-1]):
                    required.append(('FlightSearch', request.origin))
            attempted, signatures, repetitions, bad_actions = set(), set(), 0, 0
            collection_issues = []
            maximum = min(24, self.config.get('planning_max_steps', 20))
            for step in range(maximum):
                remaining = [pair for pair in required if pair not in attempted]
                report('planning_select', 'Choose the next planning tool')
                try:
                    if not attempted:
                        selected = ToolAction(tool='GuideSearch', city=request.destinations[0])
                    else:
                        selected = ToolAction.model_validate(planner.next_action(request, notebook.data, remaining))
                except Exception:
                    selected = ToolAction(tool=remaining[0][0], city=remaining[0][1]) if remaining else ToolAction(tool='Planner')
                if selected.tool == 'Planner':
                    if not remaining:
                        break
                    selected = ToolAction(tool=remaining[0][0], city=remaining[0][1])
                if selected.tool == 'WeatherSearch' and (include_weather is False or not self.config.get('weather_enabled', True)):
                    if not remaining:
                        break
                    selected = ToolAction(tool=remaining[0][0], city=remaining[0][1])
                city = selected.city or request.destinations[0]
                allowed = set(request.destinations + ([request.origin] if request.origin else []))
                valid_city = city in request.destinations or (selected.tool in ('FlightSearch', 'DistanceMatrix') and city == request.origin)
                route_scope = (selected.origin is None or selected.origin in allowed) and (selected.destination is None or selected.destination in allowed)
                date_scope = True
                if selected.departure_date:
                    try:
                        value = iso_date(selected.departure_date)
                        date_scope = bool(request.start_date and request.end_date and iso_date(request.start_date) <= value <= iso_date(request.end_date))
                    except ValueError:
                        date_scope = False
                if not valid_city or not route_scope or not date_scope:
                    bad_actions += 1
                    trace.append('Rejected tool call outside the requested destination scope.')
                    if bad_actions >= 2:
                        break
                    continue
                if selected.tool == 'WeatherSearch' and city in request.destinations and ('GuideSearch', city) not in attempted:
                    selected = ToolAction(tool='GuideSearch', city=city)

                def routing_prerequisites(selection, location):
                    if selection.tool == 'DistanceMatrix' and self.config.get('planning_data_mode', 'vistra') == 'vistra':
                        dependencies = [('GuideSearch', destination) for destination in request.destinations]
                        dependencies.extend((tool, location) for tool in ('AttractionSearch', 'RestaurantSearch', 'AccommodationSearch')
                                            if location in request.destinations)
                        missing = next((pair for pair in dependencies if pair not in attempted), None)
                        if missing:
                            return ToolAction(tool=missing[0], city=missing[1]), missing[1]
                    return selection, location

                selected, city = routing_prerequisites(selected, city)
                signature = (selected.tool, city)
                call_signature = (selected.tool, city, selected.origin, selected.destination, selected.departure_date, selected.mode)
                if call_signature in signatures:
                    repetitions += 1
                    if repetitions >= 2:
                        trace.append('Stopped repeated tool calls; remaining evidence may be incomplete.')
                        break
                    if remaining:
                        signature = remaining[0]
                        selected, city = ToolAction(tool=signature[0], city=signature[1]), signature[1]
                        selected, city = routing_prerequisites(selected, city)
                        signature = (selected.tool, city)
                        call_signature = (selected.tool, city, None, None, None, None)
                    else:
                        break
                attempted.add(signature)
                signatures.add(call_signature)
                report('planning_tool', f'Run {selected.tool}', tool=selected.tool)
                try:
                    options = {'action': selected} if any((selected.origin, selected.destination, selected.departure_date, selected.mode)) else {}
                    if selected.tool == 'DistanceMatrix' and isinstance(tools, TravelTools):
                        options['notebook'] = notebook.data
                    result = ToolResult.model_validate(tools.run(selected.tool, city, **options))
                    if result.tool != selected.tool or result.city != city:
                        raise ValueError('Tool result does not match its requested identity.')
                except Exception:
                    result = ToolResult(tool=selected.tool, city=city, status='unavailable',
                                        note='The tool could not supply validated evidence. No records were inferred.')
                report('planning_tool', f'{selected.tool} finished', 'done', tool=selected.tool, tool_status=result.status)
                try:
                    entry = notebook.write(result)
                except ValueError:
                    # A large observation must not crash an otherwise usable turn.
                    try:
                        entry = notebook.write(ToolResult(tool=selected.tool, city=city, status='unavailable',
                            note='The tool observation could not fit the bounded evidence notebook.'))
                    except ValueError:
                        collection_issues.append(ValidationIssue(severity='unknown', constraint='evidence_budget',
                            message=f'{selected.tool} ({city}) could not be retained within the evidence budget.'))
                        trace.append('NotebookWrite: evidence budget reached; stopped collection.')
                        report('planning_notebook', 'Evidence notebook reached its size limit', 'error')
                        break
                count = (f'{len(entry.weather.days)} forecast days' if entry.weather else
                         f'{len(entry.route_matrix.locations)} route locations' if entry.route_matrix else f'{len(entry.records)} records')
                trace.extend([f'{selected.tool} ({city}): {entry.status}, {count}', f'NotebookWrite: {entry.entry_id}'])
                report('planning_notebook', 'Save tool evidence to the planning notebook', 'done', entry=entry.entry_id)
            report('planning_plan', 'Build itinerary from the evidence notebook')
            plan, issues, known_cost, currency, repairs = None, [], None, None, 0
            max_repairs = self.config.get('planning_max_repairs', 1)
            for attempt in range(max_repairs+1):
                try:
                    plan = Itinerary.model_validate(planner.run(query, request, notebook.data,
                        previous=previous, revision_day=revision_day, draft=plan,
                        errors=[issue for issue in issues if issue.severity == 'error']))
                    from src.planning.scheduling import build_schedule
                    from src.service.weather_service import normalized
                    locked = bool(previous and re.search(r'\bkeep (?:the )?same venues\b|\bgiu nguyen (?:cac )?dia diem\b', normalized(query)))
                    scheduling = {}
                    scheduled = build_schedule(plan, request, notebook.data, previous=previous,
                        revision_day=revision_day, preserve_venues=locked, diagnostics=scheduling)
                    trace.append(f"ScheduleSearch: {scheduling['status']}, {scheduling['nodes']} candidates checked")
                    if scheduled is not None:
                        plan = scheduled
                    report('planning_validate', 'Check itinerary constraints')
                    issues, known_cost, currency = validate_itinerary(plan, request, notebook.data,
                        previous=previous, revision_day=revision_day)
                except Exception as error:
                    import logging
                    logging.getLogger(__name__).warning('Structured itinerary generation failed (%s).', type(error).__name__)
                    issues = [ValidationIssue(severity='error', constraint='planner',
                        message=f'The planner did not produce a valid structured itinerary ({type(error).__name__}).')]
                if not any(issue.severity == 'error' for issue in issues):
                    break
                if attempt < max_repairs:
                    repairs += 1
                    report('planning_repair', 'Repair itinerary constraint violations')
            errors = any(issue.severity == 'error' for issue in issues)
            issues.extend(collection_issues)
            # Unavailable tool observations remain visible even if the planner
            # omits their components. They can never yield a verified full trip.
            for entry in notebook.data.entries:
                if entry.status in ('unavailable', 'no_results'):
                    issues.append(ValidationIssue(severity='unknown', constraint='tool_availability', message=f'{entry.tool}: {entry.note}'))
                if entry.weather and entry.weather.status == 'partial':
                    issues.append(ValidationIssue(severity='unknown', constraint='weather', message=entry.weather.note))
            for tool, city in required:
                if (tool, city) not in attempted:
                    issues.append(ValidationIssue(severity='unknown', constraint='tool_availability', message=f'{tool} ({city}) was not completed within the tool-step budget.'))
            status = 'failed' if errors else 'partial' if any(issue.severity == 'unknown' for issue in issues) else 'verified'
            validation = PlanningReport(status=status, action=action, issues=issues[:80], tool_trace=trace[:50],
                repair_attempts=repairs, known_cost=known_cost, currency=currency)
            if not errors and plan is not None:
                plan = bind_sources(plan, notebook.data)
                answer = render_itinerary(plan, validation, notebook.data, query, request)
            else:
                plan = None
                vi = query_language(query) == 'vi'
                constraints = list(dict.fromkeys(issue.constraint.replace('_', ' ') for issue in issues if issue.severity == 'error'))
                answer = ('Bản nháp chưa vượt qua kiểm tra lịch trình. Yêu cầu chuyến đi đã được giữ lại. '
                          'Hãy thử tạo lại lịch trình. Các kiểm tra chưa đạt: ' if vi else
                          'The draft did not pass itinerary validation. Your trip requirements are retained; '
                          'please try generating it again. Failed checks: ') + ', '.join(constraints[:5]) + '.'
            validation.tool_trace = (trace + [f'Planner: {status}'])[:50]
            report('planning_validate', 'Itinerary checks finished', 'done', planning_status=status)
            from src.generation.rag_generation import build_rag_context
            response = getattr(tools, 'response', None)
            if response and plan:
                chosen = {choice.entity_id for day in plan.days for _, choice in day.choices()}
                response.related_results.sort(key=lambda hit: hit.entity_id not in chosen)
            context = build_rag_context(response, self.config) if response else {'route': 'text', 'sources': []}
            context['notebook'] = notebook.list_all()
            forecasts = [entry.weather for entry in notebook.data.entries if entry.weather]
            context['weather'] = forecasts[0].model_dump() if forecasts else None
            from src.security.context import prepare_context, answer_safety_issues
            context = prepare_context(context)
            if answer_safety_issues(answer, context):
                raise ValueError('The itinerary failed its output boundary.')
            return {'answer': answer, 'itinerary': plan.model_dump() if plan else None, 'planning': validation.model_dump(),
                'notebook': notebook.list_all(), 'context': context, 'weather': context.get('weather'),
                'retrieval': as_retrieval(response), 'pipeline_status': 'complete' if status == 'verified' else status,
                'generation_status': 'planning_'+status, 'model': getattr(planner, 'model_name', None)}


def as_retrieval(response):
    from dataclasses import asdict
    return asdict(response) if response else None
