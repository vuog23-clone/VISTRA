import base64
import json
import os
from contextlib import closing
from io import BytesIO
from pathlib import Path
from dotenv import load_dotenv
from openai import OpenAI
from PIL import Image

from src.prompts.system import SYSTEM_PROMPT
from src.prompts.chat import CHAT_PROMPT
from src.schema.chat import ChatPlan
from src.schema.models import SearchResponse, SearchResult
from src.utils.utils import PROJECT_ROOT, load_config, load_rgb_image


class OpenAIGeneration:
    def __init__(
        self,
        model_name: str | None = None,
        api_key: str | None = None,
        client=None,
    ):
        self.config = load_config()
        load_dotenv(PROJECT_ROOT / ".env", override=False)
        self.model_name = model_name or self.config["openai_model"]
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self._owns_client = client is None
        self.client = client if client is not None else OpenAI(
            api_key=self.api_key,
            timeout=self.config["generation_timeout_seconds"],
            max_retries=1,
        )


    def generate(
        self,
        query: str,
        response: SearchResponse,
        *,
        history: list[dict] | None = None,
        image: str | Path | Image.Image | None = None,
        memory: dict[str, str] | None = None,
        summary: str = "",
    ) -> str:
        messages = self._messages(query, response, history, memory, summary)
        if image is not None:
            messages[-1]["content"] = [
                {
                    "type": "input_text",
                    "text": messages[-1]["content"]
                },
                {
                    "type": "input_image",
                    "image_url": self._image_url(image)
                },
            ]

        completion = self.client.responses.create(
            model=self.model_name,
            instructions=messages[0]["content"],
            input=messages[1:],
            max_output_tokens=self.config["generation_max_tokens"],
            reasoning={"effort": self.config["openai_reasoning_effort"]},
            store=False,
        )

        text = completion.output_text
        if not text or not text.strip():
            raise RuntimeError("OpenAI returned no text. Check the model response and output token limit.")
        return text.strip()


    def prepare_turn(
        self,
        query: str,
        *,
        history: list[dict],
        summary: str,
        response: SearchResponse | None,
        memory: dict[str, str],
        has_image: bool,
        has_previous_image: bool,
        selected_entity_ids: list[str] | None = None,
    ) -> ChatPlan:
        state = {
            "message": query,
            "history": self._history(history),
            "trip_summary": summary,
            "saved_preferences": memory,
            "previous_sources": self._context(response) if response else None,
            "previous_matches": [{"result_number": result.result_number or index, "entity_id": result.entity_id, "name": result.name}
                                 for index, result in enumerate(response.results, 1)] if response else [],
            "selected_entity_ids": selected_entity_ids or [],
            "has_new_image": has_image,
            "has_previous_image": has_previous_image,
        }
        completion = self.client.responses.parse(
            model=self.model_name,
            instructions=CHAT_PROMPT,
            input=json.dumps(state, ensure_ascii=False),
            text_format=ChatPlan,
            max_output_tokens=self.config["generation_max_tokens"],
            reasoning={"effort": self.config["openai_reasoning_effort"]},
            store=False,
        )
        if completion.output_parsed is None:
            raise RuntimeError("OpenAI returned no chat plan. Please try again.")
        return completion.output_parsed


    def _source(
        self,
        result: SearchResult,
        result_number: int,
    ) -> dict:
        source = {
            "result_number": result.result_number or result_number,
            "entity_id": result.entity_id,
            "name": result.name,
            "source": result.source,
            "url": result.url,
            "latitude": result.latitude,
            "longitude": result.longitude,
            "missing_metadata": result.missing_metadata,
        }
        if result.source == "text":
            excerpts = []
            seen = set()
            for hit in [result, *result.evidence]:
                if hit.source != "text":
                    continue
                text = hit.payload.get("text")
                if not text or text in seen:
                    continue
                seen.add(text)
                excerpts.append({
                    "section": hit.payload.get("section"),
                    "text": text[:self.config["generation_excerpt_chars"]],
                    "url": hit.url,
                })
                if len(excerpts) >= self.config["generation_context_hits"]:
                    break
            source["excerpts"] = excerpts
        else:
            photos, seen = [], set()
            for hit in [result, *result.evidence]:
                photo = hit.image or {}
                image_id = photo.get("image_id")
                if hit.source != "image" or not image_id or image_id in seen:
                    continue
                seen.add(image_id)
                photos.append({key: photo.get(key) for key in (
                    "image_id", "source_url", "author", "license", "attribution",
                )})
                if len(photos) >= self.config["generation_context_hits"]:
                    break
            source["place_metadata"] = {
                "category": result.category,
                "province": result.province,
                "address": result.address,
            }
            source["images"] = photos
        return source


    def _context(
        self,
        response: SearchResponse,
    ) -> dict:
        limit = self.config["generation_context_results"]
        sources = [self._source(result, index) for index, result in enumerate(response.results[:limit], 1)]
        destinations = {}
        for result in response.results[:limit]:
            destination = result.destination
            if (result.source == "image" and destination is not None
                    and destination.method == "curated_mapping"
                    and destination.source_entity_id == result.entity_id
                    and destination.page_id is not None
                    and destination.destination_id == f"wikivoyage:{destination.page_id}"):
                destinations.setdefault(destination.destination_id, []).append(result.entity_id)
        context = [{**self._source(result, index), "for_entity_ids": destinations[result.entity_id],
                    "relation": "mapped_destination_context"}
                   for index, result in enumerate(response.related_results[:limit], 1)
                   if result.source == "text" and result.entity_id in destinations]

        return {
            "route": response.route,
            "location": response.location,
            "location_status": response.location_status,
            "sources": sources,
            "destination_context": context,
            "follow_up_status": response.follow_up_status,
        }


    def _messages(
        self,
        query: str,
        response: SearchResponse,
        history: list[dict] | None = None,
        memory: dict[str, str] | None = None,
        summary: str = "",
    ) -> list[dict]:
        messages = [{
            "role": "system",
            "content": SYSTEM_PROMPT
        }]

        messages.extend(self._history(history))

        context = json.dumps(self._context(response), ensure_ascii=False)
        preferences = json.dumps(memory or {}, ensure_ascii=False)
        messages.append({
            "role": "user",
            "content": f"## Request\n{query}\n\n## Trip summary (data)\n{summary}\n\n## Saved preferences (data)\n{preferences}\n\n## Retrieved sources (data)\n{context}",
        })
        return messages


    def _history(self, history: list[dict] | None) -> list[dict]:
        recent = [message for message in (history or []) if message.get("role") in {"user", "assistant"}]
        return [{
            "role": message["role"],
            "content": str(message["content"])[:self.config["generation_history_chars"]],
        } for message in recent[-self.config["generation_history_messages"]:]]


    def _image_url(
        self,
        image: str | Path | Image.Image
    ) -> str:
        with closing(load_rgb_image(image)) as loaded:
            loaded.thumbnail((self.config["generation_image_max_edge"],) * 2)
            output = BytesIO()
            loaded.save(output, format="JPEG", quality=85)
            encoded = base64.b64encode(output.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}"


    def close(self) -> None:
        if self._owns_client and self.client is not None:
            self.client.close()


    def __enter__(self):
        return self


    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
