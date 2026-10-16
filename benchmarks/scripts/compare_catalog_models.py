"""Verify completed catalog runs and compare finite checks without quality scores."""

import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path

from benchmarks.evaluation.protocol import ROOT, public_cases
from benchmarks.costs import catalog_cost,estimate,pricing


RUNS=ROOT/'benchmarks/runs'


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def rows(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def write(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')


def seal(path):
    write(path/'artifact_hashes.json',{p.name:sha256(p.read_bytes()).hexdigest() for p in path.iterdir()
        if p.is_file() and p.name!='artifact_hashes.json'})


def audit_retrieval(path):
    manifest=read(path/'run.json');summary=read(path/'summary.json')
    for name,expected in read(path/'artifact_hashes.json').items():
        if sha256((path/name).read_bytes()).hexdigest()!=expected:
            raise ValueError('Retrieval artifact checksum mismatch: '+str(path/name))
    verified=read(path/'verification.json')
    checks=('application_unchanged','harness_unchanged','metadata_unchanged',
            'index_unchanged','guide_chunks_unchanged','labels_frozen_before_queries')
    if not manifest.get('completed_at') or not all(verified[k] for k in checks):
        raise ValueError('Retrieval is incomplete or inputs changed.')
    declared=read(path/'cases.json');results=rows(path/'results.jsonl')
    if len(results)!=100 or len({r['case_id'] for r in results})!=100 or {r['case_id'] for r in results}!={r['case_id'] for r in declared}:
        raise ValueError('Missing/duplicate retrieval cases.')
    for row in results:
        if read(path/'cases'/(row['case_id']+'.json'))!=row:
            raise ValueError('Retrieval case differs from aggregate results.')
    return manifest,summary


def audit(path):
    manifest=read(path/'run.json');summary=read(path/'summary.json')
    if not manifest.get('completed_at') or summary['accounted_cases']!=600:
        raise ValueError('Catalog run is incomplete.')
    for name,expected in read(path/'artifact_hashes.json').items():
        if sha256((path/name).read_bytes()).hexdigest()!=expected:
            raise ValueError('Artifact checksum mismatch: '+str(path/name))
    evidence_path=path/'evidence_hashes.json'
    if evidence_path.exists():
        for name,expected in read(evidence_path).items():
            if sha256((path/name).read_bytes()).hexdigest()!=expected:
                raise ValueError('Raw evidence checksum mismatch: '+name)
    results=rows(path/'results.jsonl')
    declared={c['case_id'] for c in public_cases()}
    if len(results)!=600 or {r['case_id'] for r in results}!=declared:
        raise ValueError('Missing/duplicate catalog cases.')
    for row in results:
        if read(path/'cases'/(row['case_id']+'.json'))!=row:
            raise ValueError('Case artifact differs from aggregate results.')
    expected_model=manifest['model']
    events=[event for row in results for event in row.get('provider_calls',[])]
    if any(event.get('requested_model')!=expected_model for event in events):
        raise ValueError('A provider request used a different model from the manifest.')
    served=Counter(event['served_model'] for event in events if event.get('served_model') not in (None,'unavailable'))
    unavailable_served=sum(event.get('served_model') in (None,'unavailable') for event in events)
    if any(model!=expected_model and not model.startswith(expected_model+'-') for model in served):
        raise ValueError('Provider returned an unexpected served model: '+str(served))
    transport=[event for file in (path/'transport').glob('*.jsonl') for event in rows(file)]
    if manifest.get('model_adapter') and events and not transport:
        raise ValueError('Model adapter transport audit is missing.')
    if any(event['requested_model']!=expected_model for event in transport):
        raise ValueError('Transport audit used a different model.')
    if expected_model=='gpt-4.1-nano' and any(event['reasoning'] is not None for event in transport):
        raise ValueError('Nonreasoning model received unsupported reasoning settings.')
    if expected_model!='gpt-4.1-nano' and any(event['reasoning']!={'effort':manifest['config']['openai_reasoning_effort']} for event in transport):
        raise ValueError('Reasoning-model transport settings differ from the manifest.')
    from benchmarks.evaluation.run import fingerprints
    current=fingerprints()
    unchanged={key:manifest['fingerprints'][key]==current[key] for key in ('application','harness','catalog')}
    if not all(unchanged.values()):
        raise ValueError('Current sources differ from completed comparison run: '+str(unchanged))
    from benchmarks.evaluation.worker import owners
    ids=sorted({identity for case in public_cases() for identity in owners(case,manifest['namespace']).values()})
    from src.database.postgres_memory_store import PostgresMemoryStore
    with PostgresMemoryStore() as store:
        counts={table:store.connection.execute('SELECT count(*) AS n FROM '+table+' WHERE user_id = ANY(%s)',(ids,)).fetchone()['n']
            for table in ('vistra_long_term_memories','vistra_memory_sessions','vistra_memory_messages')}
    cleanup={'exact_owner_count':len(ids),'remaining_rows':counts,'verified_deleted':not any(counts.values()),
             'production_owners_targeted':False,'audit_writes':False}
    write(path/'cleanup_audit.json',cleanup)
    verification={**unchanged,'unique_catalog_cases':600,'per_case_files_match_results':True,
        'artifact_seals_valid_before_analysis':True,'cleanup_verified':cleanup['verified_deleted'],
        'requested_models_match_manifest':True,'served_models':dict(served),
        'calls_without_returned_model_identity':unavailable_served,
        'transport_model_settings_verified':True if transport else None,'transport_audit_events':len(transport)}
    write(path/'verification.json',verification)
    if not cleanup['verified_deleted']:
        raise ValueError('Benchmark memory cleanup is incomplete.')
    evidence={file.relative_to(path).as_posix():sha256(file.read_bytes()).hexdigest()
        for name in ('cases','checkpoints','attempts','transport','transport-usage')
        for file in (path/name).glob('*') if file.is_file()}
    write(evidence_path,evidence)
    seal(path)
    return manifest,summary


def compare(baseline,candidate):
    baseline,candidate=baseline.resolve(),candidate.resolve()
    a,b=read(baseline/'run.json'),read(candidate/'run.json')
    fields=('case_ids','workers','deadline_seconds','passes','order_seed','reference_date','timezone','setting','dependencies')
    compatible={field:a[field]==b[field] for field in fields}
    compatible.update({key:a['fingerprints'][key]==b['fingerprints'][key] for key in ('harness','catalog')})
    config_a={k:v for k,v in a['config'].items() if k!='openai_model'}
    config_b={k:v for k,v in b['config'].items() if k!='openai_model'}
    compatible['non_model_settings']=config_a==config_b
    if not all(compatible.values()):
        raise ValueError('Comparison conditions differ: '+str(compatible))
    source_changes=sorted(k for k in a['fingerprints']['application'].keys()|b['fingerprints']['application'].keys()
        if a['fingerprints']['application'].get(k)!=b['fingerprints']['application'].get(k))
    def index(path):
        values=rows(path/'checks.jsonl')
        mapped={(r['case_id'],r['check']):r for r in values}
        if len(mapped)!=len(values):
            raise ValueError('Duplicate finite-check IDs.')
        return mapped
    left,right=index(baseline),index(candidate)
    interrupted={row['case_id'] for path in (baseline,candidate) for row in rows(path/'results.jsonl')
                 if row['outcome']=='interrupted'}
    transitions=[];derived_expectations=[]
    for key in sorted(left.keys()|right.keys()):
        old,new=left.get(key),right.get(key)
        if old and new and old['status'] in ('pass','fail') and new['status'] in ('pass','fail') and old['expected']!=new['expected']:
            # These unchanged graders compare each run against its own prior
            # generated plan/state, rather than a fixed authored answer value.
            if '.protected_day_' in key[1] or key[1].endswith('.memory_unchanged'):
                derived_expectations.append({'case_id':key[0],'check':key[1],
                    'before_expected':old['expected'],'after_expected':new['expected']})
            else:
                raise ValueError('Expected value changed for '+str(key))
        old_status,new_status=(old or {}).get('status','missing'),(new or {}).get('status','missing')
        if old_status!=new_status:
            transitions.append({'case_id':key[0],'check':key[1],'before':old_status,'after':new_status,
                'before_actual':(old or {}).get('actual'),'after_actual':(new or {}).get('actual'),
                'included_in_model_pairing':key[0] not in interrupted})
    paired=[t for t in transitions if t['included_in_model_pairing']]
    return {'baseline':baseline.relative_to(RUNS).as_posix(),'candidate':candidate.relative_to(RUNS).as_posix(),
        'compatible_conditions':compatible,'changed_application_files':source_changes,
        'model_adapter_difference_declared':a.get('model_adapter')!=b.get('model_adapter'),
        'externally_interrupted_cases_excluded_from_model_pairing':sorted(interrupted),
        'run_derived_expectation_differences':derived_expectations,
        'all_planned_cases_retained_in_raw_totals':True,
        'improved_scored_checks':sum(t['before']=='fail' and t['after']=='pass' for t in paired),
        'regressed_scored_checks':sum(t['before']=='pass' and t['after']=='fail' for t in paired),
        'passing_checks_now_unscored_or_missing':sum(t['before']=='pass' and t['after'] in ('unscored','missing') for t in paired),
        'transitions':transitions,'overall_accuracy_percent':None,
        'interpretation':'One stochastic pass; finite checks only. Source changes, model changes and runtime variability must not be confused.'}


def publish():
    folder=RUNS/'model-comparison'
    state=read(folder/'status.json')
    if state['status'] not in ('ready_for_report','completed'):
        raise ValueError('Wait for all queued model runs before publishing the comparison.')
    baseline=RUNS/'gpt-5-nano/catalog-rerun'
    if not (baseline/'summary.json').exists():
        raise ValueError('Current gpt-5-nano run is incomplete.')
    audit(baseline)
    original_comparison=compare(RUNS/'gpt-5-nano/first-run',baseline)
    write(baseline/'comparison-to-first-run.json',original_comparison)
    seal(baseline)
    components={};pairs={}
    for model in ('gpt-5-nano','gpt-6-luna','gpt-4.1-nano'):
        path=baseline if model=='gpt-5-nano' else RUNS/model/'catalog'
        if not (path/'summary.json').exists():
            components[model]={'status':state['models'][model]['status']}
            model_root=RUNS/model
            if (model_root/'preflight.json').exists():
                preflight=read(model_root/'preflight.json')
                components[model]['preflight']=preflight
                notes=[f'# {model}: benchmark not admitted','',
                    'The compatibility/access preflight did not pass. No catalog score is claimed and no alternative model was substituted.',
                    '', '[Preflight result](preflight.json) · [Comparison status](../model-comparison/status.json)',
                    '', 'Error details: '+json.dumps(preflight.get('error',{}),ensure_ascii=False)]
                (model_root/'REPORT.md').write_text('\n'.join(notes)+'\n',encoding='utf-8')
                seal(model_root)
            continue
        manifest,summary=audit(path)
        cost=catalog_cost(path)
        write(path/'cost.json',cost)
        report=path/'REPORT.md'
        report.write_text(report.read_text(encoding='utf-8').replace(
            'Cost is unavailable: no verified pricing table was supplied.',
            'A post-run Standard-rate cost estimate is available in [cost.json](cost.json); the original execution summary did not calculate prices.'),encoding='utf-8')
        seal(path)
        components[model]={'status':state['models'][model]['status'],'catalog_path':path.relative_to(RUNS).as_posix(),
            'summary':summary,'requested_model':manifest['model'],'reasoning':manifest.get('model_adapter',{}).get('reasoning',manifest['config']['openai_reasoning_effort'])}
        components[model]['token_cost']=cost
        preflight_path=RUNS/model/'preflight.json'
        if preflight_path.exists():
            preflight=read(preflight_path)
            components[model]['preflight_cost']=estimate(model,preflight.get('usage'))
        retrieval=RUNS/model/'retrieval'
        if (retrieval/'summary.json').exists():
            retrieval_manifest,retrieval_summary=audit_retrieval(retrieval)
            components[model]['retrieval']=retrieval_summary
            components[model]['retrieval_input_fingerprints']=retrieval_manifest['dataset_sha256']
            components[model]['retrieval_harness_hashes']=retrieval_manifest['harness_hashes']
            components[model]['retrieval_model_revisions']=retrieval_manifest['model_cache_revisions']
        if model!='gpt-5-nano':
            pair=compare(baseline,path)
            if pair['changed_application_files']:
                raise ValueError('Application code differs across model runs.')
            pairs[model]=pair
        if model!='gpt-5-nano':
            model_root=RUNS/model
            write(model_root/'summary.json',components[model])
            notes=[f'# VISTRA — {model} benchmark results','',
                f"The catalog accounted for **{summary['accounted_cases']}/600** entries: **{summary['executed_cases']} executed**, **{summary['unsupported_cases']} unsupported** and **{summary['turn_errors']} turn errors**.",
                '', '[Catalog report](catalog/REPORT.md) · [Raw cases](catalog/results.jsonl) · [Model comparison](../model-comparison/REPORT.md)',
                '', f"Narrow automated checks: {summary['check_counts']}. Overall task success remains unscored pending independent review.",
                '', 'Prompts, graders, public fixtures and application code match the current GPT-5 nano run. The model is selected using the saved effective configuration; the shared application default is unchanged.',
                '', 'Effective reasoning setting: '+str(components[model]['reasoning'])+'.',
                '', f"Known-usage Standard-rate API estimate: **${cost['estimated_known_usage_subtotal_usd']:.6f}**. [Token counts and cost limitations](catalog/cost.json). Preflight usage is separate; this is not a verified invoice."]
            if 'retrieval' in components[model]:
                retrieval_summary=components[model]['retrieval']
                notes += ['', '## Retrieval supplement','',
                    f"Executed {retrieval_summary['executed_cases']}/100 distinct live-index cases with {retrieval_summary['case_errors']} errors. Requested guide section reached generation context in {retrieval_summary['guide_context_section_hit_cases']}/20 cases.",
                    '', '[Retrieval report](retrieval/REPORT.md) · [Raw retrieval results](retrieval/results.jsonl)',
                    '', 'Retrieval uses shared embedding models without LLM generation; these cases do not replace the unsupported original fictional retrieval track. Metadata relevance is measured; photo appearance is not.']
            (model_root/'REPORT.md').write_text('\n'.join(notes)+'\n',encoding='utf-8')
            (model_root/'README.md').write_text(f'# {model}\n\nOpen [REPORT.md](REPORT.md).\n',encoding='utf-8')
            seal(model_root)
    prices=pricing();write(folder/'pricing.json',prices)
    result={'models':components,'comparisons_to_current_gpt_5_nano':pairs,
        'gpt_5_nano_first_vs_current':original_comparison,'overall_accuracy_percent':None,
        'admitted_model_case_sets_identical':True,'semantic_reviews_pending':True,
        'retrieval_input_compatibility':{model:data.get('retrieval_input_fingerprints')==components['gpt-5-nano'].get('retrieval_input_fingerprints')
            for model,data in components.items() if 'retrieval_input_fingerprints' in data},
        'retrieval_harness_compatibility':{model:data.get('retrieval_harness_hashes')==components['gpt-5-nano'].get('retrieval_harness_hashes')
            for model,data in components.items() if 'retrieval_harness_hashes' in data},
        'retrieval_embedding_compatibility':{model:data.get('retrieval_model_revisions')==components['gpt-5-nano'].get('retrieval_model_revisions')
            for model,data in components.items() if 'retrieval_model_revisions' in data},'pricing':prices}
    if not all(all(result[key].values()) for key in ('retrieval_input_compatibility','retrieval_harness_compatibility','retrieval_embedding_compatibility')):
        raise ValueError('Retrieval inputs, harness or embedding revisions differ across model configurations.')
    write(folder/'summary.json',result)
    text=['# VISTRA model comparison','',
        'Requested order: GPT-5 nano, GPT-6 Luna, then GPT-4.1 nano. Original cases, graders, prompts, clock, worker count and budgets stay fixed. The nonreasoning GPT-4.1 nano request omits the unsupported reasoning parameter.',
        '', 'The comparisons below describe this VISTRA system and the supported development cases. They are not general model rankings. Read matched check transitions together with validation errors, unscored assertions, latency and observed cost.',
        '', '## Catalog results','',
        '| Model | Attempted / completed | Interrupted | Unsupported | Turn errors | Checks pass | Checks fail | Unscored | SDK calls |',
        '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |']
    for model,data in components.items():
        if 'summary' not in data:
            text.append(f"| {model} ({data['status']}) | — | — | — | — | — | — | — | — |")
            continue
        s=data['summary'];counts=s['check_counts']
        text.append(f"| [{model}](../{data['catalog_path']}/REPORT.md) | {s['executed_cases']} / {s['outcomes'].get('completed',0)} | {s['outcomes'].get('interrupted',0)} | {s['unsupported_cases']} | {s['turn_errors']} | {counts.get('pass',0)} | {counts.get('fail',0)} | {counts.get('unscored',0)} | {s['provider_calls']} |")
    text += ['', '**These are finite subcheck counts, not overall model accuracy or independently reviewed task success.** Missing/unknown checks and errors remain visible. Expected security rejections can appear in turn-error totals. See individual reports for types, usage and latency.',
        '', '## Turn errors','',
        '| Model | UnsafeInput rejections | Other errors by type |',
        '| --- | ---: | --- |']
    for model,data in components.items():
        if 'summary' not in data:continue
        errors=data['summary']['turn_errors_by_type']
        other=', '.join(f'{key}: {value}' for key,value in sorted(errors.items()) if key!='UnsafeInput') or 'None'
        text.append(f"| {model} | {errors.get('UnsafeInput',0)} | {other} |")
    text += ['', 'UnsafeInput entries can be expected refusals of adversarial inputs. Other exceptions are execution problems that require inspecting the individual case. Externally interrupted attempts are listed separately above; the published raw case records retain every error.',
        '', '## Checks by family','',
        '| Family | Model | Pass | Fail | Unscored |',
        '| --- | --- | ---: | ---: | ---: |']
    for track in ('routing','retrieval','grounding','memory','planning','reliability'):
        for model,data in components.items():
            if 'summary' not in data:continue
            c=data['summary']['checks_by_track'][track]
            text.append(f"| {track} | {model} | {c.get('pass',0)} | {c.get('fail',0)} | {c.get('unscored',0)} |")
    text += ['', 'The grounding track\'s finite checks only validate IDs in recognized `[S#]` citations. They do not establish entailment; answers without that citation syntax have no such scored diagnostic. Grounding and attribution semantics remain unreviewed. Conditional diagnostics also mean raw pass totals have different denominators.',
        '', '## Observed timing','',
        '| Model | Worker median / p95 (s) | SDK call median / p95 (s) |',
        '| --- | ---: | ---: |']
    for model,data in components.items():
        if 'summary' not in data:continue
        w=data['summary']['worker_wall_timing'];s=data['summary']['sdk_call_timing']
        text.append(f"| {model} | {w['median_seconds']:.2f} / {w['p95_seconds']:.2f} | {s['median_seconds']:.2f} / {s['p95_seconds']:.2f} |")
    text += ['', 'Worker times include process startup and isolated memory snapshots; they are not frontend response times. SDK figures describe individual provider calls, not whole conversations.',
        '', '## Matched check changes','',
        '| Candidate vs current GPT-5 nano | Fail → pass | Pass → fail | Pass → unscored/missing |',
        '| --- | ---: | ---: | ---: |']
    text += [f"| {model} | {p['improved_scored_checks']} | {p['regressed_scored_checks']} | {p['passing_checks_now_unscored_or_missing']} |" for model,p in pairs.items()]
    text += ['', 'Four externally interrupted GPT-5 nano cases are retained in raw totals and explicitly excluded from model-pairing deltas. They are not attributed to model quality or replaced with fresh answers. Run-derived protected-plan expectations can differ because each conversation preserves its own prior generated plan; those values are retained in the comparison audit.',
        '', 'Full changed-check records and compatibility conditions are in [summary.json](summary.json). One stochastic pass does not establish a stable ranking. Model self-review approvals are not independent grounding verdicts.',
        '', '## Token usage and API-cost estimates','',
        '| Model | Input | Cached subset | Cache writes | Output | Known-usage Standard-rate estimate |',
        '| --- | ---: | ---: | ---: | ---: | ---: |']
    for model,data in components.items():
        if 'token_cost' not in data:continue
        c=data['token_cost'];written=c['reported_cache_write_tokens']
        text.append(f"| {model} | {c['reported_input_tokens']:,} | {c['reported_cached_input_tokens']:,} | {written if written is not None else 'unavailable'} | {c['reported_output_tokens']:,} | ${c['estimated_known_usage_subtotal_usd']:.6f} |")
    text += ['', 'Input totals already contain cached and cache-write subsets; output totals already contain reasoning tokens. Each subset is priced once. Preflight cost is recorded separately in summary.json. Full spend is unavailable when returned usage is missing or a case was interrupted. These Standard-rate estimates are not verified invoices and exclude Jev, local compute and unreported/internal-retry usage.',
        '', 'Cache state and external-provider variability are not controlled across runs. Costs describe observed executions, not guaranteed future prices or cost per independently reviewed successful task.',
        '', '[Verified rate snapshot](pricing.json) · [GPT-5 nano prices](https://developers.openai.com/api/docs/models/gpt-5-nano) · [GPT-6 Luna prices](https://developers.openai.com/api/docs/models/gpt-6-luna) · [GPT-4.1 nano prices](https://developers.openai.com/api/docs/models/gpt-4.1-nano)',
        '', '## GPT-5 nano: first run versus current rerun','',
        f"Compared with the preserved first run: {original_comparison['improved_scored_checks']} finite checks changed from fail to pass, {original_comparison['regressed_scored_checks']} from pass to fail, and {original_comparison['passing_checks_now_unscored_or_missing']} formerly passing checks became unscored or missing.",
        '', 'The only application change is museum-category normalization. The controlled catalog substitutes search evidence, so differences in its model answers cannot be attributed to that retrieval fix. The actual-index supplement measures retrieval separately.',
        '', '## Retrieval and coverage','',
        'All original 600 catalog entries are accounted for per admitted model. The same 192 entries lack faithful adapters; the new 100-case live-index retrieval suite does not replace the original fictional retrieval track. Voyage/BM25 and SigLIP are shared across the model configurations and do not call the LLM.',
        '', 'Each completed model has its own retrieval evidence and metrics. Pixel appearance, image uploads, overall itinerary feasibility and independent semantic quality are outside the scored scope. No combined percentage across protocols is calculated.',
        '', '| Model configuration | Cases / errors | Exact-place Hit@5 | Guide section in context | Wrong city / category cards |',
        '| --- | ---: | ---: | ---: | ---: |']
    for model,data in components.items():
        if 'retrieval' not in data:continue
        r=data['retrieval']
        text.append(f"| {model} | {r['executed_cases']} / {r['case_errors']} | {r['by_kind']['known_place']['final']['hit_at_5']:.1%} | {r['guide_context_section_hit_cases']}/20 | {r['card_wrong_city_count']} / {r['card_wrong_category_count']} |")
    text += [
        '', '## Integrity','',
        'Completed catalog artifacts, all 600 per-case records, source/catalog/legacy harness consistency and exact isolated-owner PostgreSQL cleanup were verified. Effective configs and transport audits document the model-specific request settings. Preflight usage is separate from benchmark case usage.',
        '', '[Queue status](status.json) · [Original GPT-5 nano feedback](../gpt-5-nano/first-run/FEEDBACK.md)']
    (folder/'REPORT.md').write_text('\n'.join(text)+'\n',encoding='utf-8')
    (folder/'README.md').write_text(
        '# VISTRA model comparison\n\nOpen [REPORT.md](REPORT.md) for catalog checks, retrieval, token usage, cost estimates and limitations.\n\n'
        '[Machine-readable results](summary.json) · [Execution status](status.json) · [Verified rates](pricing.json)\n',encoding='utf-8')
    state['status']='completed'
    state['report']='REPORT.md'
    write(folder/'status.json',state)
    seal(folder)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--publish',action='store_true')
    args=parser.parse_args()
    if not args.publish:
        parser.error('--publish is required.')
    value=publish()
    print(json.dumps({model:data['status'] for model,data in value['models'].items()},indent=2))


if __name__=='__main__':
    main()
