"""Jev gate: decide whether to retrieve evidence before doing destination work."""

import os
import unicodedata

import httpx
from dotenv import load_dotenv

from src.prompts.response import RESPONSE_MODE_PROMPT, RESPONSE_MODE_CRITERIA
from src.schema.response import ResponseDecision
from src.security.guardrails import UnsafeInput, validate_text
from src.security.network import post_provider_json
from src.utils.retrieval_activity import tracked_tool, tool_metadata
from src.utils.utils import PROJECT_ROOT, load_config


_CONVERSATION = {
    "who are you", "what are you", "what can you do", "how can you help me",
    "tell me about yourself", "hello", "hi", "hey", "thanks", "thank you", "goodbye",
    "xin chao", "chao ban", "ban la ai", "ban co the lam gi", "cam on", "tam biet",
}


class JevResponseRouter:
    def __init__(self, *, config=None, api_key=None, client=None):
        self.config = load_config() if config is None else config
        load_dotenv(PROJECT_ROOT / ".env", override=False)
        self.api_key = api_key if api_key is not None else (
            os.getenv("JEV_API_KEY") or os.getenv("TYPESAFE_API_KEY"))
        self.client = client
        self._owns_client = client is None

    @staticmethod
    def _fallback(query, reason):
        # Only whole, unambiguous conversational utterances bypass retrieval
        # during an outage. Everything else keeps the evidence-backed path.
        normalized = unicodedata.normalize("NFKD", query.casefold()).replace("đ", "d")
        normalized = " ".join("".join(char for char in normalized
            if not unicodedata.combining(char)).split()).rstrip("?!., ")
        return ResponseDecision(mode="llm_only" if normalized in _CONVERSATION else "retrieval",
                                decision_source="fallback", fallback_reason=reason)

    def route(self, query, *, has_image=False, retrieval_requested=False, override=None):
        query = validate_text(query, check_intent=True)
        if type(has_image) is not bool or type(retrieval_requested) is not bool:
            raise UnsafeInput("invalid_flag", "Response routing flags must be booleans.")
        if override is not None and (not isinstance(override, str) or override not in {"retrieval", "llm_only"}):
            raise UnsafeInput("invalid_response_mode", "Response mode must be retrieval or llm_only.")
        constrained = has_image or retrieval_requested
        if override == "llm_only" and constrained:
            raise UnsafeInput("conflicting_response_mode", "Direct LLM mode cannot be combined with retrieval options or an image.")
        if override is not None:
            return ResponseDecision(mode=override, decision_source="override")
        if constrained:
            return ResponseDecision(mode="retrieval", decision_source="constraint")
        if not self.config.get("jev_response_routing_enabled", True):
            return self._fallback(query, "disabled")
        if not self.api_key:
            return self._fallback(query, "missing_key")
        try:
            if self.client is None:
                self.client = httpx.Client(timeout=self.config["jev_timeout_seconds"],
                                           follow_redirects=False, trust_env=False)
            choice = self._classify(query)
            return ResponseDecision(mode=choice, decision_source="jev")
        except (httpx.HTTPError, ValueError):
            return self._fallback(query, "provider_unavailable")

    @tracked_tool("Jev / TypeSafe", "Decide whether travel retrieval is needed")
    def _classify(self, query):
        tool_metadata(execution="network")
        value, status = post_provider_json(self.client, self.config["jev_api_url"],
            timeout=self.config["jev_timeout_seconds"],
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.config["jev_model"], "state": {"query": query},
                  "questions": {"response_mode": {"type": "choice",
                      "instructions": RESPONSE_MODE_PROMPT, "criteria": RESPONSE_MODE_CRITERIA}}})
        tool_metadata(http_status=status)
        answers = value.get("answers")
        answer = answers.get("response_mode") if isinstance(answers, dict) else None
        choice = answer.get("choice") if isinstance(answer, dict) else None
        if not isinstance(choice, str) or choice not in RESPONSE_MODE_CRITERIA:
            raise ValueError("Jev returned an invalid response-mode decision.")
        return choice

    def close(self):
        if self._owns_client and self.client is not None:
            self.client.close()
            self.client = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
