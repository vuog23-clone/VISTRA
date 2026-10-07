from collections import OrderedDict
from dataclasses import asdict
import json

from langgraph.checkpoint.memory import InMemorySaver

from src.schema.chat import Conversation
from src.utils.utils import load_config, stable_uuid


class ShortTermMemory:
    def __init__(
        self,
        max_messages: int | None = None,
        max_conversations: int | None = None,
    ):
        config = load_config()
        self.max_messages = max_messages if max_messages is not None else config["generation_history_messages"]
        self.max_conversations = max_conversations if max_conversations is not None else config["chat_max_conversations"]
        if self.max_messages < 1 or self.max_conversations < 1:
            raise ValueError("Memory limits must be positive.")
        self.checkpointer = InMemorySaver()
        self._threads: OrderedDict[str, None] = OrderedDict()


    def config(self, conversation_id: str, user_id: str) -> dict:
        thread_id = stable_uuid(json.dumps([user_id, conversation_id], ensure_ascii=False))
        return {"configurable": {"thread_id": thread_id}}


    def get(self, conversation_id: str, user_id: str, graph) -> Conversation | None:
        config = self.config(conversation_id, user_id)
        value = graph.get_state(config).values.get("conversation")
        if value is None:
            return None
        self._threads.move_to_end(config["configurable"]["thread_id"])
        return Conversation.from_dict(value)


    def save(self, conversation: Conversation, graph) -> None:
        value = asdict(conversation)
        value["messages"] = value["messages"][-self.max_messages:]
        config = self.config(conversation.conversation_id, conversation.user_id)
        thread_id = config["configurable"]["thread_id"]
        # Retain the current state instead of accumulating checkpoints for every turn.
        self.checkpointer.delete_thread(thread_id)
        graph.update_state(config, {"conversation": value}, as_node="respond")
        self._threads[thread_id] = None
        self._threads.move_to_end(thread_id)
        while len(self._threads) > self.max_conversations:
            oldest, _ = self._threads.popitem(last=False)
            self.checkpointer.delete_thread(oldest)


    def delete(self, conversation_id: str, user_id: str) -> None:
        thread_id = self.config(conversation_id, user_id)["configurable"]["thread_id"]
        self.checkpointer.delete_thread(thread_id)
        self._threads.pop(thread_id, None)
