from pathlib import Path


def open_qdrant(
    path: Path,
    config: dict,
    client_factory
):
    if url := config.get("qdrant_url"):
        return client_factory(
            url=url,
            timeout=config["qdrant_timeout_seconds"]
        )
    
    return client_factory(path=str(path))
