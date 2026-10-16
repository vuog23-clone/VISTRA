"""Fresh-process execution, provenance, immutable outputs and strict comparison."""

from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import platform
import random
import subprocess
import sys
from time import perf_counter

from benchmarks.measurement.graders import check_names, grade
from benchmarks.measurement.metrics import summarize, validate_pricing


ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
PROTOCOL = 'vistra-measurement-1'


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False)+'\n', encoding='utf-8')


def hashes(paths):
    return {path.relative_to(ROOT).as_posix(): sha256(path.read_bytes()).hexdigest() for path in sorted(paths)}


def sources():
    return hashes([ROOT/'main.py', ROOT/'config.yaml', *sorted((ROOT/'src').rglob('*.py')),
                   *sorted((ROOT/'api').glob('*.py')), *sorted((ROOT/'frontend').glob('*.py'))])


def harness():
    return hashes([*HERE.glob('*.py'), *HERE.glob('*.json'), ROOT/'benchmarks/scripts/measure_improvements.py'])


def dependencies():
    packages = {}
    for name in ('openai','pydantic','httpx','psycopg','PyYAML','numpy','qdrant-client'):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = None
    return packages


def configured_static(config):
    from src.utils.utils import resolve_path
    return {key:sha256(resolve_path(config[key]).read_bytes()).hexdigest()
            for key in ('country_scopes_path','location_mappings_path') if config.get(key)}


def git_state():
    def git(*args):
        return subprocess.run(['git', *args], cwd=ROOT, capture_output=True, timeout=10, check=True).stdout
    try:
        return {'commit': git('rev-parse','HEAD').decode().strip(),
                'dirty': bool(git('status','--porcelain')),
                'tracked_diff_sha256': sha256(git('diff','--no-ext-diff','--binary','HEAD')).hexdigest()}
    except (OSError, subprocess.SubprocessError):
        return {'commit': None, 'dirty': None, 'tracked_diff_sha256': None}


def suite_cases(profile, families=None):
    suite, fixtures = read(HERE/'suite.json'), read(HERE/'fixtures.json')
    seen, selected = set(), []
    allowed = {'offline': {'route','schedule','photos','contract','memory','review_failure'},
               'controlled': {'interpreted_route','answer','memory'}}
    for family in suite['families']:
        if family['id'] in seen or not family['expected'] or set(family['messages']) != {'en','vi'}:
            raise ValueError('Suite needs unique families, explicit expectations and both languages.')
        seen.add(family['id'])
        if family['adapter'] not in allowed[family['profile']]:
            raise ValueError('An adapter is not allowed in this profile.')
        if family['profile'] != profile or families is not None and family['id'] not in families:
            continue
        for language, message in family['messages'].items():
            if not isinstance(message,str) or not message.strip():
                raise ValueError('A case has no message.')
            selected.append({**{key: value for key,value in family.items() if key != 'messages'},
                             'case_id': family['id']+'.'+language, 'language': language, 'message': message})
    if not selected or families is not None and set(families)-{case['id'] for case in selected}:
        raise ValueError('Unknown/empty case selection for this profile.')
    for case in selected:
        if not check_names(case):
            raise ValueError('A case has no applicable checks.')
    return suite, fixtures, selected


def output_directory(output):
    output = Path(output).resolve()
    if not output.is_relative_to((ROOT/'benchmarks/runs').resolve()):
        raise ValueError('Output must be inside benchmarks/runs/.')
    if output.exists() and any(output.iterdir()):
        raise ValueError('Use a new empty output directory; measurement runs are immutable.')
    output.mkdir(parents=True, exist_ok=True)
    return output


def invoke(public_case, fixtures, reference_date, deadline):
    import os
    environment = dict(os.environ, PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1')
    start = perf_counter()
    process = subprocess.Popen([sys.executable, '-B', '-m', 'benchmarks.measurement.worker'], cwd=ROOT,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', env=environment)
    try:
        stdout, stderr = process.communicate(json.dumps({'case': public_case, 'fixtures': fixtures,
            'reference_date': reference_date}, ensure_ascii=False), timeout=deadline)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate(timeout=10)
        return {'outcome': 'timeout', 'error': {'type': 'WorkerDeadline', 'message': 'Worker terminated at the case deadline.'},
                'provider_calls': [], 'usage_lost_on_timeout': public_case['profile'] == 'controlled'}, perf_counter()-start, True
    except BaseException:
        process.kill()
        process.communicate(timeout=10)
        raise
    wall = perf_counter()-start
    try:
        if process.returncode:
            raise ValueError('Worker exited without a valid result.')
        observation = json.loads(stdout)
        if observation.get('outcome') not in {'ok','system_error','harness_error'} or not isinstance(observation.get('provider_calls'),list):
            raise ValueError('Malformed worker envelope.')
        return observation, wall, observation['outcome'] != 'harness_error'
    except (ValueError, TypeError):
        from src.utils.redaction import CredentialRedactor
        return {'outcome': 'harness_error', 'error': {'type':'WorkerProtocol',
            'message': CredentialRedactor().text(stderr[-1500:])}, 'provider_calls': []}, wall, False


def run(output, *, profile='offline', live=False, repeats=3, seed=17, deadline=300, families=None, pricing_path=None):
    from src.utils.utils import load_config
    from benchmarks.measurement.reporting import report
    if profile not in {'offline','controlled'} or profile == 'controlled' and not live:
        raise ValueError('Controlled runs require --live. Offline runs make no network calls.')
    if type(repeats) is not int or not 1 <= repeats <= 10 or type(deadline) not in (int,float) or not 0 < deadline <= 900:
        raise ValueError('Use 1-10 repetitions and a deadline of at most 900 seconds.')
    suite, fixtures, cases = suite_cases(profile, families)
    pricing = read(pricing_path) if pricing_path else None
    validate_pricing(pricing)
    output = output_directory(output)
    config = load_config()
    jobs = [(case, repeat) for repeat in range(1,repeats+1) for case in cases]
    random.Random(seed).shuffle(jobs)
    executions = [{'execution_id': f"{case['case_id']}.r{repeat}", 'case_id': case['case_id'],
        'family_id': case['id'], 'area': case['area'], 'language': case['language'], 'repeat': repeat,
        'checks': check_names(case), 'review_required': case.get('review_required',False)} for case,repeat in jobs]
    safe_keys = ('openai_model','openai_reasoning_effort','generation_timeout_seconds','generation_max_tokens',
        'generation_context_results','generation_context_hits','generation_excerpt_chars','generation_max_images',
        'memory_recent_messages','memory_message_chars','memory_context_chars','memory_preference_limit','memory_trip_limit',
        'planning_enabled','planning_max_steps','planning_max_repairs','planning_data_mode')
    settings = {key:config[key] for key in safe_keys if key in config}
    settings['generation_vision_enabled'] = False
    compatibility = {'protocol': PROTOCOL, 'profile': profile, 'suite_sha256': digest(suite), 'fixtures_sha256': digest(fixtures),
        'harness_sha256': digest(harness()), 'config_sha256': sha256((ROOT/'config.yaml').read_bytes()).hexdigest(),
        'settings': settings, 'dependencies': dependencies(), 'python': platform.python_version(),
        'platform': platform.platform(), 'processor': platform.processor(), 'machine': platform.machine(),
        'seed': seed, 'repeats': repeats, 'deadline_seconds': deadline, 'execution_order': [row['execution_id'] for row in executions],
        'reference_date': suite['reference_date'], 'timezone': suite['timezone'], 'pricing_sha256': digest(pricing) if pricing else None,
        'cache_policy': 'fresh_process_per_execution; remote prompt cache observed, not controlled', 'concurrency': 1}
    compatibility['configured_static_sha256'] = configured_static(config)
    manifest = {'protocol': PROTOCOL, 'suite_version': suite['suite_version'], 'profile': profile, 'repeats': repeats,
        'settings': settings, 'compatibility': compatibility, 'git': git_state(), 'source_sha256': sources(),
        'harness_sha256': harness(), 'fixtures': fixtures['provenance'], 'pricing': pricing,
        'executions': executions, 'started_at_utc': datetime.now(timezone.utc).isoformat(), 'integrity': False}
    write(output/'manifest.json',manifest)
    rows = []
    interrupted = False
    try:
        with (output/'results.jsonl').open('x',encoding='utf-8') as stream:
            for (case,repeat), spec in zip(jobs, executions):
                if sources() != manifest['source_sha256'] or harness() != manifest['harness_sha256'] or configured_static(config) != compatibility['configured_static_sha256']:
                    raise ValueError('Application, fixtures or harness changed during execution. Start a new run.')
                print(spec['execution_id']+': starting',flush=True)
                public = {key:case[key] for key in ('id','profile','adapter','input','message','language')}
                observation, wall, harness_ok = invoke(public,fixtures,suite['reference_date'],deadline)
                try:
                    checks = grade(case,observation,fixtures) if harness_ok else dict.fromkeys(spec['checks'],'unknown')
                except Exception as error:
                    observation['grader_error'] = {'type':type(error).__name__, 'message':str(error)[:1000]}
                    checks, harness_ok = dict.fromkeys(spec['checks'],'unknown'), False
                row = {**spec, 'message':case['message'], 'checks':checks, 'observation':observation,
                       'wall_seconds':wall, 'harness_ok':harness_ok}
                stream.write(json.dumps(row,ensure_ascii=False,allow_nan=False)+'\n')
                stream.flush()
                rows.append(row)
                print(spec['execution_id']+': '+('PASS' if all(value=='pass' for value in checks.values()) else 'FAIL/UNKNOWN'),flush=True)
    except KeyboardInterrupt:
        interrupted = True
    finally:
        manifest['integrity'] = (manifest['source_sha256'] == sources() and manifest['harness_sha256'] == harness()
            and configured_static(config) == compatibility['configured_static_sha256'])
        manifest['finished_at_utc'] = datetime.now(timezone.utc).isoformat()
        manifest['interrupted'] = interrupted
        manifest['results_sha256'] = sha256((output/'results.jsonl').read_bytes()).hexdigest()
        manifest['served_models'] = sorted({event['served_model'] for row in rows for event in row['observation']['provider_calls'] if event.get('served_model')})
        write(output/'manifest.json',manifest)
        summary = summarize(manifest,rows,pricing)
        summary['complete'] = summary['complete'] and manifest['integrity'] and not interrupted
        write(output/'summary.json',summary)
        write(output/'reviews.template.json', {'rubric_version':'measurement-review-1', 'reviewer':'',
            'results':[{'execution_id':spec['execution_id'], 'task_success':'unknown','claim_support':'unknown','language':'unknown','notes':''}
                       for spec in executions if spec['review_required']]})
        (output/'REPORT.md').write_text(report(manifest,summary),encoding='utf-8')
    return summary


def load_run(path):
    path = Path(path)
    manifest = read(path/'manifest.json')
    if manifest.get('protocol') != PROTOCOL:
        raise ValueError('This is not a versioned measurement run; legacy reports cannot be compared as a baseline.')
    if sha256((path/'results.jsonl').read_bytes()).hexdigest() != manifest.get('results_sha256'):
        raise ValueError('Raw results changed after the measurement run finished.')
    rows = [json.loads(line) for line in (path/'results.jsonl').read_text(encoding='utf-8').splitlines()]
    summary = summarize(manifest,rows,manifest.get('pricing'))
    summary['complete'] = (summary['complete'] and manifest.get('integrity',False) and not manifest.get('interrupted',False)
                           and not (path/'INVALIDATED.json').exists())
    return manifest, rows, summary


def invalidate(path, reason):
    path = Path(path).resolve()
    if not path.is_relative_to((ROOT/'benchmarks/runs').resolve()) or not isinstance(reason,str) or not reason.strip():
        raise ValueError('Invalidate only a benchmark run, with an explicit reason.')
    manifest,_,_ = load_run(path)
    with (path/'INVALIDATED.json').open('x',encoding='utf-8') as stream:
        json.dump({'reason':reason,'invalidated_at_utc':datetime.now(timezone.utc).isoformat(),
                   'results_sha256':manifest['results_sha256']},stream,ensure_ascii=False,indent=2)


def compare(before, after):
    baseline, old_rows, old_summary = load_run(before)
    candidate, new_rows, new_summary = load_run(after)
    if not old_summary['complete'] or not new_summary['complete']:
        raise ValueError('Incomplete or invalid runs cannot produce an improvement comparison.')
    mismatches = [key for key in set(baseline['compatibility']) | set(candidate['compatibility'])
                  if baseline['compatibility'].get(key) != candidate['compatibility'].get(key)]
    if baseline['served_models'] != candidate['served_models']:
        mismatches.append('served_models')
    if baseline['profile'] == 'controlled' and not baseline['served_models']:
        mismatches.append('served_models_unrecorded')
    if mismatches:
        raise ValueError('Runs are not comparable: '+', '.join(sorted(mismatches)))
    old_index = {row['execution_id']:row for row in old_rows}
    regressions, improvements = [], []
    for row in new_rows:
        old = old_index[row['execution_id']]
        for name,state in row['checks'].items():
            prior = old['checks'][name]
            if prior == 'pass' and state != 'pass':
                regressions.append({'execution_id':row['execution_id'],'check':name,'before':prior,'after':state})
            elif prior != 'pass' and state == 'pass':
                improvements.append({'execution_id':row['execution_id'],'check':name,'before':prior,'after':state})
    areas = {}
    def delta(old,new):
        return new-old if old is not None and new is not None else None
    for name, old in old_summary['areas'].items():
        new = new_summary['areas'][name]
        areas[name] = {'before':old,'after':new,'automatic_pass_delta_percentage_points':100*(new['automatic_pass_rate']-old['automatic_pass_rate']),
            'check_micro_delta_percentage_points':100*(new['check_micro_pass']-old['check_micro_pass']),
            'worker_median_delta_seconds':delta(old['worker_wall_median_seconds'],new['worker_wall_median_seconds']),
            'worker_p95_delta_seconds':delta(old['worker_wall_p95_seconds'],new['worker_wall_p95_seconds']),
            'sdk_calls_delta':delta(old['usage']['llm_sdk_calls'],new['usage']['llm_sdk_calls']),
            'input_tokens_delta':delta(old['usage']['input_tokens'],new['usage']['input_tokens']),
            'output_tokens_delta':delta(old['usage']['output_tokens'],new['usage']['output_tokens']),
            'estimated_cost_delta_usd':delta(old['usage']['estimated_cost_usd'],new['usage']['estimated_cost_usd'])}
    return {'protocol':PROTOCOL, 'comparable':True, 'baseline':str(Path(before).resolve()), 'candidate':str(Path(after).resolve()),
        'same_application_source':baseline['source_sha256']==candidate['source_sha256'],
        'changed_application_files':sorted(key for key in set(baseline['source_sha256']) | set(candidate['source_sha256'])
            if baseline['source_sha256'].get(key)!=candidate['source_sha256'].get(key)),
        'areas':areas,'regressions':regressions,'improvements':improvements,
        'interpretation':'Paired automatic development checks; model variability and pending human review prevent a whole-system quality claim.'}
