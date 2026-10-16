# VISTRA benchmarks

Benchmark designs, datasets and supporting tools are grouped here.

## GitHub publication

The golden dataset and its authoring scripts are kept locally and excluded from
Git. They are intended for separate publication on Hugging Face; no repository
URL has been configured yet. Download that dataset into `vistra-v1/golden/`
before running evaluations that require it. Golden-set links below refer to
local files and will become available after the dataset is installed.

Run folders publish Markdown reports and aggregate summary, cost and pricing
JSON files only. Raw results, prompts, labels, evidence, traces, checkpoints and
archives stay local. Consequently, the GitHub reports are a summary distribution;
their detailed artifact links and hash verification require the local run files.
Downloaded TravelPlanner search-tool databases are also excluded.

The application `api/` and `frontend/` folders are excluded from this GitHub
distribution. API/demo commands require those local folders. Railway and Vercel
entry points currently depend on `api/`, so a GitHub deployment requires that
folder to be included again or a separately packaged service.

```text
benchmarks/
  vistra-v1/                  Custom VISTRA benchmark specification and draft cases
  travelplanner/              Existing TravelPlanner query/reference files
    database/                 Optional search-tool database, when separately installed
  runs/gpt-5-nano/            Current results, previous catalog and retrieval supplement
  runs/gpt-6-luna/            Second model's catalog and retrieval supplement
  runs/gpt-4.1-nano/          Third model's catalog and retrieval supplement
  runs/model-comparison/     Cross-model results, verified rates and execution status
  scripts/                    Dataset authoring, evaluation and profiling tools
  evaluation/                 Catalog execution with explicit unsupported cases
  retrieval/                  Actual-index retrieval diagnostics
  measurement/                Repeatable component/model development measurements
  demo/                       Single-case demo server
```

## VISTRA benchmark design

Start with the [v1 specification](vistra-v1/README.md) and the
[golden-set candidate](vistra-v1/golden/README.md): 300 distinct scenario families,
50 per area, with English and Vietnamese variants (600 language cases).
The design separates planning, conversation and live-integration evaluation.

The [case schema](vistra-v1/case.schema.json) defines the authoring contract. The
golden candidate includes expected outcomes, synthetic evidence and metadata qrels.
Independent label review and the full candidate replay/grading runner remain
pending. A separate [consistent development measurement workflow](measurement/README.md)
now measures the recent improvements with fixed bilingual cases and independent
finite-rule checks. Browse all authored scenarios in the
[case index](vistra-v1/golden/CASE_INDEX.md).

Validate the dataset offline from the project root:

```powershell
.\.venv\python.exe -B -m benchmarks.scripts.validate_golden_set
```

## Try a single case

With the local developer API running on port 8067 and `VISTRA_API_KEY` configured
in the project `.env`, start the case picker:

```powershell
.\.venv\python.exe -B -m benchmarks.scripts.demo
```

Open [the single-case demo](http://127.0.0.1:8070/?case=VB1-ROUTE-101&lang=vi).
Select a scenario and language, then click **Run next turn · live**. Multi-turn
cases advance one request at a time. **Fresh run** creates new benchmark users and
sessions; completed turns and memories are saved by the developer API. The API
key stays on the local server. Stop with Ctrl+C; use `--port` or `--api-url` to
change the loopback demo/API ports.

This is an exploratory live prompt trial, with real provider usage. It uses current
data and dates; frozen evidence, fixed-clock replay and fault injection are not
implemented. Cases needing injected state, request mutations or dedicated image,
streaming and reliability adapters are preview-only. Compare replies manually with
the candidate expectations; no benchmark pass/fail score is assigned.

Offline demo contract checks:

```powershell
.\.venv\python.exe -B -m unittest discover -s benchmarks/demo -p "test_*.py" -v
```

## Results

The requested [three-model comparison](runs/model-comparison/REPORT.md) runs
`gpt-5-nano`, `gpt-6-luna`, then `gpt-4.1-nano` with the same supported catalog
cases and separate actual-index retrieval supplements. It reports checks by
family, observed timing, actual returned token usage and estimated API costs.
See [cost accounting](model_comparison/README.md#token-usage-and-cost) for the
formula, cache treatment and missing-usage limitations.

Open the **[gpt-5-nano results](runs/gpt-5-nano/REPORT.md)** for the previous
600-case catalog report and the new 100-case live-index retrieval supplement.
The original report is preserved in `first-run/`; retrieval is a separate cohort
in `retrieval/`, with its own cases, judgments, evidence and consistency checks.
They are not merged into an overall accuracy percentage.

The [results folder](runs/gpt-5-nano/README.md) also contains a machine-readable
summary and organization audit. Useful earlier development results, including
failed code versions used in comparisons, are preserved in one verified
[development archive](runs/gpt-5-nano/DEVELOPMENT_HISTORY.md). Redundant pilots,
smoke tests and interrupted launches are removed from the working layout.

## Run evaluations

Run from the project root, using a new output directory each time:

```powershell
# Read-only local embeddings/index retrieval; no LLM generation calls.
.\.venv\python.exe -B -m benchmarks.scripts.evaluate_retrieval --execute --output benchmarks/runs/gpt-5-nano/retrieval-next

# Full catalog first pass, including actual model calls and coverage gaps.
.\.venv\python.exe -B -m benchmarks.scripts.evaluate_current_system --execute --output benchmarks/runs/gpt-5-nano/catalog-next

# Offline development regression checks.
.\.venv\python.exe -B -m benchmarks.scripts.measure_improvements run --profile offline --output benchmarks/runs/gpt-5-nano/offline-next
```

Use the [retrieval protocol](retrieval/README.md),
[catalog execution scope](evaluation/README.md), and
[consistent measurement workflow](measurement/README.md) for the exact inputs,
metrics and limits. The catalog's original fictional retrieval cases still need
faithful fixture adapters; the actual-index supplement does not relabel them.

After placing a preserved catalog in `first-run/` and a completed actual-index
run in `retrieval/`, rebuild the model results index with:

```powershell
.\.venv\python.exe -B -m benchmarks.scripts.summarize_model_results --run benchmarks/runs/gpt-5-nano
```

## TravelPlanner data

Your existing query CSVs, reference-information JSONL files and example submission
are in [travelplanner/](travelplanner/). They are separate from the search-tool
database expected by VISTRA's optional benchmark adapters.

When installing that database, its paths beneath `travelplanner/database/` must be:

```text
flights/clean_Flights_2022.csv
googleDistanceMatrix/distance.csv
restaurants/clean_restaurant_2022.csv
attractions/attractions.csv
accommodations/clean_accommodations_2022.csv
background/citySet_with_states.txt
```

`config.yaml` sets `planning_benchmark_path` to
`benchmarks/travelplanner/database`. Normal operation retains
`planning_data_mode: vistra`. Downloaded task queries alone do not enable the
benchmark tool database or implement official TravelPlanner evaluation.

The production Docker image excludes benchmark material. If using the optional
tool database inside Docker, mount it read-only at
`/app/benchmarks/travelplanner/database`, or override the configured path to an
existing read-only data mount. No benchmark mount is required for normal chat.

## Profiling scripts

Run from the project root. These are existing timing tools, not the proposed v1
quality evaluator.

```powershell
# Show options without contacting providers or databases.
.\.venv\python.exe -B -m benchmarks.scripts.benchmark --help
.\.venv\python.exe -B -m benchmarks.scripts.profile_pipeline --help

# Query the configured retrieval indexes; no answer-generation calls.
.\.venv\python.exe -B -m benchmarks.scripts.benchmark --live-retrieval

# Profile the running Docker environment with real provider calls.
Get-Content benchmarks/scripts/profile_pipeline.py -Raw | docker exec -i vistra-api-1 python -B - --live --repeats 3
```

The profiler uses temporary benchmark owners and removes only their memory after
the run. Its default report is `/tmp/vistra-stage-timings.json` inside the container.
The retrieval timing script does not request LLM generation; automatic routing is
bypassed by its explicit options. Real pipeline profiling consumes provider usage.

Shared benchmark adapters remain in `src/planning/` because the application imports
them. API/frontend regression tests remain in their respective test directories.
