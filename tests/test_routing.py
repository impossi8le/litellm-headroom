import unittest
from pathlib import Path
import tempfile
import yaml
import routing

GOOD = {
    "providers": {"p1": {"key_env": "K1", "chat_base": "https://a/v1", "messages_base": "https://a"}},
    "timeouts": {"request": 120, "stream": 120, "total": 300, "retries": 1, "cooldown": 5},
    "aliases": [
        {"name": "m1", "deployments": [{"provider": "p1", "model": "x", "dialect": "openai", "order": 1}],
         "fallbacks": ["m2"], "model_info": {"supports_function_calling": True}},
        {"name": "m2", "deployments": [{"provider": "p1", "model": "y", "dialect": "openai", "order": 1}],
         "fallbacks": []},
    ],
}

class ValidateTests(unittest.TestCase):
    def test_good_config_has_no_errors(self):
        self.assertEqual(routing.validate(GOOD), [])

    def test_duplicate_alias_name_rejected(self):
        cfg = yaml.safe_load(yaml.safe_dump(GOOD))
        cfg["aliases"][1]["name"] = "m1"
        errors = routing.validate(cfg)
        self.assertTrue(any("duplicate alias" in e for e in errors), errors)

    def test_equal_order_allowed(self):
        cfg = yaml.safe_load(yaml.safe_dump(GOOD))
        cfg["aliases"][0]["deployments"].append(
            {"provider": "p1", "model": "x", "dialect": "openai", "order": 1})
        self.assertEqual(routing.validate(cfg), [])

    def test_unknown_fallback_target_rejected(self):
        cfg = yaml.safe_load(yaml.safe_dump(GOOD))
        cfg["aliases"][0]["fallbacks"] = ["nope"]
        errors = routing.validate(cfg)
        self.assertTrue(any("nope" in e for e in errors), errors)

    def test_fallback_cycle_rejected(self):
        cfg = yaml.safe_load(yaml.safe_dump(GOOD))
        cfg["aliases"][1]["fallbacks"] = ["m1"]
        errors = routing.validate(cfg)
        self.assertTrue(any("cycle" in e.lower() for e in errors), errors)

    def test_self_fallback_rejected(self):
        cfg = yaml.safe_load(yaml.safe_dump(GOOD))
        cfg["aliases"][0]["fallbacks"] = ["m1"]
        self.assertTrue(routing.validate(cfg))

    def test_unknown_dialect_rejected(self):
        cfg = yaml.safe_load(yaml.safe_dump(GOOD))
        cfg["aliases"][0]["deployments"][0]["dialect"] = "gemini"
        errors = routing.validate(cfg)
        self.assertTrue(any("dialect" in e for e in errors), errors)

    def test_missing_base_for_dialect_rejected(self):
        cfg = yaml.safe_load(yaml.safe_dump(GOOD))
        cfg["providers"]["p1"]["messages_base"] = ""
        cfg["aliases"][0]["deployments"][0]["dialect"] = "anthropic"
        errors = routing.validate(cfg)
        self.assertTrue(any("messages_base" in e for e in errors), errors)

    def test_empty_deployments_rejected(self):
        cfg = yaml.safe_load(yaml.safe_dump(GOOD))
        cfg["aliases"][0]["deployments"] = []
        errors = routing.validate(cfg)
        self.assertTrue(any("deployment" in e for e in errors), errors)

    def test_unknown_provider_rejected(self):
        cfg = yaml.safe_load(yaml.safe_dump(GOOD))
        cfg["aliases"][0]["deployments"][0]["provider"] = "ghost"
        errors = routing.validate(cfg)
        self.assertTrue(any("ghost" in e for e in errors), errors)

    def test_load_and_validate_raises_with_all_errors(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "models.yaml"
            cfg = yaml.safe_load(yaml.safe_dump(GOOD))
            cfg["aliases"][0]["fallbacks"] = ["nope", "also-nope"]
            p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
            with self.assertRaises(routing.ConfigError) as ctx:
                routing.load_and_validate(p)
            self.assertGreaterEqual(len(ctx.exception.errors), 2)


if __name__ == "__main__":
    unittest.main()
