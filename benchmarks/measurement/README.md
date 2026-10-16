# Consistent improvement measurements

This is a fixed **development regression suite** for the five recent improvements,
with failure-handling checks. It complements the authored 300-family golden-set
candidate; it is not that full benchmark or an independently adjudicated test set.
Application code and prompts are not changed by this runner.

## Fixed execution profiles

| Profile | Families | EN/VI cases | Executions at 3 repetitions | Scope |
| --- | ---: | ---: | ---: | --- |
| `offline` | 14 | 28 | 84 | Real application components with frozen interpretation, planner and provider fixtures; network disabled |
| `controlled` | 6 | 12 | 36 | Real configured OpenAI interpretation/generation/review, frozen fictional evidence and isolated in-memory state |

Each family has an accented Vietnamese and English version. Budget scenarios have
two turns and must retain correct state across both. Every execution has a fresh
process and state adapter. Only current messages, allowed memory and public evidence
reach the system. Private expected values and grader decisions are never sent to it.
The suite is development material, not held-out gold; do not claim statistical
generalization from these deliberately selected cases.

The reference date is **2026-10-16**, timezone **Asia/Bangkok**. The real interpreter's
reference-date function is patched in its isolated worker. The seed fixes case
ordering, not model sampling. Default concurrency is one, repetitions three,
and hard deadline 300 seconds per scenario. Timeouts terminate the worker and
drain it; a later case cannot inherit its in-memory mutations. There are no
production conversation writes to clean up.

| Area | Objective measurements |
| --- | --- |
| Planner routing | Actual ChatService/pipeline reaches the planner for an explicit conflicting-date request; explanations use the direct path; literal duration survives interpretation |
| Feasible schedules | Actual orchestrator constructs and renders the fixture schedule; independent slot/identity/hours/route-gap/party-cost/no-final-night checks; an impossible budget releases no invalid plan |
| Image identity/category | RAG and gallery photo coverage, ownership and category scope using a frozen metadata catalog; an empty answer cannot pass a positive photo case |
| Response contract | Rejection/acceptance of fixed malformed/valid drafts; actual generated values, requested table structure, source-ID resolution and obvious language drift |
| Memory updates | Brief acknowledgments, exact saved budget, temporary-versus-durable scope, pipeline call count, commit consistency and answering an additional question |
| Reliability | Failed commit changes no state and returns no success acknowledgment; unavailable review withholds unchecked drafts and stays within its repair budget |

The independent graders do not import production validation/filter functions or
trust a model's grounding approval. They have negative oracle tests for swapped
IDs, wrong values, missing tables, empty photo coverage, photo ownership, schedule
hours/travel gaps/budget, unauthorized durable writes and missing commits.
Application validators still run inside the system under test; their reports
are retained as diagnostics only.

Photo pixels, image embedding/search ranking, live corpus Recall@20/nDCG, actual
PostgreSQL transactions/concurrency, real routing/weather providers and full
adaptive model-authored itineraries are **not measured** here. Existing integration
tests and ad hoc live diagnostics cover other surfaces but do not share these
denominators. The controlled profile covers routing, answers and memory; schedule
construction and photo filtering are component measurements in the offline profile.

## Run the same cases after every change

From the project root:

```powershell
# Inspect the declared selection without providers.
.\.venv\python.exe -B -m benchmarks.scripts.measure_improvements list

# Validate the protocol/oracles and every offline adapter.
.\.venv\python.exe -B -m unittest benchmarks.measurement.tests.test_measurement -q

# Capture the current baseline. Output must be a new empty directory.
.\.venv\python.exe -B -m benchmarks.scripts.measure_improvements run --profile offline --output benchmarks/runs/before-change

# Make the application improvement, then repeat the same command/settings.
.\.venv\python.exe -B -m benchmarks.scripts.measure_improvements run --profile offline --output benchmarks/runs/after-change
.\.venv\python.exe -B -m benchmarks.scripts.measure_improvements compare --baseline benchmarks/runs/before-change --candidate benchmarks/runs/after-change --output benchmarks/runs/change-comparison

# Real model calls, with no live indexes or conversation database writes.
.\.venv\python.exe -B -m benchmarks.scripts.measure_improvements run --profile controlled --live --output benchmarks/runs/model-before
.\.venv\python.exe -B -m benchmarks.scripts.measure_improvements run --profile controlled --live --output benchmarks/runs/model-after
.\.venv\python.exe -B -m benchmarks.scripts.measure_improvements compare --baseline benchmarks/runs/model-before --candidate benchmarks/runs/model-after --output benchmarks/runs/model-comparison
```

`--families` makes an explicitly declared smaller subset; `--repeats`, `--seed`
and `--deadline` change the execution plan. A subset or one-repeat smoke run cannot
be compared with the full/default run. The comparator refuses differing profiles,
selection/order, repeats, clock, fixtures, harness/graders, settings, pricing,
runtime/dependencies/hardware, or observed served model versions. An application
source/prompt change is allowed and listed. A change to measurement code or gold
checks needs a new baseline; it must not be smuggled into an improvement delta.

The old 60-family sample and previous improvement reports have different scopes,
oracles and inputs. They are **not convertible into a valid before score** for this
protocol. Establish the current baseline first. Same-source comparisons only test
repeatability and must never be presented as an improvement.

Exit status: `0` means all automatic checks passed, `1` means an automatic failure
or comparison regression, and `2` means invalid/incomplete execution or arguments.
Pending narrative review does not make automatic checks a quality certification.
There is no resume/best-of-N retry: keep every attempt and start a new run after
fixing a harness problem.

## Metrics and denominators

Each execution has a declared set of applicable named checks. Inapplicable checks
are absent, not free passes. Each check is `pass`, `fail` or `unknown`.

- **Automatic case pass:** every applicable check passes. Failures, provider errors
  and terminated timeouts remain in the planned denominator.
- **Check micro pass:** passed checks / all declared applicable checks, including
  failures and unknowns.
- **Check macro pass:** mean of each execution's applicable-check pass fraction.
- **Bilingual family pass:** both languages pass in every repetition. Report
  language and repetition breakdowns; never select the best repetition.
- **Efficiency:** worker wall-time median and nearest-rank p95 over all recorded
  attempts, including failures. These include worker startup/imports and are not
  frontend/API latency. Raw component timings are also saved.
- **Usage:** logical SDK call counts, reported input/output/cached tokens, plus
  estimates from optional supplied dated prices. Failed/lost usage is unknown,
  not zero. SDK-internal HTTP retries and all billing effects are not fully observed.
- **Human-reviewed task success:** unavailable until every eligible narrative
  execution is reviewed. Passing automatic checks is necessary but insufficient.
  Missing reviews never become passes. Review costs are separate from agent usage.

Comparisons report baseline/candidate counts, percentage-point deltas, worker
latency deltas, call/token/cost deltas and every named check regression/improvement.
Three repetitions expose variability; these small correlated families do not
justify population confidence intervals or an overall weighted “VISTRA quality” score.

## Narrative review

Raw outputs contain everything required to assess the supplied fictional facts.
Copy `reviews.template.json`, set `reviewer`, and use rubric `measurement-review-1`:

- `task_success`: answer all requested parts, preserve updates/questions, no
  unnecessary refusal, no false success/verification, and respect user requirements.
- `claim_support`: every factual claim and citation association is supported by
  the current supplied evidence/user state; unknowns remain unknown. Source-ID
  resolution alone is not entailment. Inspect table rows for swapped facts.
- `language`: requested language/translation and full Vietnamese diacritics;
  allow original names and faithful quotations.

Use `pass`, `fail` or `unknown` per field and record notes. The finite language
and value-presence rules are smoke checks, not full semantic assessment. For a
release-quality study use independent bilingual reviewers and adjudicate disagreement;
this utility does not pretend a single annotation is independently validated gold.

```powershell
.\.venv\python.exe -B -m benchmarks.scripts.measure_improvements review --run benchmarks/runs/model-after --labels benchmarks/runs/model-after-reviewed-labels.json --output benchmarks/runs/model-after-review
```

Review artifacts are separate and do not rewrite the original raw run. A human
label cannot override an objectively failed required check. Cost per reviewed
success uses costs of all eligible attempts, including failures, and stays unavailable
with pending reviews, unknown pricing/usage or zero successes.

## Pricing and provenance

No current dollar prices are guessed. Without `--pricing`, cost is unavailable.
A supplied JSON file must contain `as_of` (`YYYY-MM-DD`), `currency: "USD"`, `source`,
and `models`. Each model maps to finite nonnegative `input_per_million`,
`cached_input_per_million` and `output_per_million` numbers. Supply verified rates
for the configured/served model; the manifest copies and hashes those rates.
The runner does not fetch prices or treat missing cached usage as zero.

Each run saves `manifest.json`, sealed `results.jsonl`, `summary.json`, `REPORT.md`
and `reviews.template.json`. Provenance includes suite/fixture/harness hashes,
application file hashes, commit/dirty-diff hash, settings, runtime/dependencies,
served model IDs, clock, cache policy, seed, budgets and order. API keys, `.env`,
cookies and signing keys are excluded. Files are not overwritten; edits to raw
results invalidate loading. Source/config/fixture/harness drift invalidates the run.

If manual inspection discovers a broken fixture, adapter or grader:

```powershell
.\.venv\python.exe -B -m benchmarks.scripts.measure_improvements invalidate --run benchmarks/runs/broken-run --reason "Describe the concrete harness defect."
```

This adds `INVALIDATED.json`, preserves original evidence and blocks comparisons.
Do not remove a system failure because it lowers the rate. A provider failure
on an otherwise valid run is a measured outcome, not grounds for invalidation.
