"""Local chat demo. Run with one Uvicorn worker."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, closing
from functools import partial
from io import BytesIO
import logging
import json
import os
from pathlib import Path
import re
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import httpx
from openai import APIConnectionError, AuthenticationError, RateLimitError
from PIL import Image, ImageOps, UnidentifiedImageError
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse

from src.service.chat_service import ChatService
from src.database.sqlite_metadata_store import SQLiteStore
from src.utils.utils import PROJECT_ROOT, load_config, resolve_path


STATIC = Path(__file__).parent / "static"
logger = logging.getLogger(__name__)


def save_image(data: bytes, root: Path, max_edge: int) -> Path:
    try:
        with Image.open(BytesIO(data)) as opened:
            if opened.format not in {"JPEG", "PNG", "WEBP"} or opened.width * opened.height > 20_000_000:
                raise ValueError("Use a JPEG, PNG, or WebP image under 20 megapixels.")
            with ImageOps.exif_transpose(opened).convert("RGB") as image:
                image.thumbnail((max_edge, max_edge))
                root.mkdir(parents=True, exist_ok=True)
                path = root / f"{uuid4().hex}.jpg"
                image.save(path, "JPEG", quality=85)
                return path
    except (ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombError) as error:
        raise HTTPException(400, "Use a readable JPEG, PNG, or WebP image under 20 megapixels.") from error


def create_app(service_factory=ChatService, config: dict | None = None) -> FastAPI:
    config = load_config() if config is None else config
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    upload_root = resolve_path(config["app_uploads_path"])
    photo_root = resolve_path(config["scraped_photos_directory"])
    user_id = config["app_user_id"]
    service = None
    busy, tasks = set(), set()

    def get_service():
        nonlocal service
        if service is None:
            service = service_factory()
        return service

    @asynccontextmanager
    async def lifespan(app):
        app.state.pool = ThreadPoolExecutor(max_workers=1)
        try:
            yield
        finally:
            await asyncio.gather(*tasks, return_exceptions=True)
            if service is not None:
                await asyncio.get_running_loop().run_in_executor(app.state.pool, service.close)
            app.state.pool.shutdown(wait=True)

    app = FastAPI(title="VISTRA chat", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    async def work(function):
        # Keep model and database operations on the same worker, away from the event loop.
        try:
            return await asyncio.get_running_loop().run_in_executor(app.state.pool, function)
        except HTTPException:
            raise
        except AuthenticationError as error:
            raise HTTPException(502, "OpenAI rejected the API key. Check OPENAI_API_KEY and restart the backend.") from error
        except RateLimitError as error:
            raise HTTPException(429, "OpenAI's quota or rate limit was reached. Check your account and try again.") from error
        except APIConnectionError as error:
            raise HTTPException(502, "OpenAI could not be reached. Please try again.") from error
        except (ResponseHandlingException, UnexpectedResponse) as error:
            logger.exception("Qdrant request failed")
            raise HTTPException(503, "Qdrant is unavailable. Start Docker/Qdrant and check qdrant_url in config.yaml.") from error
        except httpx.HTTPError as error:
            logger.exception("Router request failed")
            raise HTTPException(502, "Routing failed. Check the connection and JEV_API_KEY.") from error
        except Exception as error:
            logger.exception("Chat request failed")
            raise HTTPException(500, "The message could not finish. Check the backend terminal for details, then try again.") from error

    def image_url(path: str | None) -> str | None:
        if path:
            image = resolve_path(path)
            if image.parent == upload_root and image.is_file() and re.fullmatch(r"[a-f0-9]{32}\.jpg", image.name):
                return f"/api/uploads/{image.name}"
        return None

    def display_images(images: list[dict]) -> list[dict]:
        return [{
            "image_url": f"/api/photos/{photo['image_id']}",
            **{key: photo.get(key) for key in ("name", "result_number", "place_url", "source_url", "attribution", "author", "license")},
        } for photo in images if re.fullmatch(r"[a-f0-9]{64}", str(photo.get("image_id", "")))]

    def indexed_photo_path(image_id: str) -> Path:
        with closing(SQLiteStore(path=config["sqlite_path"], read_only=True)) as store:
            record = store.get_image(image_id)
        for candidate in [record.get("path"), *[item.get("path") for item in record.get("provenance", [])]] if record else []:
            if candidate:
                path = resolve_path(candidate)
                if path.is_relative_to(photo_root) and path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
                    return path
        raise HTTPException(404, "The indexed photo is not available locally.")

    def display_places(places: list[dict], images: list[dict]) -> list[dict]:
        photos = {image.get("image_id"): image for image in images}
        output = []
        for place in places:
            photo = photos.get(place.get("image_id"))
            displayed = display_images([photo]) if photo else []
            output.append({
                **{key: place.get(key) for key in (
                    "entity_id", "result_number", "name", "source", "latitude", "longitude",
                    "url", "category", "province", "address", "excerpt",
                )},
                "image": displayed[0] if displayed else None,
            })
        return output

    def message_places(message: dict) -> list[dict]:
        places = message.get("places")
        if places is None:
            sources = next((event.get("sources", []) for event in message.get("activity", []) if event["step"] == "context"), [])
            places = [{**source, **source.get("place_metadata", {}),
                       "image_id": next((photo.get("image_id") for photo in source.get("images", [])), None)} for source in sources]
            if not places:
                places = [{**photo, "source": "image", "url": photo.get("place_url")} for photo in message.get("images", [])]
        return display_places(places, message.get("images", []))

    def conversation_view(conversation_id: str) -> dict:
        conversation = get_service().long_term.load_conversation(conversation_id, user_id, None)
        if conversation is None:
            raise HTTPException(404, "Conversation not found. Start a new chat.")
        messages = [{
            "role": message["role"],
            "content": message["content"],
            "image_url": image_url(message.get("image_path")),
            "images": display_images(message.get("images", [])),
            "activity": message.get("activity", []),
            "places": message_places(message) if message["role"] == "assistant" else [],
        } for message in conversation.messages]
        # Older image turns saved their results, but did not yet save photo attachments.
        if len(messages) >= 2 and "images" not in conversation.messages[-1] and conversation.messages[-2].get("image_path") and conversation.search_response:
            photos = ChatService.result_images(conversation.search_response)
            messages[-1]["images"] = display_images(photos)
            messages[-1]["places"] = display_places(ChatService.result_places(conversation.search_response), photos)
        return {
            "id": conversation_id,
            "title": messages[0]["content"][:72] if messages else "New chat",
            "messages": messages,
        }

    def turn(conversation_id: str, message: str, data: bytes | None, on_progress=None) -> dict:
        chat_service = get_service()
        if chat_service.long_term.load_conversation(conversation_id, user_id, 0) is None:
            raise HTTPException(404, "Conversation not found. Start a new chat.")
        path = save_image(data, upload_root, config["app_image_max_edge"]) if data is not None else None
        try:
            result = chat_service.chat(message, conversation_id=conversation_id, user_id=user_id, image=path, on_progress=on_progress)
        except Exception:
            if path is not None:
                path.unlink(missing_ok=True)
            raise
        return {
            "conversation_id": result.conversation_id,
            "answer": result.answer,
            "image_url": image_url(str(path) if path else None),
            "images": display_images(result.images),
            "activity": result.activity,
            "places": display_places(result.places, result.images),
        }

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/health")
    async def health():
        return {
            "model": config["openai_model"],
            "ai_configured": bool(os.getenv("OPENAI_API_KEY")),
            "max_upload_mb": config["app_max_upload_mb"],
            "max_message_chars": config["app_message_max_chars"],
            "map": {
                "tile_url": config.get("app_map_tile_url", "https://tile.openstreetmap.org/{z}/{x}/{y}.png"),
                "attribution": config.get("app_map_attribution", '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'),
                "center": config.get("app_map_center", [16.0, 106.0]),
                "zoom": config.get("app_map_zoom", 5),
            },
        }

    @app.get("/api/conversations")
    async def list_conversations():
        return await work(lambda: get_service().long_term.list_conversation_summaries(user_id))

    @app.post("/api/conversations", status_code=201)
    async def new_conversation():
        conversation_id = await work(lambda: get_service().new_conversation(user_id))
        return {"id": conversation_id, "title": "New chat", "messages": []}

    @app.get("/api/conversations/{conversation_id}")
    async def get_conversation(conversation_id: str):
        result = await work(partial(conversation_view, conversation_id))
        return {**result, "busy": conversation_id in busy}

    @app.get("/api/memories")
    async def memories():
        return await work(lambda: get_service().memories(user_id))

    @app.delete("/api/conversations/{conversation_id}")
    async def delete_conversation(conversation_id: str):
        if conversation_id in busy:
            raise HTTPException(409, "Wait for the current response before deleting this chat.")
        busy.add(conversation_id)

        async def remove():
            try:
                deleted = await work(lambda: get_service().delete_conversation(conversation_id, user_id))
                if not deleted:
                    raise HTTPException(404, "Conversation not found.")
                return {"id": conversation_id, "deleted": True}
            finally:
                busy.discard(conversation_id)

        task = asyncio.create_task(remove())
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return await asyncio.shield(task)

    @app.post("/api/chat")
    async def chat(
        conversation_id: str = Form(...),
        message: str = Form(""),
        image: UploadFile | None = File(None),
        stream: bool = Form(False),
    ):
        message = message.strip()
        if not message and image is None:
            raise HTTPException(400, "Add a message or an image.")
        if len(message) > config["app_message_max_chars"]:
            raise HTTPException(400, f"Keep your message under {config['app_message_max_chars']} characters.")
        data = None
        if image is not None:
            try:
                data = await image.read(config["app_max_upload_mb"] * 1024 * 1024 + 1)
            finally:
                await image.close()
            if len(data) > config["app_max_upload_mb"] * 1024 * 1024:
                raise HTTPException(413, f"Use an image under {config['app_max_upload_mb']} MB.")
        if conversation_id in busy:
            raise HTTPException(409, "Wait for the current response before sending another message.")
        if len(busy) >= config["app_max_conversations"]:
            raise HTTPException(503, "The demo is busy. Please try again shortly.")
        busy.add(conversation_id)
        queue = asyncio.Queue() if stream else None
        loop = asyncio.get_running_loop()

        def progress(event):
            loop.call_soon_threadsafe(queue.put_nowait, {"type": "activity", "activity": event})

        async def run_turn():
            try:
                result = await work(partial(turn, conversation_id, message, data, progress if stream else None))
                if stream:
                    await queue.put({"type": "result", **result})
                else:
                    return result
            except HTTPException as error:
                if not stream:
                    raise
                await queue.put({"type": "error", "detail": error.detail})
            finally:
                busy.discard(conversation_id)
                if stream:
                    await queue.put(None)

        task = asyncio.create_task(run_turn())
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        if stream:
            async def events():
                while (event := await queue.get()) is not None:
                    yield json.dumps(event, ensure_ascii=False) + "\n"

            return StreamingResponse(events(), media_type="application/x-ndjson", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        return await asyncio.shield(task)

    @app.get("/api/photos/{image_id}")
    async def indexed_photo(image_id: str):
        if not re.fullmatch(r"[a-f0-9]{64}", image_id):
            raise HTTPException(404, "Photo not found.")
        path = await work(partial(indexed_photo_path, image_id))
        return FileResponse(path)

    @app.get("/api/uploads/{filename}")
    async def uploaded_image(filename: str):
        if not re.fullmatch(r"[a-f0-9]{32}\.jpg", filename):
            raise HTTPException(404, "Image not found.")
        path = upload_root / filename
        if not path.is_file():
            raise HTTPException(404, "Image not found.")
        return FileResponse(path, media_type="image/jpeg")

    return app


app = create_app()
