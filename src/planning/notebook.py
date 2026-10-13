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
        while (len(json.dumps(self.data.model_dump(exclude_none=True), ensure_ascii=False).encode('utf-8')) > 45000 or
               nodes(self.data.model_dump(exclude_none=True)) > 2500) and entry.records:
            entry.records.pop()
        if len(entry.records) < original_count:
            entry.status = 'partial'
            entry.note = entry.note[:500] + ' Some candidates were omitted to keep the planning evidence bounded.'
        if len(json.dumps(self.data.model_dump(exclude_none=True), ensure_ascii=False).encode('utf-8')) > 45000:
            self.data.entries.pop()
            raise ValueError('Notebook evidence exceeds its size budget.')
        return entry

    def list_all(self):
        return self.data.model_dump(exclude_none=True)
