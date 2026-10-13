"""Generate a safe answer directly, using the existing bounded OpenAI client."""

import json
import re

from src.generation.rag_generation import RAGGenerator
from src.prompts.direct import DIRECT_RESPONSE_PROMPT
from src.security.context import answer_safety_issues
from src.security.guardrails import validate_text
from src.utils.language import localized_message
from src.utils.redaction import CredentialRedactor


def direct_answer_issues(answer):
    issues = answer_safety_issues(answer, {"sources": []})
    if not issues and re.search(r"\[S\d+\]", answer):
        issues.append("Direct answers cannot cite sources that were not retrieved.")
    return issues


class DirectGenerator(RAGGenerator):
    """Reuse client ownership, endpoint, timeout and token limits; no RAG review."""

    def generate(self, user_query, *, conversation_memory=None):
        user_query = validate_text(user_query, check_intent=True)
        if conversation_memory is not None:
            from src.schema.memory import ConversationMemory
            conversation_memory = ConversationMemory.model_validate(conversation_memory).model_dump()
        self.last_grounding = {"status": "not_applicable", "issues": []}
        answer = self._respond(user_query, conversation_memory=conversation_memory)
        issues = direct_answer_issues(answer)
        if not issues:
            return answer
        try:
            answer = self._respond(user_query, feedback={"previous_draft": answer, "issues": issues},
                                   conversation_memory=conversation_memory)
            remaining = direct_answer_issues(answer)
        except Exception:
            remaining = ["The direct-answer repair did not complete."]
        if not remaining:
            return answer
        self.last_grounding = {"status": "direct_fallback", "issues": remaining}
        return localized_message(user_query,
            "I could not produce a safe response. Please rephrase your question.",
            "Không thể tạo câu trả lời an toàn. Vui lòng diễn đạt lại câu hỏi.")

    def _respond(self, user_query, *, feedback=None, conversation_memory=None):
        completion = self.client.responses.create(
            model=self.model_name, instructions=DIRECT_RESPONSE_PROMPT,
            input=json.dumps(CredentialRedactor().data({"question": user_query,
                **({"conversation_memory": conversation_memory} if conversation_memory is not None else {}),
                **({"repair_feedback": feedback} if feedback else {})}), ensure_ascii=False, allow_nan=False),
            reasoning={"effort": self.config["openai_reasoning_effort"]},
            max_output_tokens=self.config["generation_max_tokens"], store=False)
        if completion.status != "completed":
            raise RuntimeError("LLM response was incomplete.")
        if not isinstance(completion.output_text, str) or not completion.output_text.strip():
            raise RuntimeError("LLM returned no answer text.")
        return completion.output_text.strip()
