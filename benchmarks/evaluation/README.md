# Full-catalog first pass

This runner accounts for all 600 English/Vietnamese catalog cases, runs supported
conditions once, and explicitly records missing adapters. It does **not** claim
to implement the full v1 replay setting or produce an overall quality score.
The original catalog and application prompts/configuration are unchanged.

Current capability map: 408 executable cases and 192 unsupported cases. Coverage
is 68%, which is an execution-coverage percentage, not a model-quality score.

The original completed report is preserved under
[gpt-5-nano/first-run](../runs/gpt-5-nano/first-run/FEEDBACK.md).
The [model results index](../runs/gpt-5-nano/REPORT.md) also links a separate
actual-index retrieval supplement. Its different cases do not replace the
original unsupported retrieval rows.

```powershell
.\.venv\python.exe -B -m benchmarks.scripts.evaluate_current_system --execute --output benchmarks/runs/gpt-5-nano/catalog-next
```

Omit `--execute` to inspect coverage without provider calls. Resume the exact
command after interruption: completed cases are retained, never resampled to pick
a better answer. Changes to application, harness, selection or catalog hashes
require a new directory. Each case has a fresh process, a 420-second deadline,
its original ordered turns, and isolated owner/session IDs. Default concurrency
is four case processes. `--case-ids` is intended for separate pilot directories.

## Execution scope

- Real `gpt-5-nano` interpreter, generator, grounding review/repair and planner.
  The current Jev routers and location extractor also remain active.
- Real PostgreSQL state, with exact newly allocated owner IDs deleted and
  verified by the parent after each case, including terminated workers.
- Public frozen fictional evidence only. Workers never load private labels,
  qrels or witness plans. Private labels are used after execution for grading.
- A scoped SearchService evidence supplier replaces the index; it cannot score
  real vector retrieval or images. All retrieval-track cases are unsupported.
- Grounding cases use all public evidence, including distractors, grouped into
  numbered sources; declared evidence removal/conflict/poison overrides apply.
  This is isolated generation, not the chat-service surface in the v1 design.
- Core planning cases use the public candidate pool and directed matrix through
  the real adaptive tool/planner orchestration. Matrix location coordinates are
  deterministic schema placeholders; no times/distances are derived from them.
  Only explicit public forecast values are returned. Unmatched tools/scopes are
  unavailable. No fixed-notebook setting is claimed by this run.
- API validation cases exercise the real in-process API, asserting that backend
  creation never occurs. Such cases make no model calls.
- Public imported-memory and omitted-history setup is seeded before execution
  and recorded separately from model writes. Planning missing-price/hour/room
  overrides and the declared date closure are applied to public candidates.
- Special planning, visual and dependency-fault cases without faithful adapters
  are recorded as unsupported, never executed as if they had the fault.

The meter counts SDK `responses.create/parse` invocations and returned usage.
Cached tokens are part of input tokens. Jev usage, SDK-internal retries and calls
terminated before returning usage are unavailable. No unverified price table is
used. Worker latency includes imports, DB observations and harness overhead.

## Scoring and artifacts

`run.json` freezes code/config/catalog/dependency provenance. Individual `cases/`
results and per-turn `checkpoints/` preserve responses, actual evidence, raw
interpretation, effective pipeline inputs, state snapshots, errors and usage.
`results.jsonl`, `checks.jsonl`, `review_queue.jsonl`, `summary.json`, `REPORT.md`
and output hashes are produced at completion or with `--report-only`.

Automated checks are deliberately narrow: response-mode/action/scope agreement,
literal memory state, exact write quotes, absent/present preference keys, API
statuses/zero backend calls, core itinerary dates/meal slots/source identities,
and citation identifier validity. These do not establish preference semantics,
citation entailment, full feasibility, usefulness, or task success. An error or
missing expected output cannot pass an implemented check. No-write exact-quote
checks are unscored rather than vacuous passes. Unimplemented fields remain
unscored; they are not silently treated as passing. The application grounding
review is never an independent benchmark verdict.

The labels remain authored draft candidates pending independent bilingual
review. Every human assertion appears in the review queue; unsupported cases
are marked blocked there. There is no combined score across mixed settings or
between infrastructure validation and model responses. The first pass also
exposes author-visible proposed held-out cases, so this is a development audit,
not a pristine held-out generalization result.
