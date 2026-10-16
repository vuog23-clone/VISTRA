"""Bounded recall and structured interpretation before the existing pipeline runs."""

import json
import re

from src.generation.rag_generation import RAGGenerator
from src.prompts.memory import TURN_UNDERSTANDING_PROMPT
from src.schema.memory import ConversationMemory, TurnUnderstanding, PreferenceChange, memory_text
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


def _unquoted(query):
    return re.sub(r'```[\s\S]*?```|`[^`\n]*`|"[^"\n]*"|“[^”\n]*”|‘[^’\n]*’|(?<!\w)\'[^\'\n]+\'(?!\w)',
                  lambda match: ' ' * len(match[0]), query)


def _language_preference(query):
    # Infer only explicit durable commands; quoted translation material and
    # one-turn language overrides never establish a stored preference.
    visible = _unquoted(query)
    changes = []
    patterns = {
        'set': r'\b(?:remember|always|from now on|ghi nhớ|từ giờ|luôn)\b[^.!?;\n]{0,60}?'
               r'\b(?:reply|respond|answer|speak|language|trả lời|ngôn ngữ)\b[^.!?;\n]{0,35}?'
               r'\b(?:English|Vietnamese|tiếng Anh|tiếng Việt)\b',
        'delete': r'\b(?:forget|remove|delete|clear|quên|xóa)\b[^.!?;\n]{0,40}?'
                  r'\b(?:language(?: preference)?|ngôn ngữ(?: trả lời)?)\b',
    }
    for operation, pattern in patterns.items():
        for match in re.finditer(pattern, visible, re.I):
            if re.search(r"\b(?:not|don't|do not|never|không|đừng)\s*$", visible[max(0, match.start()-20):match.start()], re.I):
                continue
            start = max(visible.rfind(mark, 0, match.start()) for mark in '.!?;\n') + 1
            end = min((position for mark in '.!?;\n' if (position := visible.find(mark, match.end())) >= 0), default=len(visible))
            if operation == 'set' and re.search(r'\b(?:this trip|this time|today|chuyến này|lần này|hôm nay)\b', visible[start:end], re.I):
                continue
            changes.append((match.start(), PreferenceChange(operation=operation, key='language', source_quote=query[match.start():match.end()])))
    return max(changes, key=lambda change: change[0])[1] if changes else None


def _require_explicit_operations(understanding, query):
    # A second question must not silently cancel an explicit preference update.
    # Require structured operations, rather than inventing a preference value.
    visible = _unquoted(query)
    if re.search(r"\b(?:do not|don't|never|không|đừng)\s+(?:remember|forget|save|remove|ghi nhớ|quên|lưu|xóa)\b", visible, re.I):
        return  # Leave negated/contradictory instructions to interpretation.
    for clause in re.split(r'[.!?;\n]', visible):
        if re.search(r'\b(?:this trip|this time|today|for now|chuyến này|lần này|hôm nay)\b', clause, re.I):
            continue
        set_command = re.match(r'\s*(?:(?:please|can you|could you|would you|hãy|vui lòng)\s+)*'
                              r'(?:remember|save|ghi nhớ|lưu)\b', clause, re.I)
        personal_preference = re.search(r'\b(?:I|we)\s+(?:(?:usually|generally|always)\s+)?'
                                       r'(?:prefer|like|enjoy)\b|\b(?:tôi|mình|chúng tôi)\s+(?:thích|ưu tiên)\b', clause, re.I)
        delete_command = re.match(r'\s*(?:(?:please|can you|could you|would you|hãy|vui lòng)\s+)*'
                                 r'(?:forget|remove|delete|clear|quên|xóa)\b', clause, re.I)
        preference_name = re.search(r'\b(?:preference|diet|interests|language|sở thích|ngôn ngữ)\b', clause, re.I)
        operation = 'set' if set_command and personal_preference else 'delete' if delete_command and preference_name else None
        if operation and not any(change.operation == operation and (quote := _unquoted(change.source_quote).strip()) and
                (quote in clause or clause.strip() in quote)
                for change in understanding.preference_changes):
            raise ValueError('Represent each explicit remember/forget preference command in preference_changes, '
                             'even when the message also asks a question; use an exact quote from that command.')


def _trip_budget_quotes(query):
    """Identify budget facts/corrections that cannot authorize lasting preferences.

    Inspect each clause separately so an independent remember/forget command in
    the same message survives. Decimal/grouping dots are not sentence boundaries.
    A bounded vocabulary keeps this guard out of genuine personal preferences.
    """
    from src.service.weather_service import normalized
    allowed = set(('please change update set correct increase decrease raise lower make '
        'it that this the my our trip s budget amount to at total per person each now instead only '
        'vui long hay doi sua cap nhat tang giam thanh tong cong len xuong moi nguoi '
        'ngan sach chuyen nay cua toi minh la chi '
        'vnd usd eur gbp aud cad sgd jpy krw thb dong dollar dollars euro euros '
        'million thousand trieu nghin ngan').split())
    clauses = re.split(r'(?<!\d)\.|\.(?!\d)|[!?;\n]', _unquoted(query))
    trip_facts = []
    for clause in clauses:
        text = normalized(clause).strip()
        correction = (re.match(r'^(?:(?:please|vui long|hay)\s+)*(?:change|update|set|correct|increase|'
                     r'decrease|raise|lower|make|doi|sua|cap nhat|tang|giam)\b', text)
                and set(re.findall(r'[a-z]+', text)) <= allowed)
        statement = (re.match(r'^(?:(?:my|our|the|this)\b.{0,70}\bbudget\b|(?:trip )?budget\b|ngan sach\b)', text)
                and not re.search(r'\b(?:remember|save|forget|remove|delete|clear|always|usually|usual|'
                                  r'generally|prefer|like|enjoy|preference|across trips|ghi nho|luu|quen|xoa|'
                                  r'luon|thuong|noi chung|thich|uu tien|so thich)\b', text))
        if ((correction or statement) and re.search(r'\d', text)
                and re.search(r'\b(?:budget|amount|ngan sach|vnd|usd|eur|gbp|aud|cad|sgd|jpy|krw|thb|dong|dollars?|euros?)\b|[$€£¥]', text)):
            trip_facts.append(text)
    return trip_facts


def checked_understanding(value, user_query):
    understanding = TurnUnderstanding.model_validate(value)
    language = _language_preference(user_query)
    if language is not None:
        understanding.preference_changes = [change for change in understanding.preference_changes if change.key != 'language'] + [language]
    _require_explicit_operations(understanding, user_query)
    # A blank model rewrite is unnecessary for a standalone memory request.
    # Keep the validated user's original text rather than losing the turn.
    if not understanding.standalone_query.strip():
        understanding.standalone_query = user_query[:4000]
    understanding.standalone_query = validate_text(understanding.standalone_query, "resolved query",
                                                  max_chars=4000, check_intent=True)
    if understanding.summary:
        understanding.summary = memory_text(understanding.summary, maximum=2000)
    seen, accepted = set(), []
    budget_facts = _trip_budget_quotes(user_query)
    for change in understanding.preference_changes:
        if change.key in seen or change.source_quote not in user_query:
            raise ValueError("Memory changes must quote the current user message and use unique keys.")
        seen.add(change.key)
        from src.service.weather_service import normalized
        quote = normalized(_unquoted(change.source_quote)).strip().strip('.!?;')
        if quote and any(quote in clause for clause in budget_facts):
            # This also blocks a model-proposed deletion: a numeric trip update
            # cannot authorize changing any durable preference key.
            continue
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
        if (change.operation == 'set' and re.search(
                r'\b(?:my|our|this|next|upcoming)\s+(?:\w+\s+){0,3}trip\s+(?:budget|dates?|starts?|ends?|is|to)\b|'
                r'\btrip\s+budget\b|\bchuyen di\s+cua\s+(?:toi|minh|chung toi)\b', normalized(scoped_quote))
                and re.search(r'\b(?:budget|trip|dates?|chuyen di|ngan sach)\b|\d', normalized(change.source_quote))):
            raise ValueError('Trip-specific destinations, dates and budgets belong in trip_state, not durable preferences.')
        accepted.append(change)
    understanding.preference_changes = accepted
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
                    'instruction': 'Fix the validation error. Durable changes need exact latest-message quotes and unique keys; '
                                   'exclude old/temporary preferences and return [] without an explicit durable update.'}
