import unittest
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / 'litellm-config.yaml'
if not CONFIG.exists():
    CONFIG = ROOT / 'litellm-config.example.yaml'

class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.cfg = yaml.safe_load(CONFIG.read_text(encoding='utf-8'))

    def test_provider_routes(self):
        for entry in self.cfg['model_list']:
            params = entry['litellm_params']
            self.assertTrue(entry['model_name'], 'model_name must not be empty')
            self.assertTrue(params['model'], 'model must not be empty')
            if params['model'].startswith('openai/'):
                self.assertEqual(params['api_base'], 'https://relay.fast/v1')
            elif params['model'].startswith('anthropic/'):
                self.assertEqual(params['api_base'], 'https://relay.fast')
            else:
                self.fail('unsupported provider prefix: %s' % params['model'])
        self.assertTrue(self.cfg['litellm_settings']['use_chat_completions_url_for_anthropic_messages'])

    def test_compression_enabled(self):
        self.assertTrue(self.cfg['guardrails'][0]['litellm_params'].get('default_on'))

    def test_launcher_uses_correct_package(self):
        launcher = (ROOT / 'start.ps1').read_text(encoding='utf-8')
        self.assertIn('headroom.exe', launcher)
        self.assertIn('StartupTimeoutSeconds', launcher)
        self.assertIn('HasExited', launcher)
        self.assertNotIn('max proxy', launcher)

if __name__ == '__main__':
    unittest.main()
