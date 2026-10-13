"""Multi-turn wrapper around run_pipeline; completed turns commit atomically."""

from contextlib import ExitStack

from src.database.postgres_memory_store import PostgresMemoryStore
from src.memory.conversation import ConversationInterpreter, build_memory_context, checked_understanding
from src.schema.memory import MemoryRecord, TripState, checked_identity
from src.security.guardrails import validate_text, validate_request, UnsafeInput
from src.utils.redaction import CredentialRedactor
from src.utils.utils import load_config


class ChatService:
    def __init__(self, *, store=None, interpreter=None, pipeline=None, config=None):
        self.config = load_config() if config is None else config
        self.store, self.interpreter, self.pipeline = store, interpreter, pipeline
        self._resources = ExitStack()
        self._closed = False

    def _store(self):
        if self.store is None:
            store = self._resources.enter_context(PostgresMemoryStore())
            store.ensure_schema()
            self.store = store
        return self.store

    def chat(self, user_id, session_id, user_query, **pipeline_options):
        if self._closed:
            raise RuntimeError("ChatService is closed.")
        checked_identity(user_id, "user_id")
        checked_identity(session_id, "session_id")
        user_query = validate_text(user_query, check_intent=True)
        if any(key in pipeline_options for key in ('conversation_memory', 'routing_query', 'previous_itinerary')):
            raise ValueError("ChatService owns the memory and routing context.")
        validate_request(user_query, **{key: value for key, value in pipeline_options.items()
            if key in {"image", "route", "location", "categories", "limit", "group_size",
                       "retrieve_only", "auto_location", "maps_only"}})
        mode = pipeline_options.get("response_mode", "auto")
        if mode not in {"auto", "retrieval", "llm_only"}:
            raise UnsafeInput("invalid_response_mode", "Invalid response mode.")
        constrained = (pipeline_options.get("image") is not None or
            pipeline_options.get("route") not in (None, "auto") or
            pipeline_options.get("location") is not None or bool(pipeline_options.get("categories")) or
            pipeline_options.get("maps_only") or pipeline_options.get("retrieve_only"))
        if mode == "llm_only" and constrained:
            raise UnsafeInput("conflicting_response_mode", "Direct LLM mode conflicts with retrieval options.")
        store = self._store()
        snapshot = store.load_session(user_id, session_id,
            message_limit=self.config.get("memory_recent_messages", 8))
        records = store.recall(user_id, user_query,
            preference_limit=self.config.get("memory_preference_limit", 12),
            trip_limit=self.config.get("memory_trip_limit", 3))
        memory = build_memory_context(snapshot, records,
            message_chars=self.config.get("memory_message_chars", 2000),
            max_chars=self.config.get("memory_context_chars", 24000))
        if self.interpreter is None:
            self.interpreter = self._resources.enter_context(ConversationInterpreter(config=self.config))
        understanding = checked_understanding(self.interpreter.understand(user_query, memory), user_query)
        current_state = TripState.model_validate(understanding.trip_state.model_dump())
        old_plan = snapshot.trip_state.itinerary
        from src.resolver.location_resolver import LocationResolver
        def cities(state):
            return [LocationResolver._normalize(city) for city in
                    (state.destinations or ([state.destination] if state.destination else []))]
        old_cities, new_cities = cities(snapshot.trip_state), cities(current_state)
        same_trip = not old_cities or not new_cities or old_cities == new_cities
        if understanding.action == 'revise_itinerary' or (understanding.action != 'create_itinerary' and same_trip):
            current_state.itinerary = old_plan
        weather = understanding.include_weather
        if weather is None:
            weather = current_state.include_weather
        if weather is None and same_trip and understanding.action != 'create_itinerary':
            weather = snapshot.trip_state.include_weather
        current_state.include_weather = weather
        if weather is not None:
            pipeline_options.setdefault('include_weather', weather)
        if understanding.action in ('create_itinerary', 'revise_itinerary'):
            pipeline_options.setdefault('planning_action', understanding.action)
            from src.schema.planning import revision_days_from_text
            literal_days = revision_days_from_text(user_query) if understanding.action == 'revise_itinerary' else set()
            scoped_day = next(iter(literal_days)) if len(literal_days) == 1 else None
            pipeline_options.setdefault('revision_day', scoped_day if literal_days else understanding.revision_day)
            if mode == 'auto':
                pipeline_options['response_mode'] = 'retrieval'
        elif understanding.action is not None:
            pipeline_options.setdefault('planning_action', 'none')
        if mode == "auto" and understanding.intent == "memory" and not constrained:
            # Personal recall/update needs memory, not indexed venue evidence.
            pipeline_options["response_mode"] = "llm_only"
        # Never lose the literal current instruction behind a model rewrite.
        resolved = understanding.standalone_query
        routing_query = (resolved if user_query in resolved else
                         f"{resolved}\nLatest user request: {user_query}")
        if len(routing_query) > 8000:
            routing_query = user_query
        # Supply current interpreted state without turning this turn into past history.
        generation_memory = memory.model_copy(update={"trip_state": current_state})
        generation_memory.long_term = list(generation_memory.long_term)
        for change in understanding.preference_changes:
            generation_memory.long_term = [record for record in generation_memory.long_term
                if not (record.kind == "preference" and record.key == change.key)]
            if change.operation == "set":
                generation_memory.long_term.append(MemoryRecord(kind="preference", key=change.key,
                    content=change.source_quote, source_quote=change.source_quote))
        if self.pipeline is None:
            from main import run_pipeline
            self.pipeline = run_pipeline
        # Reapply the budget after current state/preferences have been updated.
        from src.schema.memory import SessionSnapshot
        generation_memory = build_memory_context(SessionSnapshot(summary=generation_memory.summary,
            trip_state=generation_memory.trip_state, recent_messages=generation_memory.recent_messages),
            generation_memory.long_term, message_chars=self.config.get("memory_message_chars", 2000),
            max_chars=self.config.get("memory_context_chars", 24000))
        if current_state.itinerary is not None and generation_memory.trip_state.itinerary is None:
            pipeline_options['previous_itinerary'] = current_state.itinerary.model_dump()
        result = self.pipeline(user_query, routing_query=routing_query,
            conversation_memory=generation_memory.model_dump(), **pipeline_options)
        answer = result.get("answer")
        # Retrieval-only is a read operation, not a chat turn.
        if pipeline_options.get("retrieve_only"):
            return {**result, "chat": {"user_id": user_id, "session_id": session_id,
                                       "version": snapshot.version, "memory_status": "read_only"}}
        if not isinstance(answer, str) or not answer.strip():
            raise RuntimeError("The turn returned no answer; no memory was changed.")
        # The default pipeline validates its answer; injected pipelines must do so too.
        from src.security.context import answer_safety_issues
        if answer_safety_issues(answer, result.get("context") or {"sources": []}):
            raise UnsafeInput("unsafe_answer", "The chat answer failed safety validation.")
        state = current_state.model_copy()
        if result.get('itinerary'):
            from src.schema.planning import Itinerary
            state.itinerary = Itinerary.model_validate(result['itinerary'])
        state.pending_clarification = (answer[:1000] if result.get("pipeline_status") == "needs_clarification" else None)
        state.pending_planning_action = ((result.get('planning') or {}).get('action')
            if state.pending_clarification else None)
        version = store.commit_turn(user_id, session_id, expected_version=snapshot.version,
            user_message=user_query, answer=answer, summary=understanding.summary, trip_state=state,
            preference_changes=understanding.preference_changes)
        result["chat"] = {"user_id": user_id, "session_id": session_id, "version": version,
            "memory_status": "saved", "recalled_messages": len(memory.recent_messages),
            "recalled_long_term": len(memory.long_term),
            "preference_changes": [{"operation": item.operation, "key": item.key}
                                   for item in understanding.preference_changes]}
        return CredentialRedactor().data(result)

    def close(self):
        self._resources.close()
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
