"""Opt-in, read-only retrieval timing inside the API image. No LLM calls."""

import argparse
import json
from time import perf_counter

from api.runtime import RetrievalRuntime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live-retrieval', action='store_true')
    args = parser.parse_args()
    if not args.live_retrieval:
        parser.error('Use --live-retrieval to query the configured local indexes.')
    runtime = RetrievalRuntime()
    checks = []
    try:
        for route in ('text', 'text', 'hybrid', 'hybrid'):
            start = perf_counter()
            result = runtime.pipeline('What cultural sights does the Hanoi guide describe?',
                response_mode='retrieval', route=route, location='Hanoi', auto_location=False,
                retrieve_only=True, limit=5)
            retrieval = result.get('retrieval') or {}
            count = len(retrieval.get('results') or [])
            assert count > 0, 'Retrieval returned no indexed text evidence.'
            related = len(retrieval.get('related_results') or [])
            if route == 'hybrid':
                assert related > 0, 'Hybrid retrieval returned no photo/place evidence.'
            checks.append({'route': route, 'elapsed_seconds': round(perf_counter()-start, 3),
                           'text_results': count, 'photo_results': related})
        print(json.dumps(checks), flush=True)
    finally:
        runtime.close()


if __name__ == '__main__':
    main()
