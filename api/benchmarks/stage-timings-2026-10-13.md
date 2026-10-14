# VISTRA developer API latency baseline

Measured on October 13, 2026 against the current Docker pipeline: CPU, four CPU quota, two inference threads, gpt-5-nano with low reasoning, retrieval limit 20. Core pipeline files and configuration matched the workspace. Real PostgreSQL, Qdrant, LLM, weather and routing providers were used.

Three serial requests per warm flow were measured, plus one first-use Q&A request. Times cover in-process HTTP request to complete API JSON response, including memory reads, interpretation and saving. They exclude client network transit, frontend rendering and concurrent load. Warm means embedding models were loaded; other caches followed their normal behavior. Each sample used a fresh, isolated user/session, so longer multi-turn histories need separate measurement.

| Stage, seconds per request | Direct answer | Grounded Q&A | 3-day itinerary attempt |
| --- | ---: | ---: | ---: |
| Load and assemble memory | 0.012 | 0.011 | 0.016 |
| Interpret query and trip state, LLM | 7.06 | 6.69 | 8.96 |
| Response routing and destination extraction | 0.34 | 1.10 | — |
| Retrieve guides and photos, including scope and metadata | — | 2.38 | 2.72 |
| Prepare photos for the LLM | — | 0.48 | — |
| Search attractions, restaurants and accommodation | — | — | 0.88 |
| Weather tool | — | — | 0.30 |
| Route matrix tool | — | — | 8.60 |
| Select next tool, six sequential LLM calls | — | — | 15.51 |
| Generate answer or structured itinerary, LLM | 3.66 | 11.26 | 10.81 |
| Grounding review, deterministic and LLM | — | 3.14 | — |
| LLM answer repair, amortized across requests | — | 3.21 | — |
| Code itinerary repair | — | — | 0.004 |
| Deterministic validation | 0.001 | 0.016 | 0.056 |
| Evidence notebook | — | — | 0.052 |
| Context formatting and itinerary rendering | — | 0.010 | 0.013 |
| Save completed turn | 0.009 | 0.016 | 0.018 |
| Other setup, teardown and API formatting | 0.044 | 0.392 | 0.522 |
| **Full request average** | **11.12** | **28.70** | **48.47** |

Stage spans do not overlap. Parallel retrieval is measured once as wall time; repeated selection, review and repair calls are added together. Zero/absent stages are shown as a dash. Displayed rounded values may not sum exactly.

The first Q&A request took **58.01 seconds**, with retrieval taking **32.85 seconds**, compared with **2.38 seconds** for warm retrieval. First-use costs include model initialization; filesystem caches may already have been warm. One warm Q&A request needed an answer repair taking **9.62 seconds**, producing the amortized 3.21-second repair row.

The three itinerary attempts took **56.89, 43.91 and 44.60 seconds**. Two produced partial three-day plans, averaging **50.74 seconds**; one failed during structured planning with a `ValueError` and produced no itinerary. Each route-matrix attempt logged an OpenRouteService `ReadTimeout`, so the route row measures unavailable-route attempts, not successful matrix latency. All requests returned HTTP 200; application status determines whether an itinerary was produced.

Each itinerary used **eight OpenAI requests**: one interpretation, six tool selections and one planner generation. Tool selection alone consumed **32%** of the itinerary-attempt average. This is the clearest first target for the proposed reduction in repeated LLM calls. Memory/database work and deterministic validation were small contributors in this sample.

The API's existing `elapsed_seconds` field starts inside the main pipeline and excludes earlier memory and interpretation work, plus final saving and API formatting. Use the full-request timings above for end-to-end comparisons. This is a small baseline sample; provider latency, destinations, successful routing, conversation length and concurrent traffic can change the result.

Raw samples: [stage-timings-2026-10-13.json](stage-timings-2026-10-13.json). Repeatable profiler: [profile_pipeline.py](../tests/profile_pipeline.py). The profiler uses temporary users and removes their memory after the run; production scheduling was not modified.

From the project root, with the API container running:

```powershell
Get-Content api/tests/profile_pipeline.py -Raw | docker exec -i vistra-api-1 python -B - --live --repeats 3
```

This makes real provider requests. The report is written to `/tmp/vistra-stage-timings.json` inside the container.
