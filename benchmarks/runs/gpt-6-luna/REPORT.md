# VISTRA — gpt-6-luna benchmark results

The catalog accounted for **600/600** entries: **408 executed**, **192 unsupported** and **13 turn errors**.

[Catalog report](catalog/REPORT.md) · [Raw cases](catalog/results.jsonl) · [Model comparison](../model-comparison/REPORT.md)

Narrow automated checks: {'pass': 788, 'unscored': 2082, 'fail': 26}. Overall task success remains unscored pending independent review.

Prompts, graders, public fixtures and application code match the current GPT-5 nano run. The model is selected using the saved effective configuration; the shared application default is unchanged.

Effective reasoning setting: low.

Known-usage Standard-rate API estimate: **$0.696135**. [Token counts and cost limitations](catalog/cost.json). Preflight usage is separate; this is not a verified invoice.

## Retrieval supplement

Executed 100/100 distinct live-index cases with 0 errors. Requested guide section reached generation context in 16/20 cases.

[Retrieval report](retrieval/REPORT.md) · [Raw retrieval results](retrieval/results.jsonl)

Retrieval uses shared embedding models without LLM generation; these cases do not replace the unsupported original fictional retrieval track. Metadata relevance is measured; photo appearance is not.
