"""Load, validate and render the routing source of truth (models.yaml)."""
from pathlib import Path
import os
import tempfile

import yaml

DIALECTS = ("openai", "anthropic")

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
    providers = models.get("providers") or {}
    aliases = models.get("aliases") or []

    if not isinstance(providers, dict) or not providers:
        errors.append("providers: at least one provider is required")
        providers = {}
    if not isinstance(aliases, list) or not aliases:
        errors.append("aliases: at least one alias is required")
        return errors

    for provider_name, provider in providers.items():
        if not isinstance(provider, dict):
            errors.append("provider '%s': expected a mapping, got %s"
                          % (provider_name, _type_name(provider)))

    names = []
    for alias in aliases:
        if not isinstance(alias, dict):
            errors.append("alias entry: expected a mapping, got %s" % _type_name(alias))
            continue
        name = alias.get("name")
        if not name:
            errors.append("alias with no name")
            continue
        if name in names:
            errors.append("duplicate alias '%s'" % name)
        names.append(name)

    known = set(names)
    for alias in aliases:
        if not isinstance(alias, dict):
            continue
        name = alias.get("name")
        if not name:
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
            provider = providers.get(provider_name)
            if provider is None:
                errors.append("alias '%s': unknown provider '%s'" % (name, provider_name))
                continue
            if not isinstance(provider, dict):
                errors.append("alias '%s': provider '%s' is not a mapping"
                              % (name, provider_name))
                continue
            field = "chat_base" if dialect == "openai" else "messages_base"
            if not provider.get(field):
                errors.append("alias '%s': provider '%s' has no %s"
                              % (name, provider_name, field))
            if not dep.get("model"):
                errors.append("alias '%s': deployment has no model" % name)

    for alias in aliases:
        if not isinstance(alias, dict):
            continue
        name = alias.get("name")
        if not name:
            continue
        fallbacks = alias.get("fallbacks") or []
        if not isinstance(fallbacks, list):
            errors.append("alias '%s': fallbacks must be a list, got %s"
                          % (name, _type_name(fallbacks)))
            continue
        for target in fallbacks:
            if target == name:
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
        if not name:
            continue
        fallbacks = alias.get("fallbacks") or []
        if not isinstance(fallbacks, list):
            fallbacks = []
        graph[name] = [t for t in fallbacks
                       if t in known and t != name]

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
            base = provider["chat_base"] if dialect == "openai" else provider["messages_base"]
            params = {
                "model": "%s/%s" % (dialect, dep["model"]),
                "api_base": base,
                "api_key": "os.environ/%s" % _deployment_key_env(dep, provider),
                "order": dep.get("order", 1),
            }
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

    return {
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
                    "api_base": "http://127.0.0.1:8787",
                    "default_on": True,
                },
            }
        ],
        "router_settings": router_settings,
    }


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
