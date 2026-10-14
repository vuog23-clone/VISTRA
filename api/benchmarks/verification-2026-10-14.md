# Optimized VISTRA verification — October 14, 2026

Historical report: these results describe the deterministic optimization before it was reverted. The current pipeline uses adaptive LLM tool selection again.

**85 executed checks passed; one external routing check failed with a timeout.** The running developer API and local demo both responded successfully. No production pipeline code needed to change during this test run.

| Test group | Result | Runtime |
| --- | --- | ---: |
| API regression suite, PostgreSQL enabled | 56 passed; 7 opt-in checks initially skipped | 2.27 s |
| Local demo, streaming and PostgreSQL media suite | 25 passed | 10.28 s |
| Real HTTP developer API tests | 4 passed | 133.31 s |
| Separate live ORS matrix request | Failed: `ReadTimeout`, surfaced as `RoutingUnavailable` | 8.52 s |

The live HTTP checks used `http://127.0.0.1:8067` and real configured providers. They verified authentication, request validation and health; hybrid text/photo retrieval without saving a chat turn; durable vegetarian preference recall across sessions; a sourced three-day Hanoi itinerary with dates, weather, unique meal/attraction choices and no validation errors; and a follow-up that moved only day 2 dinner to 19:00–20:00 while keeping venue identities and days 1/3 unchanged. The final itinerary, version 2 and four conversation messages matched PostgreSQL session state.

The separate ORS check requested a walking matrix for two public Hanoi locations. It exceeded the configured timeout, so successful live routing could not be verified. The pipeline's unavailable-tool and unknown-route behavior passed its tests; returned travel plans can remain partial until routes, prices and booking facts have evidence. A timeout does not establish the cause of the provider/network failure.

The regression suite also covered deterministic tool scheduling, multi-city dependencies, disabled weather, flight constraints, tool budgets, bounded LLM repairs, code-only repairs, scoped edits, shared model caches, API ownership, short/long-term memory, streaming final-answer replacement and media cleanup. Injected error tests deliberately logged provider errors; their assertions passed.

The seven opt-in checks skipped in the initial API suite were handled selectively: the three existing Docker checks and the direct ORS check were subsequently run, along with the newly added live itinerary/edit check. Three older opt-in routing/planning checks were not additionally run; the live HTTP itinerary/edit test exercised the optimized pipeline, and ORS was unavailable. Skipped checks are not counted as passes.

Every live memory/media/API check used a uniquely named temporary owner and cleaned up its data. Existing user conversations, preferences, containers and database volumes were retained.

Repeat the regression checks with `VISTRA_TEST_POSTGRES=1` using `unittest discover -s api/tests` and `unittest discover -s frontend/tests`. Repeat live HTTP checks with `VISTRA_TEST_DOCKER=1`, `VISTRA_TEST_LIVE=1`, `VISTRA_TEST_API_URL=http://127.0.0.1:8067` and `unittest api.tests.test_docker -v`. The live ORS check is `unittest api.tests.test_routing.ProviderTests.test_live_hanoi_matrix -v` with `VISTRA_TEST_ORS=1`.
