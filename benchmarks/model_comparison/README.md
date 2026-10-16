# Three-model catalog comparison

Requested order: `gpt-5-nano`, `gpt-6-luna`, then `gpt-4.1-nano`.
The first run uses the original evaluator. Later runs reuse its executor, worker,
public cases, frozen evidence, graders, clocks, order seed, deadlines and cleanup.
All 600 catalog entries are accounted for; 408 have supported adapters and 192
remain unsupported. Each model also has a distinct 100-case actual-index retrieval
supplement; it is not a replacement for the original fictional retrieval track.

The queue waits for the admitted gpt-5-nano run before starting the next model.
Each new model gets one recorded structured-output compatibility preflight,
excluded from case scores. Failed access checks stop that model from spending
hundreds of requests on an unusable endpoint; no alternative model is substituted.

Model configurations are isolated in result directories. Application defaults,
prompts and reasoning for existing reasoning models stay unchanged. The request
adapter omits the unsupported reasoning parameter for GPT-4.1 nano. Per-process
transport audits record requested model and effective reasoning settings without
recording credentials or prompt text. SDK usage still comes from the original
meter; preflight usage is separate.

Official compatibility references checked on 16 October 2026:
[GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna),
[GPT-4.1 nano](https://developers.openai.com/api/docs/models/gpt-4.1-nano).

Results live under `benchmarks/runs/<model>/`; queue progress is in
`benchmarks/runs/model-comparison/status.json`. Original historical artifacts
remain unchanged. A model comparison must validate unchanged catalog, original
harness/graders, application code, settings apart from model, and the documented
transport exception. Different served model versions are recorded, not hidden.

These are one-pass authored development diagnostics. Automated subchecks are not
independently reviewed task success, and runtime self-review is not a benchmark
grounding verdict. No overall accuracy score is produced.

## Token usage and cost

The catalog meter records observed OpenAI SDK calls and returned input, cached
input and output tokens. The comparison adapter additionally preserves full
provider usage, including cache writes, reasoning tokens, served model and
service tier. Preflight usage is reported separately from catalog usage. The
retrieval supplement makes no LLM generation calls.

`benchmarks/costs.py` applies the verified, dated rates in `pricing.json`:

```text
ordinary_input = input_tokens - cached_tokens - cache_write_tokens
estimated_USD = (ordinary_input * input_rate
               + cached_tokens * cached_rate
               + cache_write_tokens * write_rate
               + output_tokens * output_rate) / 1,000,000
```

Rates are USD per million tokens; documented long-context multipliers are
applied when relevant. Reasoning tokens are already included in output tokens.
Unavailable required usage or unverified service tiers remain unknown, and an
interrupted attempt prevents claiming a complete total. Reported cost is a
Standard-rate estimate, not a reconciled provider invoice. Jev, local compute
and unreported/internal-retry usage are excluded. Cache state differs between
runs, so these figures describe observed spend rather than a controlled price
comparison or cost per independently reviewed successful answer.

After the queue reaches `ready_for_report`, publish and verify the comparison:

```powershell
.\.venv\python.exe -B -m benchmarks.scripts.compare_catalog_models --publish
```
