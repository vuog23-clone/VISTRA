# VISTRA Benchmark v1 — design specification

Status: **authored golden-set candidates, not an executed benchmark**. The
[golden set](golden/README.md) contains 300 labeled scenario families, synthetic
fixtures and metadata qrels. Independent semantic review, replay/fault adapters,
graders and the scoring runner remain pending. No measured VISTRA scores are supplied.

The separate [development measurement suite](../measurement/README.md) supplies
fixed bilingual regression checks for recent engineering changes. Its component
and controlled-model results do not implement or score this full candidate suite.

## Purpose and relationship to TravelPlanner

Measure whether VISTRA can turn a travel conversation into a useful, grounded answer
or a feasible itinerary, preserve user decisions across turns, and explain missing
evidence honestly.

Borrow TravelPlanner's separation of information collection from planning and its
environment, commonsense and user-constraint checks. Extend evaluation to bilingual
discovery, photo evidence, memory, clarification, weather and scoped edits. This is
a **custom benchmark inspired by TravelPlanner**; its scores are not official
TravelPlanner scores and must not be compared numerically with that leaderboard.

References: [TravelPlanner modes](https://github.com/OSU-NLP-Group/TravelPlanner#running),
[official evaluator](https://github.com/OSU-NLP-Group/TravelPlanner/blob/main/evaluation/eval.py),
[dataset](https://huggingface.co/datasets/osunlp/TravelPlanner).

## Three evaluation settings

| Setting | Inputs and execution | Question answered |
| --- | --- | --- |
| Fixed-notebook planning | Human-checked trip requirements and a frozen notebook; invoke the real planner and its normal repair path | Can the planner satisfy constraints when evidence is supplied? |
| End-to-end replay | Natural-language turns, isolated real PostgreSQL memory, frozen indexes/catalog/photos and replayed external tool responses; run real interpretation, routing, retrieval and adaptive orchestration | Does the whole system work under reproducible evidence? |
| Live integration | Same public requests with current providers and explicitly versioned local data | What happens to quality and latency in an actual deployment? |

Run the applicable families in both controlled settings and report paired results.
Do not merge their scores. Live results are a separate integration report; changing
weather, photos or routes must not move the reproducible baseline.

In replay, tool selection, retrieval queries and LLM outputs remain real. Replay
must not hand the model a gold tool sequence or a preselected correct answer.
Unmatched fixture calls return explicit unavailability rather than invented data.
Fixed-notebook inputs contain all frozen candidate evidence, including distractors,
not a gold selected itinerary. Gold labels never enter model input.

## Dataset size, splits and coverage

The authored v1 candidate contains **300 scenario families**, each expressed in
English and Vietnamese with diacritics: **600 cases**. A family includes one or more
turns, not just one question. Language variants keep the same semantic requirements,
fixture and labels. Human review must check translation equivalence.

| Primary track | Families | Development | Validation | Held-out test |
| --- | ---: | ---: | ---: | ---: |
| Intent, routing and destination scope | 50 | 15 | 10 | 25 |
| Text/photo retrieval and discovery | 50 | 15 | 10 | 25 |
| Grounded Q&A and evidence limitations | 50 | 15 | 10 | 25 |
| Memory and conversational state | 50 | 15 | 10 | 25 |
| Itinerary creation and revision | 50 | 15 | 10 | 25 |
| Dependency failures and recovery | 50 | 15 | 10 | 25 |
| **Total** | **300** | **90** | **60** | **150** |

This produces 180 development, 120 validation and 300 proposed held-out language cases. Assign
splits before tuning. Keep translations, paraphrases, shared scenario templates and
their fixture-derived near-duplicates in the same split. Do not relabel known bugs
into the test set after developers have tuned against them.

The authored candidate has 80 easy, 110 medium and 110 hard families. Audit these
difficulty labels and split balance during independent review.
Difficulty describes the task: easy has one scope and few interacting requirements;
medium combines constraints or short follow-ups; hard includes scoped edits,
conflicts, distractors or degraded evidence. Record evidence completeness separately.

Use several indexed scopes, initially Hanoi, Phu Quoc, Ho Chi Minh City and Vietnam
country discovery, **after verifying snapshot coverage**. Include aliases, negative
photo requests, origins distinct from destinations and genuine ambiguity.

Planning core cases use one or three days with enough evidence for the requested
slots. Five/seven-day and multi-city cases are capacity/stress cases unless authors
independently prove feasibility under the declared candidate, notebook and route
limits. For example, seven days of three unique meals require 21 restaurants, more
than a single current 20-record RestaurantSearch response. Never silently enlarge
production budgets to make a benchmark case pass.

## Evidence and task authoring

Maintain two explicitly labeled fixture profiles:

1. **Snapshot evidence:** immutable exports of actual guides, Google metadata,
   indexed IDs/vectors and, where permitted, photo bytes. Preserve missing prices,
   cuisines, hours and room rules. Record file hashes, collection settings, model
   revisions and provider fetch timestamps. A URL alone is not a reproducible photo.
2. **Controlled synthetic evidence:** clearly fictional venues and records with
   exact prices/units, room rules, directed route matrices and intentional conflicts.
   Use this profile for constraint reasoning that the current real metadata cannot
   verify. Keep synthetic provenance in a fixture sidecar even if an adapter needs
   the application's existing provider enum. Never describe these as scraped facts.

Freeze reference date `2026-10-15` and timezone `Asia/Bangkok` for v1 controlled
runs. Explicit trip dates remain fixed. Pin Open-Meteo responses and their original
city/date/timezone/units/fetch time, including missing days. Pin ORS directed matrices,
mode, location identities, coordinates, null cells and failures. Do not substitute
straight-line distance for a road/walking duration.

Every scenario needs an author review of scope, required slots, evidence availability,
valid alternatives and expected handling. Known-feasible plans need at least one
independently checked witness schedule; it stays private and is not the only accepted
answer. Infeasible cases need a documented contradiction or exhaustive check over
the finite fixture. Otherwise label feasibility unknown. A null metadata value is
neither a contradiction nor proof of feasibility.

For retrieval, create graded qrels: 0 irrelevant, 1 useful context, 2 directly useful.
Annotate stable chunk IDs for guide retrieval and distinct entity IDs for place/photo
retrieval. Pool candidates from multiple retrieval configurations plus independent
catalog inspection; mark unjudged items as unjudged, not automatically irrelevant.
Record judging coverage. A named place's photo can be relevant to its identity while
failing a visual-scene request. Spatial proximity alone never establishes relevance.

## Constraint registry

| Dimension | Independent checks |
| --- | --- |
| Environment | Selected identities exist in the declared current evidence; venue category/city and citation mappings match; tools remain within request scope |
| Commonsense | Correct consecutive days/dates, requested slots, no forbidden repeated restaurants/attractions, nonoverlapping times, known hours and directed travel gaps, sensible city transitions, required lodging nights |
| Hard user requirements | Destination/origin, party size, budget basis/currency, cuisine, room type/rules, occupancy/minimum stays, explicit transport exclusions and requested edit scope |
| Grounding | Each specific factual claim is entailed by available evidence; citations resolve and actually support claims; no invented prices/availability/forecast or image-derived identity |
| Personalization | Exact user-backed durable updates, cross-session recall, current-trip overrides, temporary constraints not persisted as permanent preferences, no cross-owner recall |
| Interaction | Appropriate clarification, short-reply resolution, preserved context, requested output language, requested coverage and no unnecessary city/date/budget demand during discovery |
| Failure handling | Missing evidence disclosed, no false verification, bounded tool repetition/repair, failed edits preserve prior plans, provider exceptions do not commit partial turns |

Label each assertion as a gate, required check or advisory preference. Feasibility
claims require evidence, while preferences rank valid alternatives. For cuisines,
grade the requested property of **every selected meal**, not whether it appears once.
For lodging, verify occupancy as well as price, minimum nights and room rules.

Budget evaluation independently applies explicit units and traveler/room/night counts.
Declare included categories per case. Known zero cost requires explicit fixture
evidence. Do not convert currencies without a declared rate. Count each priced item
once; missing mandatory categories make the complete budget unknown. Approximate
price ranges are not exact booking quotes.

For edits, compare stable entity IDs, day/city/date, slot assignment, within-slot order
and start/end times on protected days. Ignore regenerated notebook/source numbers,
source ordering and harmless formatting. Test creation-plus-edit conversations and
edits from a seeded valid plan separately: creation failure must not hide edit quality.
Seeded-plan and counterfactual diagnostics are reported separately from the canonical
600 language cases and 1800 repeated executions; they do not silently enlarge those
denominators. Keep related diagnostic templates in the same split as their family.

## Expected handling and scoring

Gold expected handling is one of `complete`, `partial`, `clarify`, `reject` or
`infeasible`, `unavailable` or `conflict`. These are **benchmark outcomes**, not new values for the existing
API's `pipeline_status`. Classify from actual answer, structured output, traces and
saved state; do not copy VISTRA's self-reported planning status.

- `complete`: deliver a plan/answer meeting all required, evidence-supported checks.
- `partial`: provide the supported useful portion, disclose required unknowns and
  make no false guarantee. Nulling every slot is not a useful partial plan.
- `clarify`: ask the specific blocking question, retain known details and resume
  when answered. Do not run unrelated evidence collection before a true blocker.
- `reject`: reject invalid/unsafe input without prohibited work or state changes.
- `infeasible`: explain the evidenced conflict without silently relaxing requirements;
  preserve an existing valid plan when an edit cannot be satisfied.
- `unavailable`: report that a backend dependency cannot finish; do not fabricate a
  completed response or commit an unfinished turn.
- `conflict`: report competing request/session state and preserve the newer committed
  data. Return the specified conflict status on applicable API cases.

Each applicable assertion receives `pass`, `fail` or `unknown`; `not_applicable` is
allowed only when determined by the private case definition. Missing evidence can
make a factual constraint unknown, but its honest disclosure is a separate check
that can pass. A skipped grader is a harness error, never an unknown fact or a pass.

For case-run i, let P/F/U be counts of required checks in each state. Report:

- **Constraint micro pass:** sum(P) / sum(P+F+U), separately by dimension.
- **Constraint macro pass:** mean over applicable case-runs of P/(P+F+U).
- **All-required pass:** proportion of applicable case-runs with F=U=0 and all gates
  passed. Do not use macro terminology for this different metric.
- **Handling success:** all gates and required handling assertions pass. A correctly
  explained partial/infeasible outcome can succeed here without a fully verified plan.
- **Plan delivery:** schema-valid nonempty plans / cases requiring plan delivery.
  Empty/invalid outputs and agent timeouts fail delivery; clarification-only cases
  are not in this denominator. Determine eligibility from the gold task before the
  run. An unnecessary clarification on a delivery task counts as failed delivery.
- **Strict planning success:** all environment, commonsense and hard constraints pass
  on the predefined known-feasible planning subset. Unsupported requirements and
  timeouts cannot pass. Always report subset size and results on other subsets.
- **Conversation success:** every required turn succeeds; also show per-turn scores.

An empty or failed system response fails required delivery/answer/state assertions;
do not drop the case from denominators. If a fixture or grader is broken, flag the
entire run incomplete, repair the harness and rerun the same full comparison. A
genuine provider failure in live mode remains a system outcome.

Publish a dashboard, not a weighted universal score:

| Area | Measures |
| --- | --- |
| Decisions | Intent/action/scope accuracy; raw Jev decisions versus final policy decisions; clarification correctness |
| Retrieval | Guide chunk and distinct-place Recall@20, nDCG@10, scope/category violations, qrel coverage; actual generation-context evidence coverage separately |
| Answer | Claim support precision, supported requested-fact coverage, citation resolution and citation support separately; language compliance |
| Planning | Delivery, dimension scores, strict planning success, useful partial-plan handling, initial-draft versus released-plan quality |
| Memory/editing | Required preference-change accuracy, unauthorized durable-write rate, cross-owner leaks, protected-day and locked-venue preservation |
| Reliability | Handling success, successful failure recovery, repeated/invalid tools, repair frequency, request/turn failures |
| Efficiency | End-to-end median/p95, stage timings, LLM/provider calls, input/output tokens and estimated model cost per successful scenario |

Cost per successful scenario is total agent cost of all attempts, including failures,
divided by successful scenarios; it is undefined if none succeed. Also report mean
cost per attempt. Record the dated pricing table, and flag unavailable usage/prices
instead of assigning zero cost. Human/model grader costs are a separate budget.

For qrels, positive relevance means grade 1 or 2; nDCG gain is 2^grade-1. Deduplicate
place entities before scoring; do not compare BM25/text versus image similarity
scores directly. Do not require one exact generated answer or itinerary. A citation
can resolve yet fail to support its neighboring claim. Report unsupported claims
alongside coverage so an empty answer cannot win by making no claims.

For EN/VI, separately report each variant and the proportion of families passing
both. Vietnamese prose must use standard spelling and full diacritics. Preserve
proper names and verbatim quotations in their original form.

## Independent grading and annotation

Use code for identifiers, arithmetic, scope, dates, schema, timing, transport rules,
state diffs and protected edits. Do not import `validate_itinerary` to produce the
benchmark verdict; its result is diagnostic input only. Write small oracle fixtures
with intentionally invalid schedules, budgets, occupancy, hours and citations to
verify that independent graders detect each defect, including defects production
validation currently misses.

Use two bilingual reviewers for pilot claim support, relevance and language labels.
Adjudicate disagreements and version the final labels. Model-assisted judging may
help scale narrative evaluation, but pin its model/rubric and blind it to system name,
version and production pass status. Calibrate against human labels, report agreement
and manual corrections, and keep judge calls/cost out of agent cost. Treat answers
and source text as data, never judge instructions. Soft style scores do not override
a failed factual, user-isolation or protected-edit gate.

## Execution and reproducibility

Use three independent repetitions per language case for the release comparison.
The full planned suite is 1800 scenario executions; multi-turn cases cause additional
requests. Run a small development smoke set first to estimate tokens, runtime and
cost. Report average and per-repeat outcomes, never best-of-three alone.

Default controlled-run budgets match current configuration: retrieval_limit=20,
generation source budget=5, max planning steps=20, max repairs=1. Keep all other
limits at the pinned production configuration and record them. Proposed harness
deadlines are 300 seconds per turn and 900 per scenario; report timeouts as failures
and enforce real cancellation/worker termination, not just an HTTP client timeout.
Use an isolated worker/process/DB namespace so a timed-out turn cannot commit into
a later case. Drain or terminate workers before deleting that run's own data.

Assign fresh owner/session namespaces per family, variant and repetition. Preserve
owner identity only across explicit scenario sessions; use distinct owners for
isolation checks. Seed only declared public prior history and fixture state. Reset
memory and provider caches between cases; test warmed-model and cold-start timing
separately. Fix request order with a recorded seed, pair A/B cases and state budgets,
and disclose hardware, CPU/GPU, concurrency and network boundaries. For HTTP latency,
measure at the client; pipeline elapsed_seconds excludes some interpretation/API work.
The developer API currently returns final JSON, so first-token latency is N/A there;
measure it separately only for an actually streaming interface.

Persist a manifest containing benchmark/schema/label versions, Git commit plus dirty
patch hash, prompt/config/data/model/fixture hashes, reference clock, provider model
IDs, device, seeds, cache policy, budgets, repetitions, deadlines and dependency
versions. Model sampling need not be deterministic even when a seed is recorded.
Persist exact benchmark outputs, normalized identities, visible tool selections and
results, notebook contents, exposed intermediate drafts, repair counts, API results,
memory diffs and independent assertions. Store no keys or hidden model reasoning.
Gold labels stay separate from the public runner payload; never POST the case JSON.

The case schema validates authoring structure, not the truth of labels or the
implementation of check parameters. A future preflight must additionally resolve
fixture/witness/qrel/setup references, reject duplicate case/assertion/turn IDs,
check all referenced turns and split groups, enforce translated-case equivalence,
and bind each check/parameter combination to a versioned grader registry. Reject
unsupported checks before a run. Draft cases are never eligible for scored runs.

For changes, run paired configurations on the same frozen cases. Suggested
experiments: current system; planner with fixed notebook; text-only versus hybrid
on applicable tasks; memory on/off on memory tasks; repair on/off for planning.
These are experiment settings, not changes to the default adaptive production flow.
Use family-clustered paired bootstrap intervals; translations and repetitions are
not independent samples. Report sample sizes and avoid strong tail-latency claims
from a small pilot. Make no claim of significance before sufficient measurements.

## Integration with this repository

| Existing surface | Planned benchmark integration |
| --- | --- |
| `src/planning/planner.py: Planner.run` | Fixed-notebook adapter with public requirements and current evidence |
| `src/service/chat_service.py: ChatService.chat` | Replay conversations and real memory commits |
| `api/server.py: POST /v1/chat` | Public live/deployment contract; fresh aliases mapped to benchmark IDs |
| `src/memory/conversation.py: ConversationInterpreter` | Optional diagnostic intent/action/state capture, never gold substitution |
| `src/service/weather_service.py: reference_today` | Scoped benchmark-worker clock replacement, restored after the run |
| Injected search/planning/weather services | Replay fixtures; add a benchmark-only routing/photo transport adapter where needed |
| `benchmarks/scripts/profile_pipeline.py` | Reference instrumentation for timings/calls; extend for usage and exposed drafts |
| `benchmarks/scripts/benchmark.py` | Existing retrieval smoke timing; not a quality evaluation runner |

The API omits internal interpretation, full notebook and raw router decisions.
Capture those through benchmark-only instrumentation in an isolated process; label
black-box HTTP-only runs as missing those diagnostics. Do not change the public API
to accept private labels, arbitrary memory or server filesystem paths.

## Deliverables and rollout

Files supplied with this design:

- `case.schema.json`: draft authoring contract; private expectations are explicit.
- `golden/`: 300 authored families, 600 public language cases, private expectations,
  synthetic fixtures, qrels, planning witnesses, manifests and a readable case index.

Next deliverables are independently reviewed labels, executable fixture/fault
adapters, independent assertion graders, replay and HTTP runners, and a report
writer. Do not describe authored
candidates as independently adjudicated or as passing agent runs.

Publish no overall pass percentage until all required labels/fixtures are reviewed
and the intended split completes. Report any cross-owner leak, fabricated critical
travel fact or protected-edit violation prominently regardless of averages. Set
release quality thresholds after measuring the baseline and error costs; do not
invent a target such as 95% without evidence. Freeze v1 before comparing changes.
