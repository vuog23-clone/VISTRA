# VISTRA

Travel Q&A and multi-turn itinerary planning with PostgreSQL memory, Qdrant retrieval, a TravelPlanner-style orchestrator, and Open-Meteo weather.

## Run

From the project root, using the existing virtual environment and `.env`:

```powershell
.\.venv\python.exe -m pip install -r requirements.txt
docker compose -f compose.postgres.yaml -f compose.qdrant.yaml up -d
.\.venv\python.exe -B -m frontend.server
```

Open [the chat demo](http://127.0.0.1:8000). For terminal chat and the complete backend JSON, run `main.py --chat` and type `/last`. New installations need provider credentials, PostgreSQL settings, prepared embedding models, and indexed datasets; starting databases does not populate their data.

## Developer API

The code, tests, Dockerfile, Compose setup, and developer instructions live together in [api/](api/README.md).

```powershell
docker compose --env-file .env -f api/compose.yaml up -d --build
```

The API is available on port 8067, with [Swagger documentation](http://127.0.0.1:8067/docs). Send `X-API-Key` to `POST /v1/chat`; reuse `user_id` and `session_id` for follow-ups.

## Dependencies and structure

`requirements.txt` is the single shared runtime manifest for the pipeline, frontend, and API. Offline-only Wikivoyage parsing and browser-assisted photo indexing packages are optional; their installation commands are in [the API guide](api/README.md#optional-data-rebuilding).

```text
main.py                 One-shot pipeline and interactive chat
requirements.txt        Shared runtime dependencies
config.yaml             Models, data, retrieval and planning settings
src/                    Pipeline, memory, retrieval, planning and data preparation
frontend/               Browser server, static assets and frontend tests
api/                    Developer API, Docker deployment, guide and API tests
compose.postgres.yaml   Shared PostgreSQL service and persistent volume
compose.qdrant.yaml     Shared Qdrant service and persistent volume
```

Local state lives in `.env`, `.venv/`, `.cache/`, `datasets/`, `frontend/.runtime/`, and database volumes. Docker mounts models and datasets read-only; it does not copy them into the image. The API retains lazy embedding models across requests, while database clients remain request-scoped.

The existing Windows environment uses CUDA PyTorch. The Docker API uses CPU PyTorch; both satisfy the same root requirement. When recreating the current Windows CUDA environment, select its wheel before installing the shared requirements:

```powershell
.\.venv\python.exe -m pip install torch==2.14.0+cu130 --index-url https://download.pytorch.org/whl/cu130
.\.venv\python.exe -m pip install -r requirements.txt
```

## Verify

With PostgreSQL running:

```powershell
$env:VISTRA_TEST_POSTGRES='1'
.\.venv\python.exe -B -m unittest discover -s api/tests -q
.\.venv\python.exe -B -m unittest discover -s frontend/tests -q
node --check frontend/static/app.js
docker compose --env-file .env -f api/compose.yaml config --quiet
```
