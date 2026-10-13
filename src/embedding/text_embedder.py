from pathlib import Path
import numpy as np
import torch
from sentence_transformers import SentenceTransformer
from src.utils.utils import load_config, resolve_path
from src.utils.retrieval_activity import tracked_tool
from src.security.guardrails import validate_text


class TextEmbedder:
    def __init__(
        self,
        model_name: str | None = None,
        cache_folder: str | Path | None = None,
        truncate_dim: int | None = None,
        device: str | None = None
    ):
        self.config = load_config()
        self.truncate_dim = truncate_dim if truncate_dim is not None else self.config["text_vector_size"]

        device = device if device is not None else self.config["embedding_device"]
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu") if device == "auto" else torch.device(device)

        self.model = SentenceTransformer(
            model_name_or_path=model_name or self.config["text_embedding_model"],
            cache_folder=str(resolve_path(cache_folder or self.config["cache_folder"])),
            truncate_dim=self.truncate_dim,
            device=str(self.device),
            trust_remote_code=True,
            local_files_only=self.config["models_local_files_only"],
        )

    @tracked_tool("Local text embedding model", "Encode search query")
    def embed_query(self, query: str) -> np.ndarray:
        query = validate_text(query, check_intent=True)
        return self.model.encode_query(query)

    @tracked_tool("Local text embedding model", "Encode text records")
    def embed_documents(
        self,
        documents: list[str],
        batch_size: int | None = None,
    ) -> np.ndarray:
        return self.model.encode_document(
            documents,
            batch_size=batch_size if batch_size is not None else self.config["text_embedding_batch_size"],
        )
