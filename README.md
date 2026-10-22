# LiteLLM + relay.fast + Headroom

Gateway: `http://127.0.0.1:8001`; Headroom: `http://127.0.0.1:8787`.
Start with `bat.bat` (or `powershell -ExecutionPolicy Bypass -File .\start.ps1`); stop with `stop-litellm-headroom.bat`.
The launcher reuses healthy running services. `start.ps1 -Restart` restarts only
LiteLLM; Headroom keeps running.

To create/update the two desktop launchers, run
`powershell -ExecutionPolicy Bypass -File .\install-desktop-launchers.ps1`.
Both desktop `.bat` files call the current scripts in this checkout; previous
desktop versions are kept in `backups/desktop_*`. The stop command targets
processes belonging to this workspace, including the routing editor.

## Multi-provider gateway (2026-10-22)

The editor starts with the gateway at `http://127.0.0.1:8788/`; use
`start.ps1 -NoEditor` to skip it. Existing relay.fast aliases are preserved.
Add providers and deployments in the editor or in `models.yaml`:

- `dialect: openai`: any OpenAI-compatible endpoint; use `api_base` or
  `chat_base`, a bare upstream model name and `key_env`.
- `dialect: anthropic`: an Anthropic-compatible endpoint; use `api_base` or
  `messages_base`, a bare model name and `key_env`.
- `dialect: native`: use the full LiteLLM model string, such as
  `azure/YOUR_DEPLOYMENT`, `gemini/YOUR_MODEL`, `bedrock/YOUR_MODEL_ID`,
  `vertex_ai/YOUR_MODEL` or `ollama/YOUR_MODEL`. `api_base` is optional for
  adapters with built-in endpoints. AWS/Google credential chains work without
  an API key; `key_env` remains available where needed.

Provider `params` are inherited; deployment `params` override them. These JSON
objects in the editor map to LiteLLM parameters, including `api_version`,
`aws_region_name`, `vertex_project`, `vertex_location`, `rpm`, `tpm`, `weight`
and `timeout`. Use `os.environ/NAME` for secret parameters. Use dedicated
fields for `model`, `api_base`, `api_key` and `order`. Set provider
`anonymous: true` for a local OpenAI-compatible server that needs no key
(the OpenAI client receives a dummy `not-needed` value). Native adapter
capabilities depend on the installed LiteLLM version and provider access.

See `models.multi-provider.example.yaml` for a validated template and
`.env.example` for environment variable names. Replace model placeholders
with models available in your accounts. Fallbacks can cross providers;
deployment `order` defines priority tiers inside an alias.

Applying in the editor saves private backups under `backups/apply_*`. If the
new service fails to restart, both previous YAML files are restored and a
restart with the old configuration is attempted. The editor reports recovery
status. Configuration mutations are serialized.

Headroom uses a 15-second compression timeout and `fail_open` by default:
if it is unreachable, inference continues with the original context. Set
`compression.unreachable_fallback: fail_closed` if compression is mandatory.
`compression.enabled: false` removes the guardrail; `compression.api_base`
selects its endpoint. Local counters are enabled on newly launched Headroom
processes (`/stats`, `/metrics`, `/dashboard`); the external beacon stays off.
Existing processes keep their original environment until restarted.

References: [LiteLLM configuration](https://docs.litellm.ai/docs/proxy/configs),
[OpenAI-compatible endpoints](https://docs.litellm.ai/docs/providers/openai_compatible),
[routing](https://docs.litellm.ai/docs/routing),
[Headroom sidecar](https://docs.headroomlabs.ai/docs/proxy).

## First-time setup

1. Create both environments as described under Dependencies.
2. Generate `litellm-config.yaml` from the committed `models.yaml` with
   `.\.litellm-venv\Scripts\python.exe render.py --root .` (see Routing
   configuration). Without `models.yaml`, copy `litellm-config.example.yaml`
   to `litellm-config.yaml` instead.
3. Put `RELAY_FAST_API_KEY` (provider key) and `LITELLM_MASTER_KEY` (the key
   clients send) in `.env`, or export them. `start.ps1` imports `.env` when it
   is present. The configs reference the variables by name, so no key is
   stored in git.
4. Run `bat.bat` and verify with `smoke_check.py`.

`litellm-config.yaml`, `.env`, the other local configs, `logs/`, `backups/` and
both virtual environments are gitignored. `models.yaml` is committed and holds
no keys. Only the example config is published.

## Routing configuration

`models.yaml` is the source of truth for routing. It defines providers (the
`key_env` field holds the NAME of an environment variable, never a key),
timeouts, aliases with their deployments (`provider`, `model`, `dialect`,
optional `key_env`, `order`) and fallbacks. `litellm-config.yaml` is generated
from it, so edit `models.yaml` rather than the generated file. Deployments with
the same `order` form one load-balanced tier; a higher `order` is tried after a
lower one, and the alias `fallbacks` are used after its own deployments.

```powershell
.\.litellm-venv\Scripts\python.exe render.py --root .
```

`render.py` validates before writing: unknown providers or fallback targets,
duplicate aliases, empty deployment lists, bad dialects and fallback cycles are
reported as `error: ...` lines and exit 1, leaving the existing
`litellm-config.yaml` untouched. It exits 0 silently when `models.yaml` is
absent, so a checkout without it keeps using a hand-written config.

`start.ps1` renders whenever `models.yaml` exists, before any service starts, so
a bad `models.yaml` stops the launch instead of starting LiteLLM on a stale
config. `start.ps1 -Restart` stops and restarts only LiteLLM.

`models.yaml` is committed; it contains no credentials. The rendered config
references keys as `os.environ/<NAME>`.

### Editor

`editor.py` serves a local page on `127.0.0.1:8788` for providers (add, edit,
delete) and aliases with deployments and fallbacks. «Сохранить» validates and
writes `models.yaml`; «Применить» writes `models.yaml` and `litellm-config.yaml`
and calls the restart hook passed to `make_server`. Start it independently with:

```powershell
.\.litellm-venv\Scripts\python.exe editor.py --no-browser
```

The CLI installs a restart hook. With `--no-restart`, or `make_server` without
a restart hook, «Применить» does not restart LiteLLM; use
`start.ps1 -Restart` for that. The server binds to loopback only and rejects
requests whose `Host` is not `127.0.0.1` or `localhost`. It accepts and displays
only the env-var name, never a key value.

## Provider configuration

Official references:
- https://relay.fast/docs
- https://relay.fast/openai-compatible-api
- https://relay.fast/anthropic-compatible-api
- https://relay.fast/status
- https://docs.litellm.ai/docs/providers/openai_compatible
- https://docs.litellm.ai/docs/routing

Use `openai/<model>` with `api_base: https://relay.fast/v1` for Chat Completions.
Use `anthropic/<model>` with `api_base: https://relay.fast` for Claude Messages.
The `/v1` part is appended by the Anthropic client. Discover account models with
`GET https://relay.fast/v1/models`; being listed does not guarantee a live source.

Existing client aliases are preserved:

| Client model | Actual upstream model | Protocol |
| --- | --- | --- |
| claude-3-5-sonnet-20241022 | kimi-k3 | OpenAI Chat Completions |
| claude-3-opus-20240229 | claude-haiku-5-5 | Anthropic Messages |
| claude-3-5-haiku-20251011 | deepseek-v4.1-flash-1017 | OpenAI Chat Completions |
| claude-3-5-haiku-202609122 | gpt-6-luna | OpenAI Chat Completions |

The client aliases are labels, not guarantees of the model family actually used.
`use_chat_completions_url_for_anthropic_messages: true` keeps non-Anthropic
models on Chat Completions when a client sends `/v1/messages` requests.
Headroom compression remains enabled globally. Function calling capability
metadata belongs in `model_info`, not in the upstream request parameters.

In the rendered config the provider key is an `os.environ/<NAME>` reference in
each model's `litellm_params.api_key`; clients authenticate with the separate
`general_settings.master_key`. Keep YAML and backups private.

## Dependencies

LiteLLM Proxy 1.106.0 and Headroom 0.41.0 are already installed. Their proxy
extras require different MCP major versions, so use separate environments:
`.venv` for Headroom and `.litellm-venv` for LiteLLM. The latter was created with
`--system-site-packages` to reuse the existing working global LiteLLM packages.
It therefore depends on the base Python installation remaining available.
To recreate that environment with the current installation:

```powershell
python -m venv .litellm-venv --system-site-packages
.\.litellm-venv\Scripts\python.exe -m pip install -r requirements.txt
```

Recreate the independent Headroom environment with:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-headroom.txt
```

A clean independent LiteLLM environment can instead omit
`--system-site-packages` and install the same requirements. Do not install
`litellm[proxy]` into Headroom's environment.

## Errors and timeouts

On 2026-10-20 direct requests to relay.fast returned 503 for `kimi-k3` and
`claude-haiku-5-5`: no source could serve those models. The existing logs also
contain upstream 504 errors inside SSE streams. Those are upstream failures,
not evidence of an invalid key or a wrong base URL. DeepSeek and GPT stream
probes completed after approximately 110 seconds.

The router now has a 120-second per-attempt timeout, 120-second stream timeout,
and a 300-second overall request budget. There is at most one configured retry
for rate limits and server errors; auth, bad-request, not-found and timeout
errors are not retried by the error-specific policy. Fallback routes for all
aliases are defined in `models.yaml`. A failure after stream output starts cannot reliably be
recovered transparently; the client may need to retry the whole turn.

`disable_provider_token_counting`, `ignore_unknown_params`, and
`pessimistic_cooldown` were removed because LiteLLM 1.106.0 does not read them.
`cooldown_time` is set under `router_settings`.
The Anthropic-compatible count-tokens endpoint in this LiteLLM version first
tries the provider token API. relay.fast's OpenAI `/v1/responses/input_tokens`
returned 404 in the logs, after which LiteLLM counted tokens locally and
returned 200. Removing the nonexistent switch does not suppress those warnings;
local counts are estimates. No installed library files were patched.

## Verification

Targeted diagnostics (results go to `logs/smoke-results.json`):

```powershell
# Local health and real synthetic compression; no inference charges.
.\.litellm-venv\Scripts\python.exe smoke_check.py --health-only --compression
# A small billable request over both client protocols for one alias.
.\.litellm-venv\Scripts\python.exe smoke_check.py --model YOUR_ALIAS --protocol both
# Streaming probes reject SSE errors even under HTTP 200, and truncated streams.
.\.litellm-venv\Scripts\python.exe smoke_check.py --model YOUR_ALIAS --protocol both --stream
```

Use `--workers`, `--timeout`, `--base-url`, `--headroom-url` and `--root` to
target another deployment. `--model` can be repeated. Compression measurements
use synthetic repetitive tool logs and a protected last-user sentinel; their
savings do not predict every real workload.

```powershell
.\.litellm-venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pip check
.\.litellm-venv\Scripts\python.exe -m unittest discover -s tests -v
.\.litellm-venv\Scripts\python.exe smoke_check.py
```

The smoke check makes small billable requests for all four aliases and records
results in `logs/smoke-results.json`. Health and model listing alone do not
prove that inference is working. A fallback response also does not prove that
the preferred model recovered. Configuration backups are under `backups/`.

The unittest suite covers routing validation and rendering, the render CLI, the
editor API and the one-off migration round-trip.

## Latest live result (2026-10-20)

All four aliases passed the non-streaming `/v1/messages` check after restart.
The subsequent streaming check again received upstream 504 errors inside SSE.
Therefore the original streaming problem is not resolved by local configuration.
Use non-streaming requests where supported while relay.fast repairs its source,
or ask its support about the request IDs in `logs/relay-diagnosis.json`.
Single-message smoke requests have no older context to compress; a missing
compression header on those probes does not prove a compression failure.
