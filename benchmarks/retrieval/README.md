# Actual-index retrieval diagnostic

This fixes the absence of **measured real retrieval** by supplying a separate
100-case, 50-family English/Vietnamese diagnostic against the existing indexes.
It does not relabel the original fictional v1 cases as executed.

Current results: [gpt-5-nano results](../runs/gpt-5-nano/REPORT.md) and
[100-case retrieval supplement](../runs/gpt-5-nano/retrieval/REPORT.md).

```powershell
.\.venv\python.exe -B -m benchmarks.scripts.evaluate_retrieval --execute --output benchmarks/runs/gpt-5-nano/retrieval-next
```

The run is read-only against PostgreSQL and Qdrant. It does not write memory,
change model/config/prompts, create/drop collections, download photos or generate
LLM answers. Cached Voyage, BM25 and SigLIP models run through the production
retrievers, SearchService, enrichment, scope/category/name filters, distance
reranking, RAG context builder and frontend place-card builder. Supplied city and
route options isolate retrieval from Jev routing and conversation interpretation.

## Inputs and judgments

Before querying, save all place/photo metadata, the five guide pages' indexed
chunks, and payload/vector hashes for every image point and scoped text point.
Define inputs and judgments from this snapshot, independently of production
category/scope filters and before examining retrieved outputs. Check the same
data and code hashes after execution; a changing input invalidates the run.
These fingerprints detect changes; they do not constitute a replayable export.

- 25 city/category families: five cities × cafe/restaurant/museum/park/hotel.
- 15 known-place families: deterministic metadata-supported cafe/restaurant/hotel
  names in each city; identical names in the same city are allowed alternatives.
- 10 guide-section families: Get in and Get around for each city.
- Each family has one English and one fully accented Vietnamese variant.

The photo relevance labels concern **metadata**, not image pixels. Guide labels
use section membership as a proxy, not independently reviewed claim relevance.
No image-upload, visual-scene, scenery-quality, photo URL availability, dietary,
accessibility or routing score is claimed. This is an authored, scoped diagnostic
with metadata-derived labels pending independent review, not a generalization
score or the synthetic 100-case retrieval track from the first pass.

## Measurements

For photos, record raw SigLIP grouped rankings with only a known city constraint,
then the final pipeline's rankings after its additional filters and reranking.
High final precision may result from correct filtering rather than strong raw
visual/text ranking. For guides, retain real dense/BM25/RRF chunk rankings and
test whether the requested section survives the generation context budget.

- Recall@20, binary nDCG@10, Hit@5 and MRR@20, deduplicated by place/chunk ID.
- Standard Precision@20 divides by 20, including missing positions.
- Precision **among returned results** divides by the returned count, capped at
  20; empty positive queries score zero. Both measures are recorded explicitly.
- Recall ceilings and relevant-pool sizes explain category pools larger than 20.
- No relevant records means Recall/nDCG are unavailable; correct empty handling
  is recorded separately and never called perfect recall.
- Wrong-city, wrong-category, wrong-identity and photo-binding checks, plus
  frontend-card and generation-context observations.
- Errors remain in every applicable denominator. Raw/final ranks, full retrieved
  evidence and timings are retained. Model loading is timed separately.

See `REPORT.md`, `summary.json`, `results.jsonl`, `cases.json`, `qrels.json` and
`verification.json` in the chosen output directory. The previous 600-case nano
baseline remains immutable and must not be numerically merged with this cohort.

Offline metric checks:

```powershell
.\.venv\python.exe -B -m unittest benchmarks.retrieval.test_metrics
```
