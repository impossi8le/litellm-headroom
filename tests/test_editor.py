import json, os, threading, unittest, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
import tempfile
import yaml
import editor

CFG = {
    "providers": {"p1": {"key_env": "K1", "chat_base": "https://a/v1", "messages_base": "https://a"}},
    "timeouts": {"request": 120, "stream": 120, "total": 300, "retries": 1, "cooldown": 5},
    "aliases": [{"name": "m1", "deployments": [
        {"provider": "p1", "model": "x", "dialect": "openai", "order": 1}], "fallbacks": []}],
}


class _RecordingHandler(BaseHTTPRequestHandler):
    requests = []

    def do_GET(self):
        type(self).requests.append({"path": self.path, "headers": dict(self.headers)})
        body = b'{"data": [{"id": "model-a"}]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class EditorTests(unittest.TestCase):
    def test_apply_failure_restores_both_files_and_restarts_old_config(self):
        original = (self.root / "models.yaml").read_bytes()
        generated = self.root / "litellm-config.yaml"
        generated.write_bytes(b"old config\n")
        changed = yaml.safe_load(yaml.safe_dump(CFG))
        changed["aliases"][0]["deployments"][0]["model"] = "new"
        self.server.restart = mock.Mock(side_effect=[False, True])
        body = json.loads(self.post("/api/apply", changed).read())
        self.assertTrue(body["rolled_back"])
        self.assertTrue(body["rollback_restarted"])
        self.assertEqual((self.root / "models.yaml").read_bytes(), original)
        self.assertEqual(generated.read_bytes(), b"old config\n")
        self.assertEqual(self.server.restart.call_count, 2)
        self.assertEqual(len(list((self.root / "backups").glob("apply_*"))), 1)

    def test_different_local_port_origin_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/config", CFG, headers={"Origin": "http://127.0.0.1:1"})
        self.assertEqual(ctx.exception.code, 403)

    def test_bind_public_interface_rejected(self):
        with self.assertRaises(ValueError):
            editor.make_server(self.root, host="0.0.0.0", port=0)

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

    def post(self, path, payload, headers=None):
        merged = {"Content-Type": "application/json"}
        if headers:
            merged.update(headers)
        req = urllib.request.Request(self.base + path,
                                     data=json.dumps(payload).encode(),
                                     headers=merged)
        return urllib.request.urlopen(req, timeout=5)

    def test_get_config_returns_models(self):
        body = json.loads(urllib.request.urlopen(self.base + "/api/config", timeout=5).read())
        self.assertEqual(body["aliases"][0]["name"], "m1")

    def test_post_config_saves(self):
        payload = yaml.safe_load(yaml.safe_dump(CFG))
        payload["aliases"][0]["name"] = "m1-renamed"
        self.post("/api/config", payload)
        saved = yaml.safe_load((self.root / "models.yaml").read_text(encoding="utf-8"))
        self.assertEqual(saved["aliases"][0]["name"], "m1-renamed")

    def test_post_invalid_config_returns_400_and_does_not_save(self):
        bad = yaml.safe_load(yaml.safe_dump(CFG))
        bad["aliases"][0]["fallbacks"] = ["nope"]
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/config", bad)
        self.assertEqual(ctx.exception.code, 400)
        saved = yaml.safe_load((self.root / "models.yaml").read_text(encoding="utf-8"))
        self.assertEqual(saved["aliases"][0]["fallbacks"], [])

    def test_post_nested_non_mapping_returns_400(self):
        bad = yaml.safe_load(yaml.safe_dump(CFG))
        bad["providers"]["p1"] = "oops"
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/config", bad)
        self.assertEqual(ctx.exception.code, 400)
        self.assertTrue(json.loads(ctx.exception.read())["errors"])

    def test_apply_renders_and_calls_restart(self):
        called = []
        self.server.shutdown(); self.server.server_close()
        self.start(editor.make_server(self.root, port=0, restart=lambda: called.append(1)))
        self.post("/api/apply", CFG)
        self.assertTrue((self.root / "litellm-config.yaml").exists())
        self.assertEqual(called, [1])

    def test_apply_oserror_returns_500(self):
        with mock.patch.object(editor.routing, "write_config", side_effect=OSError("disk full")):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.post("/api/apply", CFG)
        self.assertEqual(ctx.exception.code, 500)

    def test_catalog_without_any_key_returns_400(self):
        with mock.patch.dict(os.environ, {"K1": ""}):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(self.base + "/api/catalog", timeout=5)
        self.assertEqual(ctx.exception.code, 400)

    def test_catalog_forwards_bearer_token(self):
        _RecordingHandler.requests = []
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), _RecordingHandler)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        try:
            cfg = yaml.safe_load(yaml.safe_dump(CFG))
            cfg["providers"]["p1"]["chat_base"] = (
                "http://127.0.0.1:%d/v1" % upstream.server_address[1])
            self.post("/api/config", cfg)

            with mock.patch.dict(os.environ, {"K1": "sekret"}):
                body = json.loads(
                    urllib.request.urlopen(self.base + "/api/catalog", timeout=5).read())

            self.assertEqual(body["models"], [{"id": "model-a"}])
            self.assertEqual(len(_RecordingHandler.requests), 1)
            recorded = _RecordingHandler.requests[0]
            self.assertEqual(recorded["path"], "/v1/models")
            self.assertEqual(recorded["headers"].get("Authorization"), "Bearer sekret")
        finally:
            upstream.shutdown()
            upstream.server_close()

    def test_catalog_without_chat_base_returns_400(self):
        cfg = yaml.safe_load(yaml.safe_dump(CFG))
        cfg["aliases"][0]["deployments"][0]["dialect"] = "anthropic"
        del cfg["providers"]["p1"]["chat_base"]
        self.post("/api/config", cfg)
        with mock.patch.dict(os.environ, {"K1": "sekret"}):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(self.base + "/api/catalog", timeout=5)
        self.assertEqual(ctx.exception.code, 400)

    def test_rejects_foreign_host_header(self):
        req = urllib.request.Request(self.base + "/api/config", headers={"Host": "evil.example"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 403)

    def test_rejects_foreign_origin(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/config", CFG, headers={"Origin": "https://evil.example"})
        self.assertEqual(ctx.exception.code, 403)

    def test_rejects_non_json_content_type(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/config", CFG, headers={"Content-Type": "text/plain"})
        self.assertEqual(ctx.exception.code, 415)

    def test_accepts_loopback_origin_with_json(self):
        port = self.server.server_address[1]
        response = self.post("/api/config", CFG,
                             headers={"Origin": "http://127.0.0.1:%d" % port})
        self.assertEqual(response.status, 200)

    def test_apply_without_restart_reports_null(self):
        body = json.loads(self.post("/api/apply", CFG).read())
        self.assertTrue(body["rendered"])
        self.assertIsNone(body["restarted"])

    def test_apply_with_successful_restart_reports_true(self):
        self.server.shutdown(); self.server.server_close()
        self.start(editor.make_server(self.root, port=0, restart=lambda: True))
        body = json.loads(self.post("/api/apply", CFG).read())
        self.assertTrue(body["rendered"])
        self.assertIs(body["restarted"], True)
