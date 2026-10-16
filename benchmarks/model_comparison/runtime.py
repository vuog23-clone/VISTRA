"""Isolate benchmark model selection without changing application defaults."""

from functools import wraps
import json
import os
from pathlib import Path


MODELS = ('gpt-5-nano', 'gpt-6-luna', 'gpt-4.1-nano')


def request_options(model, options):
    options = dict(options)
    if model == 'gpt-4.1-nano' or model.startswith('gpt-4.1-nano-'):
        options.pop('reasoning', None)
    return options


def activate():
    from src.utils import utils
    config_path = Path(os.environ['VISTRA_BENCHMARK_CONFIG']).resolve()
    if not config_path.is_relative_to((utils.PROJECT_ROOT/'benchmarks/runs').resolve()):
        raise ValueError('Benchmark configuration must be inside benchmarks/runs.')
    utils.CONFIG_PATH = config_path
    config = utils.load_config()
    model = config['openai_model']
    if model not in MODELS:
        raise ValueError('Model is not in the requested comparison.')
    from openai.resources.responses import Responses
    def wrap(method, name):
        @wraps(method)
        def call(self, *args, **options):
            chosen = options.get('model', model)
            effective = request_options(chosen, options)
            audit = os.getenv('VISTRA_BENCHMARK_TRANSPORT_AUDIT')
            if audit:
                folder = Path(audit).resolve()
                if not folder.is_relative_to((utils.PROJECT_ROOT/'benchmarks/runs').resolve()):
                    raise ValueError('Transport audit must remain inside benchmark results.')
                folder.mkdir(parents=True, exist_ok=True)
                with (folder/f'{os.getpid()}.jsonl').open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps({'method':name,'requested_model':chosen,
                        'reasoning':effective.get('reasoning'),'reasoning_omitted':
                            'reasoning' in options and 'reasoning' not in effective})+'\n')
            response=method(self, *args, **effective)
            usage=getattr(response,'usage',None)
            if audit and usage is not None:
                usage_folder=folder.parent/'transport-usage'
                usage_folder.mkdir(exist_ok=True)
                with (usage_folder/f'{os.getpid()}.jsonl').open('a',encoding='utf-8') as stream:
                    stream.write(json.dumps({'response_id':getattr(response,'id',None),
                        'requested_model':chosen,'served_model':getattr(response,'model',None),
                        'service_tier':getattr(response,'service_tier',None),
                        'usage':usage.model_dump()})+'\n')
            return response
        call._vistra_model_adapter = True
        return call
    for name in ('create','parse','stream'):
        method = getattr(Responses, name)
        if not getattr(method, '_vistra_model_adapter', False):
            setattr(Responses, name, wrap(method, name))
    return config
