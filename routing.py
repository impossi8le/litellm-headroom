"""Load, validate and render the routing source of truth (models.yaml)."""
from pathlib import Path
import os
import math
import re
import tempfile
from urllib.parse import urlsplit

import yaml

DIALECTS = ("openai", "anthropic", "native")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _string(value):
    return isinstance(value, str) and bool(value.strip())


def _url(value):
    if not _string(value):
        return False
    try:
        parsed = urlsplit(value)
        parsed.port  # Validate malformed/out-of-range ports.
        return (parsed.scheme in ("http", "https") and bool(parsed.hostname)
                and not parsed.username and not parsed.password
                and not parsed.query and not parsed.fragment)
    except ValueError:
        return False


def _params_errors(params, label):
    if not isinstance(params, dict):
        return [label + ": params must be a mapping"]
    errors = []
    for key, value in params.items():
        if not isinstance(key, str):
            errors.append(label + ": parameter names must be strings")
            continue
        if key in ("model", "api_base", "api_key", "order"):
            errors.append(label + ": use dedicated fields instead of params." + key)
        lower = key.lower()
        sensitive = (any(part in lower for part in
                         ("secret", "password", "credential", "authorization", "api_key", "access_key"))
                     or lower == "token" or lower.endswith("_token"))
        if sensitive and not (isinstance(value, str) and value.startswith("os.environ/")
                              and ENV_NAME.fullmatch(value[11:])):
            errors.append(label + ": " + key + " must reference os.environ/NAME")
        if isinstance(value, dict):
            errors.extend(_params_errors(value, label + "." + key))
    return errors

RETRY_POLICY = {
    "AuthenticationErrorRetries": 0,
    "BadRequestErrorRetries": 0,
    "NotFoundErrorRetries": 0,
    "TimeoutErrorRetries": 0,
    "RateLimitErrorRetries": 1,
    "InternalServerErrorRetries": 1,
    "ServiceUnavailableErrorRetries": 1,
}


class ConfigError(Exception):
    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


def load_models(path):
    path = Path(path)
    if not path.exists():
        raise ConfigError(["%s: file not found" % path])
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(["%s: %s" % (path, exc)])
    if not isinstance(data, dict):
        raise ConfigError(["%s: expected a mapping at the top level" % path])
    return data


def _type_name(value):
    return type(value).__name__


def validate(models):
    errors = []
    if not isinstance(models, dict):
        return ["config: expected a mapping"]
    providers = models.get("providers") or {}
    aliases = models.get("aliases") or []

    timeouts = models.get("timeouts", {})
    if not isinstance(timeouts, dict):
        errors.append("timeouts: expected a mapping")
    else:
        for key, value in timeouts.items():
            if key not in ("request", "stream", "total", "cooldown", "retries"):
                errors.append("timeouts: unknown setting '%s'" % key)
            elif (isinstance(value, bool) or not isinstance(value, (int, float))
                  or not math.isfinite(value) or value < (0 if key == "retries" else 0.001)
                  or (key == "retries" and not isinstance(value, int))):
                errors.append("timeouts.%s: expected a %s number" %
                              (key, "non-negative integer" if key == "retries" else "positive finite"))
        request, total = timeouts.get("request", 120), timeouts.get("total", 300)
        if (type(request) in (int, float) and type(total) in (int, float)
                and total < request):
            errors.append("timeouts.total: must be at least timeouts.request")

    compression = models.get("compression", {})
    if not isinstance(compression, dict):
        errors.append("compression: expected a mapping")
    else:
        if "enabled" in compression and not isinstance(compression["enabled"], bool):
            errors.append("compression.enabled: expected boolean")
        if "api_base" in compression and not _url(compression["api_base"]):
            errors.append("compression.api_base: expected an HTTP(S) URL without credentials")
        if compression.get("unreachable_fallback", "fail_open") not in ("fail_open", "fail_closed"):
            errors.append("compression.unreachable_fallback: expected fail_open or fail_closed")
        value = compression.get("timeout", 15)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value <= 0):
            errors.append("compression.timeout: expected a positive finite number")

    if not isinstance(providers, dict) or not providers:
        errors.append("providers: at least one provider is required")
        providers = {}
    if not isinstance(aliases, list) or not aliases:
        errors.append("aliases: at least one alias is required")
        return errors

    for provider_name, provider in providers.items():
        if not _string(provider_name):
            errors.append("provider name: expected a non-empty string")
        if not isinstance(provider, dict):
            errors.append("provider '%s': expected a mapping, got %s"
                          % (provider_name, _type_name(provider)))
            continue
        label = "provider '%s'" % provider_name
        for field in ("chat_base", "messages_base", "api_base"):
            if provider.get(field) and not _url(provider[field]):
                errors.append(label + ": " + field + " must be an HTTP(S) URL without credentials")
        if provider.get("key_env") and not (isinstance(provider["key_env"], str)
                                           and ENV_NAME.fullmatch(provider["key_env"])):
            errors.append(label + ": key_env must name an environment variable")
        if "anonymous" in provider and not isinstance(provider["anonymous"], bool):
            errors.append(label + ": anonymous must be boolean")
        errors.extend(_params_errors(provider.get("params", {}), label))

    names = []
    for alias in aliases:
        if not isinstance(alias, dict):
            errors.append("alias entry: expected a mapping, got %s" % _type_name(alias))
            continue
        name = alias.get("name")
        if not _string(name):
            errors.append("alias with no name: expected a non-empty string")
            continue
        if name in names:
            errors.append("duplicate alias '%s'" % name)
        names.append(name)

    known = set(names)
    for alias in aliases:
        if not isinstance(alias, dict):
            continue
        name = alias.get("name")
        if not _string(name):
            continue
        deployments = alias.get("deployments")
        if not isinstance(deployments, list) or not deployments:
            errors.append("alias '%s': at least one deployment is required" % name)
            continue
        for dep in deployments:
            if not isinstance(dep, dict):
                errors.append("alias '%s': deployment entry: expected a mapping, got %s"
                              % (name, _type_name(dep)))
                continue
            order = dep.get("order")
            if isinstance(order, bool) or not isinstance(order, int) or order < 1:
                errors.append("alias '%s': deployment order must be a positive integer (got %r)"
                              % (name, order))
            dialect = dep.get("dialect")
            if dialect not in DIALECTS:
                errors.append("alias '%s': unknown dialect %r (expected one of %s)"
                              % (name, dialect, ", ".join(DIALECTS)))
                continue
            provider_name = dep.get("provider")
            provider = providers.get(provider_name) if isinstance(provider_name, str) else None
            if provider is None:
                errors.append("alias '%s': unknown provider '%s'" % (name, provider_name))
                continue
            if not isinstance(provider, dict):
                errors.append("alias '%s': provider '%s' is not a mapping"
                              % (name, provider_name))
                continue
            field = "chat_base" if dialect == "openai" else "messages_base"
            if dialect != "native" and not (provider.get(field) or provider.get("api_base")):
                errors.append("alias '%s': provider '%s' has no %s"
                              % (name, provider_name, field))
            if not _string(dep.get("model")):
                errors.append("alias '%s': deployment has no model" % name)
            elif dialect == "native" and ("/" not in dep["model"] or not all(dep["model"].split("/", 1))):
                errors.append("alias '%s': native model must include provider/model prefix" % name)
            key_env = _deployment_key_env(dep, provider)
            if key_env and not (isinstance(key_env, str) and ENV_NAME.fullmatch(key_env)):
                errors.append("alias '%s': key_env must name an environment variable" % name)
            if dialect != "native" and not key_env and not provider.get("anonymous"):
                errors.append("alias '%s': key_env is required (or set provider anonymous: true)" % name)
            errors.extend(_params_errors(dep.get("params", {}), "alias '%s'" % name))
        if "model_info" in alias and not isinstance(alias["model_info"], dict):
            errors.append("alias '%s': model_info must be a mapping" % name)

    for alias in aliases:
        if not isinstance(alias, dict):
            continue
        name = alias.get("name")
        if not _string(name):
            continue
        fallbacks = alias.get("fallbacks") or []
        if not isinstance(fallbacks, list):
            errors.append("alias '%s': fallbacks must be a list, got %s"
                          % (name, _type_name(fallbacks)))
            continue
        for target in fallbacks:
            if not _string(target):
                errors.append("alias '%s': fallback target must be a non-empty string" % name)
            elif target == name:
                errors.append("alias '%s': fallback points at itself" % name)
            elif target not in known:
                errors.append("alias '%s': unknown fallback target '%s'" % (name, target))

    errors.extend(_cycles(aliases, known))
    return errors


def _cycles(aliases, known):
    graph = {}
    for alias in aliases:
        if not isinstance(alias, dict):
            continue
        name = alias.get("name")
        if not _string(name):
            continue
        fallbacks = alias.get("fallbacks") or []
        if not isinstance(fallbacks, list):
            fallbacks = []
        graph[name] = [t for t in fallbacks
                       if isinstance(t, str) and t in known and t != name]

    WHITE, GREY, BLACK = 0, 1, 2
    colour = dict.fromkeys(graph, WHITE)
    errors = []

    def visit(node, stack):
        colour[node] = GREY
        stack.append(node)
        for nxt in graph.get(node, []):
            if colour.get(nxt) == GREY:
                loop = stack[stack.index(nxt):] + [nxt]
                errors.append("aliases: cycle: " + " -> ".join(loop))
            elif colour.get(nxt) == WHITE:
                visit(nxt, stack)
        stack.pop()
        colour[node] = BLACK

    for node in graph:
        if colour[node] == WHITE:
            visit(node, [])
    return errors


def load_and_validate(path):
    models = load_models(path)
    errors = validate(models)
    if errors:
        raise ConfigError(errors)
    return models


def _deployment_key_env(dep, provider):
    return dep.get("key_env") or provider.get("key_env")


def render_config(models):
    errors = validate(models)
    if errors:
        raise ConfigError(errors)

    providers = models["providers"]
    timeouts = models.get("timeouts") or {}
    model_list = []
    fallbacks = []

    for alias in models["aliases"]:
        name = alias["name"]
        for dep in alias["deployments"]:
            provider = providers[dep["provider"]]
            dialect = dep["dialect"]
            base = provider.get("api_base")
            if dialect != "native":
                base = provider.get("chat_base" if dialect == "openai" else "messages_base") or base
            params = dict(provider.get("params", {}))
            params.update(dep.get("params", {}))
            params["model"] = dep["model"] if dialect == "native" else "%s/%s" % (dialect, dep["model"])
            params["order"] = dep["order"]
            if base:
                params["api_base"] = base.rstrip("/")
            key_env = _deployment_key_env(dep, provider)
            if key_env:
                params["api_key"] = "os.environ/%s" % key_env
            elif provider.get("anonymous"):
                params["api_key"] = "not-needed"
            entry = {"model_name": name, "litellm_params": params}
            if alias.get("model_info"):
                entry["model_info"] = dict(alias["model_info"])
            model_list.append(entry)
        fallbacks.append({name: list(alias.get("fallbacks") or [])})

    router_settings = {
        "num_retries": timeouts.get("retries", 1),
        "fallbacks": fallbacks,
        "timeout": timeouts.get("total", 300),
        "stream_timeout": timeouts.get("stream", 120),
        "cooldown_time": timeouts.get("cooldown", 5),
        "retry_policy": dict(RETRY_POLICY),
    }

    config = {
        "model_list": model_list,
        "litellm_settings": {
            "use_chat_completions_url_for_anthropic_messages": True,
            "drop_params": True,
            "request_timeout": timeouts.get("request", 120),
        },
        "general_settings": {
            "disable_ui": True,
            "master_key": "os.environ/LITELLM_MASTER_KEY",
        },
        "guardrails": [
            {
                "guardrail_name": "headroom-compression",
                "litellm_params": {
                    "guardrail": "headroom",
                    "mode": "pre_call",
                    "api_base": (models.get("compression") or {}).get("api_base", "http://127.0.0.1:8787"),
                    "default_on": True,
                    "timeout": (models.get("compression") or {}).get("timeout", 15),
                    "unreachable_fallback": (models.get("compression") or {}).get("unreachable_fallback", "fail_open"),
                },
            }
        ],
        "router_settings": router_settings,
    }
    if not (models.get("compression") or {}).get("enabled", True):
        config.pop("guardrails")
    return config


def write_config(models, path):
    path = Path(path)
    errors = validate(models)
    if errors:
        raise ConfigError(errors)
    text = yaml.safe_dump(render_config(models), sort_keys=False, allow_unicode=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
