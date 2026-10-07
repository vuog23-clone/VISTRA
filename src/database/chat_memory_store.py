from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path

from langchain_core.documents import Document
from langchain_qdrant import QdrantVectorStore
from qdrant_client import QdrantClient, models

from src.database.qdrant_connection import open_qdrant
from src.memory.embeddings import MemoryEmbeddings
from src.schema.chat import Conversation, MemoryFact
from src.utils.utils import load_config, resolve_path, stable_uuid


class ChatMemoryStore:
    def __init__(
        self,
        path: str | Path | None = None,
        collection_name: str | None = None,
        embedder=None,
        client=None,
    ):
        self.config = load_config()
        self.path = resolve_path(path, self.config["chat_memory_path"])
        self.collection_name = collection_name or self.config["chat_memory_collection"]
        self.vector_name = self.config["chat_memory_vector"]
        self.embeddings = MemoryEmbeddings(embedder)
        self._owns_client = client is None
        self.client = client if client is not None else open_qdrant(self.path, self.config, QdrantClient)

        try:
            if not self.client.collection_exists(self.collection_name):
                self.client.create_collection(
                    collection_name=self.collection_name,
                    vectors_config={self.vector_name: models.VectorParams(
                        size=self.config["text_vector_size"],
                        distance=models.Distance.COSINE,
                    )},
                )
                for field in ("metadata.user_id", "metadata.kind", "metadata.conversation_id"):
                    self.client.create_payload_index(
                        collection_name=self.collection_name,
                        field_name=field,
                        field_schema=models.PayloadSchemaType.KEYWORD,
                        wait=True,
                    )
            else:
                vectors = self.client.get_collection(self.collection_name).config.params.vectors
                vector = vectors.get(self.vector_name) if isinstance(vectors, dict) else None
                if vector is None or vector.size != self.config["text_vector_size"] or vector.distance != models.Distance.COSINE:
                    raise ValueError("Chat memory collection must match the configured text vector size and COSINE distance.")
            self.vector_store = QdrantVectorStore(
                client=self.client,
                collection_name=self.collection_name,
                embedding=self.embeddings,
                vector_name=self.vector_name,
                validate_collection_config=False,
            )
        except Exception:
            self.close()
            raise


    def get_embedder(self):
        return self.embeddings.get_embedder()


    def _id(self, *parts: str) -> str:
        return stable_uuid("chat-memory:" + json.dumps(parts, ensure_ascii=False))


    def _filter(self, user_id: str, kind: str) -> models.Filter:
        return models.Filter(must=[
            models.FieldCondition(key="metadata.user_id", match=models.MatchValue(value=user_id)),
            models.FieldCondition(key="metadata.kind", match=models.MatchValue(value=kind)),
        ])


    def _records(self, user_id: str, kind: str) -> list[dict]:
        records, offset = [], None
        while True:
            points, offset = self.client.scroll(
                collection_name=self.collection_name,
                scroll_filter=self._filter(user_id, kind),
                offset=offset,
                limit=100,
                with_vectors=False,
            )
            records.extend(point.payload["metadata"] for point in points)
            if offset is None:
                return sorted(records, key=lambda record: record["updated_at"], reverse=True)


    def _state(self, conversation: Conversation, message_count: int) -> models.PointStruct:
        return models.PointStruct(
            id=self._id("conversation", conversation.conversation_id),
            vector={},
            payload={"metadata": {
                "kind": "conversation",
                "user_id": conversation.user_id,
                "conversation_id": conversation.conversation_id,
                "title": next((message["content"][:72] for message in conversation.messages if message["role"] == "user"), "New chat"),
                "summary": conversation.summary,
                "image_path": conversation.image_path,
                "search_response": asdict(conversation.search_response) if conversation.search_response else None,
                "selected_entity_ids": conversation.selected_entity_ids,
                "message_count": message_count,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }},
        )


    def _conversation(self, conversation_id: str) -> dict | None:
        points = self.client.retrieve(
            collection_name=self.collection_name,
            ids=[self._id("conversation", conversation_id)],
            with_vectors=False,
        )
        return points[0].payload["metadata"] if points else None


    def create_conversation(self, conversation: Conversation) -> None:
        previous = self._conversation(conversation.conversation_id)
        if previous is not None and previous["user_id"] != conversation.user_id:
            raise ValueError("Conversation does not belong to this user.")
        self.client.upsert(
            collection_name=self.collection_name,
            points=[self._state(conversation, previous["message_count"] if previous else 0)],
            wait=True,
        )


    def _memory_documents(self, user_id: str, facts: list[MemoryFact]) -> list[Document]:
        return [Document(
            page_content=f"{fact.name}: {fact.value}",
            metadata={
                "kind": "memory",
                "user_id": user_id,
                "name": fact.name,
                "value": fact.value,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        ) for fact in facts]


    def save_turn(
        self,
        conversation: Conversation,
        messages: list[dict],
        memory_updates: list[MemoryFact],
    ) -> None:
        previous = self._conversation(conversation.conversation_id)
        if previous is not None and previous["user_id"] != conversation.user_id:
            raise ValueError("Conversation does not belong to this user.")
        count = previous["message_count"] if previous else 0
        documents = self._memory_documents(conversation.user_id, memory_updates)
        embeddings = self.embeddings.embed_documents([doc.page_content for doc in documents]) if documents else []
        points = [models.PointStruct(
            id=self._id("memory", conversation.user_id, doc.metadata["name"]),
            vector={self.vector_name: embedding},
            payload={"page_content": doc.page_content, "metadata": doc.metadata},
        ) for doc, embedding in zip(documents, embeddings, strict=True)]
        points.extend(models.PointStruct(
            id=self._id("message", conversation.conversation_id, str(count + index)),
            vector={},
            payload={"metadata": {
                "kind": "message",
                "user_id": conversation.user_id,
                "conversation_id": conversation.conversation_id,
                "position": count + index,
                **message,
            }},
        ) for index, message in enumerate(messages))
        state = self._state(conversation, count + len(messages))
        if previous is not None and count:
            state.payload["metadata"]["title"] = previous.get("title") or state.payload["metadata"]["title"]
        points.append(state)
        self.client.upsert(collection_name=self.collection_name, points=points, wait=True)


    def load_conversation(
        self,
        conversation_id: str,
        user_id: str,
        max_messages: int | None,
    ) -> Conversation | None:
        state = self._conversation(conversation_id)
        if state is None or state["user_id"] != user_id:
            return None
        count = state["message_count"]
        start = max(0, count - max_messages) if max_messages is not None else 0
        ids = [self._id("message", conversation_id, str(index)) for index in range(start, count)]
        points = self.client.retrieve(self.collection_name, ids=ids, with_vectors=False) if ids else []
        messages = [{
            "role": point.payload["metadata"]["role"],
            "content": point.payload["metadata"]["content"],
            **({"image_path": point.payload["metadata"]["image_path"]} if point.payload["metadata"].get("image_path") else {}),
            **({"images": point.payload["metadata"]["images"]} if "images" in point.payload["metadata"] else {}),
            **({"activity": point.payload["metadata"]["activity"]} if "activity" in point.payload["metadata"] else {}),
            **({"places": point.payload["metadata"]["places"]} if "places" in point.payload["metadata"] else {}),
        } for point in sorted(points, key=lambda point: point.payload["metadata"]["position"])
                    if point.payload["metadata"]["user_id"] == user_id]
        return Conversation.from_dict({
            "conversation_id": conversation_id,
            "user_id": user_id,
            "messages": messages,
            "summary": state["summary"],
            "image_path": state["image_path"],
            "search_response": state["search_response"],
            "selected_entity_ids": state.get("selected_entity_ids", []),
        })


    def list_conversations(self, user_id: str) -> list[str]:
        return [record["conversation_id"] for record in self._records(user_id, "conversation")]


    def delete_conversation(self, conversation_id: str, user_id: str) -> bool:
        conversation = self._conversation(conversation_id)
        if conversation is None or conversation["user_id"] != user_id:
            return False
        self.client.delete(
            collection_name=self.collection_name,
            points_selector=models.FilterSelector(filter=models.Filter(must=[
                models.FieldCondition(key="metadata.user_id", match=models.MatchValue(value=user_id)),
                models.FieldCondition(key="metadata.conversation_id", match=models.MatchValue(value=conversation_id)),
                models.FieldCondition(key="metadata.kind", match=models.MatchAny(any=["conversation", "message"])),
            ])),
            wait=True,
        )
        return True


    def list_conversation_summaries(self, user_id: str) -> list[dict]:
        return [{
            "id": record["conversation_id"],
            "title": record.get("title") or record["summary"].strip().split("\n")[0][:72] or "New chat",
        } for record in self._records(user_id, "conversation")]


    def get_memories(self, user_id: str, limit: int) -> dict[str, str]:
        return {record["name"]: record["value"] for record in self._records(user_id, "memory")[:limit]}


    def search_memories(self, user_id: str, query: str, limit: int) -> dict[str, str]:
        query_filter = self._filter(user_id, "memory")
        if self.client.count(self.collection_name, count_filter=query_filter, exact=True).count == 0:
            return {}
        documents = self.vector_store.similarity_search(query, k=limit, filter=query_filter)
        return {doc.metadata["name"]: doc.metadata["value"] for doc in documents}


    def remember(self, user_id: str, name: str, value: str) -> None:
        self.vector_store.add_documents(
            self._memory_documents(user_id, [MemoryFact(name=name, value=value)]),
            ids=[self._id("memory", user_id, name)],
            wait=True,
        )


    def forget(self, user_id: str, name: str) -> None:
        self.vector_store.delete(ids=[self._id("memory", user_id, name)], wait=True)


    def close(self) -> None:
        if self._owns_client:
            self.client.close()


    def __enter__(self):
        return self


    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
