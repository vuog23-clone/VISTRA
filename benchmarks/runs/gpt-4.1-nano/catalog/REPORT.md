# VISTRA gpt-4.1-nano: full catalog first pass

Accounted for **600/600 cases**. Executed **408**; **192** lack required adapters/assets.

**An overall model-quality percentage is unavailable.** The draft labels are not independently adjudicated; semantic reviews and some code graders remain pending.

One pass, both languages. Conversations retain their authored turn and owner/session boundaries. Application settings and prompts are unchanged.

| Track | Catalog | Executed | Unsupported | Turn errors |
| --- | ---: | ---: | ---: | ---: |
| routing | 100 | 100 | 0 | 3 |
| retrieval | 100 | 0 | 100 | 0 |
| grounding | 100 | 100 | 0 | 0 |
| memory | 100 | 98 | 2 | 22 |
| planning | 100 | 86 | 14 | 3 |
| reliability | 100 | 24 | 76 | 5 |

Observed SDK calls: **1460**. Input tokens: **4,880,345**; output: **174,779**; cached input (included in input): **596,480**.

Narrow automated checks: {'pass': 441, 'fail': 199, 'unscored': 2115}. These are subchecks with different denominators, not case success. Pending semantic assertions on executed cases: **1118**.

## Interpretation and limits

- Routing and memory use real interpretation, orchestration and isolated PostgreSQL. Evidence-dependent paths replace SearchService/tools with public fictional providers; this does not test the real vector index.
- Grounding runs the actual generator, repair and response contract against all public frozen evidence. It bypasses retrieval and conversation interpretation.
- Planning uses real interpretation and adaptive planning with the public candidate pool, directed route matrix and stipulated zero walking fare. Fixed-notebook-only results are not included.
- Matrix coordinates are deterministic schema transport placeholders (21.03 + index/100000, 105.84). Durations/distances remain the supplied matrix; coordinates never estimate travel time.
- Photo retrieval, live integration, unsupported special planning profiles and unimplemented faults remain unscored. See cases/*.json for reasons.
- API rejection cases test the real in-process API validation boundary with zero backend/model calls.
- Jev remains the configured routing/location provider. Its usage and SDK-internal HTTP retries are not counted by the OpenAI SDK meter.
- One stochastic pass does not estimate repeatability or give a release certification. Author-visible proposed test cases are not a pristine held-out test.
- Self-review approval is a runtime diagnostic, never an independent grounding or usefulness verdict.
- A post-run Standard-rate cost estimate is available in [cost.json](cost.json); the original execution summary did not calculate prices. Latency includes worker startup and isolated DB state snapshots, so it is not browser response latency.

## Files

See `run.json` for frozen source/config/catalog hashes; `results.jsonl` for all cases; `checks.jsonl` for exact subcheck denominators; `review_queue.jsonl` for pending judgments; `summary.json` for machine-readable totals.

## Automated observations

The following are implemented subchecks only. Their agreement rate excludes unscored assertions and cannot be interpreted as overall model accuracy.

| Track | Pass | Fail | Scored subcheck agreement | Unscored assertions/subchecks |
| --- | ---: | ---: | ---: | ---: |
| grounding | 0 | 0 | Unavailable | 400 |
| memory | 77 | 34 | 69.4% | 499 |
| planning | 62 | 21 | 74.7% | 318 |
| reliability | 46 | 0 | 100.0% | 282 |
| retrieval | 0 | 0 | Unavailable | 308 |
| routing | 256 | 144 | 64.0% | 308 |

Turn errors by type: {'ValidationError': 2, 'ValueError': 23, 'UnsafeInput': 8}. Expected security rejections can appear as UnsafeInput and must be distinguished from failed valid requests.

Cleanup failures: []. Worker wall timing: {'count': 408, 'median_seconds': 7.610794500000111, 'p95_seconds': 21.745888000000377}. SDK timing: {'count': 1460, 'median_seconds': 1.2695678999998563, 'p95_seconds': 3.424644000000171}.
