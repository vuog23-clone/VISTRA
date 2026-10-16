# VISTRA — gpt-5-nano benchmark results

The previous full-catalog result and the fresh actual-index retrieval supplement are collected here. They use different cases and protocols; **there is no merged accuracy score**.

## Previous full-catalog run

The original **600-case** record is preserved byte for byte: **408 executed**, **192 unsupported**, and **10 turn errors**. It has not been overwritten.

[Original feedback](first-run/FEEDBACK.md) · [Full report](first-run/REPORT.md) · [Raw results](first-run/results.jsonl)

| Track | Catalog | Executed | Unsupported | Turn errors |
| --- | ---: | ---: | ---: | ---: |
| routing | 100 | 100 | 0 | 0 |
| retrieval | 100 | 0 | 100 | 0 |
| grounding | 100 | 100 | 0 | 0 |
| memory | 100 | 98 | 2 | 1 |
| planning | 100 | 86 | 14 | 4 |
| reliability | 100 | 24 | 76 | 5 |

## Current catalog rerun

A fresh pass accounts for **600/600** original cases: **408 attempted**, including **404 completed** and **4 externally interrupted**. **192** remain unsupported; completed turns include **8 turn errors**. Interrupted attempts were preserved without replacement answers.

Finite checks: {'pass': 738, 'unscored': 2069, 'fail': 71}. These are not overall task-success scores.

[Current catalog report](catalog-rerun/REPORT.md) · [Comparison to first run](catalog-rerun/comparison-to-first-run.json) · [Three-model comparison](../model-comparison/REPORT.md)

## Token usage and estimated API cost

| Run | Input tokens | Cached subset | Output tokens | Known-usage Standard-rate estimate |
| --- | ---: | ---: | ---: | ---: |
| first-run | 4,634,135 | 1,939,968 | 810,238 | $0.468503 |
| catalog-rerun | 4,698,271 | 1,998,208 | 816,984 | $0.471788 |

Cached tokens are included in input totals and are charged at their own rate. These are estimates for reported usage, not complete invoices: three first-run calls lack usage, and four current-run attempts were interrupted before complete usage could be saved. Jev, SDK-internal retries and local compute are unpriced.

[Cost details and limitations](costs.json) · [Official GPT-5 nano prices](https://developers.openai.com/api/docs/models/gpt-5-nano)

## Fresh retrieval supplement

Executed **100/100** cases with **0 errors**: 50 English and 50 fully accented Vietnamese variants, across five cities. These are the new live-index cases selected for this update, not the original fictional VB1 retrieval cases.

| Check | Result |
| --- | ---: |
| Exact-place Hit@5 | 100.0% over 30 cases |
| Relevant city/category results among returned places | 682/682 |
| Correctly empty category queries | 2/2 |
| Wrong-city / wrong-category frontend cards | 0 / 0 |
| Photo-to-place binding violations | 0 |
| Guide-section Recall@20 | 100.0% |
| Guide-section nDCG@10 | 0.697 |
| Requested guide section reaches generator context | 16/20 |

[Detailed retrieval report](retrieval/REPORT.md) · [Raw results](retrieval/results.jsonl) · [Consistency checks](retrieval/verification.json)

Category precision among all returned places differs from standard Precision@20 and from the macro average that includes two correctly empty queries. See the detailed report for all denominators. Category Recall@20 is capped when a city/category has more than 20 eligible records.

## What remains weak

Raw SigLIP category precision among its returned top 20 is 50.6%; raw exact-name Hit@5 is 13.3%. Final results benefit from the existing city, category and identity filters. This is metadata relevance, not judged photo appearance.

The requested guide section misses the generator context in 4/20 cases, despite retrieval containing it. Improving section ranking before the excerpt budget remains the next focused retrieval change.

Context misses: `RI1-046-vi`, `RI1-045-vi`, `RI1-041-vi`, `RI1-043-en`.

## Scope and provenance

The LLM configuration remains **gpt-5-nano**. Retrieval uses cached Voyage/BM25 and SigLIP models without calling the LLM; grouping it here describes the system configuration. 1 application file(s) differ from the original historical snapshot; hashes and the filenames are recorded in [summary.json](summary.json). This is not a matched before/after model comparison.

The original unsupported retrieval row remains unchanged. Do not add this supplement to its numerator and call it 508/600 coverage: these are different scenarios. Human-reviewed overall task success and photo-pixel relevance remain unavailable.

[Development history](DEVELOPMENT_HISTORY.md) · [Organization audit](organization.json) · [Grouping verification](verification.json)

The archive retains useful earlier component runs and failed versions used in comparisons. Redundant pilots, smoke checks and interrupted retrieval launches were removed; their original file hashes and cleanup reasons are retained in the audit.
