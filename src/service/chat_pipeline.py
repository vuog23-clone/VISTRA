"""Shared pipeline policy for browser and terminal chat, before UI formatting."""

from contextlib import ExitStack

from src.router.evidence_policy import scenic_evidence_policy
from src.schema.response import ResponseDecision


def run_chat_pipeline(query, **options):
    """Apply explicit evidence preferences; leave other route choices to Jev."""
    from main import run_pipeline
    from src.router.jev_response import JevResponseRouter
    with ExitStack() as resources:
        if options.get('response_mode', 'auto') == 'auto':
            from src.schema.planning import planning_action
            from src.utils.utils import load_config
            previous = options.get('previous_itinerary') or (options.get('conversation_memory') or {}).get('trip_state', {}).get('itinerary')
            action = options.get('planning_action')
            if action is None:
                action = planning_action(query, previous)
                if action:
                    options['planning_action'] = action
            planning = (action in ('create_itinerary', 'revise_itinerary')
                and options.get('planning_mode', 'auto') != 'off'
                and load_config().get('planning_enabled', True)
                and not options.get('maps_only') and not options.get('retrieve_only'))
            constrained = (options.get('image') is not None or options.get('route') not in (None, 'auto')
                or options.get('location') is not None or options.get('categories')
                or options.get('maps_only') or options.get('retrieve_only') or planning)
            router = None if constrained else options.get('response_router') or resources.enter_context(JevResponseRouter())
            decision = (ResponseDecision(mode='retrieval', decision_source='constraint') if constrained else
                        ResponseDecision.model_validate(router.route(options.get('routing_query') or query)))
            options['response_mode'] = decision.mode
        if options.get('response_mode') == 'retrieval' and options.get('route') in (None, 'auto'):
            policy, _ = scenic_evidence_policy(options.get('routing_query') or query)
            if policy is not None:
                options['route'] = 'text' if policy == 'text_only' else 'hybrid'
        return run_pipeline(query, **options)
