"""Browse one golden-set candidate and try its prompts against the local VISTRA API.

Run from the project root: python -B -m benchmarks.scripts.demo
This is a live prompt demo, not a frozen-fixture benchmark or an automatic grader.
"""

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import secrets
import threading
from time import perf_counter
from typing import Literal
from urllib.parse import urlsplit

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

from src.utils.redaction import CredentialRedactor
from src.utils.utils import PROJECT_ROOT


BENCHMARKS = Path(__file__).resolve().parents[1]
GOLDEN = BENCHMARKS / 'vistra-v1' / 'golden'
ASSETS = BENCHMARKS / 'demo'
COOKIE = 'vistra_benchmark_demo'


class StartRun(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    family_id: str
    language: Literal['en', 'vi'] = 'vi'


def load_cases():
    public = {row['case_id']: row for row in (
        json.loads(line) for line in (GOLDEN / 'public/cases.jsonl').read_text(encoding='utf-8').splitlines())}
    private = {row['family_id']: row for path in sorted((GOLDEN / 'private').glob('*.jsonl'))
               for row in map(json.loads, path.read_text(encoding='utf-8').splitlines())}
    profiles = json.loads((GOLDEN / 'fixtures/scenario-profiles.json').read_text(encoding='utf-8'))
    return public, private, profiles


def preview_only_reason(row, profiles):
    profile = profiles[row['setup']['environment_ref'].split('#/', 1)[1]]
    if row['surface'] != 'chat_service' or row['track'] == 'reliability':
        return 'This case needs a dedicated API, image, streaming or fault adapter. Preview only.'
    if any('request_overrides' in turn for turn in row['turns']):
        return 'This case needs request mutations. Preview only.'
    if set(row['setup']) - {'environment_ref'} or profile.get('overrides') or profile.get('fault'):
        return 'This case needs injected state or evidence. Preview only.'
    return None


def create_app(*, api_url='http://127.0.0.1:8067', send_chat=None):
    parsed = urlsplit(api_url)
    if parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost'} or parsed.path not in {'', '/'} or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError('Use the loopback HTTP URL of the local VISTRA developer API.')
    load_dotenv(PROJECT_ROOT / '.env', override=False)
    api_key = os.getenv('VISTRA_API_KEY', '')
    public, private, profiles = load_cases()
    runs, guard = {}, threading.Lock()
    redactor = CredentialRedactor()

    def upstream(payload):
        if not api_key:
            raise HTTPException(503, 'Set VISTRA_API_KEY in the project .env and restart this demo.')
        try:
            with httpx.Client(timeout=300, trust_env=False) as client:
                response = client.post(api_url.rstrip('/') + '/v1/chat', json=payload,
                                       headers={'X-API-Key': api_key})
            if response.status_code != 200:
                raise HTTPException(502, f'VISTRA API returned HTTP {response.status_code}. Check its services and provider settings, then start a fresh run.')
            return response.json()
        except (httpx.HTTPError, ValueError):
            raise HTTPException(502, 'The local VISTRA API is unavailable or timed out. Check port 8067 and start a fresh run.') from None

    chat = send_chat or upstream
    app = FastAPI(title='VISTRA single-case demo', docs_url=None, redoc_url=None)

    @app.middleware('http')
    async def local_boundary(request, call_next):
        if request.url.hostname not in {'127.0.0.1', 'localhost'}:
            return JSONResponse({'detail': 'Open this demo on localhost.'}, status_code=400)
        owner = request.cookies.get(COOKIE)
        if owner is not None and (len(owner) != 64 or any(c not in '0123456789abcdef' for c in owner)):
            owner = None
        new_owner = None
        if request.method == 'GET' and request.url.path == '/' and owner is None:
            new_owner = owner = secrets.token_hex(32)
        request.state.owner = owner
        if request.method == 'POST':
            origin = request.headers.get('origin')
            if request.headers.get('x-vistra-demo') != '1' or (origin is not None and origin != f'{request.url.scheme}://{request.url.netloc}'):
                return JSONResponse({'detail': 'Reload the local demo before running a case.'}, status_code=403)
            if owner is None:
                return JSONResponse({'detail': 'Open the demo first.'}, status_code=401)
            try:
                size = int(request.headers.get('content-length', '-1'))
            except ValueError:
                size = -1
            if not 0 <= size <= 2000:
                return JSONResponse({'detail': 'Request must be at most 2,000 bytes.'}, status_code=413)
        response = await call_next(request)
        if new_owner:
            response.set_cookie(COOKIE, new_owner, httponly=True, samesite='strict', path='/')
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.get('/')
    def index():
        return FileResponse(ASSETS / 'index.html')

    @app.get('/health')
    def health():
        return {'status': 'ok', 'demo': 'vistra-single-case', 'families': len(private),
                'api_url': api_url, 'api_key_configured': bool(api_key), 'mode': 'live_prompt_trial'}

    @app.get('/api/cases')
    def cases():
        return [{'family_id': row['family_id'], 'title': row['title'], 'track': row['track'],
                 'turns': len(row['turns']), 'preview_only': bool(preview_only_reason(row, profiles))}
                for row in sorted(private.values(), key=lambda row: (row['track'], row['family_id']))]

    def family(family_id):
        if family_id not in private:
            raise HTTPException(404, 'Unknown scenario family.')
        return private[family_id]

    @app.get('/api/cases/{family_id}')
    def detail(family_id: str):
        row = family(family_id)
        return {**row, 'preview_only_reason': preview_only_reason(row, profiles)}

    @app.post('/api/runs')
    def start(body: StartRun, request: Request):
        row = family(body.family_id)
        reason = preview_only_reason(row, profiles)
        if reason:
            raise HTTPException(409, reason)
        run_id = secrets.token_hex(16)
        with guard:
            if len(runs) >= 100:
                expired = next((key for key, run in runs.items() if not run['busy']), None)
                if expired is None:
                    raise HTTPException(429, 'The demo is busy.')
                del runs[expired]
            runs[run_id] = {'owner': request.state.owner, 'case': public[body.family_id + '-' + body.language],
                            'next': 0, 'busy': False, 'failed': False}
        return {'run_id': run_id, 'mode': 'live_prompt_trial', 'total_turns': len(row['turns'])}

    @app.post('/api/runs/{run_id}/next')
    def next_turn(run_id: str, request: Request):
        with guard:
            run = runs.get(run_id)
            if run is None or run['owner'] != request.state.owner:
                raise HTTPException(404, 'Run not found. Start a fresh run.')
            if run['busy'] or run['failed']:
                raise HTTPException(409, 'This run is busy or failed. Wait, or start a fresh run.')
            turns = run['case']['turns']
            if run['next'] >= len(turns):
                raise HTTPException(409, 'All turns have completed. Start a fresh run to repeat.')
            turn = deepcopy(turns[run['next']])
            run['busy'] = True
        started = perf_counter()
        try:
            # Only public prompt text and fresh identities go to VISTRA. No gold,
            # assertions, fixture descriptors or expected answers enter its input.
            payload = {'user_id': f"benchmark-demo-{run_id}-{turn['owner_alias']}",
                       'session_id': f"benchmark-{run_id}-{turn['session_alias']}",
                       'message': turn['message']}
            result = redactor.data(chat(payload))
            with guard:
                run['next'] += 1
            return {'turn_id': turn['turn_id'], 'message': turn['message'], 'result': result,
                    'elapsed_seconds': round(perf_counter() - started, 3),
                    'completed_turns': run['next'], 'total_turns': len(turns), 'scored': False}
        except HTTPException:
            with guard:
                run['failed'] = True
            raise
        except Exception:
            with guard:
                run['failed'] = True
            raise HTTPException(502, 'The live request failed. Start a fresh run before trying again.') from None
        finally:
            with guard:
                run['busy'] = False

    app.mount('/assets', StaticFiles(directory=ASSETS), name='assets')
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8070)
    parser.add_argument('--api-url', default='http://127.0.0.1:8067')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('Port must be between 1 and 65535.')
    import uvicorn
    uvicorn.run(create_app(api_url=args.api_url), host='127.0.0.1', port=args.port, proxy_headers=False)


if __name__ == '__main__':
    main()
