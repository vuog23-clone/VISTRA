"""Resolve citations consistently for RAG, itinerary and weather replies."""


def reply_sources(result):
    sources = list((result.get('context') or {}).get('sources', []))
    if result.get('notebook') and result.get('itinerary'):
        from src.schema.planning import EvidenceNotebook, Itinerary
        notebook = EvidenceNotebook.model_validate(result['notebook'])
        plan = Itinerary.model_validate(result['itinerary'])
        selected = {choice.evidence_id for day in plan.days for _, choice in day.choices()}
        sources.extend({'source_id': record.evidence_id, 'entity_id': record.entity_id,
                        'name': record.name, 'url': record.url, 'provider': record.provider}
                       for record in notebook.records().values() if record.evidence_id in selected)
    weather = result.get('weather') or (result.get('context') or {}).get('weather')
    if weather and weather.get('days'):
        from src.schema.weather import WeatherEvidence
        checked = WeatherEvidence.model_validate(weather)
        sources.append({'source_id':'W1', 'name':'Open-Meteo weather', 'url':checked.url})
    return sources[:72]
