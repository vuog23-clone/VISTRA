# Current gpt-5-nano system: findings

The first pass accounts for **600/600 cases**: **408 executed**, **192 unsupported**. The execution-coverage percentage is 68%; it is not model accuracy.

The [coverage-gap list](COVERAGE_GAPS.md) identifies every unexecuted scenario family and why it remains unsupported.

**There is no defensible overall quality percentage yet.** This is a mixed controlled diagnostic, with incomplete replay/grading adapters and draft, unadjudicated labels.

## What was measured

| Routing subcheck | Pass | Fail | Agreement |
| --- | ---: | ---: | ---: |
| response_mode | 95 | 5 | 95.0% |
| raw_intent_agreement | 92 | 8 | 92.0% |
| effective_action | 90 | 10 | 90.0% |
| scope | 94 | 6 | 94.0% |

Response mode is observed separately from the interpreter’s initial intent and the effective action. Scope/literal-state mismatches include representation differences; spot-review caveats below must be considered.

The **100 isolated grounding cases** have runtime statuses {'checked': 61, 'contract_failed': 24, 'repaired': 14, 'unverified': 1}. Approval/repair counts are the system's own reviewer diagnostics, **not independent correctness scores**. Fallbacks and errors are listed in diagnostics.json.

The grounding track’s scored automated subchecks in REPORT.md only validate citation identifiers when a final answer contains citations. They do not score entailment, usefulness, or answers with no citations. A high identifier-validity rate must not be called grounding accuracy.

Planning status by attempted planning-track turn: {'partial': 59, 'failed': 13, 'needs_clarification': 17, 'error': 4, 'no_planning_report': 7}. Protected-day subchecks: {'pass': 12, 'fail': 2}. Complete independent feasibility grading remains unavailable.

Planning provider coverage is deliberately limited: the candidate pool and directed matrix are replayed, while GuideSearch, CitySearch, WeatherSearch and FlightSearch return unavailable in this planning adapter. These missing tool outputs can force partial plans or repeated-tool stops. Those outcomes cannot be attributed solely to gpt-5-nano or equated with failure in a fully supplied environment.

Real API validation: 18/18 requests stopped before backend creation. These cases make zero model calls and measure API behavior.

Actual returned usage: 1418 SDK calls; 4,634,135 input tokens and 810,238 output tokens. Served models: {'gpt-5-nano-2025-08-07': 1415, 'unavailable': 3}. No estimated dollar cost is supplied.

## Actionable observations from assistant spot review

These are selected diagnostic examples, reviewed after outputs were visible. They are not an unbiased sample, human adjudication, or a pass-rate estimate. Original labels, raw responses and automated grades are retained unchanged.

- **VB1-MEM-127-en — unsupported_cited_claim:** The answer recommends Sao Beach, Long Beach and Ong Lang Beach with [S1]. S1 contains only fictional Benchmark Phu Quoc Beach A and Resort A; those named real-beach claims are absent. The application grounding status is checked. [Raw response](cases/VB1-MEM-127-en.json)

- **VB1-GROUND-143-en — delivery_failure:** The missing-ticket-price explanation was replaced by a generic response-format limitation. [Raw response](cases/VB1-GROUND-143-en.json)

- **VB1-GROUND-149-en — delivery_failure:** The child-ticket question received a generic response-format limitation instead of a source-qualified explanation that the child price is unknown. [Raw response](cases/VB1-GROUND-149-en.json)

- **VB1-MEM-149-en — benign_request_rejected:** The English request explicitly asks to ignore an imported credential-revelation instruction and recall diet, but the input guard rejects it as prompt_injection. [Raw response](cases/VB1-MEM-149-en.json)

- **VB1-MEM-104-en — memory_key_mismatch:** An explicit lasting preference for walking was stored under pace instead of transport. The verbatim quote was preserved, but the structured preference category is wrong. [Raw response](cases/VB1-MEM-104-en.json)

- **VB1-MEM-133-vi — trip_setting_leak:** A trip-specific weather exclusion was persisted as a lasting interests preference. The subsequent fresh Hue trip also retained include_weather=false instead of clearing the old setting. [Raw response](cases/VB1-MEM-133-vi.json)

- **VB1-MEM-117-vi — operation_count_grader_caveat:** The zero-write check counts a requested deletion of an already absent diet key as a write. Both before and after preference records are empty, so this is not evidence of a lasting preference being added. [Raw response](cases/VB1-MEM-117-vi.json)

- **VB1-PLAN-110-en — structured_interpretation_failure:** The request failed TurnUnderstanding.trip_state validation instead of producing a useful plan or clarification. The recorded validation error reports a blank or over-limit memory field. [Raw response](cases/VB1-PLAN-110-en.json)

- **VB1-PLAN-130-en — structured_interpretation_failure:** The request failed TurnUnderstanding.trip_state validation instead of producing a useful plan or clarification. The recorded validation error reports a blank or over-limit memory field. [Raw response](cases/VB1-PLAN-130-en.json)

- **VB1-MEM-127-en — literal_grader_caveat:** The destination subcheck compares Phu Quoc, Vietnam with Phu Quoc literally. These are compatible geographic names; this literal failure does not establish a destination-memory error. [Raw response](cases/VB1-MEM-127-en.json)

- **VB1-ROUTE-120-en — literal_grader_caveat:** The system asks to clarify an unrecognized destination using an ambiguous status before catalog validation. The narrow scope check expects unresolved and fails despite an appropriate clarification. [Raw response](cases/VB1-ROUTE-120-en.json)

- **VB1-GROUND-145-en — catalog_review_needed:** The single user message refers to answering the travel question without supplying an actual travel question. This case needs author review before judging the requested answer semantically. [Raw response](cases/VB1-GROUND-145-en.json)

## Recommended order of work

1. Review citation entailment independently of the generator. A known source ID and a successful self-review are insufficient; the unsupported beach example demonstrates this.
2. Fix useful-answer delivery when evidence is missing. Preserve a concise, cited limitation instead of replacing the answer with a generic format error.
3. Review benign meta-instructions rejected by the input guard, especially the English imported-memory case.
4. Validate memory key and scope as well as exact source quotes. Check transport-versus-pace classification, trip-only facts becoming lasting preferences, and fresh-trip setting resets.
5. Recover gracefully from invalid structured interpretation: bounded repair or a targeted clarification should replace uncaught validation errors.
6. Complete faithful tool/fault/photo replay adapters, canonicalize equivalent place names and distinguish attempted operations from lasting DB changes in grading, then adjudicate candidate labels. Rerun the same fixed protocol with repeated trials before reporting overall quality or a release score.

The application, model configuration and prompts were not changed during this baseline. This report makes no comparison with earlier differently scoped 36-turn/60-family runs.
