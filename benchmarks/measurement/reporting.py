"""Readable artifacts backed by machine-readable counts."""


def percent(value):
    return 'N/A' if value is None else f'{value*100:.1f}%'


def seconds(value):
    return 'N/A' if value is None else f'{value:.3f}'


def report(manifest, summary):
    lines = ['# VISTRA improvement measurement', '',
        f"Suite: `{manifest['suite_version']}`. Profile: **{manifest['profile']}**. Repetitions: **{manifest['repeats']}**.", '',
        '**Automatic development checks, not a whole-system quality percentage.**', '',
        f"Run complete and source stable: **{summary['complete']}**.", '',
        '| Area | Automatic pass / planned | Check micro pass | Worker median (s) | Worker p95 (s) | SDK calls |',
        '| --- | ---: | ---: | ---: | ---: | ---: |']
    for name,row in summary['areas'].items():
        lines.append(f"| {name} | {row['automatic_passed']}/{row['planned_executions']} | {percent(row['check_micro_pass'])} | {seconds(row['worker_wall_median_seconds'])} | {seconds(row['worker_wall_p95_seconds'])} | {row['usage']['llm_sdk_calls']} |")
    lines.extend(['', '## Language and repetition', '',
        '| Language | Automatic pass / planned |', '| --- | ---: |'])
    for name,row in summary['languages'].items():
        lines.append(f"| {name} | {row['automatic_passed']}/{row['planned_executions']} |")
    bilingual = summary['bilingual_families']
    lines.extend(['', f"Families passing both languages and every repetition: **{bilingual['passed_all_languages_and_repetitions']}/{bilingual['families']}**.", '',
        'Per-repeat automatic pass: '+', '.join(f"r{key}: {row['automatic_passed']}/{row['planned_executions']}" for key,row in summary['repetitions'].items())+'.', '',
        '## Review and usage', '',
        f"Human narrative review: **{summary['human_review']['fully_reviewed']}/{summary['human_review']['eligible']}** fully reviewed. Task-success rate: **{percent(summary['human_review']['task_success_rate'])}**.", '',
        'Edit a copy of `reviews.template.json` using the pinned rubric in the measurement README, then use the review command. '
        'An application grounding approval is diagnostic data, never a benchmark verdict.', '',
        f"Reported input/output tokens: **{summary['overall']['usage']['input_tokens']} / {summary['overall']['usage']['output_tokens']}**. "
        f"Estimated cost: **{summary['overall']['usage']['estimated_cost_usd']} USD** ({summary['overall']['usage']['cost_status']}).", '',
        'Missing usage/pricing is unavailable, never zero. Costs use optional dated supplied rates and reported tokens; SDK retries/HTTP attempts are not fully observed.', '',
        '## Interpretation', '',
        'Failures/timeouts stay in planned denominators. Unknown checks prevent all-required success. Inapplicable checks are excluded by the fixed suite.', '',
        'Worker wall times include process startup/imports; they are not browser/API latency. Fresh workers isolate state and enforce termination at the deadline. '
        'Offline mode disables network and replays interpretation/provider fixtures. Controlled mode uses real configured OpenAI calls with frozen fictional evidence '
        'and an in-memory state adapter; it does not test production PostgreSQL, live indexes, image pixels, or the complete travel pipeline.', '',
        'Compare only matching profiles, cases, repetition/order, clock, fixtures, harness, configuration and runtime. '
        'Production source/prompt changes are tracked separately and may differ between paired runs. Historical ad hoc reports cannot serve as a comparable baseline.', '',
        'Raw outputs and named checks: `results.jsonl`. Provenance: `manifest.json`. Full metrics: `summary.json`.', ''])
    return '\n'.join(lines)


def comparison_report(result):
    lines = ['# Paired improvement comparison', '', result['interpretation'], '',
        f"Same application source: **{result['same_application_source']}**. "
        f"Regressed checks: **{len(result['regressions'])}**. Improved checks: **{len(result['improvements'])}**.", '',
        '| Area | Baseline automatic pass | Candidate automatic pass | Delta (percentage points) | Median delta (s) | SDK call delta |',
        '| --- | ---: | ---: | ---: | ---: | ---: |']
    for name,row in result['areas'].items():
        lines.append(f"| {name} | {percent(row['before']['automatic_pass_rate'])} | {percent(row['after']['automatic_pass_rate'])} | {row['automatic_pass_delta_percentage_points']:+.1f} | {seconds(row['worker_median_delta_seconds'])} | {row['sdk_calls_delta']} |")
    lines.extend(['', 'Named check regressions:', ''])
    lines.extend(f"- `{row['execution_id']}`: `{row['check']}` ({row['before']} → {row['after']})" for row in result['regressions'])
    if not result['regressions']:
        lines.append('None.')
    lines.extend(['', 'A same-source comparison is a harness repeatability check, not evidence that VISTRA improved.', ''])
    return '\n'.join(lines)
