"""Generate and review an answer from bounded retrieved evidence."""

import json
import os
import re
from dataclasses import asdict

from dotenv import load_dotenv

from src.prompts.rag import RAG_PROMPT, GROUNDING_REVIEW_PROMPT
from src.schema.rag import GroundingReview
from src.generation.place_context import google_place_context
from src.generation.vision_inputs import prepare_vision_inputs, multimodal_input
from src.utils.language import query_language
from src.utils.redaction import CredentialRedactor
from src.utils.utils import PROJECT_ROOT, load_config
from src.security.guardrails import validate_text, bounded_int, UnsafeInput
from src.security.context import prepare_context, answer_safety_issues


def build_rag_context(response, config=None, *, has_image=None):
    """Number sources and retain bounded evidence, excluding vectors/local paths."""
    config = load_config() if config is None else config
    keys = ("generation_context_results", "generation_context_hits", "generation_excerpt_chars")
    for key, maximum in zip(keys, (10, 10, 4000)):
        bounded_int(config.get(key), key, maximum)
    source_limit, hit_limit, excerpt_limit = (config[key] for key in keys)

    def bounded(value):
        return value[:excerpt_limit] if isinstance(value, str) else value

    results = [*response.results, *response.related_results]
    if response.route == "hybrid":
        # Alternate sources: a budget of at least two retains both when available.
        results = []
        for index in range(max(len(response.results), len(response.related_results))):
            for branch in (response.results, response.related_results):
                if index < len(branch):
                    results.append(branch[index])
    sources = []
    for result in results[:source_limit]:
        source = {
            "source_id": f"S{len(sources) + 1}",
            "entity_id": bounded(result.entity_id),
            "source": result.source,
            "name": bounded(result.name),
            "url": bounded(result.url),
            "latitude": result.latitude,
            "longitude": result.longitude,
            "missing_metadata": result.missing_metadata,
        }
        if result.source == "text":
            excerpts, seen = [], set()
            for hit in [result, *result.evidence]:
                text = hit.payload.get("text")
                if hit.source != "text" or not isinstance(text, str) or not text.strip() or text in seen:
                    continue
                seen.add(text)
                excerpts.append({"section": bounded(hit.payload.get("section")), "text": text[:excerpt_limit]})
                if len(excerpts) >= hit_limit:
                    break
            source["excerpts"] = excerpts
        else:
            source["place_metadata"] = google_place_context(result, excerpt_limit)
            if not response.maps_only and isinstance(result.image, dict) and result.image.get("image_url"):
                source["photo"] = {"url": result.image["image_url"], "input_status": "pending"}
            source["distance_km"] = result.distance_km
            source["metadata_source"] = "Indexed Google Maps place metadata; freshness is unknown."
            visual_input = bool(has_image)
            source["retrieval_basis"] = "image_similarity" if visual_input else "text_to_image_similarity"
            source["photo_match_note"] = ("Visual similarity only; the uploaded image's identity is unverified."
                if source["retrieval_basis"] == "image_similarity" else
                "Similarity between the query text and indexed cover photos; not an exact place-name lookup.")
            if result.metadata.get('retrieval_method') == 'metadata_lookup':
                source['retrieval_basis'] = 'metadata_lookup'
                source['photo_match_note'] = 'Place selected by indexed city and category metadata; no image-similarity search was performed.'
        sources.append(source)
    return prepare_context({
        "route": response.route,
        "location": response.location,
        "location_status": response.location_status,
        "retrieval_warnings": response.retrieval_warnings,
        "branch_status": response.branch_status,
        "image_reranking": asdict(response.image_reranking) if response.image_reranking else None,
        "sources": sources,
    })


class RAGGenerator:
    def __init__(self, *, config=None, client=None, photo_fetcher=None):
        self.config = load_config() if config is None else config
        self.model_name = self.config["openai_model"]
        bounded_int(self.config["generation_max_tokens"], "generation_max_tokens", 8000)
        timeout = self.config["generation_timeout_seconds"]
        if type(timeout) not in (int, float) or not 0 < timeout <= 120:
            raise UnsafeInput("invalid_timeout", "Generation timeout must be between 0 and 120 seconds.")
        self._owns_client = client is None
        load_dotenv(PROJECT_ROOT / ".env", override=False)
        if client is None:
            api_key = os.getenv("OPENAI_API_KEY")
            if not api_key:
                raise ValueError("Set OPENAI_API_KEY in .env. To skip answer generation, use "
                                 "--retrieve-only.")
            from openai import OpenAI
            import httpx
            # Ignore OPENAI_BASE_URL and proxy environment variables: credentials
            # must not follow environment-controlled endpoints or redirects.
            transport = httpx.Client(follow_redirects=False, trust_env=False)
            try:
                client = OpenAI(api_key=api_key, base_url="https://api.openai.com/v1",
                    http_client=transport, timeout=timeout, max_retries=1)
            except Exception:
                transport.close()
                raise
        self.client = client
        self._photo_fetcher = photo_fetcher
        self.last_grounding = None
        self.last_vision = None
        self.last_context = None

    def generate(self, user_query, context, *, conversation_memory=None):
        user_query = validate_text(user_query, check_intent=True)
        if conversation_memory is not None:
            from src.schema.memory import ConversationMemory
            conversation_memory = ConversationMemory.model_validate(conversation_memory).model_dump()
        context = prepare_context(context)
        self.last_grounding = None
        self.last_vision = None
        self.last_context = None
        if not context.get("sources") and not (context.get('weather') or {}).get('days'):
            raise ValueError("RAG generation requires retrieved sources.")
        images, self.last_vision = prepare_vision_inputs(context, self.config, fetcher=self._photo_fetcher)
        self.last_context = context
        draft = self._draft(user_query, context, images=images, conversation_memory=conversation_memory)
        issues = self._grounding_issues(user_query, context, draft, images=images,
                                       conversation_memory=conversation_memory)
        if not issues:
            self.last_grounding = {"status": "checked", "issues": []}
            return draft
        # One repair keeps API work bounded. Persistent failures return actual
        # source facts, never an unchecked model answer.
        try:
            repaired = self._draft(user_query, context, images=images,
                feedback={"previous_draft": draft, "issues": issues}, conversation_memory=conversation_memory)
            remaining = self._grounding_issues(user_query, context, repaired, images=images,
                                               conversation_memory=conversation_memory)
        except Exception:
            remaining = ['The answer repair did not complete.']
        if not remaining:
            self.last_grounding = {"status": "repaired", "issues": issues}
            return repaired
        self.last_grounding = {"status": "metadata_fallback", "issues": remaining}
        fallback = self._source_facts(context, user_query=user_query)
        if answer_safety_issues(fallback, context):
            from src.utils.language import localized_message
            return localized_message(user_query,
                "The retrieved evidence could not be safely presented.",
                "Không thể trình bày nguồn truy xuất một cách an toàn.")
        return fallback

    def _draft(self, user_query, context, *, feedback=None, images=(), conversation_memory=None):
        redactor = CredentialRedactor()
        # The LLM receives evidence, not the progress log or raw database payloads.
        completion = self._text_completion(
            model=self.model_name,
            instructions=RAG_PROMPT,
            input=multimodal_input(json.dumps(redactor.data({"question": user_query, "retrieved_context": context,
                                           **({"conversation_memory": conversation_memory} if conversation_memory is not None else {}),
                                           **({"repair_feedback": feedback} if feedback else {})}),
                             ensure_ascii=False, allow_nan=False), images),
            reasoning={"effort": self.config["openai_reasoning_effort"]},
            max_output_tokens=self.config["generation_max_tokens"],
            store=False,
        )
        if completion.status != "completed":
            raise RuntimeError("LLM response was incomplete; check the output token limit or retry.")
        if not completion.output_text or not completion.output_text.strip():
            raise RuntimeError("LLM returned no answer text.")
        # Keep raw credentials visible to the deterministic safety check, then
        # redact only an approved answer at the output boundary.
        return completion.output_text.strip()

    def _text_completion(self, **options):
        from src.generation.streaming import current_answer_stream
        preview = current_answer_stream()
        if preview is None:
            return self.client.responses.create(**options)
        preview.begin()
        with self.client.responses.stream(**options) as stream:
            for event in stream:
                if event.type == 'response.output_text.delta':
                    preview.feed(event.delta)
            completion = stream.get_final_response()
        if completion.status == 'completed':
            preview.finish()
        return completion

    def _grounding_issues(self, user_query, context, answer, *, images=(), conversation_memory=None):
        safety_issues = answer_safety_issues(answer, context)
        if safety_issues:
            return safety_issues
        allowed = {source['source_id'] for source in context['sources']}
        if (context.get('weather') or {}).get('days'):
            allowed.add('W1')
        citations = set(re.findall(r'\[([SW]\d+)\]', answer))
        if not citations:
            return ['Factual claims need citations to the supplied source IDs.']
        if citations - allowed:
            return ['The answer cites source IDs that were not retrieved.']
        try:
            completion = self.client.responses.parse(
                model=self.model_name, instructions=GROUNDING_REVIEW_PROMPT,
                input=multimodal_input(json.dumps(CredentialRedactor().data({'question': user_query,
                    'retrieved_context': context, 'draft': answer,
                    **({'conversation_memory': conversation_memory} if conversation_memory is not None else {})}),
                    ensure_ascii=False, allow_nan=False), images),
                text_format=GroundingReview,
                reasoning={'effort': self.config['openai_reasoning_effort']},
                max_output_tokens=min(2000, self.config['generation_max_tokens']), store=False)
            if completion.status != 'completed' or completion.output_parsed is None:
                return ['The grounding check did not complete.']
            review = GroundingReview.model_validate(completion.output_parsed)
            for issue in review.issues:
                validate_text(issue, "grounding issue", max_chars=1000)
            return review.issues or ([] if review.supported else ['The draft contains unsupported claims.'])
        except Exception:
            return ['The grounding check is unavailable.']

    @staticmethod
    def _source_facts(context, *, user_query=""):
        vietnamese = query_language(user_query) == "vi"
        def label(english, vietnamese_text):
            return vietnamese_text if vietnamese else english
        def literal(value):
            # Source strings remain quoted data, not active Markdown or HTML.
            value = " ".join(str(value).split())
            return re.sub(r"([\\`*_{}\[\]<>#!|])", r"\\\1", value)
        blocks = [label("## ℹ️ Indexed source facts", "## ℹ️ Thông tin từ nguồn đã lập chỉ mục"),
                  label("The draft could not be confirmed against the sources. Here are the indexed facts.",
                        "Chưa xác nhận được câu trả lời dựa trên nguồn. Dưới đây là thông tin trong dữ liệu.")]
        visual = False
        for source in context['sources']:
            citation = f"[{source['source_id']}]"
            title = label('Retrieved candidate', 'Ứng viên được truy xuất') if source['source'] == 'image' else label('Source', 'Nguồn')
            blocks.append(f"### 📍 {title}: {literal(source.get('name') or source['entity_id'])} {citation}")
            if source['source'] == 'image':
                facts = source.get('place_metadata') or {}
                for key, english, vietnamese_text in (
                    ('address', 'Address', 'Địa chỉ'), ('province', 'Province', 'Tỉnh/thành phố'),
                    ('category', 'Category', 'Loại hình'), ('rating', 'Indexed rating', 'Điểm đánh giá trong dữ liệu'),
                    ('review_count', 'Review count', 'Số lượt đánh giá'),
                ):
                    if facts.get(key) is not None:
                        blocks.append(f"- **{label(english, vietnamese_text)}:** {literal(facts[key])} {citation}")
                price = facts.get('price_range') or {}
                for key, english, vietnamese_text in (('min', 'Minimum price', 'Giá tối thiểu'),
                                                      ('max', 'Maximum price', 'Giá tối đa')):
                    if price.get(key) is not None:
                        value = f"{price[key]} {price.get('currency') or ''}".strip()
                        blocks.append(f"- **{label(english, vietnamese_text)}:** {literal(value)} {citation}")
                hours = facts.get('opening_hours') or {}
                days = zip(('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'),
                           ('Thứ Hai', 'Thứ Ba', 'Thứ Tư', 'Thứ Năm', 'Thứ Sáu', 'Thứ Bảy', 'Chủ nhật'))
                for day, vietnamese_day in days:
                    for window in hours.get(day) or []:
                        unknown = label('unknown', 'chưa rõ')
                        value = f"{window.get('open') or unknown}–{window.get('close') or unknown}"
                        blocks.append(f"- **🕒 {label(day.title(), vietnamese_day)}:** {literal(value)} {citation}")
                if hours.get('timezone'):
                    blocks.append(f"- **{label('Time zone', 'Múi giờ')}:** {literal(hours['timezone'])} {citation}")
            else:
                blocks.append(label('Original source excerpt:', 'Trích đoạn nguồn (giữ ngôn ngữ gốc):'))
                for excerpt in source.get('excerpts', [])[:1]:
                    text = excerpt.get('text', '')
                    if text:
                        shortened = text[:650] + ('…' if len(text) > 650 else '')
                        blocks.append(f"> {literal(shortened)} {citation}")
            visual |= source.get('retrieval_basis') == 'image_similarity'
        blocks.append(label('Unlisted values and hours are unknown; indexed information may be outdated.',
                            'Giá trị và giờ hoạt động không được liệt kê là chưa rõ; dữ liệu có thể đã cũ.'))
        weather = context.get('weather') or {}
        if weather:
            blocks.append(label('### Weather for the trip', '### Thời tiết cho chuyến đi'))
            blocks.append(literal(weather['note']))
            for day in weather.get('days', []):
                values = [day['date'], day['condition']]
                for key, english, vi, unit in (
                    ('temperature_min_c', 'Min', 'Thấp nhất', '°C'), ('temperature_max_c', 'Max', 'Cao nhất', '°C'),
                    ('precipitation_probability_pct', 'Rain probability', 'Khả năng mưa', '%'),
                    ('wind_max_kmh', 'Max wind', 'Gió tối đa', 'km/h')):
                    if day.get(key) is not None:
                        values.append(f"{label(english, vi)}: {day[key]} {unit}")
                blocks.append('- ' + literal(' · '.join(values)) + ' [W1]')
            if weather.get('days'):
                blocks.append(literal(f"Open-Meteo · {weather['timezone']} · {weather['fetched_at']}") + ' [W1]')
        if visual:
            blocks.append(label("Visual similarity does not confirm the uploaded image's identity.",
                                "Sự tương đồng hình ảnh không xác nhận danh tính địa điểm trong ảnh đã tải lên."))
        return CredentialRedactor().text('\n\n'.join(blocks))

    def close(self):
        if self._owns_client:
            self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
