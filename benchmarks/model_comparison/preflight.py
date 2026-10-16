"""Small structured-output availability check, separate from scored cases."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path


def main():
    import yaml
    from src.utils.utils import PROJECT_ROOT, load_config
    from benchmarks.model_comparison.runtime import activate, MODELS
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model',required=True,choices=MODELS)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    out=args.output.resolve()
    if not out.is_relative_to((PROJECT_ROOT/'benchmarks/runs').resolve()):
        parser.error('Output must be inside benchmarks/runs.')
    out.mkdir(parents=True,exist_ok=True)
    if (out/'preflight.json').exists():
        raise RuntimeError('Preflight already recorded; do not silently retry.')
    config=load_config();config['openai_model']=args.model
    path=out/'preflight-config.yaml'
    path.write_text(yaml.safe_dump(config,allow_unicode=True,sort_keys=False),encoding='utf-8')
    os.environ['VISTRA_BENCHMARK_CONFIG']=str(path)
    os.environ['VISTRA_BENCHMARK_TRANSPORT_AUDIT']=str(out/'preflight-transport')
    activate()
    from pydantic import BaseModel
    from src.generation.rag_generation import RAGGenerator
    from src.utils.redaction import CredentialRedactor
    class Probe(BaseModel):
        ready:bool
    result={'model':args.model,'started_at':datetime.now(timezone.utc).isoformat(),
        'included_in_benchmark_scores':False,'status':'failed'}
    try:
        with RAGGenerator() as generator:
            response=generator.client.responses.parse(model=args.model,
                input='Return ready=true.',text_format=Probe,reasoning={'effort':'low'},
                max_output_tokens=2048,store=False)
            result.update(status='passed' if response.status=='completed' and response.output_parsed and response.output_parsed.ready else 'failed',
                served_model=response.model,response_status=response.status,
                usage=response.usage.model_dump() if response.usage else None)
    except Exception as error:
        result['error']={'type':type(error).__name__,'status_code':getattr(error,'status_code',None),
            'code':getattr(error,'code',None),'message':CredentialRedactor().text(str(error))[:1800]}
    (out/'preflight.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,ensure_ascii=True),flush=True)
    if result['status']!='passed':
        raise SystemExit(2)


if __name__=='__main__':
    main()
