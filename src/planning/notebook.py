"""NotebookWrite stores selected tool results, not conversation recollections."""

import json
from src.schema.planning import EvidenceNotebook, NotebookEntry, ToolResult


class Notebook:
    def __init__(self, *, city_count=1, duration_days=1):
        self.data = EvidenceNotebook()
        # Reserve evidence space for every city and component instead of letting
        # the first few 20-result searches consume the entire 60-record budget.
        budget = (60 - city_count*2 - 4) // city_count
        guides = lodgings = 2 if city_count >= 3 else 3
        venue_budget = budget - guides - lodgings
        meals = 3*((duration_days+city_count-1)//city_count)
        restaurants = min(20, venue_budget-2, max(venue_budget*2//3, meals))
        self.record_limits = {'GuideSearch': guides, 'AccommodationSearch': lodgings,
            'RestaurantSearch': restaurants, 'AttractionSearch': min(20, venue_budget-restaurants),
            'FlightSearch': 2, 'DistanceMatrix': 2, 'CitySearch': 2}

    def write(self, result):
        result = ToolResult.model_validate(result)
        from src.security.context import _clean_json
        from src.schema.planning import EvidenceRecord
        from src.utils.redaction import CredentialRedactor
        safe = []
        for record in result.records:
            try:
                safe.append(EvidenceRecord.model_validate(CredentialRedactor().data(
                    _clean_json(record.model_dump(exclude_none=True)))))
            except ValueError:
                continue
        if len(safe) != len(result.records):
            result.status = 'partial' if safe else 'unavailable'
            result.note = result.note[:500] + ' Unsafe evidence records were excluded.'
        result.records = safe
        if result.route_matrix is not None:
            from src.schema.routing import RouteMatrixEvidence
            try:
                result.route_matrix = RouteMatrixEvidence.model_validate(CredentialRedactor().data(
                    _clean_json(result.route_matrix.model_dump())))
            except ValueError:
                result.route_matrix = None
                result.status = 'partial' if safe else 'unavailable'
                result.note = 'Unsafe routing evidence was excluded.'
        entry = NotebookEntry(entry_id=f'E{len(self.data.entries)+1}', **result.model_dump())
        original_count = len(entry.records)
        remaining = 60 - len(self.data.records())
        entry.records = entry.records[:max(0, min(remaining, self.record_limits.get(entry.tool, 20)))]
        for index, record in enumerate(entry.records, 1):
            record.evidence_id = f'{entry.entry_id}R{index}'
        self.data.entries.append(entry)
        def nodes(value):
            if isinstance(value, dict):
                return 1 + sum(nodes(child) for child in value.values())
            if isinstance(value, list):
                return 1 + sum(nodes(child) for child in value)
            return 1
        # Tool evidence stays bounded independently of the conversational memory.
        def oversized():
            payload = self.data.model_dump(exclude_none=True)
            return len(json.dumps(payload, ensure_ascii=False).encode('utf-8')) > 45000 or nodes(payload) > 2500
        trimmed_matrix = False
        while oversized() and (entry.records or entry.route_matrix):
            if entry.route_matrix:
                trimmed_matrix = True
                matrix = entry.route_matrix
                if len(matrix.locations) > 2:
                    matrix.locations.pop()
                    for rows in (matrix.durations_seconds, matrix.distances_meters):
                        rows.pop()
                        for row in rows:
                            row.pop()
                else:
                    entry.route_matrix = None
            else:
                entry.records.pop()
        if len(entry.records) < original_count or trimmed_matrix:
            entry.status = 'partial'
            entry.note = entry.note[:500] + ' Some candidates were omitted to keep the planning evidence bounded.'
        if oversized() and entry.weather is not None:
            entry.status = 'partial'
            entry.note = entry.note[:450] + ' Some forecast days were omitted to keep the evidence bounded.'
            entry.weather.status = 'partial'
            entry.weather.note = entry.weather.note[:450] + ' Some forecast days were omitted to keep the evidence bounded.'
            while oversized() and entry.weather.days:
                entry.weather.days.pop()
            if not entry.weather.days:
                entry.weather = None
                entry.status = 'unavailable'
                entry.note = 'Forecast evidence could not fit the bounded notebook.'
        if oversized():
            self.data.entries.pop()
            raise ValueError('Notebook evidence exceeds its size budget.')
        return entry

    def list_all(self):
        return self.data.model_dump(exclude_none=True)
