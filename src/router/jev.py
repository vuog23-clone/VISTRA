"""Select guide evidence alone or guide evidence with images."""

from time import perf_counter
import os
import re
import unicodedata

import httpx
from dotenv import load_dotenv

from src.schema.models import RetrievalRoute
from src.utils.utils import PROJECT_ROOT, load_config
from src.utils.progress import progress
from src.utils.retrieval_activity import tracked_tool, tool_metadata
from src.prompts.retrieval import RETRIEVAL_INTENT_PROMPT, RETRIEVAL_INTENT_CRITERIA
from src.security.guardrails import validate_text
from src.security.network import post_provider_json
from src.router.evidence_policy import scenic_evidence_policy


# Used only when Jev is disabled/unavailable. The normal path is semantic Jev
# classification, including negations and mixed evidence requests.
_VISUAL = re.compile(
    r"\b(?:show|find|display|send|get|give me|let me see|can i see|cho.{0,20}xem|tim|xem)\b.{0,100}"
    r"\b(?:photos?|pictures?|images?|photographs?|anh|hinh)\b|"
    r"\b(?:show|display|let me see|cho.{0,20}xem|xem)\b.{0,100}"
    r"\b(?:beach(?:es)?|mountains?|waterfalls?|landscapes?|scenery|sunsets?|views?|bai bien|nui|thac|phong canh)\b|"
    r"\b(?:similar (?:images?|photos?|places?)|(?:places?|images?|photos?) (?:like|similar to) this|"
    r"visual similarity|giong (?:anh|hinh)|look like this)\b")
_NO_VISUAL = re.compile(r"\b(?:no|without|not|exclude)\s+(?:photos?|pictures?|images?)\b|"
                        r"\b(?:do not|don't)\s+(?:show|include).{0,30}\b(?:photos?|pictures?|images?)\b|"
                        r"\bkhong.{0,20}\b(?:anh|hinh)\b|\b(?:show|xem).{0,20}\b(?:list|danh sach)\b")
_SCENIC_SELECTION = re.compile(
    r"\b(?:suggest|recommend|best|prettiest|beautiful|pretty|picturesque|scenic|"
    r"stunning|gorgeous|want|would love|would like|goi y|muon|dep)\b.{0,100}"
    r"\b(?:beach(?:es)?|mountains?|waterfalls?|landscapes?|views?|bai bien|nui|thac|phong canh)\b")


class JevRouter:
    def __init__(self, api_key: str | None = None, model_name: str | None = None, client=None, *, config=None):
        self.config = load_config() if config is None else config
        load_dotenv(PROJECT_ROOT / ".env", override=False)
        self.api_key = api_key if api_key is not None else (os.getenv("JEV_API_KEY") or os.getenv("TYPESAFE_API_KEY"))
        self.model_name = model_name or self.config["jev_model"]
        self.client = client
        self._owns_client = client is None
        self.last_decision = None

    @staticmethod
    def _fallback_intent(query, has_image):
        normalized = unicodedata.normalize("NFKD", (query or "").casefold()).replace("đ", "d")
        normalized = " ".join("".join(c for c in normalized if not unicodedata.combining(c)).split())
        visual = not _NO_VISUAL.search(normalized) and (
            has_image or bool(_VISUAL.search(normalized)) or bool(_SCENIC_SELECTION.search(normalized)))
        return "hybrid" if visual else "text_only"

    def route(self, query: str | None = None, *, has_image: bool = False,
              override: RetrievalRoute | None = None) -> RetrievalRoute:
        started = perf_counter()
        query = validate_text(query, check_intent=True)
        if type(has_image) is not bool:
            raise TypeError("has_image must be a boolean.")
        if override is not None and (not isinstance(override, str) or override not in ("text", "hybrid")):
            raise ValueError('route must be text or hybrid.')
        selected = override
        source = "override" if override else "jev"
        intent = None
        fallback_reason = None
        classifier_intent = None
        policy_intent = None
        policy_reason = None
        provider_source = source
        if selected is None:
            policy_intent, policy_reason = scenic_evidence_policy(query)
            if not self.config.get("jev_routing_enabled", True) or not self.api_key:
                fallback_reason = "disabled" if not self.config.get("jev_routing_enabled", True) else "missing_key"
            else:
                try:
                    if self.client is None:
                        self.client = httpx.Client(timeout=self.config["jev_timeout_seconds"], follow_redirects=False, trust_env=False)
                    intent = self._classify_intent(query, has_image=has_image)
                except (httpx.HTTPError, ValueError):
                    fallback_reason = "provider_unavailable"
            if fallback_reason:
                intent, source = self._fallback_intent(query, has_image), "fallback"
            classifier_intent, provider_source = intent, source
            if policy_intent is not None and intent != policy_intent:
                intent, source = policy_intent, "policy"
            selected = {"text_only": "text", "hybrid": "hybrid"}[intent]
        self.last_decision = {"route": selected, "intent": intent, "decision_source": source,
                              "fallback_reason": fallback_reason, "provider_source": provider_source,
                              "classifier_intent": classifier_intent, "policy_intent": policy_intent,
                              "policy_reason": policy_reason, "policy_applied": source == "policy"}
        progress("routing", "Retrieval route selected", selected,
                 **self.last_decision,
                 routing_seconds=round(perf_counter() - started, 4))
        return selected

    @tracked_tool("Jev / TypeSafe", "Classify retrieval intent")
    def _classify_intent(self, query, *, has_image=False):
        query = validate_text(query, check_intent=True)
        tool_metadata(execution="network")
        value, status = post_provider_json(self.client, self.config["jev_api_url"], timeout=self.config["jev_timeout_seconds"],
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model_name, "state": {"query": query, "has_image": has_image}, "questions": {"intent": {
                "type": "choice",
                "instructions": RETRIEVAL_INTENT_PROMPT,
                "criteria": RETRIEVAL_INTENT_CRITERIA}}})
        tool_metadata(http_status=status)
        answers = value.get("answers") if isinstance(value, dict) else None
        answer = answers.get("intent") if isinstance(answers, dict) else None
        choice = answer.get("choice") if isinstance(answer, dict) else None
        if not isinstance(choice, str) or choice not in ("text_only", "hybrid"):
            raise ValueError("Jev returned an invalid retrieval intent.")
        return choice

    def close(self):
        if self._owns_client and self.client is not None:
            self.client.close()
            self.client = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
