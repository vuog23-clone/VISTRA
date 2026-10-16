# Actual-index retrieval results

Executed **100/100 bilingual cases**, with **0 case errors**, using VISTRA’s real Voyage/BM25 text retriever, SigLIP text-to-image retriever, Qdrant search, metadata enrichment, filters and context/card assembly.

This is a new actual-index diagnostic cohort, **not a replay of the fictional VB1 retrieval cases**. The old 600-case first-pass report remains unchanged. Its unsupported retrieval row means unmeasured, not failed.

| Cohort | Cases | Raw nDCG@10 | Final nDCG@10 | Final precision among returned results | Final Recall@20 |
| --- | ---: | ---: | ---: | ---: | ---: |
| category | 50 | 0.695 | 1.000 | 0.960 | 0.593 |
| known_place | 30 | 0.123 | 1.000 | 1.000 | 1.000 |
| guide_section | 20 | 0.697 | 0.697 | 0.169 | 1.000 |

Known-place Hit@5: raw **0.133**, final **1.000**. MRR@20: raw **0.102**, final **1.000**.

Requested guide section reached generation context in **16/20** cases.

Photo-card wrong-city results: **0**; wrong-category results: **0**.

## What these measures establish

- Category relevance is independently labeled from city/category metadata, including museum/restaurant subtypes. Known-place relevance uses exact normalized indexed names within the declared city. It does not establish photo appearance, beauty, image identity from pixels, or suitability for dietary/accessibility requirements.
- Guide relevance uses the indexed page and Get in/Get around section labels as a proxy. It is not independently reviewed semantic relevance for every sentence.
- Raw photo ranking uses real SigLIP vectors with only an independently specified city scope. Final search adds the application’s category/name constraints, enrichment, defensive filtering and distance reranking. High final precision can therefore reflect correct filtering rather than a strong raw embedding ranker.
- precision_at_20 uses denominator 20. The table reports precision among actually returned distinct results, separately from Recall@20, result counts and empty-result cases. An empty positive query scores zero. Recall ceilings are recorded because a category may contain more than 20 relevant places.
- Routes and city options are explicitly supplied to isolate retrieval. Jev interpretation/routing, conversational rewrites, LLM generation, image uploads, photo download availability and visual-scene relevance are not evaluated.
- Cases and labels were defined before querying from a read-only metadata/index inventory. Labels are metadata-derived and have not been independently human adjudicated. These are authored diagnostics, not a random generalization sample.
- The existing live index was queried read-only. Before/after vector and payload fingerprints cover all image points and the five scoped guide pages. They detect changes but are not a replayable vector export.
- Weights are loaded once and reused; startup time is recorded separately. No application/model/prompt setting was changed and no OpenAI/Jev generation calls are made.

## Files

`cases.json` and `qrels.json` contain the declared inputs/labels; `metadata.json`, `guide_chunks.json` and index inventories record evidence provenance. `results.jsonl` and `cases/` retain raw ranks, final search results, context, cards, timings and errors. `verification.json` records source/data consistency checks.
