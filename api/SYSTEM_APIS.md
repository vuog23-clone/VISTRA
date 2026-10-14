# VISTRA system API inventory

This inventory describes the implementation in this workspace on October 13, 2026. Inputs are what callers send, responses are what the service returns, and outputs describe how VISTRA uses the result. Examples are illustrative contracts, not captured production responses. Credentials are placeholders. Provider calls have been inspected in source; no paid requests or changes to stored conversations were made.

## 1. Developer HTTP API

Default local base URL: `http://127.0.0.1:8067`. Source: [server.py](server.py).

| API | Input | Response | Output / purpose |
|---|---|---|---|
| `GET /health` | No body or authentication | `200 application/json`, `{"status":"ok"}` | Process liveness; does not check databases, models, or providers |
| `POST /v1/chat` | `X-API-Key` header and the JSON request below | `200 application/json`, `ChatResponse` below; errors in the following table | Markdown answer, retrieval evidence, weather, itinerary, planning report, and conversation version |
| `GET /openapi.json` | No body or authentication | `200 application/json`, generated OpenAPI document | Machine-readable developer request/response schemas |
| `GET /docs` | No body or authentication | `200 text/html`, Swagger UI | Interactive developer API documentation |
| `GET /docs/oauth2-redirect` | Browser redirect parameters, if used by Swagger UI | `200 text/html`, Swagger redirect helper | Framework-generated documentation helper; chat authentication uses the API-key header |
| `GET /redoc` | No body or authentication | `200 text/html`, ReDoc UI | Developer API reference |

### Chat input

```http
POST /v1/chat
X-API-Key: <VISTRA_API_KEY>
Content-Type: application/json
```

```json
{
  "user_id": "customer-123",
  "session_id": "hanoi-trip",
  "message": "Plan three relaxed days in Hanoi for two people.",
  "retrieval_limit": 20,
  "response_mode": "auto",
  "route": "auto",
  "location": "Hanoi",
  "retrieve_only": false,
  "include_weather": true,
  "start_date": "2026-10-13",
  "end_date": "2026-10-15",
  "planning_mode": "auto"
}
```

| Field | Type / accepted values | Required / default |
|---|---|---|
| `user_id` | String, 1–128 characters matching `[A-Za-z0-9_.@:-]` | Required |
| `session_id` | String, same constraints as `user_id` | Required; reuse both IDs for follow-ups |
| `message` | String, 1–8,000 characters; blank/unsafe text rejected | Required |
| `retrieval_limit` | Integer, 1–20 | `20` |
| `response_mode` | `auto`, `retrieval`, `llm_only` | `auto` |
| `route` | `auto`, `text`, `hybrid` | `auto` |
| `location` | String, 1–160 characters, or null | null |
| `retrieve_only` | Boolean | false; retrieves evidence without committing a chat turn |
| `include_weather` | Boolean or null | null; omitted/null lets saved conversation choices apply |
| `start_date`, `end_date` | ISO strings `YYYY-MM-DD`, or null | null; supply together, chronological, at most 14 inclusive days |
| `planning_mode` | `auto`, `off` | `auto` |

Extra fields and type coercion are disallowed. JSON bodies are limited to 64 KiB. `llm_only` cannot be combined with a non-auto route, a nonempty location, or `retrieve_only=true`. The API key configured at startup must contain at least 32 characters and no whitespace. API user IDs map to a stable hashed owner namespace, separate from browser users.

### Chat response and output

Example of a direct-answer response; optional travel evidence is null in this example:

```json
{
  "user_id": "customer-123",
  "session_id": "hanoi-trip",
  "answer": "I will keep your preference for a relaxed pace in mind.",
  "answer_format": "markdown",
  "pipeline_status": "complete",
  "response_mode": "llm_only",
  "version": 1,
  "memory_status": "saved",
  "sources": [],
  "retrieval": null,
  "weather": null,
  "itinerary": null,
  "planning": null,
  "elapsed_seconds": 1.25
}
```

| Response field | Type | Output meaning |
|---|---|---|
| `user_id`, `session_id` | String | Echoed application identifiers |
| `answer` | String or null | Markdown reply; can be absent for evidence-only requests |
| `answer_format` | String | Normally `markdown` |
| `pipeline_status` | String | Completion/clarification state; the HTTP model does not constrain it to an enum |
| `response_mode` | String or null | Selected response path |
| `version` | Integer | Conversation version returned by the memory service |
| `memory_status` | String | Memory outcome; the HTTP model does not constrain it to an enum |
| `sources` | Array of objects | Up to 72 citation records; common keys: `source_id`, `name`, `url`; some include `entity_id`, `provider`, route mode/fetch time |
| `retrieval` | Object or null | Route, results, related results, location/status, branch statuses, warnings, and image ranking metadata |
| `weather` | Object or null | Normalized `WeatherEvidence`, described in section 3 |
| `itinerary` | Object or null | `days[]`; each day has day/date/city, transport, meals, attractions, and accommodation choices |
| `planning` | Object or null | `status`, `action`, `issues[]`, `tool_trace[]`, `repair_attempts`, `known_cost`, `currency` |
| `elapsed_seconds` | Number or null | Backend elapsed time |

Retrieval hits include place/entity identity, score, source, payload, name, coordinates, source URL, category/address, image/metadata, missing metadata, distance, and supporting evidence. Itinerary choices contain an `evidence_id`, optional start/end times, and optional resolved entity ID/name. Planning statuses are `verified`, `partial`, `failed`, or `needs_clarification`; issues contain severity (`error` or `unknown`), constraint, message, and optional day.

The top-level HTTP response is typed, but the developer server exposes nested evidence as dictionaries. The authoritative nested models are [models.py](../src/schema/models.py), [weather.py](../src/schema/weather.py), [planning.py](../src/schema/planning.py), and [reply_sources.py](../src/utils/reply_sources.py).

### Developer error responses

All explicit developer error responses use `{"error":{"code":"...","message":"..."}}`.

| HTTP status | Code | Trigger / response behavior |
|---|---|---|
| 400 | Guardrail-specific code | Blank or unsafe text; redacted message |
| 401 | `401` | Missing/invalid `X-API-Key` |
| 409 | `409` | This user already has a running request, or memory version conflict |
| 413 | `body_too_large` | Actual POST body exceeds 64 KiB |
| 422 | `invalid_request` | Invalid JSON/fields/types, limits, incompatible options, or date range |
| 429 | `429` | Concurrent-request capacity reached; `Retry-After: 5` |
| 503 | `503` | Backend could not complete the turn |

Responses use `Cache-Control: no-store` and `X-Content-Type-Options: nosniff`. A client disconnect does not cancel a backend turn; a timed-out client request can still have saved its turn.

## 2. Browser/demo HTTP APIs

Default local base URL: `http://127.0.0.1:8000`. Source: [frontend/server.py](../frontend/server.py).

The browser owner comes from the signed HttpOnly `vistra_demo` cookie. Opening `/` or `/api/bootstrap` creates it when needed. Mutating calls require `X-Vistra-Demo: 1`; if `Origin` is present, it must match the server origin. The host must be `localhost` or `127.0.0.1`. Browser users do not send `user_id` or the developer API key.

| API | Input | Successful response | Output / purpose |
|---|---|---|---|
| `GET /api/health` | None; cookie not required | `200`, `{"status":"ok"}` | Local process liveness |
| `GET /api/bootstrap` | Optional existing cookie; otherwise a new browser owner is issued | `200`, `{"ready":true,"sessions":[...]}`; new owners receive `Set-Cookie` | Initialize browser identity and retrieve up to 50 saved conversations |
| `GET /api/sessions` | Owner cookie | `200`, `{"sessions":[...]}` | List up to 50 conversations, most recently updated first |
| `GET /api/sessions/{session_id}` | Owner cookie; valid session ID in path | `200`, `{"messages":[...],"version":0,"summary":"...","trip_state":{...},"memories":[...],"busy":false}` | Restore up to 100 saved messages, trip state, long-term memory, and saved reply media |
| `DELETE /api/sessions/{session_id}` | Owner cookie, mutation header, valid session ID; no body | `200`, `{"ok":true}` | Clear conversation state/messages/media; retains long-term memories |
| `DELETE /api/memory/{kind}/{memory_key}` | Owner cookie, mutation header; kind=`preference` or `trip`; valid memory key; no body | `200`, `{"ok":true,"removed":true}`; `removed` can be false | Delete the selected long-term memory |
| `GET /api/photos/{session_id}/{turn}/{index}` | Owner cookie; valid session ID; turn≥1; index=0–19 | `200 image/jpeg`, binary JPEG | Load an authorized saved reply photo, scaled within 640×480 |
| `POST /api/chat` | Owner cookie, mutation header, JSON below | `200 text/event-stream`; progress/result/error events below | Generate a reply, commit the turn, and save display media |

Session list records have `id`, `title`, ISO `updated_at`, and nullable `destination`. Detail message records have `turn`, `role`, `content`; assistant records may additionally have `cards`, `sources`, `weather`, `itinerary`, and `planning`. Long-term memory records have `kind`, `key`, `content`, and `source_quote`.

`/` serves the HTML application; `/assets/*` serves bundled static assets. These are UI resources. The browser app disables `/docs`, `/redoc`, and `/openapi.json`.

### Browser chat input and streamed response

```json
{
  "session_id": "hanoi-trip",
  "message": "Plan three relaxed days in Hanoi.",
  "retrieval_limit": 20
}
```

All three fields have the same limits as the developer request, except the browser session pattern is `[A-Za-z0-9_.:-]{1,128}` (no `@`); `retrieval_limit` defaults to 20. Extra fields/type coercion are rejected. POST requests require a declared `Content-Length` of 0–40,000 bytes. The browser request does not expose the developer's other options; trip details are inferred from the conversation.

Illustrative SSE frames:

```text
event: progress
data: {"message":"Looking through travel sources"}

event: result
data: {"answer":"Your reply...","cards":[],"sources":[],"weather":null,"itinerary":null,"planning":null,"media_error":null,"version":1,"recalled_messages":0,"recalled_long_term":0,"needs_clarification":false}

```

`progress` carries a human-readable stage. `result` carries the final Markdown answer, media, version, memory recall counts, clarification flag, and optional media-save warning. Backend failures after the stream starts produce a terminal `event: error` with `data: {"message":"..."}`; the HTTP status may already be 200. A keep-alive comment is emitted while waiting. This stream reports processing stages, not incremental answer tokens.

Browser `sources` use `{id,name,url}`, whereas developer sources use `source_id`. Place cards include identity/name/address/category/province, rating/review count, coordinates/distance, price/hours, Maps URL, source ID, and an owner-authorized local `image_url`. The provider photo URL is removed. Source: [frontend/media.py](../frontend/media.py).

### Browser errors

| Status | Trigger / response |
|---|---|
| 400 | Non-local host or unsafe input; `{"error":"..."}` |
| 401 | Owner cookie missing; middleware uses `{"error":"..."}`, route exceptions use FastAPI `{"detail":"..."}` |
| 403 | Mutation header missing or Origin mismatch; `{"error":"..."}` |
| 404 | Missing/invalid/unloadable saved photo; `{"detail":"..."}` |
| 409 | Owner has an active request; `{"detail":"..."}` |
| 413 | Missing/invalid/oversized declared POST body length; `{"error":"..."}` |
| 422 | Request/path validation failure; `{"error":"..."}` |
| 429 | Two owners are already generating replies; `{"detail":"..."}` |
| 503 | Repository failure: `{"error":"..."}`; bootstrap: `{"ready":false,"sessions":[],"error":"..."}`; photo capacity/shutdown route errors: `{"detail":"..."}` |

Most browser APIs use `Cache-Control: no-store`. Failed media persistence can return a successful chat result with a non-null `media_error`: the reply itself was already saved.

## 3. External provider APIs used by the runtime

The following contracts describe the fields actually sent/read by VISTRA. Provider responses may contain additional metadata.

### Jev / TypeSafe classification

All three operations call configured `POST https://api.typesafe.ai/v1/systemone` with `Authorization: Bearer <JEV_API_KEY or TYPESAFE_API_KEY>`. Configured model: `jev-latest`.

| Operation | Input JSON | Provider response read by VISTRA | VISTRA output |
|---|---|---|---|
| Response-mode classification | `model`, `state:{query}`, `questions.response_mode:{type:"choice",instructions,criteria}` | `{"answers":{"response_mode":{"choice":"retrieval"}}}` or choice=`llm_only` | `ResponseDecision`: `mode`, `decision_source`, nullable `fallback_reason` |
| Retrieval-intent classification | `model`, `state:{query,has_image}`, `questions.intent:{type:"choice",instructions,criteria}` | `{"answers":{"intent":{"choice":"hybrid"}}}` or choice=`text_only` | Route `hybrid` or `text`, plus decision/policy/fallback diagnostics |
| Destination classification | `model`, `state:{query,location_candidates}`, `questions.location:{type:"choice",instructions,criteria}` | `{"answers":{"location":{"choice":"place_0"}}}`; other choices: `no_location`, `ambiguous`, `unknown_destination` | `LocationIntent`: `status`, `location`, `candidates`, `clarification`; candidate choices resolve through the local catalog |

The response-mode and retrieval routers fall back when the key is missing, routing is disabled, or the provider is unavailable. Invalid destination choices are rejected; ambiguous/unknown choices produce clarification instead of inventing a destination. Provider HTTP responses must be JSON and are bounded to 64 KiB, with redirects disallowed.

Sources: [jev_response.py](../src/router/jev_response.py), [jev.py](../src/router/jev.py), [jev_location_extractor.py](../src/resolver/jev_location_extractor.py), [network.py](../src/security/network.py).

### OpenAI Responses

VISTRA uses the OpenAI SDK against `https://api.openai.com/v1`, with `OPENAI_API_KEY`, through `responses.create` and `responses.parse`. Both target the Responses API (`POST /v1/responses`). Configured model: `gpt-5-nano`; reasoning effort: `low`. The actual selected model is configuration-dependent. Every call sets `store=false`.

Common input: `model`, `instructions`, `input`, `reasoning:{effort}`, `max_output_tokens`. Structured SDK calls also specify `text_format` with a Pydantic schema, translated by the SDK into the wire format. Generation/review may include sanitized retrieved image data in multimodal input. Successful results must have `status="completed"`.

| Operation | Input payload inside `input` | SDK response consumed | VISTRA output |
|---|---|---|---|
| Direct reply | `question`, optional `conversation_memory` and `repair_feedback` | Nonblank `output_text` | Markdown answer string |
| Grounded travel reply | `question`, `retrieved_context`, optional memory/repair feedback and images | Nonblank `output_text` | Markdown answer checked against supplied evidence/citations |
| Grounding review | `question`, `retrieved_context`, `draft`, optional memory/images; schema=`GroundingReview` | `output_parsed:{supported:boolean,issues:string[]}` | Review issues; unsupported/unavailable review triggers repair or source-fact fallback |
| Conversation interpretation | `latest_user_message`, `reference_date`, `reference_timezone:"Asia/Bangkok"`, `conversation_memory`, optional repair feedback; schema=`TurnUnderstanding` | Parsed intent/query/summary/trip state/action/revision/weather/preference changes | Validated turn understanding; preference changes require source quotes |
| Planning tool selection | `request`, `notebook`, `remaining_required`; schema=`ToolAction` | Parsed `tool`, nullable city/origin/destination/departure date/mode | Next internal planning action |
| Itinerary generation/repair | `query`, `request`, `notebook`, `previous_itinerary`, `revision_day`, `rejected_draft`, `validation_errors`; dynamic itinerary schema | Parsed day-by-day draft | Validated itinerary with evidence choices, preserved unrequested days, and separate planning report |

`output_text` and `output_parsed` are SDK accessors; VISTRA does not return the raw OpenAI response to clients. Incomplete/empty generation and structured planning responses are rejected. Conversation interpretation failure does not commit a memory update. Grounding review failure becomes an issue rather than an approval.

Sources: [rag_generation.py](../src/generation/rag_generation.py), [direct_generation.py](../src/generation/direct_generation.py), [conversation.py](../src/memory/conversation.py), [planner.py](../src/planning/planner.py), [rag.py](../src/schema/rag.py), [memory.py](../src/schema/memory.py).

### Open-Meteo forecast

Endpoint: `GET https://api.open-meteo.com/v1/forecast`. If `OPEN_METEO_API_KEY` is configured, VISTRA instead uses `https://customer-api.open-meteo.com/v1/forecast` with query parameter `apikey`.

| Input | Provider response consumed | VISTRA output |
|---|---|---|
| Query parameters: `latitude`, `longitude`, `daily=weather_code,temperature_2m_min,temperature_2m_max,precipitation_probability_max,precipitation_sum,wind_speed_10m_max`, `forecast_days=16`, `timezone=auto`, `temperature_unit=celsius`, `wind_speed_unit=kmh`, `precipitation_unit=mm` | JSON `timezone`, `daily_units`, and `daily` arrays keyed by `time` and each requested metric | `WeatherEvidence`: `source_id:"W1"`, provider/URL/status/location/coordinates/timezone, requested start/end dates, fetch timestamp, `days[]`, and `note` |

The internal tool input also includes `location`, `start_date`, and `end_date`. VISTRA fetches a 16-day forecast and locally filters it to the trip dates; it does not send those dates as provider query parameters. Each normalized day contains `date`, `condition`, `temperature_min_c`, `temperature_max_c`, `precipitation_probability_pct`, `precipitation_mm`, and `wind_max_kmh` (metrics may be null).

Statuses: `available`, `partial`, `missing_dates`, `missing_location`, `outside_forecast_window`, `unavailable`, `disabled`. Missing coordinates/dates or provider failures remain explicit. Timeout defaults to 8 seconds; cache lifetime is 30 minutes. Source: [weather_service.py](../src/service/weather_service.py), [weather.py](../src/schema/weather.py).

### OpenRouteService matrix

Endpoint: `POST https://api.heigit.org/openrouteservice/v2/matrix/{profile}`. Authentication: `Authorization: <ORS_API_KEY>` (the key itself). Profiles: `foot-walking` for walking; `driving-car` for driving/taxi.

```json
{
  "locations": [[105.85, 21.03], [105.86, 21.04]],
  "metrics": ["duration", "distance"],
  "units": "m"
}
```

| Input | Provider response consumed | VISTRA output |
|---|---|---|
| 2–16 distinct indexed location records and mode; wire coordinates are `[longitude,latitude]` | JSON `durations` and `distances`: directed square matrices, allowing null unreachable cells | `RouteMatrixEvidence`: provider, URL, mode/profile, fetch timestamp, original locations, `durations_seconds`, `distances_meters` |

The matrices support travel-gap validation and source citations through planning evidence. The configured default candidate limit is 12; timeout is 8 seconds; matrices are cached for 30 minutes. Missing key, unsupported public-transit mode, HTTP 401/403, quota 429, malformed data, or provider outages raise a fixed `RoutingUnavailable` message and leave route times unknown. Taxi uses the driving profile and does not supply fares. Source: [routing_service.py](../src/service/routing_service.py), [routing.py](../src/schema/routing.py).

### Google-hosted photo retrieval

This is an HTTP image fetch from an already indexed, allowlisted photo URL. The runtime does not invoke a Google Places search/details API.

| Input | Provider response | VISTRA output |
|---|---|---|
| `GET` to a validated saved Google photo URL; no Google API key is sent by this photo-fetch code | Image bytes, subject to network/image checks | Decoded image for visual context or an authorized browser JPEG from `/api/photos/...`; failed photos leave metadata available |

Google Maps URLs in source cards are links, not live Maps API calls. Wikivoyage source URLs are also citation links; guide contents are retrieved from existing local/indexed data. Sources: [google_maps_photos.py](../src/data/preprocessing/google_maps_photos.py), [vision_inputs.py](../src/generation/vision_inputs.py), [frontend/server.py](../frontend/server.py).

## 4. Database service APIs

These are runtime dependencies accessed through SDK/SQL interfaces rather than VISTRA HTTP endpoints.

| Service API | Input | Service response | VISTRA output |
|---|---|---|---|
| Qdrant `query_points` | Collection name, dense vector or BM25 sparse vector, optional vector name/filter, limit/threshold, payload/vector options | SDK result with scored `points` (IDs, scores, payloads) | Ranked text/image retrieval hits |
| Qdrant `query_points_groups` | Image collection, query vector, optional place filter, `group_by:"place_id"`, limit, group size, threshold | SDK result with `groups` and each group's `hits` | Place-grouped image evidence |
| Qdrant collection/index/upsert/vector-update operations | Collection/vector configuration, payload index, point IDs/payloads/vectors | SDK collection metadata and operation results | Create/check/update prepared retrieval indexes; mainly initialization/indexing paths |
| PostgreSQL memory reads | Owner/session IDs, message/recall limits; parameterized SQL | Session rows, messages, memory rows | Session snapshot, trip state, conversation context, saved-session browser responses |
| PostgreSQL memory commit/delete | Owner/session, expected version, user/assistant content, interpreted state/preferences, or selected memory key | Transaction outcome/new version, deletion boolean; version mismatch raises `MemoryConflict` | Persisted chat turn, durable preferences/trips, clear/forget results |
| PostgreSQL place/photo metadata reads | Place IDs and photo point IDs; parameterized SQL | Place/image metadata rows | Enriched retrieval evidence with names, addresses, coordinates, photos, and other known metadata |
| PostgreSQL browser-media save/read | Owner/session/turn and validated media JSON, or photo index | Saved media records / write confirmation / selected photo URL | Reply cards, saved weather/itinerary/planning, authorized photo lookup |

Qdrant defaults to `http://127.0.0.1:6333`, with configurable URL/local storage. Configured collections are `wikivoyage_voyage` and `google_maps_images`. Text vectors are 2,048-dimensional; image vectors are 768-dimensional; BM25 uses named sparse vector `bm25`. PostgreSQL uses `POSTGRES_DSN` or `POSTGRES_HOST/PORT/DB/USER/PASSWORD`; default host/port/database/user are `127.0.0.1:5432`, `vistra`, `vistra`. No credentials are reproduced here.

Sources: [qdrant_wikivoyage_store.py](../src/database/qdrant_wikivoyage_store.py), [image_retriever.py](../src/retrieval/image_retriever.py), [postgres_memory_store.py](../src/database/postgres_memory_store.py), [postgres_google_metadata_store.py](../src/database/postgres_google_metadata_store.py), [frontend/server.py](../frontend/server.py).

## 5. Internal planning tools and local models

`GuideSearch`, `AttractionSearch`, `RestaurantSearch`, `AccommodationSearch`, `FlightSearch`, `DistanceMatrix`, `CitySearch`, and `WeatherSearch` are internal planning tools, not separate HTTP endpoints. Their input is a `ToolAction` plus trip request/evidence context; output is a `ToolResult` containing tool/city/status, records, optional weather/route matrix, and a note. `NotebookWrite` records evidence; selecting `Planner` moves to itinerary generation. In normal VISTRA mode, `FlightSearch` has no current schedule/fare provider and `CitySearch` has no region-to-city selection provider. Flight/booking availability and unsupported prices are not supplied by an implemented live booking API; optional benchmark mode can read separately supplied CSV data. Sources: [tools.py](../src/planning/tools.py), [planning.py](../src/schema/planning.py).

Voyage (`voyageai/voyage-4-nano`), SigLIP (`google/siglip2-base-patch16-224`), and BM25 (`Qdrant/bm25`) run through local model libraries. Their input is text/documents or image pixels; output is dense/sparse vectors for Qdrant. They are not hosted embedding API calls in the current implementation. Model-loading behavior follows the offline/cache configuration; optional preprocessing/indexing is outside the client HTTP API contract. Sources: [text_embedder.py](../src/embedding/text_embedder.py), [image_embedder.py](../src/embedding/image_embedder.py), [sparse_embedder.py](../src/embedding/sparse_embedder.py), [config.yaml](../config.yaml).
