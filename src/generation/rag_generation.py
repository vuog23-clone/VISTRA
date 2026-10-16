"""Generate and review an answer from bounded retrieved evidence."""

import json
import logging
import os
import re
from dataclasses import asdict, dataclass

from dotenv import load_dotenv

from src.prompts.rag import RAG_PROMPT, GROUNDING_REVIEW_PROMPT
from src.schema.rag import GroundingReview
from src.generation.place_context import google_place_context
from src.generation.vision_inputs import prepare_vision_inputs, multimodal_input
from src.utils.language import localized_message
from src.utils.redaction import CredentialRedactor
from src.utils.utils import PROJECT_ROOT, load_config
from src.security.guardrails import validate_text, bounded_int, UnsafeInput
from src.security.context import prepare_context
from src.retrieval.image_filters import bound_photo_url, image_matches_filters
from src.generation.response_contract import (resolve_response_contract, response_contract_issues,
                                               contract_limitation)


LOGGER = logging.getLogger('vistra.grounding')


@dataclass(frozen=True)
class GroundingCheck:
    status: str  # supported, unsupported, or unavailable
    issues: list[str]


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
    results = [result for result in results if result.source == 'text' or image_matches_filters(result,
        categories=response.requested_categories,excluded=response.excluded_categories,place_ids=response.requested_place_ids,
        excluded_place_ids=response.excluded_place_ids)]
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
            photo_url = bound_photo_url(result) if not response.maps_only else None
            if photo_url:
                source["photo"] = {"url": photo_url, "input_status": "pending"}
            source["distance_km"] = result.distance_km
            source["metadata_source"] = "Indexed Google Maps place metadata; freshness is unknown."
            visual_input = bool(has_image)
            source["retrieval_basis"] = "image_similarity" if visual_input else "text_to_image_similarity"
            source["photo_match_note"] = ("Visual similarity only; the uploaded image's identity is unverified."
                if source["retrieval_basis"] == "image_similarity" else
                "Cover photos ranked by text similarity within eligible indexed place IDs; similarity alone does not establish place identity.")
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
        self.last_contract = None
        self.last_vision = None
        self.last_context = None

    def generate(self, user_query, context, *, conversation_memory=None):
        user_query = validate_text(user_query, check_intent=True)
        if conversation_memory is not None:
            from src.schema.memory import ConversationMemory
            conversation_memory = ConversationMemory.model_validate(conversation_memory).model_dump()
        context = prepare_context(context)
        self.last_grounding = None
        self.last_contract = None
        self.last_vision = None
        self.last_context = None
        if not context.get("sources") and not (context.get('weather') or {}).get('days'):
            raise ValueError("RAG generation requires retrieved sources.")
        images, self.last_vision = prepare_vision_inputs(context, self.config, fetcher=self._photo_fetcher)
        self.last_context = context
        draft = self._draft(user_query, context, images=images, conversation_memory=conversation_memory)
        check = self._check_grounding(user_query, context, draft, images=images,
                                      conversation_memory=conversation_memory)
        attempts = [{'stage': 'draft', 'status': check.status, 'issues': check.issues}]
        if check.status == 'supported':
            self.last_grounding = {"status": "checked", "issues": []}
            return draft
        # Repair actual draft violations once. An unavailable reviewer supplies
        # no corrective feedback, so do not regenerate the same unverified answer.
        if check.status == 'unsupported':
            initial_issues = check.issues
            try:
                repaired = self._draft(user_query, context, images=images,
                    feedback={"previous_draft": draft, "issues": initial_issues}, conversation_memory=conversation_memory)
                check = self._check_grounding(user_query, context, repaired, images=images,
                                              conversation_memory=conversation_memory)
            except Exception:
                check = GroundingCheck('unavailable', ['The answer repair did not complete.'])
                prior = self.last_contract or {}
                self.last_contract = {**prior, 'status': 'unavailable', 'issues': check.issues,
                    'attempts': [*prior.get('attempts', []),
                                 {'stage': 'repair', 'status': 'unavailable', 'issues': check.issues}]}
            attempts.append({'stage': 'repair', 'status': check.status, 'issues': check.issues})
            if check.status == 'supported':
                self.last_grounding = {"status": "repaired", "issues": initial_issues, 'attempts': attempts}
                return repaired
        contract_failed = (self.last_contract or {}).get('status') == 'failed'
        status = ('contract_failed' if contract_failed else
                  'verification_unavailable' if check.status == 'unavailable' else 'unverified')
        self.last_grounding = {'status': status, 'issues': check.issues, 'attempts': attempts}
        LOGGER.warning('Answer verification %s: %s', status,
                       CredentialRedactor().text('; '.join(check.issues))[:2000])
        contract = resolve_response_contract(user_query, conversation_memory)
        if contract_failed:
            return contract_limitation(contract)
        # Use the same resolved language for deterministic limitations as drafts.
        return self._unverified_answer('Trả lời bằng tiếng Việt.' if contract.language == 'vi' else 'Answer in English.',
                                       verification_unavailable=check.status == 'unavailable')

    def _draft(self, user_query, context, *, feedback=None, images=(), conversation_memory=None):
        redactor = CredentialRedactor()
        # The LLM receives evidence, not the progress log or raw database payloads.
        completion = self._text_completion(
            model=self.model_name,
            instructions=RAG_PROMPT,
            input=multimodal_input(json.dumps(redactor.data({"question": user_query, "retrieved_context": context,
                                           "response_contract": resolve_response_contract(user_query, conversation_memory).payload(),
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
        answer = completion.output_text.strip()
        allowed = {source['source_id'] for source in context['sources']}
        if (context.get('weather') or {}).get('days'):
            allowed.add('W1')
        # Normalize a known source's section annotation before semantic review.
        # This changes citation syntax only; it cannot add a source or establish
        # claim support. Unknown IDs remain invalid and still fail validation.
        return re.sub(r'\[([SW]\d+)(?::[^\]\n]{1,120}|\s+)\]',
            lambda match: f'[{match[1]}]' if match[1] in allowed else match[0], answer)

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

    def _check_grounding(self, user_query, context, answer, *, images=(), conversation_memory=None):
        contract = resolve_response_contract(user_query, conversation_memory)
        issues = response_contract_issues(answer, context, contract)
        previous = (self.last_contract or {}).get('attempts', [])
        attempts = [*previous, {'stage': 'repair' if previous else 'draft',
                               'status': 'failed' if issues else 'passed', 'issues': issues}]
        self.last_contract = {'status': 'failed' if issues else
                              'repaired' if any(item['status'] == 'failed' for item in previous) else 'checked',
                              'requirements': contract.payload(), 'issues': issues, 'attempts': attempts}
        if issues:
            return GroundingCheck('unsupported', issues)
        # A truthful limitation can be citation-free. The semantic reviewer must
        # still reject unsupported facts and uncited factual claims.
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
                return GroundingCheck('unavailable', ['The grounding check did not complete.'])
            review = GroundingReview.model_validate(completion.output_parsed)
            for issue in review.issues:
                validate_text(issue, "grounding issue", max_chars=1000)
            issues = review.issues or ([] if review.supported else ['The draft contains unsupported claims.'])
            return GroundingCheck('unsupported' if issues else 'supported', issues)
        except Exception as error:
            LOGGER.warning('Grounding review unavailable (%s)', type(error).__name__)
            return GroundingCheck('unavailable', ['The grounding check is unavailable.'])

    @staticmethod
    def _unverified_answer(user_query, *, verification_unavailable):
        # An unchecked draft and a raw evidence inventory are both unsuitable
        # substitutes for answering the question. Preserve the failure's meaning.
        if verification_unavailable:
            return localized_message(user_query,
                "I couldn’t finish verifying an answer against the retrieved sources. Please try again.",
                "Mình chưa hoàn tất việc kiểm chứng câu trả lời dựa trên các nguồn đã truy xuất. Bạn vui lòng thử lại.")
        return localized_message(user_query,
            "I couldn’t produce a sufficiently supported answer to your question from the retrieved sources. "
            "Please narrow the question or provide relevant source information.",
            "Mình chưa thể đưa ra câu trả lời có đủ căn cứ từ các nguồn đã truy xuất. "
            "Bạn có thể hỏi cụ thể hơn hoặc cung cấp thông tin từ nguồn phù hợp.")

    def close(self):
        if self._owns_client:
            self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
