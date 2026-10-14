# Deterministic itinerary planning: before and after

Historical report: this deterministic version was subsequently reverted at the user's request. The current pipeline uses adaptive LLM tool selection again; the two-call result below describes the reverted version.

The orchestrator now schedules tool calls in code and sends their validated notebook to the planner once. It loads all destination guides first, collects each city's venues before routing, and attempts optional weather and outbound/return flight evidence. Tool budgets, source binding, progress callbacks, validation, scoped edits and unknown-fact reporting remain enforced. Code repairs run before at most one LLM repair. The unused LLM selection adapter was removed.

The Docker developer API was rebuilt and restarted. The existing local demo server was restarted to load the shared backend; its chat data and signing key were retained.

| Warm 3-day itinerary metric | Baseline | Optimized |
| --- | ---: | ---: |
| Requests measured | 3 | 3 |
| LLM requests per attempt | 8 | **2** |
| LLM tool selection | 6 requests, 15.51 s | **0 requests** |
| Code scheduling | — | 0.00006 s |
| Query interpretation | 8.96 s | 13.12 s |
| Structured generation | 10.81 s | 17.05 s |
| Retrieval | 2.72 s | 2.79 s |
| Venue tools | 0.88 s | 0.90 s |
| Route attempts | 8.60 s | 5.83 s |
| Weather | 0.30 s | 0.29 s |
| All other stages combined | 0.68 s | 0.67 s |
| **Full attempt average** | **48.47 s** | **40.65 s** |
| Returned 3-day drafts | 2 partial; 1 failed | **3 partial; 0 failed** |
| Mean for attempts that returned drafts | 50.74 s (n=2) | **40.65 s (n=3)** |

The measured attempt average decreased **16.1%**, while logical LLM requests decreased **75%**. Returned-draft averages decreased **19.9%**. These are small real-provider samples, not isolated estimates of the scheduling change: interpretation and generation slowed in this run, while one ORS request failed quickly with HTTP 502 and two timed out. Routes, prices and booking availability still remain unverified where evidence is missing. No routing errors were converted into verified routes.

The benchmark used the same CPU Docker configuration, gpt-5-nano with low reasoning, retrieval limit 20, Hanoi queries and fresh isolated sessions as the baseline. Provider latency, conversation length, route availability and concurrency affect actual latency. First-use model loading was excluded from the warm comparisons. Direct-chat and grounded-Q&A code paths were not changed, so their faster readings in this run should not be attributed to this optimization.

Verification covered the API regression suite, local demo compatibility, live short/long-term PostgreSQL memory, and ten schedule tests. The new checks exercise multi-city dependencies, flight exclusions and directions, weather flags, unavailable tools, tool budgets, a maximum of one LLM repair, code-only repairs and protected days during a follow-up edit. All passed; opt-in checks not selected in the regression suites remained skipped. Ten live profiling requests also returned HTTP 200; all three itinerary requests returned partial drafts without planner exceptions. Temporary benchmark users were deleted after profiling.

Raw results: [optimized samples](stage-timings-2026-10-14-optimized.json) and [baseline samples](stage-timings-2026-10-13.json). Reproduce with [profile_pipeline.py](../tests/profile_pipeline.py) using `--live --repeats 3`; this makes real provider requests.
