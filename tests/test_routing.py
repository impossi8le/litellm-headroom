import unittest
from pathlib import Path
import tempfile
import yaml
import routing
import copy

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
    def test_malformed_json_values_return_errors_without_crashing(self):
        for value in (None, [], "oops", 42, True):
            with self.subTest(value=value):
                self.assertTrue(routing.validate(value))
        for path in (("aliases", 0, "name"), ("aliases", 0, "fallbacks"),
                     ("aliases", 0, "deployments", 0, "provider"),
                     ("aliases", 0, "model_info"), ("timeouts",)):
            for value in ([{}], {"bad": []}, True, 23):
                if path[-1] == "model_info" and isinstance(value, dict):
                    continue
                cfg = copy.deepcopy(GOOD)
                node = cfg
                for part in path[:-1]:
                    node = node[part]
                node[path[-1]] = value
                with self.subTest(path=path, value=value):
                    self.assertTrue(routing.validate(cfg))

    def test_missing_or_literal_key_env_rejected(self):
        for value in (None, "sk-literal-secret", ["KEY"]):
            cfg = copy.deepcopy(GOOD)
            cfg["providers"]["p1"]["key_env"] = value
            self.assertTrue(routing.validate(cfg))

    def test_invalid_timeouts_rejected(self):
        for key, value in (("request", 0), ("stream", -1), ("total", float("inf")),
                           ("cooldown", True), ("retries", 0.5), ("request", "120")):
            cfg = copy.deepcopy(GOOD)
            cfg["timeouts"][key] = value
            self.assertTrue(routing.validate(cfg))

    def test_credentials_in_url_rejected(self):
        cfg = copy.deepcopy(GOOD)
        cfg["providers"]["p1"]["chat_base"] = "https://user:password@example.com/v1"
        self.assertTrue(routing.validate(cfg))

    def test_native_adapter_accepts_ambient_credentials(self):
        cfg = copy.deepcopy(GOOD)
        cfg["providers"]["cloud"] = {"params": {"aws_region_name": "us-east-1"}}
        cfg["aliases"][0]["deployments"] = [{"provider": "cloud", "dialect": "native",
                                             "model": "bedrock/test-model", "order": 1}]
        self.assertEqual(routing.validate(cfg), [])
        out = routing.render_config(cfg)["model_list"][0]["litellm_params"]
        self.assertEqual(out["model"], "bedrock/test-model")
        self.assertEqual(out["aws_region_name"], "us-east-1")
        self.assertNotIn("api_key", out)
        self.assertNotIn("api_base", out)

    def test_params_override_and_credential_references(self):
        cfg = copy.deepcopy(GOOD)
        cfg["providers"]["p1"]["params"] = {"rpm": 20, "timeout": 60,
            "aws_secret_access_key": "os.environ/AWS_SECRET_ACCESS_KEY"}
        cfg["aliases"][0]["deployments"][0]["params"] = {"rpm": 10}
        out = routing.render_config(cfg)["model_list"][0]["litellm_params"]
        self.assertEqual(out["rpm"], 10)
        self.assertEqual(out["timeout"], 60)
        cfg["providers"]["p1"]["params"]["aws_secret_access_key"] = "literal"
        self.assertTrue(routing.validate(cfg))

    def test_anonymous_compatible_endpoint(self):
        cfg = copy.deepcopy(GOOD)
        cfg["providers"]["p1"] = {"anonymous": True, "api_base": "http://127.0.0.1:1234/v1/"}
        out = routing.render_config(cfg)["model_list"][0]["litellm_params"]
        self.assertEqual(out["api_key"], "not-needed")
        self.assertEqual(out["api_base"], "http://127.0.0.1:1234/v1")

    def test_compression_is_configurable(self):
        cfg = copy.deepcopy(GOOD)
        cfg["compression"] = {"enabled": False}
        self.assertNotIn("guardrails", routing.render_config(cfg))
        cfg["compression"] = {"timeout": 8, "unreachable_fallback": "fail_closed"}
        params = routing.render_config(cfg)["guardrails"][0]["litellm_params"]
        self.assertEqual(params["timeout"], 8)
        self.assertEqual(params["unreachable_fallback"], "fail_closed")

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

    def test_provider_value_not_a_mapping_reported(self):
        cfg = yaml.safe_load(yaml.safe_dump(GOOD))
        cfg["providers"]["p1"] = "oops"
        errors = routing.validate(cfg)
        self.assertTrue(any("p1" in e and "mapping" in e for e in errors), errors)

    def test_alias_entry_not_a_mapping_reported(self):
        cfg = yaml.safe_load(yaml.safe_dump(GOOD))
        cfg["aliases"].append("oops")
        errors = routing.validate(cfg)
        self.assertTrue(any("mapping" in e for e in errors), errors)

    def test_deployment_entry_not_a_mapping_reported(self):
        cfg = yaml.safe_load(yaml.safe_dump(GOOD))
        cfg["aliases"][0]["deployments"].append("oops")
        errors = routing.validate(cfg)
        self.assertTrue(any("mapping" in e for e in errors), errors)

    def test_deployment_order_null_rejected(self):
        cfg = yaml.safe_load(yaml.safe_dump(GOOD))
        cfg["aliases"][0]["deployments"][0]["order"] = None
        errors = routing.validate(cfg)
        self.assertTrue(any("order" in e for e in errors), errors)

    def test_deployment_order_string_rejected(self):
        cfg = yaml.safe_load(yaml.safe_dump(GOOD))
        cfg["aliases"][0]["deployments"][0]["order"] = "1"
        errors = routing.validate(cfg)
        self.assertTrue(any("order" in e for e in errors), errors)

    def test_deployment_order_zero_rejected(self):
        cfg = yaml.safe_load(yaml.safe_dump(GOOD))
        cfg["aliases"][0]["deployments"][0]["order"] = 0
        errors = routing.validate(cfg)
        self.assertTrue(any("order" in e for e in errors), errors)

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


class RenderTests(unittest.TestCase):
    def test_one_entry_per_deployment(self):
        cfg = yaml.safe_load(yaml.safe_dump(GOOD))
        cfg["aliases"][0]["deployments"].append(
            {"provider": "p1", "model": "x", "dialect": "openai", "order": 1, "key_env": "K2"})
        out = routing.render_config(cfg)
        entries = [e for e in out["model_list"] if e["model_name"] == "m1"]
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[1]["litellm_params"]["api_key"], "os.environ/K2")

    def test_api_key_is_env_reference_never_literal(self):
        out = routing.render_config(GOOD)
        for entry in out["model_list"]:
            self.assertTrue(entry["litellm_params"]["api_key"].startswith("os.environ/"))
        self.assertNotIn("sk-", yaml.safe_dump(out))

    def test_dialect_selects_base_and_prefix(self):
        cfg = yaml.safe_load(yaml.safe_dump(GOOD))
        cfg["aliases"][0]["deployments"][0]["dialect"] = "anthropic"
        out = routing.render_config(cfg)
        params = out["model_list"][0]["litellm_params"]
        self.assertEqual(params["model"], "anthropic/x")
        self.assertEqual(params["api_base"], "https://a")

    def test_fallbacks_render_as_single_key_dicts(self):
        out = routing.render_config(GOOD)
        self.assertEqual(out["router_settings"]["fallbacks"], [{"m1": ["m2"]}, {"m2": []}])

    def test_wrapper_is_fixed(self):
        out = routing.render_config(GOOD)
        self.assertTrue(out["litellm_settings"]["use_chat_completions_url_for_anthropic_messages"])
        self.assertTrue(out["litellm_settings"]["drop_params"])
        self.assertEqual(out["litellm_settings"]["request_timeout"], 120)
        self.assertTrue(out["general_settings"]["disable_ui"])
        self.assertEqual(out["general_settings"]["master_key"], "os.environ/LITELLM_MASTER_KEY")
        self.assertEqual(out["guardrails"][0]["guardrail_name"], "headroom-compression")
        self.assertTrue(out["guardrails"][0]["litellm_params"]["default_on"])
        self.assertNotIn("enable_weighted_failover", out["router_settings"])

    def test_render_is_idempotent(self):
        first = yaml.safe_dump(routing.render_config(GOOD), sort_keys=False)
        second = yaml.safe_dump(routing.render_config(GOOD), sort_keys=False)
        self.assertEqual(first, second)

    def test_write_config_leaves_old_file_on_validation_error(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "litellm-config.yaml"
            target.write_text("sentinel", encoding="utf-8")
            bad = yaml.safe_load(yaml.safe_dump(GOOD))
            bad["aliases"][0]["fallbacks"] = ["nope"]
            with self.assertRaises(routing.ConfigError):
                routing.write_config(bad, target)
            self.assertEqual(target.read_text(encoding="utf-8"), "sentinel")
