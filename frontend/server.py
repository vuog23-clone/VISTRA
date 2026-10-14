"""Local chat UI and same-origin adapter to VISTRA's existing memory service.

Run from the project root: ./.venv/python.exe -B -m frontend.server
"""

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import hashlib
import hmac
from io import BytesIO
import json
import logging
from pathlib import Path
import queue
import secrets
import threading
from time import monotonic
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, Path as APIPath
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from frontend.media import ReplyMedia, build_place_cards, public_media
from src.data.preprocessing.google_maps_photos import checked_photo_url, fetch_photo

from src.database.postgres_memory_store import MemoryConflict, PostgresMemoryStore
from src.schema.memory import checked_identity
from src.security.guardrails import UnsafeInput, validate_text
from src.service.chat_service import ChatService
from src.service.chat_pipeline import run_chat_pipeline
from src.utils.reply_sources import reply_sources
from src.utils.redaction import CredentialRedactor


ROOT = Path(__file__).resolve().parent
COOKIE = "vistra_demo"
LOGGER = logging.getLogger('vistra.frontend')

MEDIA_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS vistra_demo_reply_media (
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    turn BIGINT NOT NULL,
    media JSONB NOT NULL,
    PRIMARY KEY (user_id, session_id, turn),
    FOREIGN KEY (user_id, session_id) REFERENCES vistra_memory_sessions(user_id, session_id) ON DELETE CASCADE
);
"""


def run_demo_pipeline(query, **options):
    """Include place photos for travel answers while preserving direct memory chat."""
    return run_chat_pipeline(query, **options)


def make_demo_chat():
    return ChatService(pipeline=run_demo_pipeline)


def signing_key():
    directory = ROOT / ".runtime"
    directory.mkdir(exist_ok=True)
    path = directory / "signing-key"
    try:
        with path.open("xb") as file:
            file.write(secrets.token_bytes(32))
    except FileExistsError:
        pass
    key = path.read_bytes()
    if len(key) != 32:
        raise RuntimeError("Invalid local demo signing key.")
    return key


class MemoryRepository:
    def __enter__(self):
        self.store = PostgresMemoryStore()
        try:
            self.store.ensure_schema()
            with self.store.connection.transaction():
                self.store.connection.execute(MEDIA_SCHEMA_SQL, prepare=False)
        except Exception:
            self.store.close()
            raise
        return self

    def __exit__(self, *_):
        self.store.close()

    def sessions(self, owner):
        with self.store.connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute("""SELECT s.session_id, s.updated_at, s.trip_state,
                (SELECT left(content, 70) FROM vistra_memory_messages m
                 WHERE m.user_id=s.user_id AND m.session_id=s.session_id AND m.role='user'
                 ORDER BY turn LIMIT 1) AS title
                FROM vistra_memory_sessions s WHERE s.user_id=%s AND EXISTS
                (SELECT 1 FROM vistra_memory_messages m WHERE m.user_id=s.user_id AND m.session_id=s.session_id)
                ORDER BY s.updated_at DESC LIMIT 50""", (owner,))
            return [{"id": row["session_id"], "title": row["title"] or "New conversation",
                     "updated_at": row["updated_at"].isoformat(),
                     "destination": row["trip_state"].get("destination")} for row in cursor.fetchall()]

    def detail(self, owner, session):
        snapshot = self.store.load_session(owner, session, message_limit=20)
        with self.store.connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute("""SELECT turn, role, content FROM vistra_memory_messages
                WHERE user_id=%s AND session_id=%s ORDER BY turn DESC,
                CASE role WHEN 'assistant' THEN 0 ELSE 1 END LIMIT 100""", (owner, session))
            messages = list(reversed(cursor.fetchall()))
            cursor.execute("SELECT turn, media FROM vistra_demo_reply_media WHERE user_id=%s AND session_id=%s",
                           (owner, session))
            media = {row["turn"]: row["media"] for row in cursor.fetchall()}
        for message in messages:
            if message["role"] == "assistant":
                message.update(public_media(media.get(message["turn"], {}), session, message["turn"]))
        return {"messages": messages, "version": snapshot.version,
                "summary": snapshot.summary, "trip_state": snapshot.trip_state.model_dump(),
                "memories": [record.model_dump() for record in self.store.recall(owner)]}

    def clear(self, owner, session):
        with self.store.connection.transaction():
            self.store.clear_session(owner, session)
            self.store.connection.execute("DELETE FROM vistra_demo_reply_media WHERE user_id=%s AND session_id=%s", (owner, session))

    def save_media(self, owner, session, turn, media):
        checked = ReplyMedia.model_validate(media).model_dump()
        with self.store.connection.transaction():
            cursor = self.store.connection.execute("""INSERT INTO vistra_demo_reply_media (user_id, session_id, turn, media)
                SELECT %s, %s, %s, %s WHERE EXISTS (SELECT 1 FROM vistra_memory_messages
                    WHERE user_id=%s AND session_id=%s AND turn=%s AND role='assistant')
                ON CONFLICT (user_id, session_id, turn) DO UPDATE SET media=EXCLUDED.media""",
                (owner, session, turn, Jsonb(checked), owner, session, turn))
            return cursor.rowcount > 0

    def photo(self, owner, session, turn, index):
        with self.store.connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute("""SELECT r.media FROM vistra_demo_reply_media r JOIN vistra_memory_messages m
                ON (r.user_id,r.session_id,r.turn)=(m.user_id,m.session_id,m.turn) AND m.role='assistant'
                WHERE r.user_id=%s AND r.session_id=%s AND r.turn=%s""", (owner, session, turn))
            row = cursor.fetchone()
        if row is None:
            return None
        cards = ReplyMedia.model_validate(row["media"]).cards
        return cards[index].photo_url if index < len(cards) else None

    def forget(self, owner, kind, key):
        return self.store.delete_memory(owner, kind, key)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    session_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    message: str = Field(min_length=1, max_length=8000)
    retrieval_limit: int = Field(default=20, ge=1, le=20)


def public_error(error):
    if isinstance(error, MemoryConflict):
        return "This conversation changed in another window. Reload it and try again."
    if isinstance(error, UnsafeInput):
        return str(error)
    return "VISTRA could not finish this request. Check the local services and API settings, then try again."


def public_stage(event):
    step = event.get("step", "")
    if step.startswith('planning_'):
        if step == 'planning_tool':
            return 'Looking up ' + {'GuideSearch': 'destination guides', 'AttractionSearch': 'attractions',
                'RestaurantSearch': 'restaurants', 'AccommodationSearch': 'accommodation', 'FlightSearch': 'flights',
                'DistanceMatrix': 'transport routes', 'CitySearch': 'cities', 'WeatherSearch': 'weather'}.get(event.get('tool'), 'trip information')
        return {'planning_details': 'Checking trip details', 'planning_select': 'Choosing the next planning tool',
            'planning_notebook': 'Recording planning evidence', 'planning_plan': 'Building your itinerary',
            'planning_validate': 'Checking itinerary constraints', 'planning_repair': 'Adjusting the itinerary'}.get(step, 'Planning your trip')
    if step.startswith("location"):
        return "Finding your destination"
    if step == "generation" or step == "vision":
        return "Writing your reply"
    if step == "context":
        return "Organizing the travel sources"
    if step == 'weather':
        return 'Checking weather for your trip'
    if step == "output":
        return "Saving your conversation"
    if step == "response_routing":
        return "Choosing how to help"
    return "Looking through travel sources"


def create_app(*, chat_factory=make_demo_chat, repository_factory=MemoryRepository, photo_fetcher=fetch_photo, secret=None):
    key = secret if secret is not None else signing_key()
    owners_busy = set()
    guard = threading.Lock()
    photo_guard = threading.Lock()
    photo_slots = threading.BoundedSemaphore(4)
    photo_cache = {}
    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="vistra-demo")

    @asynccontextmanager
    async def lifespan(_):
        yield
        executor.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(title="VISTRA local demo", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    def token_for(owner):
        return owner + "." + hmac.new(key, owner.encode(), hashlib.sha256).hexdigest()

    def token_owner(token):
        try:
            owner, signature = token.rsplit(".", 1)
            checked_identity(owner, "owner")
            if not owner.startswith("web_") or not hmac.compare_digest(token_for(owner), token):
                return None
            return owner
        except (AttributeError, ValueError, UnsafeInput):
            return None

    @app.middleware("http")
    async def local_boundary(request, call_next):
        if request.url.hostname not in {"localhost", "127.0.0.1"}:
            return JSONResponse({"error": "Use this demo on localhost."}, status_code=400)
        request.state.owner = token_owner(request.cookies.get(COOKIE))
        new_owner = None
        if request.state.owner is None and request.method == "GET" and request.url.path in {"/", "/api/bootstrap"}:
            new_owner = "web_" + secrets.token_hex(16)
            request.state.owner = new_owner
        if request.method in {"POST", "DELETE"}:
            origin = request.headers.get("origin")
            if request.headers.get("x-vistra-demo") != "1" or (origin is not None and origin != f"{request.url.scheme}://{request.url.netloc}"):
                return JSONResponse({"error": "Reload the demo before making changes."}, status_code=403)
            if request.method == "POST":
                try:
                    size = int(request.headers.get("content-length", "-1"))
                except ValueError:
                    size = -1
                if not 0 <= size <= 40000:
                    return JSONResponse({"error": "The message is too large or has no declared size."}, status_code=413)
            if request.state.owner is None:
                return JSONResponse({"error": "Open the demo first to start a private browser session."}, status_code=401)
        response = await call_next(request)
        if new_owner:
            response.set_cookie(COOKIE, token_for(new_owner), httponly=True, samesite="strict",
                                max_age=365 * 24 * 3600, path="/")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_, __):
        return JSONResponse({"error": "Use a message up to 8,000 characters, a valid conversation ID, and a result limit from 1 to 20."}, status_code=422)

    @app.exception_handler(UnsafeInput)
    async def unsafe_request(_, error):
        return JSONResponse({"error": public_error(error)}, status_code=400)

    def owner_of(request):
        if request.state.owner is None:
            raise HTTPException(401, "Open the demo first.")
        return request.state.owner

    def reserve(owner):
        with guard:
            if owner in owners_busy:
                raise HTTPException(409, "Wait for the current reply to finish.")
            if len(owners_busy) >= 2:
                raise HTTPException(429, "The demo is busy. Try again shortly.")
            owners_busy.add(owner)

    def release(owner):
        with guard:
            owners_busy.discard(owner)

    @app.get("/")
    def index():
        return FileResponse(ROOT / "static" / "index.html")

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/bootstrap")
    def bootstrap(request: Request):
        try:
            with repository_factory() as repository:
                sessions = repository.sessions(owner_of(request))
            return {"ready": True, "sessions": sessions}
        except Exception as error:
            return JSONResponse({"ready": False, "sessions": [], "error": public_error(error)}, status_code=503)

    @app.get("/api/sessions")
    def sessions(request: Request):
        try:
            with repository_factory() as repository:
                return {"sessions": repository.sessions(owner_of(request))}
        except HTTPException:
            raise
        except Exception as error:
            return JSONResponse({"error": public_error(error)}, status_code=503)

    @app.get("/api/sessions/{session_id}")
    def detail(request: Request, session_id: str):
        checked_identity(session_id, "session_id")
        owner = owner_of(request)
        try:
            with repository_factory() as repository:
                result = repository.detail(owner, session_id)
            with guard:
                result["busy"] = owner in owners_busy
            return result
        except Exception as error:
            return JSONResponse({"error": public_error(error)}, status_code=503)

    @app.delete("/api/sessions/{session_id}")
    def clear(request: Request, session_id: str):
        checked_identity(session_id, "session_id")
        owner = owner_of(request)
        reserve(owner)
        try:
            with repository_factory() as repository:
                repository.clear(owner, session_id)
            return {"ok": True}
        except Exception as error:
            return JSONResponse({"error": public_error(error)}, status_code=503)
        finally:
            release(owner)

    @app.delete("/api/memory/{kind}/{memory_key}")
    def forget(request: Request, kind: Literal["preference", "trip"], memory_key: str):
        checked_identity(memory_key, "memory_key")
        owner = owner_of(request)
        reserve(owner)
        try:
            with repository_factory() as repository:
                removed = repository.forget(owner, kind, memory_key)
            return {"ok": True, "removed": removed}
        except Exception as error:
            return JSONResponse({"error": public_error(error)}, status_code=503)
        finally:
            release(owner)

    @app.get("/api/photos/{session_id}/{turn}/{index}")
    def photo(request: Request, session_id: str, turn: Annotated[int, APIPath(ge=1)], index: Annotated[int, APIPath(ge=0, le=19)]):
        checked_identity(session_id, "session_id")
        owner = owner_of(request)
        try:
            with repository_factory() as repository:
                url = repository.photo(owner, session_id, turn, index)
            if not url:
                raise HTTPException(404, "This photo is unavailable.")
            url = checked_photo_url(url)
            with photo_guard:
                cached = photo_cache.get(url)
            if cached and monotonic() - cached[0] < 600:
                return Response(cached[1], media_type="image/jpeg")
            if not photo_slots.acquire(timeout=5):
                raise HTTPException(503, "Photo loading is busy.")
            try:
                import httpx
                from src.security.images import load_safe_image
                with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
                    with photo_fetcher(url, client=client) as image:
                        with load_safe_image(image) as clean:
                            clean.thumbnail((640, 480))
                            with BytesIO() as output:
                                clean.save(output, format="JPEG", quality=82)
                                data = output.getvalue()
                with photo_guard:
                    if len(photo_cache) >= 32:
                        photo_cache.pop(next(iter(photo_cache)))
                    photo_cache[url] = (monotonic(), data)
                return Response(data, media_type="image/jpeg")
            finally:
                photo_slots.release()
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(404, "This photo could not be loaded.") from None

    @app.post("/api/chat")
    async def chat(request: Request, body: ChatRequest):
        message = validate_text(body.message, check_intent=True)
        owner = owner_of(request)
        reserve(owner)
        events = queue.Queue(maxsize=128)

        def offer(kind, data):
            item = (kind, data)
            try:
                events.put_nowait(item)
            except queue.Full:
                # Slow/disconnected clients must never block a completed memory commit.
                try:
                    events.get_nowait()
                except queue.Empty:
                    pass
                events.put_nowait(item)

        def work():
            previous_stage = None
            def progress(event):
                nonlocal previous_stage
                if event.get("status") != "running" and event.get("step") != "output":
                    return
                stage = public_stage(event)
                if stage != previous_stage:
                    offer("progress", {"message": stage})
                    previous_stage = stage
            try:
                offer("progress", {"message": "Remembering the conversation"})
                from src.generation.streaming import answer_stream
                with chat_factory() as service, answer_stream(lambda data: offer('answer', data)):
                    result = service.chat(owner, body.session_id, message, limit=body.retrieval_limit, on_progress=progress)
                sources = [{"id": source["source_id"], "name": source.get("name") or "Travel source",
                            "url": source.get("url")} for source in reply_sources(result)]
                weather = result.get('weather') or (result.get('context') or {}).get('weather')
                metadata = result.get("chat") or {}
                version = metadata.get("version")
                cards = build_place_cards(result, body.retrieval_limit)
                media = {"sources": sources[:72], "cards": cards, 'weather': weather,
                         'itinerary': result.get('itinerary'), 'planning': result.get('planning')}
                media_error = None
                try:
                    with repository_factory() as repository:
                        if not repository.save_media(owner, body.session_id, version, media):
                            raise RuntimeError("The turn changed before its media was saved.")
                    display_media = public_media(media, body.session_id, version)
                except Exception:
                    display_media = {"sources": sources, "cards": [], 'weather': weather,
                                     'itinerary': result.get('itinerary'), 'planning': result.get('planning')}
                    media_error = "The reply was saved, but its travel details could not be saved for reload."
                offer("result", {"answer": result["answer"], **display_media, "media_error": media_error,
                    "version": metadata.get("version"),
                    "recalled_messages": metadata.get("recalled_messages", 0),
                    "recalled_long_term": metadata.get("recalled_long_term", 0),
                    "needs_clarification": result.get("pipeline_status") == "needs_clarification"})
            except Exception as error:
                import traceback
                LOGGER.error('Chat request failed:\n%s', CredentialRedactor().text(traceback.format_exc()))
                offer("error", {"message": public_error(error)})
            finally:
                release(owner)

        try:
            executor.submit(work)
        except Exception:
            release(owner)
            raise HTTPException(503, "The demo is shutting down.") from None

        async def stream():
            while not await request.is_disconnected():
                try:
                    kind, data = await asyncio.to_thread(events.get, True, 10)
                except queue.Empty:
                    yield ": keep-alive\n\n"
                    continue
                yield f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                if kind in {"result", "error"}:
                    return

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    app.mount("/assets", StaticFiles(directory=ROOT / "static"), name="assets")
    return app


app = create_app()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Port must be between 1 and 65535.")
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=args.port, proxy_headers=False)
