"""Generate a safe answer directly, using the existing bounded OpenAI client."""

import json

from src.generation.rag_generation import RAGGenerator
from src.prompts.direct import DIRECT_RESPONSE_PROMPT
from src.security.guardrails import validate_text
from src.utils.redaction import CredentialRedactor
from src.generation.response_contract import (resolve_response_contract, response_contract_issues,
                                               contract_limitation)


def direct_answer_issues(answer, user_query=None, conversation_memory=None):
    from src.generation.response_contract import ResponseContract
    return response_contract_issues(answer, {'sources': []},
        resolve_response_contract(user_query, conversation_memory) if user_query is not None else ResponseContract(None),
        direct=True)


class DirectGenerator(RAGGenerator):
    """Reuse client ownership, endpoint, timeout and token limits; no RAG review."""

    def generate(self, user_query, *, conversation_memory=None):
        user_query = validate_text(user_query, check_intent=True)
        if conversation_memory is not None:
            from src.schema.memory import ConversationMemory
            conversation_memory = ConversationMemory.model_validate(conversation_memory).model_dump()
        self.last_grounding = {"status": "not_applicable", "issues": []}
        self.last_contract = None
        contract = resolve_response_contract(user_query, conversation_memory)
        answer = self._respond(user_query, conversation_memory=conversation_memory)
        issues = direct_answer_issues(answer, user_query, conversation_memory)
        attempts = [{'stage': 'draft', 'status': 'failed' if issues else 'passed', 'issues': issues}]
        self.last_contract = {'status': 'failed' if issues else 'checked', 'requirements': contract.payload(),
                              'issues': issues, 'attempts': attempts}
        if not issues:
            return answer
        try:
            answer = self._respond(user_query, feedback={"previous_draft": answer, "issues": issues},
                                   conversation_memory=conversation_memory)
            remaining = direct_answer_issues(answer, user_query, conversation_memory)
        except Exception:
            remaining = ["The direct-answer repair did not complete."]
        attempts.append({'stage': 'repair', 'status': 'failed' if remaining else 'passed', 'issues': remaining})
        self.last_contract = {'status': 'failed' if remaining else 'repaired', 'requirements': contract.payload(),
                              'issues': remaining, 'attempts': attempts}
        if not remaining:
            return answer
        self.last_grounding = {"status": "direct_fallback", "issues": remaining}
        return contract_limitation(contract)

    def _respond(self, user_query, *, feedback=None, conversation_memory=None):
        completion = self._text_completion(
            model=self.model_name, instructions=DIRECT_RESPONSE_PROMPT,
            input=json.dumps(CredentialRedactor().data({"question": user_query,
                "response_contract": resolve_response_contract(user_query, conversation_memory).payload(),
                **({"conversation_memory": conversation_memory} if conversation_memory is not None else {}),
                **({"repair_feedback": feedback} if feedback else {})}), ensure_ascii=False, allow_nan=False),
            reasoning={"effort": self.config["openai_reasoning_effort"]},
            max_output_tokens=self.config["generation_max_tokens"], store=False)
        if completion.status != "completed":
            raise RuntimeError("LLM response was incomplete.")
        if not isinstance(completion.output_text, str) or not completion.output_text.strip():
            raise RuntimeError("LLM returned no answer text.")
        return completion.output_text.strip()
