import os
from pathlib import Path


def open_qdrant(
    path: Path,
    config: dict,
    client_factory
):
    if url := config.get("qdrant_url"):
        options = {"url": url, "timeout": config["qdrant_timeout_seconds"]}
        if api_key := os.getenv("QDRANT_API_KEY"):
            options["api_key"] = api_key
        return client_factory(**options)
    
    return client_factory(path=str(path))
