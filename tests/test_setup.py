import unittest
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / 'models.yaml'

class ConfigurationTests(unittest.TestCase):
    def test_provider_routes(self):
        import routing
        cfg = routing.render_config(routing.load_and_validate(MODELS))
        for entry in cfg['model_list']:
            params = entry['litellm_params']
            self.assertTrue(entry['model_name'], 'model_name must not be empty')
            self.assertTrue(params['api_key'].startswith('os.environ/'), params['api_key'])
            if params['model'].startswith('openai/'):
                self.assertTrue(params['api_base'].endswith('/v1'), params['api_base'])
            elif params['model'].startswith('anthropic/'):
                self.assertFalse(params['api_base'].endswith('/v1'), params['api_base'])
            else:
                self.fail('unsupported dialect: %s' % params['model'])
        self.assertTrue(cfg['litellm_settings']['use_chat_completions_url_for_anthropic_messages'])

    def test_compression_enabled(self):
        import routing
        cfg = routing.render_config(routing.load_and_validate(MODELS))
        self.assertTrue(cfg['guardrails'][0]['litellm_params'].get('default_on'))

    def test_launcher_uses_correct_package(self):
        launcher = (ROOT / 'start.ps1').read_text(encoding='utf-8')
        self.assertIn('headroom.exe', launcher)
        self.assertIn('StartupTimeoutSeconds', launcher)
        self.assertIn('HasExited', launcher)
        self.assertNotIn('max proxy', launcher)

if __name__ == '__main__':
    unittest.main()
