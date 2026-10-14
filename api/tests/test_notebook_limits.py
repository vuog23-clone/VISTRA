"""Large tool observations stay bounded and usable by the planning loop."""

from datetime import date, timedelta
import json
import unittest

from src.planning.notebook import Notebook
from src.schema.planning import EvidenceRecord, ToolResult
from src.schema.weather import WeatherEvidence


class NotebookLimitTests(unittest.TestCase):
    def test_large_forecast_is_trimmed_instead_of_aborting_the_turn(self):
        book = Notebook()
        for i in range(10):
            records = [EvidenceRecord(entity_id=f'wikivoyage:limit-{i}-{j}', kind='guide', name='Hanoi',
                city='Hanoi', provider='Wikivoyage', excerpt=('Historic streets and museums. '*65)[:1800]) for j in range(3)]
            book.write(ToolResult(tool='GuideSearch', city='Hanoi', status='available', records=records, note='Guide evidence.'))
            if len(json.dumps(book.list_all(), ensure_ascii=False).encode('utf-8')) > 43000:
                break
        forecast = WeatherEvidence(status='available', start_date='2026-10-14', end_date='2026-10-27',
            timezone='Asia/Bangkok', fetched_at='2026-10-14T00:00:00+00:00', location='Hanoi',
            days=[{'date': (date(2026, 10, 14)+timedelta(days=i)).isoformat(), 'condition': 'Clear sky',
                'temperature_min_c': 22, 'temperature_max_c': 30, 'precipitation_probability_pct': 10,
                'precipitation_mm': 1, 'wind_max_kmh': 10} for i in range(14)], note='Forecast.')
        entry = book.write(ToolResult(tool='WeatherSearch', city='Hanoi', status='available',
                                      weather=forecast, note='Forecast.'))
        self.assertIn(entry.status, ('partial', 'unavailable'))
        if entry.weather:
            WeatherEvidence.model_validate(entry.weather.model_dump())
            self.assertEqual(entry.weather.status, 'partial')
            self.assertLess(len(entry.weather.days), 14)
        self.assertLessEqual(len(json.dumps(book.list_all(), ensure_ascii=False).encode('utf-8')), 45000)
