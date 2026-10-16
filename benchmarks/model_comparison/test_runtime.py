import unittest
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
from benchmarks.model_comparison.runtime import request_options


class OptionsTests(unittest.TestCase):
    def test_nonreasoning_model_omits_only_unsupported_parameter(self):
        options={'model':'gpt-4.1-nano','reasoning':{'effort':'low'},'store':False,'text_format':'schema'}
        self.assertEqual(request_options('gpt-4.1-nano',options),{'model':'gpt-4.1-nano','store':False,'text_format':'schema'})
        self.assertIn('reasoning',options)

    def test_reasoning_model_options_unchanged(self):
        options={'reasoning':{'effort':'low'},'store':False}
        for model in ('gpt-5-nano','gpt-6-luna'):
            self.assertEqual(request_options(model,options),options)

    def test_snapshot_uses_same_nonreasoning_capability(self):
        self.assertEqual(request_options('gpt-4.1-nano-2025-04-14',{'reasoning':{'effort':'low'}}),{})

    def test_isolated_config_reaches_generator_and_actual_sdk_boundary(self):
        import yaml
        from openai.resources.responses import Responses
        from src.utils import utils
        from benchmarks.model_comparison.runtime import activate
        from src.generation.rag_generation import RAGGenerator
        shared=utils.CONFIG_PATH.read_bytes()
        base=utils.PROJECT_ROOT/'benchmarks/runs/model-comparison'
        with tempfile.TemporaryDirectory(dir=base,prefix='test-runtime-') as name:
            folder=Path(name).resolve()
            self.assertTrue(folder.is_relative_to(base.resolve()))
            config=yaml.safe_load(shared);config['openai_model']='gpt-4.1-nano'
            path=folder/'config.yaml';path.write_text(yaml.safe_dump(config),encoding='utf-8')
            captured=[]
            def fake_parse(self,**options):
                captured.append(options)
                return options
            with patch.object(utils,'CONFIG_PATH',utils.CONFIG_PATH),patch.dict(os.environ,{
                'VISTRA_BENCHMARK_CONFIG':str(path),'VISTRA_BENCHMARK_TRANSPORT_AUDIT':str(folder/'transport')}),patch.object(Responses,'parse',fake_parse):
                activate()
                with RAGGenerator(client=SimpleNamespace()) as generator:
                    self.assertEqual(generator.model_name,'gpt-4.1-nano')
                Responses.parse(object(),model='gpt-4.1-nano',reasoning={'effort':'low'},store=False)
            self.assertNotIn('reasoning',captured[0])
            event=json.loads(next((folder/'transport').glob('*.jsonl')).read_text(encoding='utf-8'))
            self.assertTrue(event['reasoning_omitted'])
            self.assertEqual(event['requested_model'],'gpt-4.1-nano')
        self.assertEqual(utils.CONFIG_PATH.read_bytes(),shared)
