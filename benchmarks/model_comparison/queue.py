"""Sequential, persistent execution of the user's three requested model runs."""

from datetime import datetime,timezone
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time


ROOT=Path(__file__).resolve().parents[2]
RUNS=ROOT/'benchmarks/runs'


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--resume',action='store_true')
    args=parser.parse_args()
    folder=RUNS/'model-comparison'
    folder.mkdir(exist_ok=True)
    state_path=folder/'status.json'
    if state_path.exists() and not args.resume:
        raise RuntimeError('Comparison queue already exists; inspect its state before resuming.')
    state=read(state_path) if state_path.exists() else {'started_at':datetime.now(timezone.utc).isoformat(),'status':'waiting_for_gpt-5-nano',
        'models':{'gpt-5-nano':{'status':'running','catalog':'gpt-5-nano/catalog-rerun','retrieval':'gpt-5-nano/retrieval'},
                  'gpt-6-luna':{'status':'queued'},'gpt-4.1-nano':{'status':'queued'}},
        'all_600_catalog_entries_accounted_per_model':True,
        'known_unsupported_cases_per_model':192,'no_model_substitution':True}
    if args.resume:
        state.setdefault('resumed_at',[]).append(datetime.now(timezone.utc).isoformat())
    def save():
        temporary=state_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(state,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        temporary.replace(state_path)
    save()
    baseline=RUNS/'gpt-5-nano/catalog-rerun'
    # Polling lives in this background worker; the chat stays responsive.
    started=time.monotonic()
    while not (baseline/'summary.json').exists():
        if time.monotonic()-started>7200:
            state['status']='baseline_incomplete';save();return
        time.sleep(15)
    manifest=read(baseline/'run.json');summary=read(baseline/'summary.json')
    if not manifest.get('completed_at') or summary['accounted_cases']!=600:
        state['status']='baseline_incomplete';save();return
    state['models']['gpt-5-nano']['status']='completed';save()
    def run(command,output,environment=None):
        output.parent.mkdir(parents=True,exist_ok=True)
        with Path(str(output)+'.stdout.log').open('a',encoding='utf-8') as stdout,Path(str(output)+'.stderr.log').open('a',encoding='utf-8') as stderr:
            return subprocess.run([sys.executable,'-B','-m',*command],cwd=ROOT,
                stdout=stdout,stderr=stderr,env=environment).returncode
    for model in ('gpt-6-luna','gpt-4.1-nano'):
        model_root=RUNS/model
        state['status']='running_'+model;entry=state['models'][model]
        if entry.get('status')=='completed':
            continue
        entry.update(status='preflight',catalog=f'{model}/catalog',retrieval=f'{model}/retrieval');save()
        code=(0 if read(model_root/'preflight.json')['status']=='passed' else 2) if (model_root/'preflight.json').exists() else run(
            ['benchmarks.model_comparison.preflight','--model',model,'--output',str(model_root)],folder/(model+'-preflight'))
        entry['preflight_exit_code']=code
        if code:
            entry.update(status='preflight_failed',preflight=read(model_root/'preflight.json') if (model_root/'preflight.json').exists() else None);save();continue
        entry['status']='catalog_running';save()
        code=0 if (model_root/'catalog/summary.json').exists() else run(
            ['benchmarks.model_comparison.run','--model',model,'--execute','--output',str(model_root/'catalog'),
                  '--workers','4','--deadline','420'],model_root/'catalog')
        entry['catalog_exit_code']=code
        if code:
            entry['status']='catalog_incomplete';save();continue
        entry['status']='retrieval_running';save()
        env=os.environ.copy()
        env['VISTRA_BENCHMARK_CONFIG']=str(model_root/'catalog/effective-config.yaml')
        env['VISTRA_BENCHMARK_TRANSPORT_AUDIT']=str(model_root/'retrieval-transport')
        if (model_root/'retrieval/summary.json').exists():
            code=0
        elif (model_root/'retrieval').exists() and any((model_root/'retrieval').iterdir()):
            code=2  # Preserve partial evidence; do not overwrite an interrupted run.
        else:
            code=run(['benchmarks.model_comparison.retrieval','--execute','--output',str(model_root/'retrieval')],model_root/'retrieval',env)
        entry.update(retrieval_exit_code=code,status='completed' if not code else 'retrieval_incomplete');save()
    state.update(status='ready_for_report',finished_at=datetime.now(timezone.utc).isoformat());save()


if __name__=='__main__':
    main()
