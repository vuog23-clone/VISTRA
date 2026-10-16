"""Frozen fictional provider inputs; never use private answers or witness plans.

This adapter substitutes SearchService, not its vector index. Consequently no
retrieval ranking, visual matching, or live integration scores are claimed.
"""

from copy import deepcopy
import json
from datetime import date

from benchmarks.evaluation.protocol import read


def frozen_environment(case):
    from benchmarks.evaluation.protocol import profile
    env, overrides = deepcopy(read('fixtures/environment.json')), profile(case).get('overrides', {})
    if 'allowed_evidence_entities' in overrides:
        env['entities'] = {k:v for k,v in env['entities'].items() if v['entity_id'] in overrides['allowed_evidence_entities']}
        # An irrelevant-source case must not accidentally receive other evidence.
        return {'entities': env['entities']}
    for entity in env['entities'].values():
        for field in overrides.get('remove_fields', []):
            entity.pop(field, None)
    if overrides.get('source_versions'):
        identity = overrides['source_versions'][0]['entity_id']
        for entity in env['entities'].values():
            if entity['entity_id'] == identity:
                entity.pop('closes', None)
                entity['source_versions'] = overrides['source_versions']
    if overrides.get('poisoned_excerpt'):
        env['untrusted_source_excerpt'] = overrides['poisoned_excerpt']
        env['safe_fact'] = overrides['safe_fact']
    return env


def grounding_context(case):
    """Group *all* public entities, including distractors, without consulting gold."""
    env = frozen_environment(case)
    entities = list(env.pop('entities').values())
    groups = [{'entities':entities[n:n+3]} for n in range(0, len(entities), 3)]
    if env:
        groups.append(env)
    return {'route':'text', 'sources':[
        {'source_id':f'S{i+1}', 'entity_id':f'wikivoyage:synthetic_public_bundle_{i+1}',
         'source':'text', 'name':f'Frozen fictional benchmark evidence {i+1}', 'url':None,
         'excerpts':[{'section':'Synthetic evidence; not a live source', 'text':json.dumps(group,ensure_ascii=False)}]}
        for i,group in enumerate(groups)]}


class SearchReplay:
    """Return public scoped guide evidence; this is an evidence supplier, not IR."""
    def __init__(self, resources, observed):
        self.resources, self.observed = resources, observed

    def search(self, query, *, route=None, location=None, **options):
        from src.schema.models import SearchResponse, SearchResult
        from src.router.jev import JevRouter
        from src.resolver.location_resolver import LocationResolver
        env = read('fixtures/environment.json')
        selected_route = self.resources.enter_context(JevRouter()).route(query,has_image=False,override=route)
        normalize = LocationResolver._normalize
        location_key = normalize(location or '')
        guides = [(city, raw) for city,raw in env['guides'].items()
                  if not location or location_key == normalize('Vietnam') or normalize(city) == location_key]
        hits = []
        for city, raw in guides:
            # Complete public records within this scope; no qrels-based selection.
            facts = [e for e in env['entities'].values() if normalize(e['city']) == normalize(city)]
            text = json.dumps({'provenance':'synthetic_not_live','guide':raw['text'],'entities':facts},ensure_ascii=False)
            hits.append(SearchResult(place_id=raw['entity_id'].split(':',1)[1],entity_id=raw['entity_id'],
                source='text',score=1.0,name=city,latitude=raw['latitude'],longitude=raw['longitude'],
                payload={'text':text,'section':'Frozen fictional benchmark guide'}))
        response = SearchResponse(route=selected_route,location=location,
            location_status='resolved' if guides else 'unresolved',results=hits[:options.get('limit',20)],
            retrieval_status='complete' if hits else 'no_results',
            branch_status={'text':'complete','image':'unavailable'} if selected_route=='hybrid' else {'text':'complete'},
            retrieval_warnings=['Frozen provider replay: no vector ranking or photo evidence is available.'])
        self.observed.append({'query':query,'route':selected_route,'location':location,'returned_entity_ids':[r.entity_id for r in response.results]})
        return response


class WeatherReplay:
    def run(self, *, latitude,longitude,location,start_date,end_date):
        from src.schema.weather import WeatherEvidence
        from src.resolver.location_resolver import LocationResolver
        env = read('fixtures/environment.json')['weather']
        days = []
        if LocationResolver._normalize(location) == 'hanoi' and start_date <= '2026-10-16' <= end_date:
            days = [{'date':'2026-10-16','condition':'Condition not supplied in frozen evidence',
                     'temperature_max_c':env['hanoi_oct16_max_c'],'precipitation_probability_pct':env['hanoi_oct16_rain_pct']}]
        span = (date.fromisoformat(end_date)-date.fromisoformat(start_date)).days+1
        return WeatherEvidence(location=location,latitude=latitude,longitude=longitude,
            start_date=start_date,end_date=end_date,timezone='Asia/Bangkok',
            fetched_at=env['provenance']['fetched_at'],days=days,
            status=('available' if len(days)==span else 'partial') if days else 'unavailable',
            note='Frozen fictional forecast; only explicitly supplied daily values are available.').model_dump()


class PlanningReplay:
    def __init__(self, case=None):
        self.calls = []
        self.case = case

    def run(self, name, city, **options):
        from src.schema.planning import EvidenceRecord, ToolResult
        from src.schema.routing import RouteLocation, RouteMatrixEvidence
        self.calls.append({'tool':name,'city':city})
        raw = deepcopy(read('fixtures/planning-environment.json'))
        if self.case:
            from benchmarks.evaluation.protocol import profile
            overrides = profile(self.case).get('overrides',{})
            for item in raw['records']:
                for field in overrides.get('remove_fields',[]):
                    item.pop(field,None)
                if item['kind']=='accommodation':
                    for field in overrides.get('remove_lodging_fields',[]):
                        item.pop(field,None)
                if item['entity_id']==overrides.get('closed_entity'):
                    # In the authored three-day range this is exactly Friday.
                    windows = item.get('opening_hours',{}).get('all_days',[])
                    item['opening_hours'] = {day:[{'open':a,'close':b} for a,b in windows] for day in
                        ('monday','tuesday','wednesday','thursday','friday','saturday','sunday')}
                    item['opening_hours'][date.fromisoformat(overrides['closed_date']).strftime('%A').lower()] = []
        kind = {'RestaurantSearch':'restaurant','AttractionSearch':'attraction','AccommodationSearch':'accommodation'}.get(name)
        records = []
        if city == 'Hanoi' and kind:
            for item in raw['records']:
                if item['kind'] != kind:
                    continue
                value = {k:v for k,v in item.items() if k in EvidenceRecord.model_fields}
                value['provider'] = 'Google metadata'
                if value.get('opening_hours',{}).get('all_days'):
                    windows = [{'open':a,'close':b} for a,b in value['opening_hours']['all_days']]
                    value['opening_hours'] = {day:windows for day in ('monday','tuesday','wednesday','thursday','friday','saturday','sunday')}
                records.append(EvidenceRecord.model_validate(value))
        matrix = None
        if name == 'DistanceMatrix' and city == 'Hanoi':
            matrix_raw = raw['route_matrix']
            by_id = {r['entity_id']:r for r in raw['records']}
            # Schema transport coordinates only; never used to estimate routes.
            # The complete directed seconds/metres matrices are unchanged.
            points = [RouteLocation(entity_id=identity,name=by_id[identity]['name'],city='Hanoi',
                latitude=21.03+i/100000,longitude=105.84) for i,identity in enumerate(matrix_raw['locations'])]
            matrix = RouteMatrixEvidence(mode='walking',profile='foot-walking',fetched_at='2026-10-15T00:00:00+07:00',
                locations=points,durations_seconds=matrix_raw['durations'],distances_meters=matrix_raw['distances_meters'])
            records = [EvidenceRecord(entity_id='google:bench_walking_fare',kind='transport',name='Stipulated walking fare',
                city=city,provider='Google metadata',mode='walking',cost=raw['walking_fare_vnd'],currency='VND',cost_unit='total')]
        return ToolResult(tool=name,city=city,status='available' if records or matrix else 'unavailable',
            records=records,route_matrix=matrix,note='Frozen fictional public candidates. Unmatched tools/scopes are unavailable.')
