# VISTRA flow audit — October 14, 2026

The adaptive TravelPlanner-style flow remains active: validate input, load memory, interpret the turn, route it, check trip details, retrieve an initial guide, repeatedly let the LLM select a tool from notebook observations, collect evidence, build a structured itinerary, validate/repair, check output and commit the completed turn. Direct chat and grounded Q&A use their corresponding generation paths. The fixed tool scheduler was not reintroduced.

Three issues were reproduced with failing tests and corrected:

- Explicit edits to multiple numbered days cleared the old single-day restriction and could change untouched days. Edit scopes now support day lists and ranges; preservation, validation and both code repair paths honor the same scope. Comparison/protected days are excluded from explicit targets, and invalid day numbers trigger clarification before tools run. The planner receives a JSON list of edit days.
- A route action with an out-of-scope endpoint could be replaced with a prerequisite before the original action was checked. Scope/date validation now occurs before prerequisite substitution.
- Adding a large forecast to a nearly full notebook raised an uncaught size-budget exception. Forecast evidence can now be shortened and explicitly marked partial/unavailable. If an observation cannot fit, a bounded unavailable observation is attempted; if even that cannot fit, collection stops and the missing evidence is reported as unknown. Existing valid evidence remains usable.

Verification results:

| Group | Result | Runtime |
| --- | --- | ---: |
| Full API regression suite, PostgreSQL enabled | 64 passed; 8 live-only tests skipped in that run | 3.12 s |
| Local demo, streaming and PostgreSQL media | 25 passed | 10.84 s |
| Real developer API HTTP checks | 5 passed | 237.67 s |
| Separate live ORS matrix request | Failed with `ReadTimeout` / `RoutingUnavailable` | 8.57 s |

This is **94 passing executed checks and one failing external routing check**. The new grounded-answer HTTP case was added after the initial regression run and executed in the live suite. Three older optional route/planning checks were not additionally executed; skipped checks are not counted as passes.

The live API checks verified authentication/validation, grounded answers with resolvable citations, related text/photo evidence, direct greetings, durable preference recall across sessions, retrieval-only reads without committing memory, a dated three-day itinerary with weather and unique sourced choices, a day-2-only dinner edit, and a days-2-and-3 dinner edit preserving day 1. PostgreSQL matched the final itinerary at version 3 with six conversation messages. Temporary live-test users were removed afterward.

Failure-path checks confirmed that rejected edits retain the previous saved itinerary, provider exceptions commit no partial messages or preference updates, unavailable tools do not invent records, repeated selections stay bounded, code repairs can avoid another planner call, and excess notebook evidence cannot be treated as verified. Unit tests exercise successful directed route matrices, unreachable cells, source identities, mode constraints, timing gaps and malformed provider responses.

The API image was rebuilt and restarted; the existing local demo backend was reloaded. Both health checks passed, and Compose configuration and edited-file whitespace checks passed. Existing conversations, signing keys and database volumes were retained.

The tested flows work, but an absolute "perfect" claim would exceed this evidence. Successful live ORS routing could not be verified because requests timed out. Prices, booking availability and unsupported transportation facts still require evidence; free-text requirements without structured confirmation remain unverified. LLM/provider latency and behavior can vary. The system exposes partial/failed outcomes rather than treating unknown facts as confirmed.
