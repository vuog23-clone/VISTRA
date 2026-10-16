# VISTRA model comparison

Requested order: GPT-5 nano, GPT-6 Luna, then GPT-4.1 nano. Original cases, graders, prompts, clock, worker count and budgets stay fixed. The nonreasoning GPT-4.1 nano request omits the unsupported reasoning parameter.

The comparisons below describe this VISTRA system and the supported development cases. They are not general model rankings. Read matched check transitions together with validation errors, unscored assertions, latency and observed cost.

## Catalog results

| Model | Attempted / completed | Interrupted | Unsupported | Turn errors | Checks pass | Checks fail | Unscored | SDK calls |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| [gpt-5-nano](../gpt-5-nano/catalog-rerun/REPORT.md) | 408 / 404 | 4 | 192 | 8 | 738 | 71 | 2069 | 1424 |
| [gpt-6-luna](../gpt-6-luna/catalog/REPORT.md) | 408 / 408 | 0 | 192 | 13 | 788 | 26 | 2082 | 1563 |
| [gpt-4.1-nano](../gpt-4.1-nano/catalog/REPORT.md) | 408 / 408 | 0 | 192 | 33 | 441 | 199 | 2115 | 1460 |

**These are finite subcheck counts, not overall model accuracy or independently reviewed task success.** Missing/unknown checks and errors remain visible. Expected security rejections can appear in turn-error totals. See individual reports for types, usage and latency.

## Turn errors

| Model | UnsafeInput rejections | Other errors by type |
| --- | ---: | --- |
| gpt-5-nano | 8 | None |
| gpt-6-luna | 8 | ValueError: 5 |
| gpt-4.1-nano | 8 | ValidationError: 2, ValueError: 23 |

UnsafeInput entries can be expected refusals of adversarial inputs. Other exceptions are execution problems that require inspecting the individual case. Externally interrupted attempts are listed separately above; the published raw case records retain every error.

## Checks by family

| Family | Model | Pass | Fail | Unscored |
| --- | --- | ---: | ---: | ---: |
| routing | gpt-5-nano | 382 | 40 | 308 |
| routing | gpt-6-luna | 406 | 15 | 308 |
| routing | gpt-4.1-nano | 256 | 144 | 308 |
| retrieval | gpt-5-nano | 0 | 0 | 308 |
| retrieval | gpt-6-luna | 0 | 0 | 308 |
| retrieval | gpt-4.1-nano | 0 | 0 | 308 |
| grounding | gpt-5-nano | 78 | 0 | 400 |
| grounding | gpt-6-luna | 84 | 0 | 400 |
| grounding | gpt-4.1-nano | 0 | 0 | 400 |
| memory | gpt-5-nano | 139 | 21 | 456 |
| memory | gpt-6-luna | 144 | 8 | 466 |
| memory | gpt-4.1-nano | 77 | 34 | 499 |
| planning | gpt-5-nano | 93 | 10 | 315 |
| planning | gpt-6-luna | 108 | 3 | 318 |
| planning | gpt-4.1-nano | 62 | 21 | 318 |
| reliability | gpt-5-nano | 46 | 0 | 282 |
| reliability | gpt-6-luna | 46 | 0 | 282 |
| reliability | gpt-4.1-nano | 46 | 0 | 282 |

The grounding track's finite checks only validate IDs in recognized `[S#]` citations. They do not establish entailment; answers without that citation syntax have no such scored diagnostic. Grounding and attribution semantics remain unreviewed. Conditional diagnostics also mean raw pass totals have different denominators.

## Observed timing

| Model | Worker median / p95 (s) | SDK call median / p95 (s) |
| --- | ---: | ---: |
| gpt-5-nano | 15.13 / 51.28 | 3.77 / 10.49 |
| gpt-6-luna | 11.12 / 37.55 | 2.13 / 7.13 |
| gpt-4.1-nano | 7.61 / 21.75 | 1.27 / 3.42 |

Worker times include process startup and isolated memory snapshots; they are not frontend response times. SDK figures describe individual provider calls, not whole conversations.

## Matched check changes

| Candidate vs current GPT-5 nano | Fail → pass | Pass → fail | Pass → unscored/missing |
| --- | ---: | ---: | ---: |
| gpt-6-luna | 46 | 5 | 39 |
| gpt-4.1-nano | 25 | 156 | 173 |

Four externally interrupted GPT-5 nano cases are retained in raw totals and explicitly excluded from model-pairing deltas. They are not attributed to model quality or replaced with fresh answers. Run-derived protected-plan expectations can differ because each conversation preserves its own prior generated plan; those values are retained in the comparison audit.

Full changed-check records and compatibility conditions are in [summary.json](summary.json). One stochastic pass does not establish a stable ranking. Model self-review approvals are not independent grounding verdicts.

## Token usage and API-cost estimates

| Model | Input | Cached subset | Cache writes | Output | Known-usage Standard-rate estimate |
| --- | ---: | ---: | ---: | ---: | ---: |
| gpt-5-nano | 4,698,271 | 1,998,208 | unavailable | 816,984 | $0.471788 |
| gpt-6-luna | 5,367,228 | 893,257 | 4333410 | 262,941 | $0.696135 |
| gpt-4.1-nano | 4,880,345 | 596,480 | 0 | 174,779 | $0.513210 |

Input totals already contain cached and cache-write subsets; output totals already contain reasoning tokens. Each subset is priced once. Preflight cost is recorded separately in summary.json. Full spend is unavailable when returned usage is missing or a case was interrupted. These Standard-rate estimates are not verified invoices and exclude Jev, local compute and unreported/internal-retry usage.

Cache state and external-provider variability are not controlled across runs. Costs describe observed executions, not guaranteed future prices or cost per independently reviewed successful task.

[Verified rate snapshot](pricing.json) · [GPT-5 nano prices](https://developers.openai.com/api/docs/models/gpt-5-nano) · [GPT-6 Luna prices](https://developers.openai.com/api/docs/models/gpt-6-luna) · [GPT-4.1 nano prices](https://developers.openai.com/api/docs/models/gpt-4.1-nano)

## GPT-5 nano: first run versus current rerun

Compared with the preserved first run: 17 finite checks changed from fail to pass, 27 from pass to fail, and 39 formerly passing checks became unscored or missing.

The only application change is museum-category normalization. The controlled catalog substitutes search evidence, so differences in its model answers cannot be attributed to that retrieval fix. The actual-index supplement measures retrieval separately.

## Retrieval and coverage

All original 600 catalog entries are accounted for per admitted model. The same 192 entries lack faithful adapters; the new 100-case live-index retrieval suite does not replace the original fictional retrieval track. Voyage/BM25 and SigLIP are shared across the model configurations and do not call the LLM.

Each completed model has its own retrieval evidence and metrics. Pixel appearance, image uploads, overall itinerary feasibility and independent semantic quality are outside the scored scope. No combined percentage across protocols is calculated.

| Model configuration | Cases / errors | Exact-place Hit@5 | Guide section in context | Wrong city / category cards |
| --- | ---: | ---: | ---: | ---: |
| gpt-5-nano | 100 / 0 | 100.0% | 16/20 | 0 / 0 |
| gpt-6-luna | 100 / 0 | 100.0% | 16/20 | 0 / 0 |
| gpt-4.1-nano | 100 / 0 | 100.0% | 16/20 | 0 / 0 |

## Integrity

Completed catalog artifacts, all 600 per-case records, source/catalog/legacy harness consistency and exact isolated-owner PostgreSQL cleanup were verified. Effective configs and transport audits document the model-specific request settings. Preflight usage is separate from benchmark case usage.

[Queue status](status.json) · [Original GPT-5 nano feedback](../gpt-5-nano/first-run/FEEDBACK.md)
