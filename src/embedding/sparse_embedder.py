from pathlib import Path
from fastembed import SparseTextEmbedding
from huggingface_hub import try_to_load_from_cache
from qdrant_client.models import SparseVector

from src.utils.utils import load_config, resolve_path


class SparseEmbedder:
    def __init__(
        self,
        model_name: str | None = None,
        cache_folder: str | Path | None = None,
    ):

        self.config = load_config()

        model_name = model_name or self.config.get("sparse_embedding_model", "Qdrant/bm25")
        cache_folder = resolve_path(cache_folder or self.config["cache_folder"])
        model_options = {}
        if model_name.casefold() == "qdrant/bm25":
            stopwords = try_to_load_from_cache(
                model_name,
                f"{self.config.get('bm25_language', 'english')}.txt",
                cache_dir=str(cache_folder),
            )
            if isinstance(stopwords, str) and Path(stopwords).is_file():
                model_options["specific_model_path"] = str(Path(stopwords).parent)

        self.model = SparseTextEmbedding(
            model_name=model_name,
            cache_dir=str(cache_folder),
            k=self.config.get("bm25_k", 1.2),
            b=self.config.get("bm25_b", 0.75),
            avg_len=self.config.get("bm25_avg_len", 256.0),
            language=self.config.get("bm25_language", "english"),
            local_files_only=self.config.get(
                "sparse_models_local_files_only", False
            ),
            **model_options,
        )


    def embed_query(self, query: str) -> SparseVector:
        embedding = next(self.model.query_embed(query.strip()))

        return SparseVector(
            indices=embedding.indices.tolist(),
            values=embedding.values.tolist(),
        )


    def embed_documents(
        self,
        documents: list[str],
        batch_size: int | None = None,
    ) -> list[SparseVector]:

        batch_size = (
            batch_size if batch_size is not None else self.config.get("sparse_embedding_batch_size", 256)
        )

        embeddings = self.model.embed(
            documents,
            batch_size=batch_size
        )

        return [
            SparseVector(
                indices=embedding.indices.tolist(),
                values=embedding.values.tolist(),
            )
            for embedding in embeddings
        ]
