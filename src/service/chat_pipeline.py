"""Shared pipeline policy for browser and terminal chat, before UI formatting."""

from contextlib import ExitStack

from src.router.evidence_policy import scenic_evidence_policy
from src.schema.response import ResponseDecision


def run_chat_pipeline(query, **options):
    """Include travel photos while preserving direct answers and memory recall."""
    from main import run_pipeline
    from src.router.jev_response import JevResponseRouter
    with ExitStack() as resources:
        if options.get('response_mode', 'auto') == 'auto':
            constrained = (options.get('image') is not None or options.get('route') not in (None, 'auto')
                or options.get('location') is not None or options.get('categories')
                or options.get('maps_only') or options.get('retrieve_only'))
            router = None if constrained else options.get('response_router') or resources.enter_context(JevResponseRouter())
            decision = (ResponseDecision(mode='retrieval', decision_source='constraint') if constrained else
                        ResponseDecision.model_validate(router.route(options.get('routing_query') or query)))
            options['response_mode'] = decision.mode
        if options.get('response_mode') == 'retrieval' and options.get('route') in (None, 'auto'):
            policy, _ = scenic_evidence_policy(options.get('routing_query') or query)
            options['route'] = 'text' if policy == 'text_only' else 'hybrid'
        return run_pipeline(query, **options)
