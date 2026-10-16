"""Bounded, resumable execution of every catalog entry with explicit gaps."""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime,timezone
from hashlib import sha256
import json
from pathlib import Path
import random
import secrets
import subprocess
import sys
from time import perf_counter

from benchmarks.evaluation.protocol import ROOT,GOLDEN,PROTOCOL,capability,public_cases,track


def write(path,value):
    text = json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n'
    temp = path.with_suffix('.tmp')
    temp.write_text(text,encoding='utf-8')
    temp.replace(path)


def fingerprints():
    from benchmarks.measurement.engine import sources,hashes
    return {'application':sources(), 'harness':hashes([*Path(__file__).parent.glob('*.py'),
        ROOT/'benchmarks/scripts/evaluate_current_system.py',ROOT/'benchmarks/measurement/usage.py']),
        'catalog':hashes([p for p in GOLDEN.rglob('*') if p.is_file()])}


def execute(case,manifest,out):
    from benchmarks.evaluation.worker import cleanup
    mode,reason = capability(case)
    base = {'case_id':case['case_id'],'family_id':case['family_id'],'track':track(case),'language':case['language'],
            'mode':mode,'planned_turns':len(case['turns'])}
    if reason:
        return {**base,'outcome':'unsupported','reason':reason,'turns':[],'provider_calls':[]}
    payload = {'case':case,'namespace':manifest['namespace'],'checkpoint':str(out/'checkpoints'/f"{case['case_id']}.json")}
    start = perf_counter()
    attempt = out/'attempts'/f"{case['case_id']}.json"
    if attempt.exists():
        # A process interrupted before its result was sealed must not become an
        # undisclosed second model attempt when the run resumes.
        checkpoint = Path(payload['checkpoint'])
        result = json.loads(checkpoint.read_text(encoding='utf-8')) if checkpoint.exists() else {'turns':[],'provider_calls':[]}
        result.update(base,outcome='interrupted',reason='Earlier admitted attempt was interrupted; no model resampling.')
        if mode == 'chat_provider_replay':
            result['cleanup'] = cleanup(case,manifest['namespace'])
        return result
    write(attempt,{'admitted_at':datetime.now(timezone.utc).isoformat(),'case_id':case['case_id']})
    try:
        process = subprocess.run([sys.executable,'-B','-m','benchmarks.evaluation.worker'],
            input=json.dumps(payload,ensure_ascii=True),text=True,encoding='utf-8',capture_output=True,
            cwd=ROOT,timeout=manifest['deadline_seconds'])
        if process.returncode:
            from src.utils.redaction import CredentialRedactor
            result = {'outcome':'harness_error','error':CredentialRedactor().text(process.stderr[-2000:]),'turns':[]}
        else:
            result = json.loads(process.stdout)
    except subprocess.TimeoutExpired:
        checkpoint = Path(payload['checkpoint'])
        result = json.loads(checkpoint.read_text(encoding='utf-8')) if checkpoint.exists() else {'turns':[],'provider_calls':[]}
        result.update(outcome='timeout',reason='Case deadline expired; uncompleted SDK calls have unavailable usage.')
    except Exception as error:
        result = {'outcome':'harness_error','error':type(error).__name__,'turns':[]}
    result.update(base,wall_seconds=perf_counter()-start)
    if mode == 'chat_provider_replay':
        try:
            result['cleanup'] = cleanup(case,manifest['namespace'])
        except Exception as error:
            result['cleanup'] = {'verified_deleted':False,'error_type':type(error).__name__}
    return result


def report(out, cases, manifest):
    from benchmarks.evaluation.grading import grade
    private = {r['family_id']:r for p in (GOLDEN/'private').glob('*.jsonl') for r in map(json.loads,p.read_text(encoding='utf-8').splitlines())}
    rows, checks, review = [],[],[]
    for case in cases:
        path = out/'cases'/f"{case['case_id']}.json"
        if not path.exists():
            continue
        row = json.loads(path.read_text(encoding='utf-8'))
        row_checks = grade(case,private[case['family_id']],row)
        for check in row_checks:
            checks.append({'case_id':case['case_id'],'track':track(case),'language':case['language'],**check})
        for assertion in private[case['family_id']]['private_expectations']['assertions']:
            if assertion['grader']=='human':
                review.append({'case_id':case['case_id'],'assertion':assertion,'status':
                    'blocked_unsupported' if row['outcome']=='unsupported' else 'pending','verdict':None,'reviewer':None})
        rows.append(row)
    for name,values in [('results',rows),('checks',checks),('review_queue',review)]:
        (out/(name+'.jsonl')).write_text(''.join(json.dumps(v,ensure_ascii=False,allow_nan=False)+'\n' for v in values),encoding='utf-8')
    executed = [r for r in rows if r['outcome']!='unsupported']
    events = [e for r in rows for e in r.get('provider_calls',[])]
    usage = {key:sum((e.get('usage') or {}).get(key) or 0 for e in events) for key in ('input_tokens','output_tokens','cached_input_tokens')}
    def timing(values):
        values = sorted(values)
        if not values:
            return None
        from math import ceil
        from statistics import median
        return {'count':len(values),'median_seconds':median(values),'p95_seconds':values[ceil(.95*len(values))-1]}
    summary = {'catalog_cases':len(cases),'accounted_cases':len(rows),'executed_cases':len(executed),
        'unsupported_cases':sum(r['outcome']=='unsupported' for r in rows),'outcomes':dict(Counter(r['outcome'] for r in rows)),
        'planned_turns':sum(len(c['turns']) for c in cases),'attempted_turns':sum(len(r.get('turns',[])) for r in rows),
        'turn_errors':sum('error' in t for r in rows for t in r.get('turns',[])),
        'cases_with_turn_errors':sum(any('error' in t for t in r.get('turns',[])) for r in rows),
        'turn_errors_by_type':dict(Counter(t['error']['type'] for r in rows for t in r.get('turns',[]) if 'error' in t)),
        'case_error_ids':[r['case_id'] for r in rows if r['outcome'] not in {'completed','unsupported'} or any('error' in t for t in r.get('turns',[]))],
        'automated_failure_ids':sorted({c['case_id'] for c in checks if c['status']=='fail'}),
        'provider_calls':len(events),'served_models':dict(Counter(e.get('served_model','unavailable') for e in events)),
        'provider_usage':usage,'calls_missing_usage':sum(e.get('usage') is None for e in events),
        'worker_wall_timing':timing([r['wall_seconds'] for r in executed if 'wall_seconds' in r]),
        'sdk_call_timing':timing([e['elapsed_seconds'] for e in events if 'elapsed_seconds' in e]),
        'checks_by_track':{area:dict(Counter(c['status'] for c in checks if c['track']==area)) for area in sorted({track(c) for c in cases})},
        'checks_by_language':{lang:dict(Counter(c['status'] for c in checks if c['language']==lang)) for lang in ('en','vi')},
        'check_counts':dict(Counter(c['status'] for c in checks)),
        'semantic_reviews_pending':sum(r['status']=='pending' for r in review),
        'overall_task_success_percent':None,'monetary_cost':None,
        'cleanup_failures':[r['case_id'] for r in rows if r.get('cleanup',{}).get('verified_deleted') is False]}
    write(out/'summary.json',summary)
    text = ['# VISTRA gpt-5-nano: full catalog first pass','',
        f"Accounted for **{len(rows)}/{len(cases)} cases**. Executed **{len(executed)}**; **{summary['unsupported_cases']}** lack required adapters/assets.",
        '', '**An overall model-quality percentage is unavailable.** The draft labels are not independently adjudicated; semantic reviews and some code graders remain pending.',
        '', 'One pass, both languages. Conversations retain their authored turn and owner/session boundaries. Application settings and prompts are unchanged.',
        '', '| Track | Catalog | Executed | Unsupported | Turn errors |', '| --- | ---: | ---: | ---: | ---: |']
    for area in ('routing','retrieval','grounding','memory','planning','reliability'):
        group = [r for r in rows if r['track']==area]
        text.append(f"| {area} | {sum(track(c)==area for c in cases)} | {sum(r['outcome']!='unsupported' for r in group)} | {sum(r['outcome']=='unsupported' for r in group)} | {sum('error' in t for r in group for t in r.get('turns',[]))} |")
    text += ['', f"Observed SDK calls: **{len(events)}**. Input tokens: **{usage['input_tokens']:,}**; output: **{usage['output_tokens']:,}**; cached input (included in input): **{usage['cached_input_tokens']:,}**.",
        '', f"Narrow automated checks: {summary['check_counts']}. These are subchecks with different denominators, not case success. Pending semantic assertions on executed cases: **{summary['semantic_reviews_pending']}**.",
        '', '## Interpretation and limits','',
        '- Routing and memory use real interpretation, orchestration and isolated PostgreSQL. Evidence-dependent paths replace SearchService/tools with public fictional providers; this does not test the real vector index.',
        '- Grounding runs the actual generator, repair and response contract against all public frozen evidence. It bypasses retrieval and conversation interpretation.',
        '- Planning uses real interpretation and adaptive planning with the public candidate pool, directed route matrix and stipulated zero walking fare. Fixed-notebook-only results are not included.',
        '- Matrix coordinates are deterministic schema transport placeholders (21.03 + index/100000, 105.84). Durations/distances remain the supplied matrix; coordinates never estimate travel time.',
        '- Photo retrieval, live integration, unsupported special planning profiles and unimplemented faults remain unscored. See cases/*.json for reasons.',
        '- API rejection cases test the real in-process API validation boundary with zero backend/model calls.',
        '- Jev remains the configured routing/location provider. Its usage and SDK-internal HTTP retries are not counted by the OpenAI SDK meter.',
        '- One stochastic pass does not estimate repeatability or give a release certification. Author-visible proposed test cases are not a pristine held-out test.',
        '- Self-review approval is a runtime diagnostic, never an independent grounding or usefulness verdict.',
        '- Cost is unavailable: no verified pricing table was supplied. Latency includes worker startup and isolated DB state snapshots, so it is not browser response latency.',
        '', '## Files','', 'See `run.json` for frozen source/config/catalog hashes; `results.jsonl` for all cases; `checks.jsonl` for exact subcheck denominators; `review_queue.jsonl` for pending judgments; `summary.json` for machine-readable totals.']
    text += ['', '## Automated observations','',
        'The following are implemented subchecks only. Their agreement rate excludes unscored assertions and cannot be interpreted as overall model accuracy.',
        '', '| Track | Pass | Fail | Scored subcheck agreement | Unscored assertions/subchecks |', '| --- | ---: | ---: | ---: | ---: |']
    for area,counts in summary['checks_by_track'].items():
        passed,failed = counts.get('pass',0),counts.get('fail',0)
        rate = f'{100*passed/(passed+failed):.1f}%' if passed+failed else 'Unavailable'
        text.append(f"| {area} | {passed} | {failed} | {rate} | {counts.get('unscored',0)} |")
    text += ['', f"Turn errors by type: {summary['turn_errors_by_type']}. Expected security rejections can appear as UnsafeInput and must be distinguished from failed valid requests.",
        '', f"Cleanup failures: {summary['cleanup_failures']}. Worker wall timing: {summary['worker_wall_timing']}. SDK timing: {summary['sdk_call_timing']}."]
    (out/'REPORT.md').write_text('\n'.join(text)+'\n',encoding='utf-8')
    write(out/'artifact_hashes.json',{p.name:sha256(p.read_bytes()).hexdigest() for p in out.glob('*') if p.is_file() and p.name!='artifact_hashes.json'})
    return summary


def main():
    from src.utils.utils import load_config
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--workers',type=int,choices=(1,2,4),default=4)
    parser.add_argument('--deadline',type=int,default=420)
    parser.add_argument('--case-ids',nargs='+')
    parser.add_argument('--report-only',action='store_true')
    args = parser.parse_args()
    out = args.output.resolve()
    if not out.is_relative_to((ROOT/'benchmarks/runs').resolve()):
        parser.error('Output must be inside benchmarks/runs.')
    cases = public_cases()
    if args.case_ids:
        cases = [c for c in cases if c['case_id'] in args.case_ids]
        if set(args.case_ids) != {c['case_id'] for c in cases}:
            parser.error('Unknown case ID.')
    if not args.execute and not args.report_only:
        print(json.dumps({'cases':len(cases),'modes':dict(Counter(capability(c)[0] for c in cases))},indent=2))
        return
    out.mkdir(parents=True,exist_ok=True)
    for name in ('cases','checkpoints','attempts'):
        (out/name).mkdir(exist_ok=True)
    manifest_path = out/'run.json'
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        if manifest['fingerprints'] != fingerprints() or manifest['case_ids'] != [c['case_id'] for c in cases]:
            raise RuntimeError('Source/catalog/harness/selection changed; use a new output directory.')
    else:
        from benchmarks.measurement.engine import dependencies,git_state
        config = load_config()
        if config['openai_model']!='gpt-5-nano':
            raise RuntimeError('This baseline requires the unchanged gpt-5-nano configuration.')
        manifest = {'protocol':PROTOCOL,'namespace':secrets.token_hex(8),'started_at':datetime.now(timezone.utc).isoformat(),
            'fingerprints':fingerprints(),'case_ids':[c['case_id'] for c in cases],
            'model':config['openai_model'],'config':config,'dependencies':dependencies(),'git':git_state(),
            'workers':args.workers,'deadline_seconds':args.deadline,'passes':1,'order_seed':16,
            'canonical_end_to_end_replay':False,'production_prompts_changed':False,
            'reference_date':'2026-10-15','timezone':'Asia/Bangkok','setting':'mixed controlled diagnostics; not full v1 replay'}
        write(manifest_path,manifest)
    if args.report_only:
        print(json.dumps(report(out,cases,manifest),indent=2))
        return
    from src.database.postgres_memory_store import PostgresMemoryStore
    with PostgresMemoryStore() as store:
        store.ensure_schema()
    pending = [c for c in cases if not (out/'cases'/f"{c['case_id']}.json").exists()]
    random.Random(manifest['order_seed']).shuffle(pending)
    start = perf_counter()
    with ThreadPoolExecutor(max_workers=manifest['workers']) as pool:
        futures = {pool.submit(execute,c,manifest,out):c for c in pending}
        for index,future in enumerate(as_completed(futures),1):
            value = future.result()
            write(out/'cases'/f"{value['case_id']}.json",value)
            print(f"{index}/{len(pending)} {value['case_id']} {value['outcome']} turns={len(value.get('turns',[]))} elapsed={perf_counter()-start:.1f}s",flush=True)
    if fingerprints() != manifest['fingerprints']:
        raise RuntimeError('Source/catalog changed during evaluation; results cannot be treated as one baseline.')
    manifest['completed_at'] = datetime.now(timezone.utc).isoformat()
    write(manifest_path,manifest)
    print(json.dumps(report(out,cases,manifest),indent=2),flush=True)


if __name__ == '__main__':
    main()
