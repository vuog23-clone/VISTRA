from pathlib import Path


class LocalStorageTooLargeError(RuntimeError):
    """A public error raised before embedded storage can exhaust memory."""


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