# LiteLLM + relay.fast + Headroom

Gateway: `http://127.0.0.1:8001`; Headroom: `http://127.0.0.1:8787`.
Start with `bat.bat` (or `powershell -ExecutionPolicy Bypass -File .\start.ps1`); stop with `stop-litellm-headroom.bat`.
The launcher reuses healthy running services. `start.ps1 -Restart` restarts only
LiteLLM; Headroom keeps running.

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
and calls the restart hook passed to `make_server`. There is no
`python editor.py` entrypoint yet; start the server with:

```powershell
.\.litellm-venv\Scripts\python.exe -c "import editor; editor.make_server('.').serve_forever()"
```

Without a restart hook, «Применить» does not restart LiteLLM; use
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
errors are not retried by the error-specific policy. The existing Kimi/Claude
fallback lists are preserved. There are no additional fallback routes for
DeepSeek or GPT. A failure after stream output starts cannot reliably be
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
