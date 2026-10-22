import unittest
from pathlib import Path
import tempfile
import yaml
import render

CFG = {
    "providers": {"p1": {"key_env": "K1", "chat_base": "https://a/v1", "messages_base": "https://a"}},
    "timeouts": {"request": 120, "stream": 120, "total": 300, "retries": 1, "cooldown": 5},
    "aliases": [{"name": "m1", "deployments": [
        {"provider": "p1", "model": "x", "dialect": "openai", "order": 1}], "fallbacks": []}],
}

class RenderCliTests(unittest.TestCase):
    def test_writes_config_next_to_models(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "models.yaml").write_text(yaml.safe_dump(CFG), encoding="utf-8")
            self.assertEqual(render.main(["--root", d]), 0)
            out = yaml.safe_load((Path(d) / "litellm-config.yaml").read_text(encoding="utf-8"))
            self.assertEqual(out["model_list"][0]["model_name"], "m1")

    def test_invalid_config_returns_one_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            bad = yaml.safe_load(yaml.safe_dump(CFG))
            bad["aliases"][0]["fallbacks"] = ["nope"]
            (Path(d) / "models.yaml").write_text(yaml.safe_dump(bad), encoding="utf-8")
            self.assertEqual(render.main(["--root", d]), 1)
            self.assertFalse((Path(d) / "litellm-config.yaml").exists())

    def test_missing_models_yaml_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(render.main(["--root", d]), 0)


if __name__ == "__main__":
    unittest.main()
