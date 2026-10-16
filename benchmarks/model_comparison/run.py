"""Same catalog/graders/worker with isolated model configuration and SDK options."""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import random
import secrets
import subprocess
import sys
from time import perf_counter
from types import SimpleNamespace

from benchmarks.evaluation import run as original
from benchmarks.evaluation.protocol import ROOT, PROTOCOL, public_cases, capability
from benchmarks.model_comparison.runtime import MODELS, activate


def main():
    import yaml
    from benchmarks.measurement.engine import dependencies, git_state, hashes
    from src.utils.utils import load_config
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', required=True, choices=MODELS)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--workers', type=int, choices=(1,2,4), default=4)
    parser.add_argument('--deadline', type=int, default=420)
    args = parser.parse_args()
    if not args.execute:
        parser.error('--execute is required for real model calls.')
    out = args.output.resolve()
    if not out.is_relative_to((ROOT/'benchmarks/runs').resolve()):
        parser.error('Output must be inside benchmarks/runs.')
    out.mkdir(parents=True, exist_ok=True)
    config_path = out/'effective-config.yaml'
    if not config_path.exists():
        config = load_config()
        config['openai_model'] = args.model
        config_path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding='utf-8')
    os.environ['VISTRA_BENCHMARK_CONFIG'] = str(config_path)
    os.environ['VISTRA_BENCHMARK_TRANSPORT_AUDIT'] = str(out/'transport')
    config = activate()
    if config['openai_model'] != args.model:
        raise ValueError('Model selection disagrees with saved effective configuration.')
    cases = public_cases()
    def fingerprint():
        return {**original.fingerprints(),'model_adapter':hashes(Path(__file__).parent.glob('*.py')),
                'effective_config':hashes([config_path])}
    manifest_path = out/'run.json'
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        if manifest['fingerprints'] != fingerprint() or manifest['model'] != args.model:
            raise RuntimeError('Sources/configuration changed; use a new output directory.')
    else:
        manifest = {'protocol':PROTOCOL,'namespace':secrets.token_hex(8),
            'started_at':datetime.now(timezone.utc).isoformat(),'fingerprints':fingerprint(),
            'case_ids':[c['case_id'] for c in cases],'model':args.model,'config':config,
            'dependencies':dependencies(),'git':git_state(),'workers':args.workers,
            'deadline_seconds':args.deadline,'passes':1,'order_seed':16,
            'canonical_end_to_end_replay':False,'production_prompts_changed':False,
            'reference_date':'2026-10-15','timezone':'Asia/Bangkok',
            'setting':'mixed controlled diagnostics; not full v1 replay',
            'model_adapter':{'configuration':'isolated effective-config.yaml; shared defaults unchanged',
                'worker':'original worker via startup adapter','reasoning':
                    'omitted: model has no reasoning parameter' if args.model=='gpt-4.1-nano' else config['openai_reasoning_effort'],
                'official_sources':['https://developers.openai.com/api/docs/models/'+args.model]}}
        original.write(manifest_path, manifest)
    # The original executor still owns deadlines, interrupted-attempt handling,
    # checkpoints and cleanup. Only its child startup module changes.
    subprocess_run = subprocess.run
    def launch(command, **options):
        command = [str(item) for item in command]
        if command[-1] != 'benchmarks.evaluation.worker':
            raise ValueError('Unexpected catalog worker command.')
        return subprocess_run([*command[:-1], 'benchmarks.model_comparison.worker'], **options)
    original.subprocess = SimpleNamespace(run=launch, TimeoutExpired=subprocess.TimeoutExpired)
    for name in ('cases','checkpoints','attempts'):
        (out/name).mkdir(exist_ok=True)
    from src.database.postgres_memory_store import PostgresMemoryStore
    with PostgresMemoryStore() as store:
        store.ensure_schema()
    pending = [c for c in cases if not (out/'cases'/f"{c['case_id']}.json").exists()]
    random.Random(16).shuffle(pending)
    start = perf_counter()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(original.execute,c,manifest,out) for c in pending]
        for index,future in enumerate(as_completed(futures),1):
            value = future.result()
            original.write(out/'cases'/f"{value['case_id']}.json",value)
            print(f"{index}/{len(pending)} {value['case_id']} {value['outcome']} turns={len(value.get('turns',[]))} elapsed={perf_counter()-start:.1f}s",flush=True)
    if fingerprint() != manifest['fingerprints']:
        raise RuntimeError('Inputs changed during evaluation.')
    manifest['completed_at'] = datetime.now(timezone.utc).isoformat()
    original.write(manifest_path,manifest)
    summary = original.report(out,cases,manifest)
    report = out/'REPORT.md'
    report.write_text(report.read_text(encoding='utf-8').replace('VISTRA gpt-5-nano:',f'VISTRA {args.model}:',1),encoding='utf-8')
    original.write(out/'artifact_hashes.json',{p.name:sha256(p.read_bytes()).hexdigest() for p in out.iterdir()
        if p.is_file() and p.name!='artifact_hashes.json'})
    print(json.dumps(summary,indent=2),flush=True)


if __name__ == '__main__':
    main()
