# VISTRA developer API

This folder contains the HTTP API, request-scoped runtime adapter, Docker deployment, and API tests. It runs the same `ChatService`, memory, photo retrieval, weather, and itinerary orchestration as the browser and terminal.

```text
api/
  server.py               API-key authentication and /v1/chat
  runtime.py              Lazy shared embedding models and bounded query caches
  Dockerfile              CPU runtime image
  Dockerfile.dockerignore Runtime-only build context
  compose.yaml            API deployment; includes existing database services
  tests/                  API, concurrency, runtime and memory checks
  README.md               Setup and developer contract
```

## Run locally

From the project root, use the existing `.env`, indexed data, and model cache. On a new installation, provide `OPENAI_API_KEY`, `JEV_API_KEY` (or `TYPESAFE_API_KEY`), `POSTGRES_PASSWORD`, and `VISTRA_API_KEY`. The developer key must be a random secret of at least 32 characters. Generate one with `python -c "import secrets; print(secrets.token_urlsafe(32))"` and put it in `.env`.

```powershell
.\.venv\python.exe -m pip install -r requirements.txt
docker compose -f compose.postgres.yaml -f compose.qdrant.yaml up -d
.\.venv\python.exe -B -m uvicorn api.server:create_app --factory --host 127.0.0.1 --port 8067 --workers 1 --no-proxy-headers
```

Open [Swagger](http://127.0.0.1:8067/docs), click **Authorize**, and enter `VISTRA_API_KEY`. `GET /health` reports process liveness without loading models or testing providers/data. The browser frontend can run separately on port 8000.

### Route estimates

Add `ORS_API_KEY=your_key` to `.env` to enable OpenRouteService in the shared itinerary pipeline. Obtain a key from the [HeiGIT account dashboard](https://account.heigit.org/); its quota applies to API matrix calls. No extra Python dependency is needed. Restart local servers, or rebuild/recreate the Docker API using the command below.

`DistanceMatrix` collects indexed coordinates after venue searches, then gets directed duration/distance estimates. The planner leaves enough time between covered stops; validation rejects shorter gaps. Replies show estimated minutes, distance, mode, source and fetch time. Walking is the default; request driving or taxi explicitly. Public transit, live traffic, fares and booking availability remain unknown. Intercity estimates use a separate cached city matrix, with exact city-center coordinates from guides or the local Wikivoyage geo index; a missing departure-city coordinate remains unknown.

`config.yaml` controls `ors_enabled`, `ors_timeout_seconds` (8 by default) and `ors_max_locations` (12, maximum 16). A bounded per-process cache reuses matrices for 30 minutes. Candidates beyond the limit, unreachable pairs, missing keys, quota errors and outages leave partial plans. Routing uses the [current HeiGIT endpoint](https://ask.openrouteservice.org/t/deprecating-api-openrouteservice-org-in-favour-of-api-heigit-org/7912) and [official matrix format](https://giscience.github.io/openrouteservice/api-reference/endpoints/matrix/).

Walking candidates favor proximity to the indexed city center, retaining saved venue identities for edits. Where enough route-covered meal/attraction candidates exist, the planner's output schema restricts those choices to covered venues. This does not infer walking times from straight-line distances.

Itinerary planning uses the TravelPlanner-style adaptive loop: retrieve an initial guide, let the LLM select the next tool from the request and notebook observations, execute it, write its evidence to the notebook, and repeat before structured planning and validation. Code enforces tool prerequisites, request scope, repetition limits and `planning_max_steps`; it provides a fallback when selection fails. Weather and routing remain available alongside venue tools. `planning_max_repairs` controls bounded repair passes, with code repairs attempted before another planner call.

Explicit edits to one or several numbered days preserve the other days; invalid day numbers trigger clarification. If tool evidence exceeds the notebook budget, excess forecasts are marked partial or unavailable, and collection stops safely if even a failure observation cannot fit. Missing evidence cannot become verified travel facts.

## Docker

Docker Compose 2.20.3 or later is required for `include`. Run from the project root:

```powershell
docker compose --env-file .env -f api/compose.yaml up -d --build
docker compose --env-file .env -f api/compose.yaml ps
```

To update only the API while the existing databases are running:

```powershell
docker compose --env-file .env -f api/compose.yaml up -d --build --no-deps api
```

The Compose project remains `vistra`, using the existing PostgreSQL/Qdrant volume names. The default API port is 8067 on loopback. Set `VISTRA_API_BIND=0.0.0.0` for network access and use HTTPS through a reverse proxy when exposing it publicly. Keep the developer key in trusted server code; a shared API key allows its holder to select any API `user_id`.

`VISTRA_MODEL_CACHE` defaults to the root `.cache/docker-models`. A custom relative path is resolved from `api/`; an absolute path is least ambiguous. This read-only mount must contain the configured Voyage, SigLIP, and BM25 assets with Linux-readable files/symlinks. Windows model-cache symlinks pointing outside the mount will not work in Linux. Prepared `datasets/` are also mounted read-only. Deployment uses existing data; it does not populate empty databases or fetch missing dense models.

The CPU image retains PyTorch, Transformers, SentenceTransformers, safetensors, NumPy, Pillow, FastEmbed, Qdrant, PostgreSQL, and provider libraries because the live pipeline uses them. Models and datasets are mounted rather than baked into the image. Hugging Face runs offline with its hub cache set to the model mount; temporary module/support caches go to `/tmp`. This prevents optional model-file lookups from downloading assets or writing into the read-only home directory. Optional preprocessing packages are excluded.

Docker optimizations:

- CPU PyTorch avoids CUDA runtime packages; its separate layer survives application changes.
- BuildKit's pip cache speeds dependency rebuilds; the cache lives outside the final image. It trades build-cache disk space for download speed.
- A multi-stage build copies only the installed environment and runtime code into the final image. It omits generated package bytecode, the venv's installer, and PyTorch C++ extension headers. This CPU image is for prebuilt inference; custom C++/JIT extensions need a separate development image. The build context excludes secrets, local environments, datasets, model caches, frontend assets, and API tests.
- One worker retains one lazy copy of each embedding model. Models and small query-vector caches survive between requests; PostgreSQL and Qdrant clients are closed after each request. Direct chat and health checks do not load embeddings.
- Each model serializes its own inference calls; text and image branches can still run concurrently. `VISTRA_API_CONCURRENCY` defaults to 2 (range 1–8), and `VISTRA_CPU_THREADS` defaults to 2. More workers duplicate model memory and defeat process-local request coordination.
- The container root filesystem is read-only, with a bounded 256 MB temporary filesystem and rotating logs. Dependency downloads and request photo pixels do not accumulate in the image.

These changes do not prune existing images, unrelated containers, build caches, or database volumes. Check host storage with `docker system df`; image size is separate from model-cache and database disk usage. Optimization of model reuse does not reduce external LLM/provider latency.

Build-cache and context design follow [Docker's cache guidance](https://docs.docker.com/build/cache/optimize/); database reuse uses [Compose include](https://docs.docker.com/compose/how-tos/multiple-compose-files/include/).

## Vercel

Deploy from the **repository root** with the **FastAPI** framework preset. The root
`app.py` exports `app = create_app()` from `api.server`; `vercel.json` selects the
preset. `main.py` is the terminal entry point and does not export an ASGI app, so
`main:app` fails with `Attribute "app" not found in module "main"`.

In the Vercel project settings:

1. Set **Root Directory** to the repository root (`./`).
2. Use the **FastAPI** preset and leave **Build Command** and **Output Directory**
   at their defaults. Remove any old command or entry-point override targeting
   `main:app`; Vercel detects the root `app.py`.
3. Add `VISTRA_API_KEY` as a secret environment variable: at least 32 characters,
   with no whitespace. Application creation requires it, including when Vercel
   imports the app during a build. Use the same key in clients' `X-API-Key` header.
4. For chat, also set `OPENAI_API_KEY`, `JEV_API_KEY` (or `TYPESAFE_API_KEY`), and
   `POSTGRES_DSN` for a PostgreSQL server reachable from Vercel. Retrieval also
   needs a reachable `QDRANT_URL` and the configured models and indexed data.
   The default loopback database URLs refer to the Vercel instance, not your PC.
5. Redeploy, then check `/health` for `{"status":"ok"}` and `/docs` for Swagger.
   This deployment serves the developer API; the browser demo runs separately.

To check the same entry point locally, after configuring `.env`:

```powershell
.\.venv\python.exe -B -m uvicorn app:app --host 127.0.0.1 --port 8067
```

This resolves app discovery. A successful health check does not verify chat,
database connectivity, providers, or retrieval. The full retrieval pipeline uses
PyTorch, Transformers, local embedding models, and prepared datasets; the models
and datasets are ignored by Git and are not supplied by importing the repository.
Vercel's standard Python function bundle limit is **500 MB uncompressed**, so
check the installed dependencies and model assets against your deployment limits
before using full retrieval. See the [Python runtime documentation](https://vercel.com/docs/functions/runtimes/python).
The existing Docker deployment supports the full pipeline with persistent model
and dataset mounts when it cannot fit a Vercel function.

## Developer request

Send JSON to `POST /v1/chat` with `X-API-Key: <your developer key>`:

```json
{
  "user_id": "customer-123",
  "session_id": "hanoi-trip",
  "message": "Plan three relaxed days in Hanoi for two people.",
  "retrieval_limit": 20,
  "include_weather": true,
  "start_date": "2026-10-13",
  "end_date": "2026-10-15"
}
```

Replace the dates with the actual trip dates. Reuse both IDs for follow-ups; reuse `user_id` with a new `session_id` to retain durable preferences across chats. API owners retain the previous `api_` + SHA-256 user namespace, separate from browser owners. Rotating the developer key does not change that namespace.

Optional fields: `response_mode` (`auto`, `retrieval`, `llm_only`), `route` (`auto`, `text`, `hybrid`), `location`, `retrieve_only`, `include_weather`, paired ISO `start_date`/`end_date`, and `planning_mode` (`auto`, `off`). The retrieval limit is 1–20 and dates span at most 14 days. Omitting `include_weather` lets the conversation's saved choice apply. `retrieve_only` returns evidence without committing a chat turn. Server filesystem image paths and arbitrary pipeline arguments are not accepted.

The response contains `answer` as Markdown, `sources`, `retrieval`, `weather`, `itinerary`, `planning`, `version`, and `memory_status` where applicable. Planning/source IDs resolve consistently, including selected notebook records and weather citations. Unsupported flight, route, booking, and price facts stay explicitly unverified.

Requests are limited to 64 KiB and 8,000 message characters. Errors use `{"error":{"code":"...","message":"..."}}`: 401 for authentication, 400/422 for invalid input, 409 for an active request for this user or a memory conflict, 429 when capacity is reached, and 503 for backend failure. Retry 429 after the `Retry-After` interval. A client disconnect does not cancel a running backend turn; an HTTP timeout alone does not prove the turn was not saved.

## Optional data rebuilding

Only install these when preprocessing raw Wikivoyage dumps or resolving Maps photos during indexing:

```powershell
.\.venv\python.exe -m pip install mwsql mwparserfromhell tqdm 'playwright>=1.55,<2'
.\.venv\python.exe -m playwright install chromium
```

They are not required for chat, retrieval over existing indexes, memory, itinerary planning, or the API. `tqdm` may still be installed transitively by the model libraries; removing its direct requirement does not guarantee it disappears from the image.

Optional benchmark mode reads historical TravelPlanner CSVs from `planning_benchmark_path` (default `datasets/benchmark/travelplanner`). This data is separately supplied; the normal VISTRA flow uses the existing indexed places and guides.

## Verify

From the project root:

```powershell
$env:VISTRA_TEST_POSTGRES='1'
.\.venv\python.exe -B -m unittest discover -s api/tests -q
.\.venv\python.exe -B -m unittest discover -s frontend/tests -q
docker compose --env-file .env -f api/compose.yaml config --quiet
```

The optional PostgreSQL tests create unique test owners and delete only those owners. Normal API tests use injected backends; they do not call LLM providers.

To exercise the running API with real providers, retrieval, cross-session memory and a three-day itinerary followed by a scoped edit:

```powershell
$env:VISTRA_TEST_DOCKER='1'
$env:VISTRA_TEST_LIVE='1'
$env:VISTRA_TEST_API_URL='http://127.0.0.1:8067'
.\.venv\python.exe -B -m unittest api.tests.test_docker -v
```

These checks use the configured developer key, make real provider requests, and delete only their temporary users. Without the URL override they target an isolated API at port 18067.

To also test real ORS calls, set `$env:VISTRA_TEST_ORS='1'` and run `python -B -m unittest api.tests.test_routing -v`. This checks public Hanoi coordinates and indexed venues; it consumes matrix quota without saving chat turns.

On October 13, 2026, the image built successfully and Docker reported 1.71 GB versus 2.17 GB for the previous image (about 21% smaller). A source-only rebuild reused dependency layers and took about four seconds. In one read-only CPU benchmark against the existing Hanoi indexes, text retrieval took 41.2 seconds on first load and 1.45 seconds on the repeated query; hybrid retrieval took 16.4 seconds when first loading its image model and 1.98 seconds on repetition. These are local retrieval measurements, excluding LLM generation; cold requests still load models, and real chat latency depends on providers and evidence.
