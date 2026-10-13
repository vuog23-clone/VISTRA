"""Terminal demo: query → Jev response mode → direct LLM or retrieval/RAG.

    ./.venv/python.exe -B main.py --query "things to do in Hanoi"
    ./.venv/python.exe -B main.py --query "things to do in Hanoi" --retrieve-only
    ./.venv/python.exe -B main.py --query "things to do in Hanoi" --show-context
    ./.venv/python.exe -B main.py --query "Who are you?"
    ./.venv/python.exe -B main.py --chat
    ./.venv/python.exe -B main.py --chat --json

Read run_pipeline() for the complete flow, including itinerary orchestration.
"""

import argparse
from contextlib import ExitStack, redirect_stdout
from dataclasses import asdict
import json
from math import isfinite
from pathlib import Path
import sys
from time import perf_counter
from urllib.parse import urlencode

from src.utils.redaction import CredentialRedactor
from src.utils.language import localized_message
from src.utils.utils import load_config
from src.security.guardrails import validate_request, validate_text, validate_output_path, UnsafeInput
from src.security.images import load_safe_image


def run_pipeline(
    user_query, *, image=None, route=None, location=None, categories=None,
    limit=20, group_size=None, retrieve_only=False, on_progress=None,
    on_retrieval=None, on_context=None, search_service=None, generator=None,
    auto_location=True, location_extractor=None,
    maps_only=False, response_mode="auto", response_router=None, direct_generator=None,
    routing_query=None, conversation_memory=None,
    trip_start=None, trip_end=None, include_weather=None, weather_tool=None,
    planning_action=None, revision_day=None, itinerary_planner=None, planning_tools=None, planning_mode='auto',
    previous_itinerary=None,
):
    """Return retrieval, exact source context, answer, and stage progress.

    Inject services, routers, or generators for offline checks. Injected resources belong
    to the caller; resources created here are closed before this function returns.
    """
    # Do this before creating providers or loading models, even with injected services.
    user_query = validate_text(user_query, "user_query", check_intent=True)
    search_query = (validate_text(routing_query, "routing_query", check_intent=True)
                    if routing_query is not None else user_query)
    from src.service.weather_service import trip_dates, wants_weather, weather_for_trip
    if include_weather is not None and type(include_weather) is not bool:
        raise ValueError('include_weather must be a boolean.')
    if trip_start is not None or trip_end is not None:
        trip_dates(user_query, start=trip_start, end=trip_end)
    if conversation_memory is not None:
        from src.schema.memory import ConversationMemory
        conversation_memory = ConversationMemory.model_validate(conversation_memory).model_dump()
    if previous_itinerary is not None:
        from src.schema.planning import Itinerary
        previous_itinerary = Itinerary.model_validate(previous_itinerary)
    user_query, location, _, categories = validate_request(
        user_query, image=image, route=route, location=location, categories=categories,
        limit=limit, group_size=group_size, retrieve_only=retrieve_only,
        auto_location=auto_location, maps_only=maps_only)
    if not isinstance(response_mode, str) or response_mode not in {"auto", "retrieval", "llm_only"}:
        raise UnsafeInput("invalid_response_mode", "response_mode must be auto, retrieval, or llm_only.")
    retrieval_requested = (route not in (None, "auto") or location is not None
                           or bool(categories) or maps_only or retrieve_only)
    if response_mode == "llm_only" and (image is not None or retrieval_requested):
        raise UnsafeInput("conflicting_response_mode", "Direct LLM mode cannot be combined with retrieval options or an image.")
    config = load_config()
    if planning_mode not in ('auto', 'off') or planning_action not in (None, 'none', 'create_itinerary', 'revise_itinerary'):
        raise ValueError('Invalid planning mode or action.')
    if revision_day is not None and (type(revision_day) is not int or not 1 <= revision_day <= 14):
        raise ValueError('The revision day must be from 1 to 14.')
    if response_mode == 'auto' and config.get('planning_enabled', True) and planning_mode != 'off':
        from src.schema.planning import planning_action as detect_planning
        if planning_action in ('create_itinerary', 'revise_itinerary') or (
                planning_action is None and detect_planning(user_query, previous_itinerary or (conversation_memory or {}).get('trip_state', {}).get('itinerary'))):
            retrieval_requested = True
    redactor = CredentialRedactor()
    started = perf_counter()
    events = []
    response_routing = None
    planning_output = {}
    location_detection = {
        "status": "explicit" if location else "disabled",
        "source": "manual" if location else "disabled",
        "location": location,
        "resolved_location": None,
        "validation_status": "pending" if location else "not_applicable",
        "candidates": [],
        "clarification": None,
    }

    def report(event):
        event = redactor.data(event)
        events.append(event)
        if on_progress is not None:
            on_progress(event)

    def finish(retrieval_output, context, answer, generation_status, model, pipeline_status="complete"):
        report({"step": "output", "title": "Pipeline finished", "status": "done"})
        return redactor.data({
            "user_query": user_query,
            "response_mode": response_routing["mode"] if response_routing else None,
            "response_routing": response_routing,
            "location_detection": location_detection,
            "retrieval": retrieval_output,
            "context": context,
            "weather": context.get('weather') if context else None,
            "answer": answer,
            "answer_format": "markdown",
            "generation_status": generation_status,
            "grounding": getattr(direct_generator if response_routing and
                response_routing["mode"] == "llm_only" else generator, "last_grounding", None),
            "pipeline_status": pipeline_status,
            "model": model,
            "generation_vision": getattr(generator, "last_vision", None),
            "progress": events,
            "elapsed_seconds": round(perf_counter() - started, 3),
            **{key: planning_output[key] for key in ('itinerary', 'planning', 'notebook') if key in planning_output},
        })

    # 1. INPUT: auto leaves evidence selection to Jev after the response gate.
    selected_route = None if route in (None, "auto") else route
    report({"step": "input", "title": "User query received", "status": "done",
            "query": user_query, "route": selected_route or "auto"})

    with ExitStack() as resources:
        if image is not None:
            image = load_safe_image(image)
            resources.callback(image.close)

        # 1b. RESPONSE MODE: skip destination/vector/database work for direct answers.
        report({"step": "response_routing", "title": "Decide whether retrieved evidence is needed", "status": "running"})
        from src.schema.response import ResponseDecision
        try:
            if response_mode != "auto":
                decision = ResponseDecision(mode=response_mode, decision_source="override")
            elif image is not None or retrieval_requested:
                decision = ResponseDecision(mode="retrieval", decision_source="constraint")
            else:
                if response_router is None:
                    from src.router.jev_response import JevResponseRouter
                    response_router = resources.enter_context(JevResponseRouter(config=config))
                from src.utils.progress import retrieval_progress
                with retrieval_progress(report):
                    decision = ResponseDecision.model_validate(response_router.route(search_query))
        except Exception:
            report({"step": "response_routing", "title": "Response routing failed", "status": "error"})
            raise
        response_routing = decision.model_dump()
        report({"step": "response_routing", "title": "Use a direct LLM answer" if decision.mode == "llm_only"
                else "Use retrieved travel evidence", "status": "done", **response_routing})
        if decision.mode == "llm_only":
            location_detection.update(status="skipped", source="not_applicable", location=None,
                resolved_location=None, validation_status="not_applicable")
            report({"step": "location", "title": "Destination extraction is unnecessary for a direct answer", "status": "skipped"})
            report({"step": "retrieve", "title": "Direct answer needs no indexed evidence", "status": "skipped"})
            from src.generation.direct_generation import DirectGenerator, direct_answer_issues
            if direct_generator is None:
                direct_generator = resources.enter_context(DirectGenerator(config=config))
            model = direct_generator.model_name
            report({"step": "generation", "title": "Generate a direct LLM answer", "status": "running", "model": model})
            try:
                answer = (direct_generator.generate(user_query, conversation_memory=conversation_memory)
                          if conversation_memory is not None else direct_generator.generate(user_query))
                if direct_answer_issues(answer):
                    raise UnsafeInput("unsafe_answer", "The direct answer failed safety validation.")
            except Exception:
                report({"step": "generation", "title": "Direct LLM generation failed", "status": "error"})
                raise
            grounding = getattr(direct_generator, "last_grounding", None) or {}
            status = "direct_fallback" if grounding.get("status") == "direct_fallback" else "direct_complete"
            report({"step": "generation", "title": "Direct LLM answer completed", "status": "done"})
            return finish(None, None, redactor.text(answer), status, model)

        from src.schema.planning import planning_action as detect_planning, Itinerary
        state = (conversation_memory or {}).get('trip_state') or {}
        previous = previous_itinerary or (Itinerary.model_validate(state['itinerary']) if state.get('itinerary') else None)
        action = planning_action or detect_planning(user_query, previous)
        if action == 'revise_itinerary':
            from src.schema.planning import revision_days_from_text
            literal_days = revision_days_from_text(user_query)
            if literal_days:
                revision_day = next(iter(literal_days)) if len(literal_days) == 1 else None
        if (config.get('planning_enabled', True) and planning_mode != 'off' and not retrieve_only and not maps_only
                and action in ('create_itinerary', 'revise_itinerary')):
            if conversation_memory is None:
                from src.memory.conversation import ConversationInterpreter
                from src.schema.memory import ConversationMemory
                interpreter = resources.enter_context(ConversationInterpreter(config=config))
                parsed = interpreter.understand(user_query, ConversationMemory())
                state = parsed.trip_state.model_dump()
                if include_weather is None:
                    include_weather = parsed.include_weather
            if include_weather is None:
                include_weather = state.get('include_weather')
            from src.planning.orchestrator import TravelOrchestrator, trip_request
            preferences = [record['content'] for record in (conversation_memory or {}).get('long_term', [])
                           if record.get('kind') == 'preference']
            request = trip_request(user_query, state, location=location, start=trip_start, end=trip_end, preferences=preferences)
            if request.destinations:
                location_detection.update(status='explicit' if location else 'destination',
                    source='manual' if location else 'memory' if conversation_memory else 'interpreter',
                    location=request.destinations[0], validation_status='pending')
            orchestrator = TravelOrchestrator(config=config, planner=itinerary_planner, tools=planning_tools)
            planning_output = orchestrator.run(user_query, request, previous=previous, action=action,
                revision_day=revision_day, search_service=search_service, weather_tool=weather_tool,
                limit=limit, route=selected_route, include_weather=include_weather, on_progress=report)
            if planning_output.get('retrieval'):
                retrieved_location = planning_output['retrieval'].get('location')
                resolved = planning_output['retrieval'].get('location_status') not in ('', 'unresolved')
                location_detection.update(validation_status='resolved' if resolved else 'unresolved',
                    resolved_location=retrieved_location if resolved else None)
            if on_retrieval and planning_output.get('retrieval'):
                on_retrieval(planning_output['retrieval'])
            if on_context:
                on_context(planning_output.get('context'))
            return finish(planning_output.get('retrieval'), planning_output.get('context'), planning_output['answer'],
                          planning_output['generation_status'], planning_output.get('model'),
                          planning_output.get('pipeline_status', 'complete'))

        from src.generation.rag_generation import build_rag_context, RAGGenerator

        # 1c. DESTINATION: infer a location only for retrieval-backed requests.
        if location is not None:
            report({"step": "location", "title": f"Use explicit location: {location}", "status": "done"})
        elif auto_location:
            report({"step": "location", "title": "Extract destination from the user query", "status": "running"})
            try:
                if location_extractor is None:
                    from src.resolver.jev_location_extractor import JevLocationExtractor
                    location_extractor = resources.enter_context(JevLocationExtractor(config=config))
                from src.schema.location import LocationIntent
                intent = LocationIntent.model_validate(location_extractor.extract_location(search_query))
            except Exception:
                report({"step": "location", "title": "Destination extraction failed", "status": "error"})
                raise
            location = validate_text(intent.location, "detected location", max_chars=160, optional=True)
            intent.candidates = [validate_text(name, "location candidate", max_chars=160)
                                 for name in intent.candidates]
            if intent.clarification is not None:
                intent.clarification = validate_text(intent.clarification, "clarification", max_chars=1000)
            location_detection.update(
                status=intent.status, source=getattr(location_extractor, "provider", "jev"), location=location,
                validation_status="pending" if location else "not_applicable",
                candidates=intent.candidates, clarification=intent.clarification,
            )
            if intent.status == "ambiguous":
                report({"step": "location", "title": "Destination needs clarification", "status": "done",
                        "detail": intent.clarification, "candidates": intent.candidates})
                # No vector/database searches or answer generation happen here.
                return finish(None, None, intent.clarification, "skipped_ambiguous_location",
                              None, pipeline_status="needs_clarification")
            report({"step": "location", "title": f"Detected destination: {location}" if location else
                    "No destination constraint; search without a location filter", "status": "done",
                    "location": location})
        else:
            report({"step": "location", "title": "Automatic location extraction disabled", "status": "skipped"})

        # 2. RETRIEVE: query embeddings -> Qdrant matches -> metadata enrichment.
        # SearchService validates the location and constrains vectors BEFORE search.
        if search_service is None:
            from src.service.search_service import SearchService
            search_service = resources.enter_context(SearchService())
        report({"step": "retrieve", "title": "Retrieve source evidence", "status": "running"})
        response = search_service.search(
            search_query, image=image, route=selected_route, location=location,
            categories=categories, limit=limit, group_size=group_size,
            on_progress=report,
            maps_only=maps_only,
        )
        retrieval_output = redactor.data(asdict(response))
        if maps_only:
            from src.data.google_payload import without_photo_fields
            retrieval_output = without_photo_fields(retrieval_output)
        if location is not None:
            resolved = response.location_status != "unresolved"
            location_detection["validation_status"] = "resolved" if resolved else "unresolved"
            location_detection["resolved_location"] = response.location if resolved else None
            report({"step": "location_validation", "title": "Destination scope validated" if resolved else
                    f"Destination is not recognized by the retrieval catalog: {location}",
                    "status": "done" if resolved else "skipped", "location": response.location,
                    "location_status": response.location_status})
        report({"step": "retrieve", "title": "Retrieved results are ready", "status": "done",
                "results": len(response.results) + len(response.related_results), "route": response.route})
        if on_retrieval is not None:
            on_retrieval(retrieval_output)

        # 3. AUGMENT: turn the best retrieved evidence into bounded, numbered sources.
        # Progress messages explain execution; retrieved evidence grounds the LLM.
        context = redactor.data(build_rag_context(response, config, has_image=image is not None))
        # WeatherSearch is a planning tool; its evidence is separate from venue facts.
        state = (conversation_memory or {}).get('trip_state') or {}
        requested_weather = include_weather if include_weather is not None else wants_weather(search_query + ' ' + (state.get('goal') or ''))
        if requested_weather:
            report({'step': 'weather', 'title': 'Check weather for the trip dates', 'status': 'running'})
            context['weather'] = weather_for_trip(user_query, response, state=state,
                start=trip_start, end=trip_end, tool=weather_tool,
                enabled=config.get('weather_enabled', True), timeout=config.get('weather_timeout_seconds', 8))
            from src.security.context import prepare_context
            context = prepare_context(context)
            report({'step': 'weather', 'title': 'Trip weather evidence prepared', 'status': 'done',
                    'weather_status': context['weather']['status'], 'days': len(context['weather']['days'])})
        if context.get("guardrail_warnings"):
            report({"step": "guardrails_context", "title": "Unsafe source evidence excluded",
                    "status": "done", "excluded_sources": len(context["guardrail_warnings"])})
        report({"step": "context", "title": "RAG source context prepared", "status": "done",
                "sources": len(context["sources"])})

        # 4. GENERATE: one LLM call receives the original question and source context.
        answer = None
        model = None
        if retrieve_only:
            generation_status = "skipped_retrieval_only"
            report({"step": "generation", "title": "Stopped after retrieval", "status": "skipped"})
        elif not context["sources"] and not (context.get('weather') or {}).get('days'):
            generation_status = "skipped_no_sources"
            answer = (localized_message(user_query,
                      "ℹ️ The destination could not be resolved in the retrieval catalog. "
                      "Please clarify its name or supply `--location`.",
                      "ℹ️ Không xác định được địa điểm trong danh mục. "
                      "Vui lòng làm rõ tên hoặc dùng `--location`.")
                      if location_detection["validation_status"] == "unresolved" else localized_message(user_query,
                      "ℹ️ No indexed evidence matched this request. Try a different query or location.",
                      "ℹ️ Không có nguồn dữ liệu phù hợp. Hãy thử câu hỏi hoặc địa điểm khác."))
            report({"step": "generation", "title": "No sources available for RAG", "status": "skipped"})
        else:
            if generator is None:
                generator = resources.enter_context(RAGGenerator(config=config))
            model = generator.model_name
            report({"step": "generation", "title": "Generate an answer from retrieved sources",
                    "status": "running", "model": model})
            try:
                from src.utils.progress import retrieval_progress
                with retrieval_progress(report):
                    answer = (generator.generate(user_query, context, conversation_memory=conversation_memory)
                              if conversation_memory is not None else generator.generate(user_query, context))
                prepared_context = getattr(generator, "last_context", None)
                if isinstance(prepared_context, dict):
                    context = prepared_context
                vision = getattr(generator, "last_vision", None)
                if isinstance(vision, dict):
                    report({"step": "vision", "title": "Retrieved photos supplied to the LLM", "status": "done", **vision})
                # Injected generators receive the same output boundary.
                from src.security.context import answer_safety_issues
                if answer_safety_issues(answer, context):
                    raise UnsafeInput("unsafe_answer", "The generated answer failed safety validation.")
                answer = redactor.text(answer)
            except Exception:
                report({"step": "generation", "title": "LLM generation failed", "status": "error"})
                raise
            grounding = getattr(generator, "last_grounding", None) or {}
            generation_status = "metadata_fallback" if grounding.get("status") == "metadata_fallback" else "complete"
            report({"step": "generation", "title": "Indexed facts returned" if generation_status == "metadata_fallback"
                    else "RAG answer completed", "status": "done", "grounding": grounding.get("status")})

    if on_context is not None:
        on_context(context)
    # 5. OUTPUT: retain both the retrieval results and the generated answer.
    return finish(retrieval_output, context, answer, generation_status, model)


def print_progress(event):
    status = event.get("status", "done")
    detail = ""
    if event.get("step") == "routing":
        detail = f" | route={event.get('route')} | intent={event.get('intent')} | source={event.get('decision_source')}"
        if event.get("policy_applied"):
            detail += f" | classifier_intent={event.get('classifier_intent')}"
    elif event.get("step") == "vision":
        detail = f" | images={len(event.get('attached_source_ids', []))} | unavailable={len(event.get('unavailable_source_ids', []))}"
    print(f"[{event.get('step', 'retrieval')}: {status}] {event.get('title', '')}{detail}",
          file=sys.stderr, flush=True)
    if event.get("step") == "location" and event.get("detail"):
        print(event["detail"], file=sys.stderr, flush=True)


def print_retrieval(response):
    results = [*response["results"], *response.get("related_results", [])]
    print(f"\nRetrieved results | route={response['route']} | count={len(results)}")
    if response.get("branch_status"):
        print(f"Retrieval branches: {response['branch_status']}")
    if response.get("location"):
        print(f"Location filter: {response['location']}")
    if response.get("location_status"):
        print(f"Location status: {response['location_status']}")
    ranking = response.get("image_reranking")
    if ranking:
        print(f"Image distance ranking: {ranking['status']} | reference={ranking.get('reference_name')}")
    for index, result in enumerate(results, 1):
        print(f"{index}. [{result['source']}] {result.get('name') or result['entity_id']} | score={result['score']:.6f}")
        if result.get("distance_km") is not None:
            print(f"   Distance from destination: {result['distance_km']:.3f} km (straight-line)")
        excerpt = " ".join(str(result.get("payload", {}).get("text") or "").split())
        if excerpt:
            print(f"   {excerpt[:250]}")
        if result.get("url"):
            print(f"   {result['url']}")
        if result["source"] == "text":
            latitude, longitude = result.get("latitude"), result.get("longitude")
            if (all(type(value) in (int, float) and isfinite(value) for value in (latitude, longitude))
                    and -90 <= latitude <= 90 and -180 <= longitude <= 180):
                query = urlencode({"api": 1, "query": f"{latitude},{longitude}"})
                print(f"   Maps: https://www.google.com/maps/search/?{query}")
    for warning in response.get("retrieval_warnings", []):
        print(f"Warning: {warning}")


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def print_pipeline_error(error):
    if type(error).__name__ == 'MemoryConflict':
        message = 'This chat session changed during the request. Retry with its latest memory.'
    else:
        message = str(error) if isinstance(error, UnsafeInput) else 'Check service configuration and availability.'
    message = CredentialRedactor().text(message)
    print(f'Pipeline failed ({type(error).__name__}): {message}', file=sys.stderr)
    return {'error': {'code': getattr(error, 'code', type(error).__name__), 'message': message}}


def terminal_chat(args, options):
    """Inspect ChatService results before browser Markdown/media presentation."""
    from src.service.chat_service import ChatService
    from src.service.chat_pipeline import run_chat_pipeline
    from src.schema.memory import checked_identity
    from uuid import uuid4

    owner, session = args.user_id, args.session_id
    checked_identity(owner, 'user_id')
    checked_identity(session, 'session_id')
    print(f'VISTRA terminal chat | user: {owner} | session: {session}', file=sys.stderr)
    help_text = ('Commands: /last (full backend JSON), /new (new session), '
                 '/session ID (resume/switch), /help, /exit. Ctrl+C also exits.')
    print(help_text, file=sys.stderr)
    pending, last_result = args.user_query, None
    while True:
        try:
            if pending is None:
                print('You> ', file=sys.stderr, end='', flush=True)
                query = input().strip()
            else:
                query, pending = pending.strip(), None
            if not query:
                continue
            if query.startswith('/'):
                command, _, argument = query.partition(' ')
                command, argument = command.casefold(), argument.strip()
                if command in ('/exit', '/quit'):
                    break
                if command == '/help':
                    print(help_text, file=sys.stderr)
                elif command == '/last':
                    if last_result is None:
                        print('No backend response in this session yet.', file=sys.stderr)
                    else:
                        print(json.dumps(last_result, ensure_ascii=False,
                            indent=None if args.json else 2, allow_nan=False), flush=True)
                elif command in ('/new', '/session'):
                    if command == '/new':
                        if argument:
                            print('Use /new for a fresh session or /session ID to choose one.', file=sys.stderr)
                            continue
                        session = 'chat-' + uuid4().hex
                    else:
                        session = checked_identity(argument, 'session_id')
                    last_result = None
                    print(f'Active session: {session}. Durable preferences remain available.', file=sys.stderr)
                else:
                    print('Unknown command. ' + help_text, file=sys.stderr)
                continue
            # Match the browser's per-request service lifecycle; history lives in
            # PostgreSQL, and a failed connection need not poison later turns.
            with redirect_stdout(sys.stderr), ChatService(pipeline=run_chat_pipeline) as chat:
                result = chat.chat(owner, session, query, **options)
            last_result = CredentialRedactor().data(result)
            if args.json:
                # One complete backend object per line; logs/prompts use stderr.
                print(json.dumps(last_result, ensure_ascii=False, allow_nan=False), flush=True)
            elif last_result.get('answer') is not None:
                print('\nVISTRA:\n' + last_result['answer'], flush=True)
            else:
                print(json.dumps(last_result, ensure_ascii=False, indent=2, allow_nan=False), flush=True)
        except (EOFError, KeyboardInterrupt):
            break
        except Exception as error:
            failure = print_pipeline_error(error)
            if args.json:
                print(json.dumps(failure, ensure_ascii=False, allow_nan=False), flush=True)
    print('\nChat ended. Saved memory is retained.', file=sys.stderr)
    return 0


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--query", "--text", dest="user_query", help="User's question; prompts if omitted")
    parser.add_argument('--chat', action='store_true', help='Interactive multi-turn chat using the frontend pipeline; /last shows backend JSON')
    parser.add_argument("--user-id", help="Memory owner ID; requires --session-id")
    parser.add_argument("--session-id", help="Persist/reuse a chat session; requires --user-id")
    parser.add_argument("--image", type=Path, help="Optional image similarity input")
    parser.add_argument("--route", choices=("text", "hybrid", "auto"),
                        help="Default: Jev chooses text or text plus images; override to force a retrieval route")
    parser.add_argument("--response-mode", choices=("auto", "retrieval", "llm_only"), default="auto",
                        help="Default: Jev decides whether retrieval is needed; override with retrieval or llm_only")
    parser.add_argument("--location", help="Override automatic destination extraction")
    parser.add_argument("--no-auto-location", action="store_true", help="Skip Jev location recognition")
    parser.add_argument("--category", action="append", help="Optional photo category; repeat for alternatives")
    parser.add_argument("--limit", type=positive_int, default=20)
    parser.add_argument("--group-size", type=positive_int)
    parser.add_argument('--start-date', help='Trip start in YYYY-MM-DD; requires --end-date')
    parser.add_argument('--end-date', help='Trip end in YYYY-MM-DD; requires --start-date')
    parser.add_argument('--no-weather', action='store_true', help='Skip weather lookup for this request')
    parser.add_argument('--planning-mode', choices=('auto', 'off'), default='auto', help='Use TravelPlanner flow for itinerary requests, or retain RAG-only behavior')
    parser.add_argument("--retrieve-only", action="store_true",
                        help="Force retrieval and stop before answer generation; automatic location extraction still runs")
    parser.add_argument("--maps-only", action="store_true",
                        help="Return place metadata and Maps links without photo fields or local photo lookups")
    parser.add_argument("--show-context", action="store_true", help="Print the exact retrieved context sent to the LLM")
    parser.add_argument("--json", action="store_true", help="Print complete backend JSON; --chat emits one object per reply (JSON Lines)")
    parser.add_argument("--output", type=Path, help="Save retrieval, context, answer and progress as JSON")
    args = parser.parse_args(argv)
    if args.chat and args.output is not None:
        parser.error('Use --chat --json for response inspection; --output is for one-shot --query requests.')
    if args.chat and args.limit > 20:
        parser.error('Terminal chat uses the frontend retrieval limit: 1 to 20.')
    if args.chat and args.user_id is None and args.session_id is None:
        args.user_id, args.session_id = 'cli-local', 'terminal-chat'
    if bool(args.start_date) != bool(args.end_date):
        parser.error('Provide both --start-date and --end-date.')
    if bool(args.user_id) != bool(args.session_id):
        parser.error("Provide both --user-id and --session-id to use chat memory.")
    if args.user_query is None and not args.chat:
        try:
            print("User query: ", file=sys.stderr, end="", flush=True)
            args.user_query = input()
        except EOFError:
            parser.error("Provide --query when running without interactive input.")
    if args.user_query is not None and not args.user_query.strip():
        parser.error("The query must not be blank.")
    if args.output is not None:
        try:
            args.output = validate_output_path(args.output)
        except UnsafeInput as error:
            parser.error(str(error))

    def show_retrieval(response):
        # Model-loading output goes to stderr; educational output goes to stdout.
        with redirect_stdout(output_stream):
            print_retrieval(response)

    def show_context(context):
        with redirect_stdout(output_stream):
            print("\nRetrieved context sent to the LLM:")
            print(json.dumps(context, ensure_ascii=False, indent=2, allow_nan=False))

    output_stream = sys.stdout
    options = dict(
        image=args.image, route=args.route, location=args.location,
        categories=args.category, limit=args.limit, group_size=args.group_size,
        trip_start=args.start_date, trip_end=args.end_date, include_weather=False if args.no_weather else None,
        planning_mode=args.planning_mode,
        auto_location=not args.no_auto_location,
        response_mode=args.response_mode,
        maps_only=args.maps_only,
        retrieve_only=args.retrieve_only, on_progress=print_progress,
        on_retrieval=None if args.json or args.chat else show_retrieval,
        on_context=show_context if args.show_context and not args.json else None,
    )
    try:
        if args.chat:
            return terminal_chat(args, options)
        with redirect_stdout(sys.stderr):
            if args.session_id:
                from src.service.chat_service import ChatService
                with ChatService() as chat:
                    result = chat.chat(args.user_id, args.session_id, args.user_query, **options)
            else:
                result = run_pipeline(args.user_query, **options)
        serialized = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
        if args.output is not None:
            args.output = validate_output_path(args.output)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            # Exclusive creation prevents overwriting existing exports/project files.
            with args.output.open("x", encoding="utf-8") as output:
                output.write(serialized + "\n")
            print(f"Saved pipeline result: {args.output}", file=sys.stderr)
        if args.json:
            print(serialized)
        elif result["answer"] is not None:
            label = (localized_message(args.user_query, "Clarification needed", "Cần làm rõ địa điểm")
                     if result["pipeline_status"] == "needs_clarification" else
                     localized_message(args.user_query, "LLM answer", "Câu trả lời LLM")
                     if result["response_mode"] == "llm_only" else
                     localized_message(args.user_query, "RAG answer", "Câu trả lời RAG"))
            print(f"\n{label}:\n{result['answer']}")
            if result["context"] and result["context"]["sources"]:
                print("\n" + localized_message(args.user_query, "Sources:", "Nguồn:"))
                for source in result["context"]["sources"]:
                    print(f"[{source['source_id']}] {source.get('name') or source['entity_id']}"
                          f" | {source.get('url') or source['entity_id']}")
        return 0
    except Exception as error:
        # Provider/decoder errors may contain request data or credentials.
        print_pipeline_error(error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
