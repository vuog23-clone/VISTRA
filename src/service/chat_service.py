from contextlib import ExitStack
from contextvars import ContextVar
from dataclasses import asdict
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from src.database.chat_memory_store import ChatMemoryStore
from src.memory.short_term_memory import ShortTermMemory
from src.resolver.location_resolver import LocationResolver
from src.schema.chat import ChatResponse, ChatState, Conversation, MemoryFact, restore_search_response
from src.schema.models import RetrievalRoute, SearchResponse
from src.utils.utils import load_config, resolve_path


_progress = ContextVar("chat_progress", default=None)


class ChatService:
    def __init__(
        self,
        search_service=None,
        generator=None,
        memory_store: ChatMemoryStore | None = None,
        short_term_memory: ShortTermMemory | None = None,
        memory_path: str | Path | None = None,
    ):
        self.config = load_config()
        self._resources = ExitStack()
        self._closed = False
        self._search_service = search_service
        self._generator = generator
        self.short_term = short_term_memory if short_term_memory is not None else ShortTermMemory()
        self.long_term = memory_store if memory_store is not None else ChatMemoryStore(memory_path)
        if memory_store is None:
            self._resources.callback(self.long_term.close)
        workflow = StateGraph(ChatState)
        workflow.add_node("respond", self._respond)
        workflow.add_edge(START, "respond")
        workflow.add_edge("respond", END)
        self.graph = workflow.compile(checkpointer=self.short_term.checkpointer)


    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("ChatService is closed.")


    def _search(self):
        if self._search_service is None:
            from src.service.search_service import SearchService

            self._search_service = SearchService(text_embedder_factory=self.long_term.get_embedder)
            self._resources.callback(self._search_service.close)
        return self._search_service


    def _generation(self):
        if self._generator is None:
            from src.generation.openai_generation import OpenAIGeneration

            self._generator = OpenAIGeneration()
            self._resources.callback(self._generator.close)
        return self._generator


    def new_conversation(self, user_id: str = "local") -> str:
        self._check_open()
        conversation = Conversation(conversation_id=str(uuid4()), user_id=user_id)
        self.long_term.create_conversation(conversation)
        self.short_term.save(conversation, self.graph)
        return conversation.conversation_id


    def get_conversation(self, conversation_id: str, user_id: str = "local") -> Conversation:
        self._check_open()
        conversation = self.short_term.get(conversation_id, user_id, self.graph)
        if conversation is None:
            conversation = self.long_term.load_conversation(conversation_id, user_id, self.short_term.max_messages)
            if conversation is None:
                raise ValueError("Conversation not found for this user.")
            self.short_term.save(conversation, self.graph)
        return conversation


    def delete_conversation(self, conversation_id: str, user_id: str = "local") -> bool:
        self._check_open()
        if not self.long_term.delete_conversation(conversation_id, user_id):
            return False
        self.short_term.delete(conversation_id, user_id)
        return True


    def memories(self, user_id: str = "local") -> dict[str, str]:
        self._check_open()
        facts = self.long_term.get_memories(user_id, self.config["chat_memory_limit"])
        return {name: value[:self.config["chat_memory_fact_chars"]] for name, value in facts.items()}


    def remember(self, name: str, value: str, user_id: str = "local") -> None:
        self._check_open()
        name, value = name.strip(), value.strip()
        if not name or not value:
            raise ValueError("A memory needs a name and a value.")
        self.long_term.remember(user_id, name[:80], value[:self.config["chat_memory_fact_chars"]])


    def forget(self, name: str, user_id: str = "local") -> None:
        self._check_open()
        self.long_term.forget(user_id, name.strip())


    def chat(
        self,
        message: str | None = None,
        *,
        conversation_id: str | None = None,
        user_id: str = "local",
        image: str | Path | None = None,
        route: RetrievalRoute | None = None,
        limit: int | None = None,
        on_progress=None,
    ) -> ChatResponse:
        self._check_open()
        image_path = str(resolve_path(image)) if image is not None else None
        if image_path is not None and not Path(image_path).is_file():
            raise FileNotFoundError(f"Image not found: {image_path}")
        message = (message or "").strip()
        if not message:
            if image_path is None:
                raise ValueError("Provide a message or an image.")
            message = "Find places with scenery like this image."

        conversation = (
            self.get_conversation(conversation_id, user_id) if conversation_id
            else Conversation(conversation_id=str(uuid4()), user_id=user_id)
        )
        token = _progress.set(on_progress)
        try:
            inputs = {
                "conversation": asdict(conversation),
                "message": message,
                "image_path": image_path,
                "route": route,
                "limit": limit,
            }
            config = self.short_term.config(conversation.conversation_id, user_id)
            state = self.graph.invoke(inputs, config)
        except Exception:
            self.short_term.save(conversation, self.graph)
            raise
        finally:
            _progress.reset(token)
        self.short_term.save(Conversation.from_dict(state["conversation"]), self.graph)
        result = state["result"]
        return ChatResponse(
            **{key: value for key, value in result.items() if key != "search_response"},
            search_response=restore_search_response(result["search_response"]),
        )


    @staticmethod
    def result_images(response: SearchResponse) -> list[dict]:
        images, seen = [], set()
        for index, result in enumerate(response.results, 1):
            for hit in [result, *result.evidence]:
                photo = hit.image or {}
                image_id = photo.get("image_id")
                if not image_id or image_id in seen:
                    continue
                seen.add(image_id)
                images.append({
                    **photo,
                    "name": result.name or result.entity_id,
                    "place_url": result.url,
                    "entity_id": result.entity_id,
                    "result_number": result.result_number or index,
                })
                break
        return images


    @staticmethod
    def result_places(response: SearchResponse) -> list[dict]:
        return [{
            "entity_id": result.entity_id,
            "result_number": result.result_number or index,
            "name": result.name or result.entity_id,
            "source": result.source,
            "latitude": result.latitude,
            "longitude": result.longitude,
            "url": result.url,
            "category": result.category,
            "province": result.province,
            "address": result.address,
            "excerpt": str(result.payload.get("text") or "")[:350] if result.source == "text" else "",
            "image_id": next((hit.image["image_id"] for hit in [result, *result.evidence]
                              if hit.image and hit.image.get("image_id")), None),
        } for index, result in enumerate(response.results, 1)]


    def _respond(self, state: ChatState) -> dict:
        writer = _progress.get()
        activity, started = [], {}

        def progress(step: str, title: str, detail: str = "", status: str = "done", **data):
            if status == "running":
                started[step] = perf_counter()
            event = {"step": step, "title": title, "detail": detail, "status": status, **data}
            if status == "done" and step in started:
                event["seconds"] = round(perf_counter() - started[step], 1)
            activity[:] = [item for item in activity if item["step"] != step]
            activity.append(event)
            if writer is not None:
                writer(event)

        conversation = Conversation.from_dict(state["conversation"])
        message, image_path = state["message"], state["image_path"]
        route, limit = state["route"], state["limit"]
        progress("memory", "Reading conversation and saved preferences", status="running")
        memory = self.long_term.search_memories(
            conversation.user_id,
            f"{conversation.summary}\n{message}",
            self.config["chat_memory_limit"],
        )
        progress("memory", "Conversation context loaded", f"{len(conversation.messages)} recent messages; {len(memory)} relevant saved preferences.")
        generation = self._generation()
        previous_image = conversation.image_path
        has_previous_image = previous_image is not None and Path(previous_image).is_file()
        progress("plan", "Planning the request", status="running")
        plan = generation.prepare_turn(
            message,
            history=conversation.messages,
            summary=conversation.summary,
            response=conversation.search_response,
            memory=memory,
            has_image=image_path is not None,
            has_previous_image=has_previous_image,
            selected_entity_ids=conversation.selected_entity_ids,
        )

        active_image = image_path or (previous_image if plan.use_previous_image and has_previous_image else None)
        action = "search" if image_path is not None or route is not None else plan.action
        search_query = plan.search_query.strip() or message
        previous = conversation.search_response
        location = plan.location.strip() if plan.location and plan.location.strip() else None
        search_options = {}
        if location is not None:
            search_options["location"] = location
        if plan.visual_query.strip():
            search_options["visual_query"] = plan.visual_query.strip()
            if active_image is None and route is None:
                route = "image_text"
        references = list(dict.fromkeys(plan.referenced_entity_ids))
        by_id = {result.entity_id: result for result in previous.results} if previous else {}
        follow_up = plan.reference == "selected" or bool(references) or action == "reuse"
        if follow_up and not references:
            references = conversation.selected_entity_ids or (list(by_id) if len(by_id) == 1 else [])
        ambiguous = (plan.reference == "ambiguous"
                     or any(entity_id not in by_id for entity_id in references)
                     or (follow_up and not references))
        location_changed = (previous is not None and location is not None
                            and LocationResolver._normalize(location) != LocationResolver._normalize(previous.location or ""))
        if location_changed:
            action, follow_up, ambiguous = "search", False, False
            references = []

        progress("plan", "Request planned", f"Query: {search_query}\nLocation: {location or 'Unrestricted'}")
        progress("retrieval", "Retrieving sources", "Routing the request and loading the required model if needed.", status="running")
        if image_path is not None:
            response = self._search().search(search_query, image=active_image, route=route, limit=limit, on_progress=lambda event: progress(**event), **search_options)
        elif ambiguous:
            action, active_image = "reply", None
            response = SearchResponse(
                route=previous.route if previous else "text",
                results=previous.results if previous else [],
                follow_up_status="clarification_required",
                location=previous.location if previous else None,
                location_status=previous.location_status if previous else "",
            )
        elif follow_up:
            active_image = None
            selected = [result for result in previous.results if result.entity_id in references]
            page_ids = {result.destination.page_id for result in selected if result.destination is not None}
            response = SearchResponse(
                route=previous.route,
                results=selected,
                related_results=[result for result in previous.related_results if result.place_id in page_ids],
                follow_up_status=previous.follow_up_status,
                location=previous.location,
                location_status=previous.location_status,
            )
            if action == "search":
                response = self._search().search_follow_up(search_query, response, limit=limit)
            else:
                action = "reuse"
        elif action == "search":
            response = self._search().search(search_query, image=active_image, route=route, limit=limit, on_progress=lambda event: progress(**event), **search_options)
            conversation.image_path = active_image
        else:
            action = "reply"
            response = SearchResponse(route="text")

        lookup = (image_path is not None or (action == "search" and not ambiguous)) and response.follow_up_status != "metadata_only"
        title = "Follow-up sources prepared" if follow_up and not ambiguous and image_path is None else "Search completed" if lookup else "Previous sources reused" if response.results else "Retrieval skipped"
        detail = f"Route: {response.route}\n{len(response.results)} accepted places or destinations; {len(response.related_results)} related results."
        if response.location:
            detail += f"\nLocation: {response.location}"
        if response.location_status or response.follow_up_status:
            detail += f"\nStatus: {response.location_status or response.follow_up_status}"
        if not lookup:
            detail += "\nNo new travel search was performed."
        if response.location_status in {"unresolved", "no_indexed_text"}:
            detail += "\nNo travel index query was performed."
        progress("retrieval", title, detail, route=response.route, lookup=lookup)

        if action == "search" and not (follow_up and image_path is None):
            for index, result in enumerate(response.results, 1):
                result.result_number = index

        updates = [MemoryFact(
            name=fact.name.strip()[:80],
            value=fact.value.strip()[:self.config["chat_memory_fact_chars"]],
        ) for fact in plan.memory_updates[:self.config["chat_memory_limit"]] if fact.name.strip() and fact.value.strip()]
        memory.update({fact.name: fact.value for fact in updates})
        summary = plan.summary[:self.config["chat_summary_chars"]]
        limited = action == "search" and response.location_status and not response.results
        context = generation._context(response)
        sources = [*context["sources"], *context["destination_context"]] if not limited else []
        excerpts = sum(len(source.get("excerpts", [])) for source in sources)
        photos = sum(len(source.get("images", [])) for source in sources)
        if limited:
            detail = "The app will return the location data limitation without calling the answer model."
        elif sources:
            detail = f"{excerpts} Wikivoyage excerpts; {photos} photo metadata records; {len(sources)} source entries."
        else:
            detail = "No retrieved travel sources supplied. The model receives your message, conversation context and preferences."
        if not limited:
            detail += "\nUploaded photo pixels sent to the LLM: " + ("yes." if active_image is not None else "no.")
            if photos:
                detail += "\nRetrieved photos are supplied as metadata, not image pixels."
        progress("context", "Retrieved sources supplied" if sources else "No retrieval evidence supplied", detail, sources=sources)
        if limited:
            reasons = {
                "unresolved": "I could not reliably resolve this location from the available metadata.",
                "no_indexed_images": "I have no indexed photos for the places verified in this location.",
                "no_indexed_text": "I have no matching destination pages available for this location.",
                "no_matches": "No matches passed this search within the requested location.",
            }
            answer = f"**{response.location}**: {reasons[response.location_status]}\n\nYou can ask me to search a different location or anywhere."
            progress("generation", "Returned the data limitation", "No answer-generation call was made.")
        else:
            progress("generation", "Generating the response", status="running")
            answer = generation.generate(
                message,
                response,
                history=conversation.messages,
                image=active_image,
                memory=memory,
                summary=summary,
            )
            progress("generation", "Response generated", "Source availability is shown above; individual claims have not been independently verified.")

        images = self.result_images(response)
        places = self.result_places(response)
        messages = [{"role": "user", "content": message}, {"role": "assistant", "content": answer, "images": images, "activity": activity, "places": places}]
        if image_path is not None:
            messages[0]["image_path"] = image_path
            conversation.image_path = image_path
        conversation.messages.extend(messages)
        conversation.summary = summary
        if action != "reply":
            if follow_up and image_path is None:
                updated = {result.entity_id: result for result in response.results}
                previous.results = [updated.get(result.entity_id, result) for result in previous.results]
                related = {result.entity_id: result for result in [*previous.related_results, *response.related_results]}
                previous.related_results = list(related.values())
                previous.follow_up_status = response.follow_up_status
                conversation.selected_entity_ids = references
            else:
                conversation.search_response = response
                conversation.selected_entity_ids = []
        self.long_term.save_turn(conversation, messages, updates)
        result = ChatResponse(
            conversation_id=conversation.conversation_id,
            answer=answer,
            search_query=search_query if action == "search" else "",
            search_response=response,
            action=action,
            images=images,
            activity=activity,
            places=places,
        )
        conversation.messages = conversation.messages[-self.short_term.max_messages:]
        return {"conversation": asdict(conversation), "result": asdict(result)}


    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._resources.close()


    def __enter__(self):
        return self


    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
