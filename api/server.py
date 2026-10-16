"""Run from the project root: uvicorn api.server:create_app --factory."""

from contextlib import asynccontextmanager
import hashlib
import hmac
import logging
import os
import threading
import traceback
from typing import Annotated, Literal

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, ConfigDict, Field, model_validator

from api.runtime import RetrievalRuntime
from src.database.postgres_memory_store import MemoryConflict
from src.security.guardrails import UnsafeInput, validate_text
from src.service.chat_service import ChatService
from src.utils.redaction import CredentialRedactor
from src.utils.reply_sources import reply_sources
from src.utils.utils import PROJECT_ROOT, load_config


MAX_BODY_BYTES = 64 * 1024
IDENTITY_PATTERN = r'^[A-Za-z0-9_.@:-]{1,128}$'
LOGGER = logging.getLogger('vistra.api')


class BodyLimitMiddleware:
    """Bound actual request bytes, including chunked bodies."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)

        async def secure_send(event):
            if event['type'] == 'http.response.start':
                headers = [(name, value) for name, value in event.get('headers', [])
                           if name.lower() not in (b'cache-control', b'x-content-type-options')]
                event = {**event, 'headers': [*headers, (b'cache-control', b'no-store'),
                                             (b'x-content-type-options', b'nosniff')]}
            await send(event)

        if scope['method'] != 'POST':
            return await self.app(scope, receive, secure_send)
        chunks, size = [], 0
        while True:
            event = await receive()
            if event['type'] == 'http.disconnect':
                return
            chunk = event.get('body', b'')
            size += len(chunk)
            if size > MAX_BODY_BYTES:
                response = JSONResponse({'error': {'code': 'body_too_large', 'message': 'Request exceeds 64 KiB.'}},
                                        status_code=413)
                return await response(scope, receive, secure_send)
            chunks.append(chunk)
            if not event.get('more_body', False):
                break
        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {'type': 'http.request', 'body': b''.join(chunks), 'more_body': False}
            return await receive()

        await self.app(scope, bounded_receive, secure_send)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, json_schema_extra={
        'examples': [{'user_id': 'customer-123', 'session_id': 'example-session', 'message': 'Who are you?'}],
    })
    user_id: str = Field(pattern=IDENTITY_PATTERN, description="Your application's user identifier.")
    session_id: str = Field(pattern=IDENTITY_PATTERN, description='Reuse for follow-up messages.')
    message: str = Field(min_length=1, max_length=8000)
    retrieval_limit: int = Field(default=20, ge=1, le=20)
    response_mode: Literal['auto', 'retrieval', 'llm_only'] = 'auto'
    route: Literal['auto', 'text', 'hybrid'] = 'auto'
    location: str | None = Field(default=None, min_length=1, max_length=160,
                                description='Optional destination override. Omit to detect it from the conversation.')
    retrieve_only: bool = False
    include_weather: bool | None = None
    start_date: str | None = Field(default=None,
        description='Optional trip start in YYYY-MM-DD format. Supply with end_date, or omit both.')
    end_date: str | None = Field(default=None,
        description='Optional trip end in YYYY-MM-DD format. Supply with start_date, at most 14 inclusive days.')
    planning_mode: Literal['auto', 'off'] = 'auto'

    @model_validator(mode='after')
    def compatible_options(self):
        if self.response_mode == 'llm_only' and (self.route != 'auto' or self.location or self.retrieve_only):
            raise ValueError('llm_only conflicts with retrieval options.')
        if (self.start_date is None) != (self.end_date is None):
            raise ValueError('Supply both start_date and end_date.')
        if self.start_date is not None:
            from src.schema.weather import iso_date
            start, end = iso_date(self.start_date), iso_date(self.end_date)
            if end < start or (end - start).days >= 14:
                raise ValueError('Dates must be chronological and span at most 14 days.')
        return self


class ChatResponse(BaseModel):
    user_id: str
    session_id: str
    answer: str | None
    answer_format: str = 'markdown'
    pipeline_status: str
    response_mode: str | None = None
    version: int
    memory_status: str
    sources: list[dict] = Field(default_factory=list)
    retrieval: dict | None = None
    weather: dict | None = None
    itinerary: dict | None = None
    planning: dict | None = None
    elapsed_seconds: float | None = None


def create_app(*, api_key=None, chat_factory=None, max_concurrent=None):
    load_dotenv(PROJECT_ROOT / '.env', override=False)
    key = api_key if api_key is not None else os.getenv('VISTRA_API_KEY', '')
    if not isinstance(key, str) or len(key) < 32 or any(char.isspace() for char in key):
        raise RuntimeError('Set VISTRA_API_KEY to a random secret of at least 32 characters.')
    capacity = max_concurrent if max_concurrent is not None else int(os.getenv('VISTRA_API_CONCURRENCY', '2'))
    if type(capacity) is not int or not 1 <= capacity <= 8:
        raise ValueError('VISTRA_API_CONCURRENCY must be between 1 and 8.')
    key_digest = hashlib.sha256(key.encode()).digest()
    header = APIKeyHeader(name='X-API-Key', auto_error=False)
    runtime = RetrievalRuntime(cache_size=load_config()['query_embedding_cache_size'])
    factory = chat_factory or (lambda: ChatService(pipeline=runtime.pipeline))
    guard, busy_users = threading.Lock(), set()
    slots = threading.BoundedSemaphore(capacity)

    @asynccontextmanager
    async def lifespan(_):
        try:
            yield
        finally:
            runtime.close()

    app = FastAPI(title='VISTRA Developer API', version='2.0.0', lifespan=lifespan,
        description='Use X-API-Key with /v1/chat. Reuse user_id and session_id for multi-turn memory.')
    app.add_middleware(BodyLimitMiddleware)

    def authenticate(candidate: Annotated[str | None, Depends(header)]):
        if candidate is None or not hmac.compare_digest(hashlib.sha256(candidate.encode()).digest(), key_digest):
            raise HTTPException(401, 'Missing or invalid X-API-Key.')

    @app.exception_handler(HTTPException)
    async def http_error(_, error):
        return JSONResponse({'error': {'code': str(error.status_code), 'message': error.detail}},
                            status_code=error.status_code, headers=error.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_, __):
        return JSONResponse({'error': {'code': 'invalid_request',
            'message': 'Invalid request. See /docs for accepted fields and limits.'}}, status_code=422)

    @app.exception_handler(UnsafeInput)
    async def unsafe_error(_, error):
        return JSONResponse({'error': {'code': error.code,
            'message': CredentialRedactor(extra_secrets=(key,)).text(str(error))}}, status_code=400)

    @app.get('/health', tags=['health'])
    async def health():
        """Process liveness; no model loads or database/provider calls."""
        return {'status': 'ok'}

    @app.post('/v1/chat', response_model=ChatResponse, dependencies=[Depends(authenticate)], tags=['chat'],
        responses={401: {'description': 'Invalid API key'}, 409: {'description': 'User busy or memory conflict'},
                   429: {'description': 'API capacity reached'}, 503: {'description': 'Backend unavailable'}})
    def chat(body: ChatRequest):
        message = validate_text(body.message, check_intent=True)
        # Preserve the previous API namespace and keep browser owners separate.
        owner = 'api_' + hashlib.sha256(body.user_id.encode()).hexdigest()
        with guard:
            if owner in busy_users:
                raise HTTPException(409, 'This user has a request in progress. Wait before retrying.')
            if not slots.acquire(blocking=False):
                raise HTTPException(429, 'API is busy. Try again shortly.', headers={'Retry-After': '5'})
            busy_users.add(owner)
        try:
            weather_options = {'include_weather': body.include_weather} if body.include_weather is not None else {}
            with factory() as service:
                result = service.chat(owner, body.session_id, message, limit=body.retrieval_limit,
                    response_mode=body.response_mode, route=body.route, location=body.location,
                    retrieve_only=body.retrieve_only, trip_start=body.start_date, trip_end=body.end_date,
                    planning_mode=body.planning_mode, **weather_options)
            metadata = result['chat']
            return CredentialRedactor(extra_secrets=(key,)).data({
                'user_id': body.user_id, 'session_id': body.session_id, 'answer': result.get('answer'),
                'answer_format': result.get('answer_format', 'markdown'),
                'pipeline_status': result.get('pipeline_status', 'complete'),
                'response_mode': result.get('response_mode'),
                'version': metadata['version'], 'memory_status': metadata['memory_status'],
                'sources': reply_sources(result),
                **{name: result.get(name) for name in ('retrieval', 'weather', 'itinerary', 'planning', 'elapsed_seconds')},
            })
        except MemoryConflict:
            raise HTTPException(409, 'Conversation changed during this request. Retry using its latest state.') from None
        except UnsafeInput:
            raise
        except Exception:
            LOGGER.error('Chat backend failed:\n%s', CredentialRedactor(extra_secrets=(key,)).text(traceback.format_exc()))
            raise HTTPException(503, 'VISTRA could not finish this request. Check services and provider settings.') from None
        finally:
            with guard:
                busy_users.discard(owner)
                slots.release()

    return app
