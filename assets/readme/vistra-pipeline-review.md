# VISTRA pipeline review

Reviewed against the current working tree on 16 October 2026. This is an implementation review, not a live provider-health check. The repository has existing uncommitted changes; this review uses those current files.

The original diagram remains a useful overview, but it is no longer fully accurate. The replacement keeps its visual theme and corrects the behavior below.

| Area | Current implementation | Code reference |
| --- | --- | --- |
| Conversation and routing | Load recent messages, summary, trip state and durable memories; interpret the current turn; route memory acknowledgements, direct answers, grounded searches or itinerary work. Some explicit memory changes return an acknowledgement without answer generation. | [ChatService](../../src/service/chat_service.py), [acknowledgement](../../src/memory/acknowledgement.py) |
| Planning entry | Create/revise requests enter the orchestrator before ordinary Q&A retrieval. The orchestrator checks trip details, then gathers its own evidence through tools. Destination cities and duration are required; calendar dates can remain unknown. Conflicting dates/duration trigger clarification. | [pipeline](../../main.py), [clarification and orchestration](../../src/planning/orchestrator.py) |
| Retrieval | Text uses Voyage dense vectors plus BM25; photos use SigLIP. Hybrid vector searches run concurrently, followed by metadata enrichment and image distance ranking where coordinates support it. Filters bind category and place identity. | [SearchService](../../src/service/search_service.py), [image filters](../../src/retrieval/image_filters.py), [configuration](../../config.yaml) |
| Planning venues | AttractionSearch, RestaurantSearch and AccommodationSearch select indexed PostgreSQL metadata by city/category. They do not require a prior Qdrant photo search. Indexed venue metadata does not provide live booking availability, complete prices, cuisine or room restrictions. | [TravelTools](../../src/planning/tools.py) |
| Route estimates | DistanceMatrix now uses optional OpenRouteService walking/driving matrices; taxi uses driving estimates. Missing configuration, unsupported modes or provider failures leave travel times unknown. No live traffic or fares are supplied. The original “routes: unavailable” label is obsolete. | [DistanceMatrix adapter](../../src/planning/tools.py), [routing service](../../src/service/routing_service.py) |
| Forecasts and unsupported providers | WeatherSearch uses Open-Meteo with explicit date/location/window availability. Normal VISTRA mode still has no live flight schedule/fare provider or automatic country-to-city selection provider. Separate historical benchmark adapters are outside this normal runtime diagram. | [tools](../../src/planning/tools.py), [weather](../../src/service/weather_service.py) |
| Schedule construction | After a structured evidence-ID draft, bounded schedule search considers candidate venues, known opening hours and supplied travel gaps before final constraint validation. Repairs preserve edit scope and honor explicit venue locks. | [orchestrator](../../src/planning/orchestrator.py), [schedule builder](../../src/planning/scheduling.py), [planner](../../src/planning/planner.py) |
| Answer checks | Direct answers get deterministic safety/presentation/citation checks and at most one repair. Grounded Q&A also gets semantic grounding review; an unsupported draft can be repaired once. Unavailable review or failed repair produces an explicit limitation, not approval of the draft. These are branch-specific checks, not one universal final grounding pass. | [direct answers](../../src/generation/direct_generation.py), [grounded answers](../../src/generation/rag_generation.py), [response contract](../../src/generation/response_contract.py) |
| Planning outcomes | Verified means checks passed against supplied evidence. Unknown facts yield a partial draft; remaining errors yield no new itinerary. Failed revisions retain the previous saved itinerary. Verification does not establish live availability or data freshness. | [validation](../../src/planning/validation.py), [orchestrator](../../src/planning/orchestrator.py), [memory commit](../../src/service/chat_service.py) |
| Persistence and delivery | Valid completed chat replies, including clarification/limitation replies, commit messages, summary, trip state and explicit preference operations atomically with version checking. Browser media saves separately. Browser SSE emits progress stages and a final result; developer API returns JSON. Retrieval-only calls do not commit a chat turn. | [ChatService](../../src/service/chat_service.py), [browser server](../../frontend/server.py), [API contracts](../../api/SYSTEM_APIS.md) |

## Diagram files

- Original preserved: [vistra-pipeline.png](vistra-pipeline.png)
- Updated illustration: [vistra-pipeline-v2.png](vistra-pipeline-v2.png)
- Editable vector source: [vistra-pipeline-v2.svg](vistra-pipeline-v2.svg)
- Reproducible diagram builder: [build_vistra_pipeline.py](build_vistra_pipeline.py)
- Visual design specification: [vistra-pipeline-v2.prompt.txt](vistra-pipeline-v2.prompt.txt)

The final diagram uses native SVG shapes and text for precise connections and an exported PNG for the README. Image generation was used to explore the supplied visual theme. The diagram is a conceptual overview; the linked implementation files define exact behavior, limits and failure handling.

