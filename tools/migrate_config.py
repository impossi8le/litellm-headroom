"""One-off: turn a rendered litellm-config.yaml into models.yaml."""
import argparse
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import routing

HEADER = """\
# Source of truth for routing. render.py turns this into litellm-config.yaml.
# Holds no credentials: key_env names an environment variable.
"""


def _slug(host):
    slug = re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-")
    return slug or "provider"


def _provider_name(api_base):
    host = re.sub(r"^https?://", "", api_base or "").split("/")[0]
    return _slug(host)


def migrate(config):
    model_list = config.get("model_list") or []
    providers = {}
    aliases = []
    order_by_base = {}

    for entry in model_list:
        params = entry.get("litellm_params") or {}
        model = params.get("model") or ""
        dialect = "anthropic" if model.startswith("anthropic/") else "openai"
        upstream = model.split("/", 1)[1] if "/" in model else model
        api_base = params.get("api_base") or ""
        name = _provider_name(api_base)

        provider = providers.setdefault(name, {"key_env": "RELAY_FAST_API_KEY"})
        field = "chat_base" if dialect == "openai" else "messages_base"
        provider.setdefault(field, api_base)

        group = entry.get("model_name")
        tier_key = (group, api_base)
        if tier_key not in order_by_base:
            order_by_base[tier_key] = len({k[0] for k in order_by_base if k[0] == group}) + 1

        deployment = {
            "provider": name,
            "model": upstream,
            "dialect": dialect,
            "order": order_by_base[tier_key],
        }
        alias = next((a for a in aliases if a["name"] == group), None)
        if alias is None:
            alias = {"name": group, "deployments": [], "fallbacks": []}
            if entry.get("model_info"):
                alias["model_info"] = dict(entry["model_info"])
            aliases.append(alias)
        alias["deployments"].append(deployment)

    fallbacks = (config.get("router_settings") or {}).get("fallbacks") or []
    for item in fallbacks:
        for name, targets in item.items():
            alias = next((a for a in aliases if a["name"] == name), None)
            if alias is not None:
                alias["fallbacks"] = list(targets or [])

    timeouts = {
        "request": (config.get("litellm_settings") or {}).get("request_timeout", 120),
        "stream": (config.get("router_settings") or {}).get("stream_timeout", 120),
        "total": (config.get("router_settings") or {}).get("timeout", 300),
        "retries": (config.get("router_settings") or {}).get("num_retries", 1),
        "cooldown": (config.get("router_settings") or {}).get("cooldown_time", 5),
    }

    break_cycles(aliases)
    return {"providers": providers, "timeouts": timeouts, "aliases": aliases}


def break_cycles(aliases):
    """Drop the edge that closes each fallback cycle; LiteLLM cannot express one."""
    by_name = {a["name"]: a for a in aliases}
    while True:
        path = _find_cycle(aliases)
        if path is None:
            return
        loop = path[:-1]
        closing_from = loop[-1]
        closing_to = loop[0]
        targets = by_name[closing_from]["fallbacks"]
        targets.remove(closing_to)
        print("warning: dropped fallback %s -> %s to break a cycle"
              % (closing_from, closing_to), file=sys.stderr)


def _find_cycle(aliases):
    graph = {a["name"]: [t for t in a["fallbacks"] if t in {b["name"] for b in aliases}]
             for a in aliases}
    seen = set()
    stack = []

    def visit(node):
        if node in stack:
            return stack[stack.index(node):] + [node]
        if node in seen:
            return None
        stack.append(node)
        for nxt in graph.get(node, []):
            found = visit(nxt)
            if found:
                return found
        stack.pop()
        seen.add(node)
        return None

    for node in graph:
        found = visit(node)
        if found:
            return found
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    args = parser.parse_args(argv)
    root = Path(args.root)

    live = yaml.safe_load((root / "litellm-config.yaml").read_text(encoding="utf-8"))
    models = migrate(live)
    errors = routing.validate(models)
    if errors:
        for error in errors:
            print("error: %s" % error, file=sys.stderr)
        return 1

    text = HEADER + yaml.safe_dump(models, sort_keys=False, allow_unicode=True)
    (root / "models.yaml").write_text(text, encoding="utf-8")
    print("wrote %s" % (root / "models.yaml"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
