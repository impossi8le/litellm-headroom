"""Local HTTP API for editing and applying models.yaml.

Run ``python editor.py`` (or ``make_server(root)``) to serve the editor; it
binds to the loopback interface by default and refuses requests whose Host
header is not a loopback name, as well as cross-origin requests and non-JSON
POST bodies.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import datetime
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

import routing

ALLOWED_HOSTS = ("127.0.0.1", "localhost")
CATALOG_TIMEOUT = 5
MAX_BODY_BYTES = 2 * 1024 * 1024


def _origin_allowed(raw):
    """True when an Origin header names a loopback host (or is absent)."""
    if raw is None:
        return True
    try:
        hostname = urllib.parse.urlsplit(raw).hostname
    except ValueError:
        return False
    return hostname in ALLOWED_HOSTS


def _is_json_content_type(raw):
    if not raw:
        return False
    return raw.split(";", 1)[0].strip().lower() == "application/json"


def make_restart(root):
    """Return a hook that restarts LiteLLM via ``start.ps1 -Restart``."""
    root = Path(root).resolve()

    def restart():
        command = [
            "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(root / "start.ps1"), "-Restart",
        ]
        completed = subprocess.run(command, cwd=str(root))
        return completed.returncode == 0

    return restart


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


def _write_bytes(path, content):
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


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

    def _guard(self):
        """Reject foreign hosts, cross-origin requests and non-JSON bodies.

        Returns True when the request may proceed; otherwise a response has
        already been sent.
        """
        if not self._host_allowed():
            self._json(403, {"error": "forbidden host"})
            return False
        if not _origin_allowed(self.headers.get("Origin")):
            self._json(403, {"error": "forbidden origin"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin.rstrip("/") != "http://" + self.headers.get("Host", ""):
            self._json(403, {"error": "same origin required"})
            return False
        if self.command == "POST" and not _is_json_content_type(
                self.headers.get("Content-Type")):
            self._json(415, {"error": "Content-Type must be application/json"})
            return False
        return True

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
        if length > MAX_BODY_BYTES:
            return None, "request body exceeds 2 MiB"
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
        if not self._guard():
            return
        path = urllib.parse.urlsplit(self.path).path
        if path == "/":
            return self._page()
        if path == "/api/config":
            return self._get_config()
        if path == "/api/catalog":
            return self._catalog()
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._guard():
            return
        path = urllib.parse.urlsplit(self.path).path
        with self.server.mutation_lock:
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
        try:
            _write_models(self._models_path(), models)
        except OSError:
            return self._json(500, {"error": "cannot write models.yaml"})
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
        try:
            previous = {p: p.read_bytes() if p.exists() else None for p in
                        (root / "models.yaml", root / "litellm-config.yaml")}
        except OSError:
            return self._json(500, {"error": "cannot read previous configuration; nothing applied"})

        def restore():
            for path, content in previous.items():
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    _write_bytes(path, content)

        try:
            backup = root / "backups" / datetime.datetime.now(datetime.timezone.utc).strftime("apply_%Y%m%d_%H%M%S_%f")
            backup.mkdir(parents=True)
            for path, content in previous.items():
                if content is not None:
                    (backup / path.name).write_bytes(content)
            _write_models(root / "models.yaml", models)
            routing.write_config(models, root / "litellm-config.yaml")
        except routing.ConfigError as exc:
            return self._json(400, {"errors": exc.errors})
        except OSError:
            try:
                restore()
            except OSError:
                return self._json(500, {"error": "apply and restore failed; recover from backups before restarting"})
            return self._json(500, {"error": "apply failed; previous files restored"})
        restart = self.server.restart
        if restart is None:
            return self._json(200, {
                "rendered": True,
                "restarted": None,
                "restart_error": None,
            })
        try:
            restarted = restart() is not False
            restart_error = None if restarted else "restart hook reported failure"
        except Exception:  # noqa: BLE001 - report, don't crash the request
            restarted = False
            restart_error = "restart hook failed"
        rolled_back = False
        rollback_restarted = None
        if not restarted:
            try:
                restore()
            except OSError:
                return self._json(500, {"error": "restart and restore failed; recover from backups"})
            rolled_back = True
            try:
                rollback_restarted = restart() is not False
            except Exception:
                rollback_restarted = False
        self._json(200, {
            "rendered": True,
            "restarted": restarted,
            "restart_error": restart_error,
            "rolled_back": rolled_back,
            "rollback_restarted": rollback_restarted,
        })

    def _catalog(self):
        models, error = self._load()
        if error:
            return self._json(400, error)

        chosen = None
        for name, provider in (models.get("providers") or {}).items():
            if not isinstance(provider, dict):
                continue
            key_env = provider.get("key_env")
            key = os.environ.get(key_env) if key_env else None
            if key:
                chosen = (name, provider, key)
                break
        if chosen is None:
            return self._json(400, {"error": "no provider has its API key set"})

        name, provider, key = chosen
        base = (provider.get("chat_base") or "").rstrip("/")
        if not base:
            return self._json(400, {"error": "provider %r has no chat_base" % name})

        request = urllib.request.Request(base + "/models",
                                         headers={"Authorization": "Bearer %s" % key})
        try:
            with urllib.request.urlopen(request, timeout=CATALOG_TIMEOUT) as resp:
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
    if host not in ALLOWED_HOSTS:
        raise ValueError("editor must bind to loopback")
    server = ThreadingHTTPServer((host, port), _Handler)
    server.daemon_threads = True
    server.root = Path(root).resolve()
    server.restart = restart
    server.mutation_lock = threading.RLock()
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local editor for models.yaml.")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent),
                        help="directory holding models.yaml (default: the script directory)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--no-restart", action="store_true",
                        help="do not restart LiteLLM after «Применить»")
    parser.add_argument("--no-browser", action="store_true",
                        help="do not open the editor page in a browser")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    restart = None if args.no_restart else make_restart(root)
    server = make_server(root, host=args.host, port=args.port, restart=restart)
    host, port = server.server_address[:2]
    url = "http://%s:%d/" % (host, port)
    print("Editor: %s (root %s)" % (url, root), flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
