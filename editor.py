"""Local HTTP API for editing and applying models.yaml.

Run a server with ``make_server(root)``; it binds to the loopback interface by
default and refuses requests whose Host header is not a loopback name.
"""
import json
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

import routing

ALLOWED_HOSTS = ("127.0.0.1", "localhost")
CATALOG_TIMEOUT = 5


def _write_models(path, models):
    """Atomically write the models mapping (the source of truth)."""
    path = Path(path)
    text = yaml.safe_dump(models, sort_keys=False, allow_unicode=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


class _Handler(BaseHTTPRequestHandler):
    server_version = "HeadroomEditor/1.0"

    # -- plumbing ---------------------------------------------------------

    def log_message(self, fmt, *args):  # keep test output quiet
        pass

    def _host_allowed(self):
        raw = self.headers.get("Host")
        if not raw:
            return False
        try:
            hostname = urllib.parse.urlsplit("//" + raw).hostname
        except ValueError:
            return False
        return hostname in ALLOWED_HOSTS

    def _send(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _text(self, status, text, content_type="text/plain; charset=utf-8"):
        self._send(status, text.encode("utf-8"), content_type)

    def _read_json(self):
        """Return (mapping, None) or (None, error message)."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None, "invalid Content-Length"
        if length <= 0:
            return None, "request body is required"
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None, "invalid JSON"
        if not isinstance(data, dict):
            return None, "expected a JSON object"
        return data, None

    def _models_path(self):
        return self.server.root / "models.yaml"

    def _load(self):
        """Return (models, None) or (None, error payload)."""
        try:
            return routing.load_and_validate(self._models_path()), None
        except routing.ConfigError as exc:
            return None, {"errors": exc.errors}

    # -- routes -----------------------------------------------------------

    def do_GET(self):
        if not self._host_allowed():
            return self._json(403, {"error": "forbidden host"})
        path = urllib.parse.urlsplit(self.path).path
        if path == "/":
            return self._page()
        if path == "/api/config":
            return self._get_config()
        if path == "/api/catalog":
            return self._catalog()
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_allowed():
            return self._json(403, {"error": "forbidden host"})
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/config":
            return self._post_config()
        if path == "/api/render":
            return self._post_render()
        if path == "/api/apply":
            return self._post_apply()
        return self._json(404, {"error": "not found"})

    def _page(self):
        page = self.server.root / "editor_page.html"
        if not page.exists():
            return self._text(500, "editor_page.html is missing from %s" % self.server.root)
        self._text(200, page.read_text(encoding="utf-8"), "text/html; charset=utf-8")

    def _get_config(self):
        models, error = self._load()
        if error:
            return self._json(400, error)
        self._json(200, models)

    def _post_config(self):
        models, error = self._read_json()
        if error:
            return self._json(400, {"error": error})
        errors = routing.validate(models)
        if errors:
            return self._json(400, {"errors": errors})
        _write_models(self._models_path(), models)
        self._json(200, {"ok": True})

    def _post_render(self):
        models, error = self._read_json()
        if error:
            return self._json(400, {"error": error})
        try:
            rendered = routing.render_config(models)
        except routing.ConfigError as exc:
            return self._json(400, {"errors": exc.errors})
        self._json(200, rendered)

    def _post_apply(self):
        models, error = self._read_json()
        if error:
            return self._json(400, {"error": error})
        errors = routing.validate(models)
        if errors:
            return self._json(400, {"errors": errors})
        root = self.server.root
        _write_models(root / "models.yaml", models)
        try:
            routing.write_config(models, root / "litellm-config.yaml")
        except routing.ConfigError as exc:
            return self._json(400, {"errors": exc.errors})
        restart = self.server.restart
        if restart is not None:
            try:
                restart()
            except Exception as exc:  # noqa: BLE001 - report, don't crash the request
                return self._json(500, {"error": "restart failed: %s" % exc})
        self._json(200, {"ok": True})

    def _catalog(self):
        models, error = self._load()
        if error:
            return self._json(400, error)

        chosen = None
        for name, provider in (models.get("providers") or {}).items():
            if not isinstance(provider, dict):
                continue
            key_env = provider.get("key_env")
            if key_env and os.environ.get(key_env):
                chosen = (name, provider)
                break
        if chosen is None:
            return self._json(400, {"error": "no provider has its API key set"})

        name, provider = chosen
        base = (provider.get("chat_base") or "").rstrip("/")
        if not base:
            return self._json(502, {"error": "provider %r has no chat_base" % name, "status": 502})

        try:
            with urllib.request.urlopen(base + "/models", timeout=CATALOG_TIMEOUT) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            return self._json(502, {
                "error": "upstream returned HTTP %d %s" % (exc.code, exc.reason),
                "status": exc.code,
            })
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            return self._json(502, {"error": "upstream request failed: %s" % reason, "status": None})

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return self._json(502, {"error": "upstream returned invalid JSON", "status": 502})

        data = payload.get("data") if isinstance(payload, dict) else None
        self._json(200, {"provider": name, "models": data if data is not None else payload})


def make_server(root, host="127.0.0.1", port=8788, restart=None):
    server = ThreadingHTTPServer((host, port), _Handler)
    server.daemon_threads = True
    server.root = Path(root).resolve()
    server.restart = restart
    return server
