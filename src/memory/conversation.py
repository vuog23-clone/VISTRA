"""Bounded recall and structured interpretation before the existing pipeline runs."""

import json
import re

from src.generation.rag_generation import RAGGenerator
from src.prompts.memory import TURN_UNDERSTANDING_PROMPT
from src.schema.memory import ConversationMemory, TurnUnderstanding, memory_text
from src.security.guardrails import validate_text, UnsafeInput
from src.utils.redaction import CredentialRedactor


def build_memory_context(snapshot, records, *, message_chars=2000, max_chars=24000):
    if type(message_chars) is not int or not 100 <= message_chars <= 8000:
        raise ValueError("Memory message budget must be between 100 and 8000 characters.")
    if type(max_chars) is not int or not 4000 <= max_chars <= 32000:
        raise ValueError("Memory context budget must be between 4000 and 32000 characters.")
    messages = [message.model_copy(update={"content": message.content[:message_chars],
        "truncated": len(message.content) > message_chars}) for message in snapshot.recent_messages]
    # Trim before full context validation: large persisted records are still bounded
    # individually, but their combined serialized size can exceed the model budget.
    context = ConversationMemory.model_construct(summary=snapshot.summary,
        trip_state=snapshot.trip_state.model_copy(deep=True), recent_messages=messages,
        long_term=[record.model_copy(deep=True) for record in records],
        itinerary_available=snapshot.trip_state.itinerary is not None)
    while len(json.dumps(context.model_dump(), ensure_ascii=False)) > max_chars:
        if len(context.recent_messages) > 1:
            context.recent_messages.pop(0)
        elif any(record.kind == "trip" for record in context.long_term):
            index = next(i for i, record in reversed(list(enumerate(context.long_term))) if record.kind == "trip")
            context.long_term.pop(index)
        elif context.long_term:
            context.long_term.pop()
        elif context.summary:
            context.summary = ""
        elif context.recent_messages:
            context.recent_messages.pop(0)
        elif context.trip_state.itinerary is not None:
            # Keep the canonical plan in PostgreSQL. Large plans must not make
            # every subsequent turn fail before interpretation; the planner
            # receives the full previous plan separately from ChatService.
            context.trip_state.itinerary = None
        else:
            raise ValueError("Active trip state exceeds the memory context budget.")
    return ConversationMemory.model_validate(context.model_dump())


def checked_understanding(value, user_query):
    understanding = TurnUnderstanding.model_validate(value)
    # A blank model rewrite is unnecessary for a standalone memory request.
    # Keep the validated user's original text rather than losing the turn.
    if not understanding.standalone_query.strip():
        understanding.standalone_query = user_query[:4000]
    understanding.standalone_query = validate_text(understanding.standalone_query, "resolved query",
                                                  max_chars=4000, check_intent=True)
    if understanding.summary:
        understanding.summary = memory_text(understanding.summary, maximum=2000)
    seen = set()
    for change in understanding.preference_changes:
        if change.key in seen or change.source_quote not in user_query:
            raise ValueError("Memory changes must quote the current user message and use unique keys.")
        seen.add(change.key)
        # Reject common trip-scoped qualifiers even if the model misclassified them.
        start = user_query.find(change.source_quote)
        # Include a qualifier before the quote in the same sentence, so quoting
        # just "I prefer ..." cannot strip an earlier "For this trip ...".
        sentence_start = max(user_query.rfind(token, 0, start) for token in (".", "!", "?", "\n")) + 1
        scoped_quote = user_query[sentence_start:start + len(change.source_quote)]
        if change.operation == "set" and re.search(
                r"\b(?:this trip|this time|today|for now|chuyến này|chuyen nay|lần này|lan nay|hôm nay|hom nay)\b",
                scoped_quote, re.I):
            raise ValueError("Temporary trip requirements cannot become durable preferences.")
    return understanding


class ConversationInterpreter(RAGGenerator):
    def understand(self, user_query, memory):
        memory = ConversationMemory.model_validate(memory)
        from src.service.weather_service import reference_today, normalized
        payload = {"latest_user_message": user_query, "reference_date": reference_today().isoformat(),
                   "reference_timezone": "Asia/Bangkok", "conversation_memory": memory.model_dump()}
        for attempt in range(2):
            completion = self.client.responses.parse(model=self.model_name,
                instructions=TURN_UNDERSTANDING_PROMPT,
                input=json.dumps(CredentialRedactor().data(payload), ensure_ascii=False, allow_nan=False),
                text_format=TurnUnderstanding,
                reasoning={"effort": self.config["openai_reasoning_effort"]},
                max_output_tokens=min(3000, self.config["generation_max_tokens"]), store=False)
            if completion.status != "completed" or completion.output_parsed is None:
                raise RuntimeError("Conversation interpretation did not complete; no memory was changed.")
            value = TurnUnderstanding.model_validate(completion.output_parsed)
            durable = re.search(r'\b(remember|durable|always|usually|in general|across trips|ghi nho|luon|noi chung)\b',
                                normalized(user_query))
            if value.action in ('create_itinerary', 'revise_itinerary') and not durable:
                # Trip constraints are session state. A model-proposed preference
                # update cannot turn them into permanent personal preferences.
                value.preference_changes = []
            try:
                return checked_understanding(value, user_query)
            except (ValueError, UnsafeInput) as error:
                if attempt:
                    raise
                payload['repair_feedback'] = {'validation_error': str(error),
                    'rejected_understanding': value.model_dump(),
                    'instruction': 'Correct the interpretation. Preference changes must quote the latest message exactly, '
                                   'use unique keys, and never copy preferences from older memory or temporary trip requirements. '
                                   'Use an empty preference_changes list when no durable update was requested.'}
