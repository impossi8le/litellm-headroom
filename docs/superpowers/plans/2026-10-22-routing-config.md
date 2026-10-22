# Routing Config and Editor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move routing out of the hand-maintained `litellm-config.yaml` into a validated `models.yaml` source of truth, generate the LiteLLM config from it, and add a local HTML editor that writes both files.

**Architecture:** `routing.py` holds all logic as pure functions (load, validate, render to a dict, atomic write). `render.py` is a thin CLI over it. `editor.py` is a stdlib HTTP server that reads and writes `models.yaml` and calls the same functions. `start.ps1` renders before launching and gains a `-Restart` switch that only restarts LiteLLM.

**Tech Stack:** Python 3.12 (stdlib only for the editor), PyYAML, PowerShell 5.1, LiteLLM 1.106.0.

**Spec:** `docs/superpowers/specs/2026-10-22-routing-config-design.md`

## Global Constraints

- Python 3.12.7. Editor and routing modules use **stdlib only** (plus PyYAML, already installed) so they run in either `.venv` or `.litellm-venv`.
- Never write an API key into any committed file. Configs reference `os.environ/<NAME>` only.
- `models.yaml` is committed; `litellm-config.yaml` stays gitignored.
- Model names and provider hosts are account data. Never hardcode them in code or tests. Read them from files at runtime.
- All file writes to `litellm-config.yaml` are atomic (temp file in the same directory, then `os.replace`).
- Windows paths; scripts must work when run from any cwd (`Path(__file__).resolve().parent`).
- LiteLLM 1.106.0 order-based fallback is native (`router.py:7642`); do **not** add `enable_weighted_failover`.

## Review Focus

- **Two deployments of one alias with equal `order`** — must produce two `model_list` entries that LiteLLM treats as one load-balanced tier, not a validation error.
- **A fallback target that does not exist** — must be rejected at validate time with the alias name in the message, and nothing written to disk.
- **A cycle in fallbacks (A to B to A)** — must be rejected; naive recursion would hang.
- **`models.yaml` missing or malformed at startup** — `start.ps1` must not launch LiteLLM on a stale or half-written config.
- **A key env var that is unset** — validation must not crash; `/api/catalog` must return a readable 400 rather than a traceback.

---

### Task 1: `routing.py` — load and validate

**Files:**
- Create: `routing.py`
- Test: `tests/test_routing.py`

**Interfaces:**
- Produces: `DIALECTS: tuple[str, ...]`, `class ConfigError(Exception)` with `.errors: list[str]`, `load_models(path: Path) -> dict`, `validate(models: dict) -> list[str]`, `load_and_validate(path: Path) -> dict` (raises `ConfigError`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_routing.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.litellm-venv/Scripts/python.exe -m unittest tests.test_routing -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'routing'`.

- [ ] **Step 3: Implement `routing.py`**

`DIALECTS = ("openai", "anthropic")`. `ConfigError` stores `self.errors` and its `__str__` joins them with `"; "`.

`load_models(path)` raises `ConfigError` with one message when the file is missing or does not parse to a mapping. `validate(models)` returns a list of strings, each naming the alias or provider it concerns. Check in this order: duplicate alias names, empty `deployments`, unknown `dialect`, unknown provider, provider base empty for that dialect, unknown fallback target, self-reference, cycle. The cycle check is an iterative or recursive DFS over the alias graph that records the path and reports the first cycle as `cycle: a -> b -> a`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.litellm-venv/Scripts/python.exe -m unittest tests.test_routing -v`
Expected: PASS, 11 tests.

- [ ] **Step 5: Commit**

```bash
git add routing.py tests/test_routing.py
git commit -m "feat: add models.yaml loader and validator"
```

---

### Task 2: `routing.py` — render the LiteLLM config

**Files:**
- Modify: `routing.py`
- Test: `tests/test_routing.py`

**Interfaces:**
- Consumes: `validate`, `ConfigError` from Task 1.
- Produces: `render_config(models: dict) -> dict` and `write_config(models: dict, path: Path) -> None`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_routing.py

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.litellm-venv/Scripts/python.exe -m unittest tests.test_routing.RenderTests -v`
Expected: FAIL with `AttributeError: module 'routing' has no attribute 'render_config'`.

- [ ] **Step 3: Implement `render_config` and `write_config`**

`model_list` is built in alias order, and deployments keep file order within an alias — do not sort, because equal-order tiers depend on it. Each entry holds `model_name`, `litellm_params` (`model`, `api_base`, `api_key`, `order`) and `model_info` copied from the alias when present. `model` is `<dialect>/<model>`; `api_base` is `chat_base` for `openai` and `messages_base` for `anthropic`; `api_key` is `os.environ/<deployment key_env or provider key_env>`.

`router_settings` gets `num_retries`, `timeout`, `stream_timeout`, `cooldown_time` from `timeouts`, the `fallbacks` list (one single-key dict per alias, in alias order), and `retry_policy` copied verbatim from the current config.

`write_config` validates first, then writes `yaml.safe_dump(..., sort_keys=False, allow_unicode=True)` to `<path>.tmp` in the same directory and `os.replace`s it.

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.litellm-venv/Scripts/python.exe -m unittest tests.test_routing -v`
Expected: PASS, 18 tests.

- [ ] **Step 5: Commit**

```bash
git add routing.py tests/test_routing.py
git commit -m "feat: render litellm config from models.yaml"
```

---

### Task 3: `render.py` CLI

**Files:**
- Create: `render.py`
- Test: `tests/test_render_cli.py`

**Interfaces:**
- Consumes: `routing.load_and_validate`, `routing.write_config`, `routing.ConfigError`.
- Produces: `main(argv: list[str] | None = None) -> int` returning `0` on success and `1` on `ConfigError` after printing each error to stderr as `error: ...`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_render_cli.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.litellm-venv/Scripts/python.exe -m unittest tests.test_render_cli -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'render'`.

- [ ] **Step 3: Implement `render.py`**

`argparse` with `--root` defaulting to the script's directory. Missing `models.yaml` exits `0` silently, because the launcher then uses the existing config. A validation failure prints each error to stderr and exits `1`.

- [ ] **Step 4: Run test to verify it passes**

Run: `./.litellm-venv/Scripts/python.exe -m unittest tests.test_render_cli -v`
Expected: PASS, 3 tests.

- [ ] **Step 5: Commit**

```bash
git add render.py tests/test_render_cli.py
git commit -m "feat: add render CLI"
```

---

### Task 4: Migration from the live config

**Files:**
- Create: `tools/migrate_config.py`
- Create: `models.yaml` (generated, committed)
- Test: `tests/test_migration.py`

**Interfaces:**
- Consumes: `routing.validate`, `routing.render_config`.
- Produces: `migrate(config: dict) -> dict` returning a `models.yaml`-shaped dict.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_migration.py
import sys, unittest
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
        key = lambda d: list(d)[0]
        self.assertEqual(sorted(rendered["router_settings"]["fallbacks"], key=key),
                         sorted(live["router_settings"]["fallbacks"], key=key))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.litellm-venv/Scripts/python.exe -m unittest tests.test_migration -v`
Expected: FAIL, cannot import `migrate_config`.

- [ ] **Step 3: Implement `tools/migrate_config.py`**

Group `model_list` entries by `model_name`. Dialect comes from the `model` prefix (`openai/` to `openai`, `anthropic/` to `anthropic`). Provider keys are derived from `api_base` with a stable slug; `key_env` defaults to `RELAY_FAST_API_KEY` for the base host already present in the file. Assign `order` per group in file order starting at 1, and give entries that repeat the same `api_base` the same `order` so they form one tier. Copy `model_info` from the first entry of each group. Carry `fallbacks` across unchanged. The CLI writes `models.yaml` next to `litellm-config.yaml` with a header comment saying it is the source of truth from now on.

Run it once against the real config, commit the result, and confirm the round-trip test passes.

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.litellm-venv/Scripts/python.exe -m unittest tests.test_migration tests.test_routing -v`
Expected: PASS.

- [ ] **Step 5: Verify the generated config parses and renders**

Run: `./.litellm-venv/Scripts/python.exe render.py --root .` then `./.litellm-venv/Scripts/python.exe -c "import yaml;yaml.safe_load(open('litellm-config.yaml',encoding='utf-8'))"`
Expected: exit 0 both times.

- [ ] **Step 6: Commit**

```bash
git add tools/migrate_config.py models.yaml tests/test_migration.py
git commit -m "feat: migrate live config to models.yaml"
```

---

### Task 5: Fix the existing tests and smoke check

**Files:**
- Modify: `tests/test_setup.py`
- Modify: `smoke_check.py`

**Interfaces:**
- Consumes: `models.yaml` from Task 4.

- [ ] **Step 1: Rewrite `test_provider_routes` to assert invariants**

Replace the hardcoded host list. Render through `routing.render_config(routing.load_and_validate(ROOT / "models.yaml"))` and assert, for every entry: `model_name` is non-empty; `api_key` starts with `os.environ/`; if `model` starts with `openai/` then `api_base` ends with `/v1`; if it starts with `anthropic/` then `api_base` does not end with `/v1`.

- [ ] **Step 2: Rewrite `smoke_check.py` to resolve the env reference and dedupe aliases**

`master_key` is now `os.environ/LITELLM_MASTER_KEY`. Resolve it by stripping the `os.environ/` prefix and reading the variable, and exit with a clear message when it is unset. Iterate over unique `model_name` values so a two-deployment alias is probed once.

- [ ] **Step 3: Run the suite**

Run: `./.litellm-venv/Scripts/python.exe -m unittest discover -s tests -v`
Expected: PASS, including the pre-existing launcher test.

- [ ] **Step 4: Commit**

```bash
git add tests/test_setup.py smoke_check.py
git commit -m "test: assert routing invariants instead of fixed hosts"
```

---

### Task 6: `start.ps1` — render before launch, add `-Restart`

**Files:**
- Modify: `start.ps1`

**Interfaces:**
- Produces: `start.ps1 [-StartupTimeoutSeconds N] [-Restart]`. `-Restart` stops only the LiteLLM process and leaves Headroom running.

- [ ] **Step 1: Add the parameter and a LiteLLM-only stop helper**

Change the `param` line to `param([int]$StartupTimeoutSeconds = 120, [switch]$Restart)`. Add `function Stop-LiteLLM` that stops processes whose command line matches `litellm.proxy.proxy_cli`, then polls `http://127.0.0.1:8001/health/liveliness` until it stops answering, for at most 15 seconds.

- [ ] **Step 2: Render before launching**

Before `Start-ServiceProcess 'litellm'`, if `models.yaml` exists, run `& $litellm (Join-Path $PSScriptRoot 'render.py') --root $PSScriptRoot` and throw `"Config render failed: see the errors above"` when it exits non-zero. This runs before any service starts, so a bad config never launches a proxy.

- [ ] **Step 3: Honour `-Restart`**

When `-Restart` is set, call `Stop-LiteLLM` before starting LiteLLM. The Headroom health check stays as it is.

- [ ] **Step 4: Verify by hand**

Run: `powershell -ExecutionPolicy Bypass -File .\start.ps1 -Restart`
Expected: Headroom reported as already running, LiteLLM stopped and restarted, both health checks OK, gateway URL printed.

- [ ] **Step 5: Commit**

```bash
git add start.ps1
git commit -m "feat: render config before start and support restart"
```

---

### Task 7: `editor.py` — server and API

**Files:**
- Create: `editor.py`
- Test: `tests/test_editor.py`

**Interfaces:**
- Consumes: `routing.load_and_validate`, `routing.validate`, `routing.write_config`, `routing.ConfigError`.
- Produces: `make_server(root: Path, host: str = "127.0.0.1", port: int = 8788, restart=None) -> ThreadingHTTPServer`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_editor.py
import json, threading, unittest, urllib.request, urllib.error
from pathlib import Path
import tempfile
import yaml
import editor

CFG = {
    "providers": {"p1": {"key_env": "K1", "chat_base": "https://a/v1", "messages_base": "https://a"}},
    "timeouts": {"request": 120, "stream": 120, "total": 300, "retries": 1, "cooldown": 5},
    "aliases": [{"name": "m1", "deployments": [
        {"provider": "p1", "model": "x", "dialect": "openai", "order": 1}], "fallbacks": []}],
}

class EditorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "models.yaml").write_text(yaml.safe_dump(CFG), encoding="utf-8")
        self.start(editor.make_server(self.root, port=0))

    def start(self, server):
        self.server = server
        self.base = "http://127.0.0.1:%d" % server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def post(self, path, payload):
        req = urllib.request.Request(self.base + path,
                                     data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        return urllib.request.urlopen(req, timeout=5)

    def test_get_config_returns_models(self):
        body = json.loads(urllib.request.urlopen(self.base + "/api/config", timeout=5).read())
        self.assertEqual(body["aliases"][0]["name"], "m1")

    def test_post_config_saves(self):
        self.post("/api/config", CFG)
        saved = yaml.safe_load((self.root / "models.yaml").read_text(encoding="utf-8"))
        self.assertEqual(saved["aliases"][0]["name"], "m1")

    def test_post_invalid_config_returns_400_and_does_not_save(self):
        bad = yaml.safe_load(yaml.safe_dump(CFG))
        bad["aliases"][0]["fallbacks"] = ["nope"]
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/config", bad)
        self.assertEqual(ctx.exception.code, 400)
        saved = yaml.safe_load((self.root / "models.yaml").read_text(encoding="utf-8"))
        self.assertEqual(saved["aliases"][0]["fallbacks"], [])

    def test_apply_renders_and_calls_restart(self):
        called = []
        self.server.shutdown(); self.server.server_close()
        self.start(editor.make_server(self.root, port=0, restart=lambda: called.append(1)))
        self.post("/api/apply", CFG)
        self.assertTrue((self.root / "litellm-config.yaml").exists())
        self.assertEqual(called, [1])

    def test_catalog_without_any_key_returns_400(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self.base + "/api/catalog", timeout=5)
        self.assertEqual(ctx.exception.code, 400)

    def test_rejects_foreign_host_header(self):
        req = urllib.request.Request(self.base + "/api/config", headers={"Host": "evil.example"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 403)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.litellm-venv/Scripts/python.exe -m unittest tests.test_editor -v`
Expected: FAIL, cannot import `editor`.

- [ ] **Step 3: Implement `editor.py`**

`ThreadingHTTPServer` plus `BaseHTTPRequestHandler`. Routes: `GET /`, `GET /api/config`, `POST /api/config`, `POST /api/render`, `POST /api/apply`, `GET /api/catalog`. `make_server` attaches `root` and `restart` to the server object so the handler can read them.

Host check: reject with `403` when the `Host` header hostname is neither `127.0.0.1` nor `localhost`. JSON parse failures return `400`. Validation errors return `400` with `{"errors": [...]}`.

`/api/catalog` takes the first provider in declaration order whose `key_env` resolves to a non-empty environment variable; when there is none it returns `400 {"error": "no provider has its API key set"}`. It then requests `GET {chat_base}/models` with a 5-second timeout; an upstream failure returns `502` with the status and a short reason, never the upstream body.

`/api/apply` writes `models.yaml`, then `litellm-config.yaml`, then calls `restart()` when one was supplied.

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.litellm-venv/Scripts/python.exe -m unittest tests.test_editor -v`
Expected: PASS, 6 tests.

- [ ] **Step 5: Commit**

```bash
git add editor.py tests/test_editor.py
git commit -m "feat: add local editor API"
```

---

### Task 8: Editor page

**Files:**
- Create: `editor_page.html`
- Modify: `editor.py`

- [ ] **Step 1: Write the page**

One HTML file, no build step, no external assets. Sections: Providers (name, `key_env`, `chat_base`, `messages_base`); Aliases (one card per alias, each with deployments of provider/model/dialect/order/key, a fallbacks list backed by a `datalist` of existing alias names, and a delete button); a toolbar with «Добавить модель», «Сохранить», «Применить».

The new-model form defaults to the first provider, `dialect: openai`, `order: 1`, and empty model and fallbacks.

Every input remembers the last typed value under `localStorage["<alias>.<field>"]` and shows a «вернуть сохранённое» button that fills in the value currently on disk. These are two separate behaviours and both are required.

Saving posts to `/api/config`; on `400` the `errors` array is rendered next to the affected alias card. «Применить» posts to `/api/apply` and shows the result.

- [ ] **Step 2: Serve it**

`GET /` returns `editor_page.html` as `text/html; charset=utf-8`. A missing file returns `500` with a one-line message.

- [ ] **Step 3: Verify in a browser**

Run: `./.litellm-venv/Scripts/python.exe editor.py`
Expected: the page loads at `http://127.0.0.1:8788/`, existing aliases render, and adding a model then saving updates `models.yaml` on disk.

- [ ] **Step 4: Commit**

```bash
git add editor_page.html editor.py
git commit -m "feat: add editor page"
```

---

### Task 9: Docs and final check

**Files:**
- Modify: `README.md`
- Modify: `.gitignore`

- [ ] **Step 1: Document the new flow**

Add to README: `models.yaml` is the source of truth; `render.py` writes `litellm-config.yaml`; `start.ps1` renders automatically; `editor.py` opens the editor; `-Restart` restarts only LiteLLM. Note that `models.yaml` is committed and holds no keys.

- [ ] **Step 2: Ignore the render temp file**

Add `litellm-config.yaml.tmp` to `.gitignore`.

- [ ] **Step 3: Run everything**

Run: `./.litellm-venv/Scripts/python.exe -m unittest discover -s tests -v`
Expected: PASS, all tests.

- [ ] **Step 4: Commit and push**

```bash
git add README.md .gitignore
git commit -m "docs: describe models.yaml workflow"
git push origin main
```
