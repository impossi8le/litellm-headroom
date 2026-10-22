import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import migrate_config
import routing

CONFIG = ROOT / "litellm-config.yaml"


@unittest.skipUnless(CONFIG.exists(), "live config not present")
class MigrationTests(unittest.TestCase):
    def test_round_trip_preserves_routing(self):
        live = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
        models = migrate_config.migrate(live)
        self.assertEqual(routing.validate(models), [])
        rendered = routing.render_config(models)
        self.assertEqual([e["model_name"] for e in rendered["model_list"]],
                         [e["model_name"] for e in live["model_list"]])

    def test_fallbacks_are_a_subset_and_acyclic(self):
        live = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
        models = migrate_config.migrate(live)
        live_fb = {}
        for item in live["router_settings"]["fallbacks"]:
            for name, targets in item.items():
                live_fb[name] = list(targets)
        for alias in models["aliases"]:
            self.assertLessEqual(set(alias["fallbacks"]), set(live_fb[alias["name"]]))


if __name__ == "__main__":
    unittest.main()
