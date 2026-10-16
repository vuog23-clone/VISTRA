"""Index a preserved catalog run and a distinct actual-index retrieval supplement.

This publishes navigation and separate denominators, never a merged quality score.
It does not change the original run's evidence, metrics, or artifact seals.
"""

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[2]


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def rows(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')


def publish(root):
    root = root.resolve()
    if not root.is_relative_to((ROOT/'benchmarks/runs').resolve()):
        raise ValueError('Results must be inside benchmarks/runs.')
    original, retrieval = root/'first-run', root/'retrieval'
    catalog, supplement = read(original/'summary.json'), read(retrieval/'summary.json')
    original_manifest, retrieval_manifest = read(original/'run.json'), read(retrieval/'run.json')
    old_rows, new_rows = rows(original/'results.jsonl'), rows(retrieval/'results.jsonl')
    audit = read(root/'organization.json')
    move = next(m for m in audit['moves'] if m['destination'].endswith('/first-run'))
    for old_path, expected in move['files'].items():
        path = original/Path(old_path).relative_to(move['name'])
        if not path.is_file() or sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f'Original run changed: {path}')
    for name, expected in read(original/'artifact_hashes.json').items():
        if sha256((original/name).read_bytes()).hexdigest() != expected:
            raise ValueError(f'Original artifact seal failed: {name}')
    for name, expected in read(retrieval/'artifact_hashes.json').items():
        if sha256((retrieval/name).read_bytes()).hexdigest() != expected:
            raise ValueError(f'Retrieval artifact seal failed: {name}')
    verification = read(retrieval/'verification.json')
    checks = ('application_unchanged', 'harness_unchanged', 'metadata_unchanged',
              'index_unchanged', 'guide_chunks_unchanged', 'labels_frozen_before_queries')
    if not all(verification[k] for k in checks) or not retrieval_manifest.get('completed_at'):
        raise ValueError('Retrieval execution is incomplete or its consistency checks failed.')
    if original_manifest['model'] != root.name:
        raise ValueError('Model folder name does not match the original model setting.')
    declared = read(retrieval/'cases.json')
    if len(old_rows) != catalog['catalog_cases'] or len(new_rows) != supplement['catalog_cases']:
        raise ValueError('Execution rows do not match declared case counts.')
    if len({r['case_id'] for r in new_rows}) != len(new_rows) or {r['case_id'] for r in new_rows} != {c['case_id'] for c in declared}:
        raise ValueError('Duplicate or missing retrieval cases.')
    for row in new_rows:
        if read(retrieval/'cases'/(row['case_id']+'.json')) != row:
            raise ValueError('Per-case artifact disagrees with execution log.')
    history = root/audit['archive_file']['path']
    if sha256(history.read_bytes()).hexdigest() != audit['archive_file']['sha256']:
        raise ValueError('Development history archive changed.')
    with ZipFile(history) as archive:
        if archive.testzip() is not None:
            raise ValueError('Development history archive is corrupt.')
    tracks = {}
    for track in ('routing','retrieval','grounding','memory','planning','reliability'):
        group = [r for r in old_rows if r['track']==track]
        tracks[track] = {'catalog':len(group), 'executed':sum(r['outcome']=='completed' for r in group),
                         'unsupported':sum(r['outcome']=='unsupported' for r in group),
                         'turn_errors':sum(bool(t.get('error')) for r in group for t in r.get('turns',[]))}
    qrels = read(retrieval/'qrels.json')
    categories = [r for r in new_rows if r['kind']=='category']
    empty_categories = sum(not qrels[r['case_id']] for r in categories)
    category_ids = [(r['case_id'], pid) for r in categories for pid in r['final_ids']]
    guides = [r for r in new_rows if r['kind']=='guide_section']
    misses = [{k:r[k] for k in ('case_id','query','language','city')} for r in guides if not r.get('context_section_hit')]
    before = original_manifest['fingerprints']['application']
    after = retrieval_manifest['source_hashes']
    changed_sources = sorted(k for k in before.keys() | after.keys() if before.get(k)!=after.get(k))
    summary = {'model_configuration':root.name, 'generated_at':datetime.now(timezone.utc).isoformat(),
        'original_catalog':{'path':'first-run','protocol':original_manifest['protocol'],
            'catalog_cases':catalog['catalog_cases'],'executed_cases':catalog['executed_cases'],
            'unsupported_cases':catalog['unsupported_cases'],'turn_errors':catalog['turn_errors'],'by_track':tracks,
            'rerun_in_this_update':False,'byte_for_byte_preserved':True},
        'retrieval_supplement':{'path':'retrieval','protocol':supplement['protocol'],
            'catalog_cases':supplement['catalog_cases'],'executed_cases':supplement['executed_cases'],
            'case_errors':supplement['case_errors'],'llm_generation_used':False,
            'original_retrieval_cases_replaced':False,'by_kind':supplement['by_kind'],
            'category_returned_results':len(category_ids),
            'category_metadata_relevant_results':sum(pid in qrels[cid] for cid,pid in category_ids),
            'correct_empty_cases':sum(bool(r['final']['correct_empty']) for r in categories),
            'guide_context_section_hit_cases':supplement['guide_context_section_hit_cases'],
            'guide_context_section_hit_denominator':len(guides),'guide_context_misses':misses,
            'guide_context_by_language':{lang:{'hit':sum(bool(r.get('context_section_hit')) for r in guides if r['language']==lang),
                'cases':sum(r['language']==lang for r in guides)} for lang in ('en','vi')},
            'card_wrong_city_count':supplement['card_wrong_city_count'],
            'card_wrong_category_count':supplement['card_wrong_category_count'],
            'photo_binding_violations':supplement['photo_binding_violations']},
        'application_files_changed_since_original':changed_sources,
        'application_source_changes':{name:{'first_run_sha256':before.get(name),
            'retrieval_run_sha256':after.get(name)} for name in changed_sources},
        'overall_accuracy_percent':None,'original_catalog_coverage_recomputed_with_supplement':False,
        'notes':['Two different protocols and case sets; results are presented side by side.',
                 'The original 100 fictional retrieval cases remain unsupported.',
                 'Retrieval labels judge metadata and guide sections, not photo pixels or complete answers.',
                 'The original catalog is a historical snapshot, not a fresh current-system rerun.']}
    rerun_path = root/'catalog-rerun'
    rerun = None
    if (rerun_path/'summary.json').exists():
        rerun_manifest = read(rerun_path/'run.json')
        if not rerun_manifest.get('completed_at'):
            raise ValueError('Catalog rerun is not complete.')
        for name, expected in read(rerun_path/'artifact_hashes.json').items():
            if sha256((rerun_path/name).read_bytes()).hexdigest() != expected:
                raise ValueError('Catalog rerun artifact seal failed: '+name)
        rerun = read(rerun_path/'summary.json')
        summary['current_catalog'] = {'path':'catalog-rerun', **rerun}
        summary['notes'].append('The current catalog rerun is reported separately from the preserved historical snapshot.')
    costs=read(root/'costs.json') if (root/'costs.json').exists() else None
    if costs:
        summary['token_costs']=costs
    write(root/'summary.json', summary)
    text = ['# VISTRA — gpt-5-nano benchmark results','',
        'The previous full-catalog result and the fresh actual-index retrieval supplement are collected here. They use different cases and protocols; **there is no merged accuracy score**.',
        '', '## Previous full-catalog run','',
        f"The original **{catalog['catalog_cases']}-case** record is preserved byte for byte: **{catalog['executed_cases']} executed**, **{catalog['unsupported_cases']} unsupported**, and **{catalog['turn_errors']} turn errors**. It has not been overwritten.",
        '', '[Original feedback](first-run/FEEDBACK.md) · [Full report](first-run/REPORT.md) · [Raw results](first-run/results.jsonl)',
        '', '| Track | Catalog | Executed | Unsupported | Turn errors |',
        '| --- | ---: | ---: | ---: | ---: |']
    text += [f"| {track} | {v['catalog']} | {v['executed']} | {v['unsupported']} | {v['turn_errors']} |" for track,v in tracks.items()]
    if rerun:
        text += ['', '## Current catalog rerun','',
            f"A fresh pass accounts for **{rerun['accounted_cases']}/{rerun['catalog_cases']}** original cases: **{rerun['executed_cases']} attempted**, including **{rerun['outcomes'].get('completed',0)} completed** and **{rerun['outcomes'].get('interrupted',0)} externally interrupted**. **{rerun['unsupported_cases']}** remain unsupported; completed turns include **{rerun['turn_errors']} turn errors**. Interrupted attempts were preserved without replacement answers.",
            '', f"Finite checks: {rerun['check_counts']}. These are not overall task-success scores.",
            '', '[Current catalog report](catalog-rerun/REPORT.md) · [Comparison to first run](catalog-rerun/comparison-to-first-run.json) · [Three-model comparison](../model-comparison/REPORT.md)']
    if costs:
        text += ['', '## Token usage and estimated API cost','',
            '| Run | Input tokens | Cached subset | Output tokens | Known-usage Standard-rate estimate |',
            '| --- | ---: | ---: | ---: | ---: |']
        for name,value in costs.items():
            text.append(f"| {name} | {value['reported_input_tokens']:,} | {value['reported_cached_input_tokens']:,} | {value['reported_output_tokens']:,} | ${value['estimated_known_usage_subtotal_usd']:.6f} |")
        text += ['', 'Cached tokens are included in input totals and are charged at their own rate. These are estimates for reported usage, not complete invoices: three first-run calls lack usage, and four current-run attempts were interrupted before complete usage could be saved. Jev, SDK-internal retries and local compute are unpriced.',
            '', '[Cost details and limitations](costs.json) · [Official GPT-5 nano prices](https://developers.openai.com/api/docs/models/gpt-5-nano)']
    text += ['', '## Fresh retrieval supplement','',
        f"Executed **{supplement['executed_cases']}/{supplement['catalog_cases']}** cases with **{supplement['case_errors']} errors**: 50 English and 50 fully accented Vietnamese variants, across five cities. These are the new live-index cases selected for this update, not the original fictional VB1 retrieval cases.",
        '', '| Check | Result |','| --- | ---: |',
        f"| Exact-place Hit@5 | {supplement['by_kind']['known_place']['final']['hit_at_5']:.1%} over 30 cases |",
        f"| Relevant city/category results among returned places | {summary['retrieval_supplement']['category_metadata_relevant_results']}/{len(category_ids)} |",
        f"| Correctly empty category queries | {summary['retrieval_supplement']['correct_empty_cases']}/{empty_categories} |",
        f"| Wrong-city / wrong-category frontend cards | {supplement['card_wrong_city_count']} / {supplement['card_wrong_category_count']} |",
        f"| Photo-to-place binding violations | {supplement['photo_binding_violations']} |",
        f"| Guide-section Recall@20 | {supplement['by_kind']['guide_section']['final']['recall_at_20']:.1%} |",
        f"| Guide-section nDCG@10 | {supplement['by_kind']['guide_section']['final']['ndcg_at_10']:.3f} |",
        f"| Requested guide section reaches generator context | {supplement['guide_context_section_hit_cases']}/{len(guides)} |",
        '', '[Detailed retrieval report](retrieval/REPORT.md) · [Raw results](retrieval/results.jsonl) · [Consistency checks](retrieval/verification.json)',
        '', 'Category precision among all returned places differs from standard Precision@20 and from the macro average that includes two correctly empty queries. See the detailed report for all denominators. Category Recall@20 is capped when a city/category has more than 20 eligible records.',
        '', '## What remains weak','',
        f"Raw SigLIP category precision among its returned top 20 is {supplement['by_kind']['category']['raw']['precision_of_returned_at_20']:.1%}; raw exact-name Hit@5 is {supplement['by_kind']['known_place']['raw']['hit_at_5']:.1%}. Final results benefit from the existing city, category and identity filters. This is metadata relevance, not judged photo appearance.",
        '', f"The requested guide section misses the generator context in {len(misses)}/{len(guides)} cases, despite retrieval containing it. Improving section ranking before the excerpt budget remains the next focused retrieval change.",
        '', 'Context misses: '+', '.join('`'+r['case_id']+'`' for r in misses)+'.',
        '', '## Scope and provenance','',
        f"The LLM configuration remains **{root.name}**. Retrieval uses cached Voyage/BM25 and SigLIP models without calling the LLM; grouping it here describes the system configuration. {len(changed_sources)} application file(s) differ from the original historical snapshot; hashes and the filenames are recorded in [summary.json](summary.json). This is not a matched before/after model comparison.",
        '', 'The original unsupported retrieval row remains unchanged. Do not add this supplement to its numerator and call it 508/600 coverage: these are different scenarios. Human-reviewed overall task success and photo-pixel relevance remain unavailable.',
        '', '[Development history](DEVELOPMENT_HISTORY.md) · [Organization audit](organization.json) · [Grouping verification](verification.json)',
        '', 'The archive retains useful earlier component runs and failed versions used in comparisons. Redundant pilots, smoke checks and interrupted retrieval launches were removed; their original file hashes and cleanup reasons are retained in the audit.']
    (root/'REPORT.md').write_text('\n'.join(text)+'\n', encoding='utf-8')
    (root/'README.md').write_text('# gpt-5-nano results\n\nStart with [REPORT.md](REPORT.md).\n\n'
        '- [first-run/](first-run/FEEDBACK.md): preserved original 600-case catalog evaluation.\n'
        + ('- [catalog-rerun/](catalog-rerun/REPORT.md): fresh rerun of the original catalog.\n' if rerun else '') +
        '- [retrieval/](retrieval/REPORT.md): fresh 100-case actual-index retrieval supplement.\n'
        '- [summary.json](summary.json): separate machine-readable metrics.\n'
        '- [Development history](DEVELOPMENT_HISTORY.md): earlier diagnostics in one verified archive.\n'
        '- [organization.json](organization.json): moves, removals and original file hashes.\n\n'
        'The two result sets have separate denominators. This folder does not claim an overall model accuracy.\n', encoding='utf-8')
    write(root/'verification.json', {'original_files_byte_for_byte_preserved':len(move['files']),
        'original_artifact_seals_valid':True,'retrieval_artifact_seals_valid':True,
        'retrieval_consistency_checks_valid':True,'retrieval_unique_cases':len(new_rows),
        'retrieval_per_case_files_match_log':True,'history_archive_valid':True,
        'different_cohorts_kept_separate':True,'original_case_results_overwritten':False})
    write(root/'artifact_hashes.json', {p.name:sha256(p.read_bytes()).hexdigest() for p in root.iterdir()
        if p.is_file() and p.name not in {'artifact_hashes.json','retrieval.stdout.log','retrieval.stderr.log'}})
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, default=ROOT/'benchmarks/runs/gpt-5-nano')
    args = parser.parse_args()
    result = publish(args.run)
    print(f"Indexed {result['original_catalog']['catalog_cases']} original cases and "
          f"{result['retrieval_supplement']['executed_cases']} separate retrieval cases; original evidence verified.")


if __name__ == '__main__':
    main()
