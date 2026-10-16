"""One isolated case. Parent terminates the process on deadline; no database writes."""

from contextlib import redirect_stdout, ExitStack
from datetime import date
import io
import json
import sys
import traceback
from time import perf_counter
from unittest.mock import patch


def main():
    from benchmarks.measurement.adapters import ADAPTERS
    from benchmarks.measurement.usage import Meter
    from src.utils.redaction import CredentialRedactor
    sys.stdout.reconfigure(encoding='utf-8')
    payload = json.loads(sys.stdin.read())
    case, fixtures = payload['case'], payload['fixtures']
    if case['adapter'] not in ADAPTERS:
        raise ValueError('Unknown measurement adapter.')
    meter, start = Meter(), perf_counter()
    try:
        with redirect_stdout(io.StringIO()), ExitStack() as resources:
            resources.enter_context(patch('src.service.weather_service.reference_today', return_value=date.fromisoformat(payload['reference_date'])))
            if case['profile'] == 'offline':
                resources.enter_context(patch('socket.socket.connect', side_effect=RuntimeError('Network is disabled in the offline profile.')))
            data = ADAPTERS[case['adapter']](case, fixtures, meter)
        result = {'outcome': 'ok', 'data': data}
    except Exception as error:
        origin = traceback.extract_tb(error.__traceback__)[-1].filename.replace('\\','/')
        wiring_error = isinstance(error,(TypeError,KeyError,ImportError)) and origin.endswith('/benchmarks/measurement/adapters.py')
        result = {'outcome': 'harness_error' if wiring_error else 'system_error',
                  'error': {'type': type(error).__name__, 'message': str(error)[:1500]}}
    result.update(component_seconds=perf_counter()-start, provider_calls=meter.events)
    print(json.dumps(CredentialRedactor().data(result), ensure_ascii=False, allow_nan=False))


if __name__ == '__main__':
    main()
