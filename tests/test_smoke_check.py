"""Tests for the smoke check.

Everything here is local: the HTTP layer is faked, no request ever leaves the
machine and no paid inference is performed.
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import smoke_check  # noqa: E402

SECRET = 'sk-do-not-print-0123456789'


class FakeResponse:
    """Minimal stand-in for a requests response."""

    def __init__(self, status_code=200, payload=None, lines=None, text=''):
        self.status_code = status_code
        self._payload = payload
        self._lines = list(lines or [])
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError('not json')
        return self._payload

    def iter_lines(self):
        return iter(self._lines)


class FakeSession:
    """Fake requests.Session that records calls and replays canned responses."""

    def __init__(self, responses=None, errors=None):
        self.responses = dict(responses or {})
        self.errors = dict(errors or {})
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(('GET', url, kwargs))
        return self._respond(url)

    def post(self, url, **kwargs):
        self.calls.append(('POST', url, kwargs))
        return self._respond(url)

    def urls(self, method=None):
        return [url for verb, url, _ in self.calls if method is None or verb == method]

    def _respond(self, url):
        for pattern, error in self.errors.items():
            if pattern in url:
                raise error
        for pattern, response in self.responses.items():
            if pattern in url:
                return response
        return FakeResponse(status_code=404, payload={'error': {'type': 'not_found'}})


def sse(*pairs):
    """Build SSE lines from (event|None, data) pairs."""
    lines = []
    for name, data in pairs:
        if name:
            lines.append('event: %s' % name)
        lines.append('data: %s' % data)
    return lines


MESSAGES_BODY = {
    'type': 'message',
    'model': 'kimi-k3',
    'content': [{'type': 'text', 'text': 'OK'}],
}

CHAT_BODY = {
    'model': 'kimi-k3',
    'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'OK'},
                 'finish_reason': 'stop'}],
}

COMPRESS_BODY = {
    'messages': [
        {'role': 'user', 'content': 'Read the build log and list the failing steps.'},
        {'role': 'assistant', 'content': '',
         'tool_calls': [{'id': 'call_smoke_1', 'type': 'function',
                         'function': {'name': 'read_log', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'call_smoke_1', 'content': 'PASS suite=module_00'},
        {'role': 'user', 'content': smoke_check.SENTINEL + ' Summarise the failing steps.'},
    ],
    'tokens_before': 4000,
    'tokens_after': 500,
    'tokens_saved': 3500,
    'compression_ratio': 0.125,
}


def default_responses():
    return {
        'http://127.0.0.1:8787/health': FakeResponse(status_code=200, payload={'status': 'ok'}),
        'http://127.0.0.1:8001/health/liveliness': FakeResponse(status_code=200, payload={'status': 'healthy'}),
        'http://127.0.0.1:8001/v1/models': FakeResponse(status_code=200, payload={'data': []}),
        'http://127.0.0.1:8001/v1/messages': FakeResponse(status_code=200, payload=MESSAGES_BODY),
        'http://127.0.0.1:8001/v1/chat/completions': FakeResponse(status_code=200, payload=CHAT_BODY),
        'http://127.0.0.1:8787/v1/compress': FakeResponse(status_code=200, payload=COMPRESS_BODY),
    }


class FakeGatewayHandler(BaseHTTPRequestHandler):
    """A local HTTP server that speaks the LiteLLM and Headroom wire formats.

    The model name selects the behaviour, so one server covers the healthy,
    the truncated, the error-event and the failing case.
    """

    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):  # keep the test output clean
        pass

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode('utf-8')
        self.send_response(status)
        self.send_header('content-type', 'application/json')
        self.send_header('content-length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_sse(self, events):
        body = ''.join('%s\n\n' % block for block in events).encode('utf-8')
        self.send_response(200)
        self.send_header('content-type', 'text/event-stream')
        self.send_header('content-length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.endswith('/health/liveliness'):
            self._send_json(200, {'status': 'healthy'})
        elif self.path.endswith('/health'):
            self._send_json(200, {'status': 'ok'})
        elif self.path.endswith('/v1/models'):
            self._send_json(200, {'data': [{'id': 'alias-a'}]})
        else:
            self._send_json(404, {'error': {'type': 'not_found'}})

    def do_POST(self):
        length = int(self.headers.get('content-length') or 0)
        try:
            body = json.loads(self.rfile.read(length) or b'{}')
        except ValueError:
            body = {}
        model = body.get('model')
        if self.path.endswith('/v1/compress'):
            self._send_json(200, COMPRESS_BODY)
            return
        if self.path.endswith('/v1/messages'):
            if body.get('stream'):
                self._send_sse(self._message_events(model))
                return
            if model == 'http-500':
                self._send_json(500, {'error': {'type': 'internal_server_error'}})
                return
            self._send_json(200, MESSAGES_BODY)
            return
        if self.path.endswith('/v1/chat/completions'):
            if body.get('stream'):
                self._send_sse(self._chat_events(model))
                return
            self._send_json(200, CHAT_BODY)
            return
        self._send_json(404, {'error': {'type': 'not_found'}})

    @staticmethod
    def _message_events(model):
        start = ('event: message_start\ndata: %s'
                 % json.dumps({'type': 'message_start', 'message': {'model': 'kimi-k3'}}))
        delta = ('event: content_block_delta\ndata: %s'
                 % json.dumps({'type': 'content_block_delta', 'delta': {'text': 'OK'}}))
        if model == 'error-event':
            return [start, 'event: error\ndata: %s'
                    % json.dumps({'type': 'error', 'error': {'type': 'overloaded_error'}})]
        if model == 'truncated':
            return [start, delta]
        return [start, delta, 'event: message_stop\ndata: %s'
                % json.dumps({'type': 'message_stop'})]

    @staticmethod
    def _chat_events(model):
        chunk = ('data: %s' % json.dumps({'model': 'kimi-k3',
                                          'choices': [{'delta': {'content': 'OK'}}]}))
        if model == 'chat-truncated':
            return [chunk]
        return [chunk, 'data: [DONE]']


class TempRootCase(unittest.TestCase):
    """Base class providing a throwaway project root."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def write_project(self, master_key='sk-test-master', models=('alias-a', 'alias-b'),
                      env_name='LITELLM_MASTER_KEY'):
        config = {
            'model_list': [{'model_name': name} for name in models],
            'general_settings': {'master_key': 'os.environ/%s' % env_name},
            'guardrails': [{
                'guardrail_name': 'headroom-compression',
                'litellm_params': {'guardrail': 'headroom', 'api_base': 'http://127.0.0.1:8787'},
            }],
        }
        (self.root / 'litellm-config.yaml').write_text(
            yaml.safe_dump(config), encoding='utf-8')
        if master_key is not None:
            (self.root / '.env').write_text('%s=%s\n' % (env_name, master_key), encoding='utf-8')
        return self.root

    def run_main(self, argv, session=None):
        """Call main() with stdout captured, so the test output stays clean."""
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = smoke_check.main(argv, session=session)
        return code, stdout.getvalue()


class ImportSafetyTests(unittest.TestCase):
    def test_import_has_no_side_effects(self):
        """Importing the module must not exit, print or touch the network."""
        proc = subprocess.run(
            [sys.executable, '-c', 'import smoke_check'],
            cwd=str(ROOT), capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, '')

    def test_main_is_callable_without_running(self):
        self.assertTrue(callable(smoke_check.main))


class CliTests(unittest.TestCase):
    def test_defaults(self):
        args = smoke_check.build_parser().parse_args([])
        self.assertIsNone(args.root)
        self.assertEqual(args.base_url, 'http://127.0.0.1:8001')
        self.assertEqual(args.protocol, 'messages')
        self.assertEqual(args.workers, 2)
        self.assertEqual(args.timeout, 330)
        self.assertFalse(args.stream)
        self.assertFalse(args.health_only)
        self.assertFalse(args.compression)
        self.assertIsNone(args.model)

    def test_model_is_repeatable(self):
        args = smoke_check.build_parser().parse_args(
            ['--model', 'a', '--model', 'b', '--model', 'c'])
        self.assertEqual(args.model, ['a', 'b', 'c'])

    def test_protocol_choices(self):
        parser = smoke_check.build_parser()
        self.assertEqual(parser.parse_args(['--protocol', 'chat']).protocol, 'chat')
        self.assertEqual(parser.parse_args(['--protocol', 'both']).protocol, 'both')
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(['--protocol', 'nope'])

    def test_flags(self):
        args = smoke_check.build_parser().parse_args(
            ['--stream', '--health-only', '--compression', '--workers', '4',
             '--timeout', '12', '--root', '.', '--base-url', 'http://h:1'])
        self.assertTrue(args.stream)
        self.assertTrue(args.health_only)
        self.assertTrue(args.compression)
        self.assertEqual(args.workers, 4)
        self.assertEqual(args.timeout, 12)
        self.assertEqual(args.root, '.')
        self.assertEqual(args.base_url, 'http://h:1')


class CredentialTests(TempRootCase):
    def test_resolve_env_ref_uses_environment(self):
        value = smoke_check.resolve_env_ref('os.environ/SOME_KEY', env={'SOME_KEY': 'abc'})
        self.assertEqual(value, 'abc')

    def test_resolve_env_ref_falls_back_to_env_file(self):
        env_file = self.root / '.env'
        env_file.write_text('SOME_KEY=from-file\n', encoding='utf-8')
        value = smoke_check.resolve_env_ref('os.environ/SOME_KEY', env={}, env_file=env_file)
        self.assertEqual(value, 'from-file')

    def test_read_env_file_skips_comments_and_quotes(self):
        env_file = self.root / '.env'
        env_file.write_text('# comment\nexport A=1\nB="two"\n\nbroken\n', encoding='utf-8')
        values = smoke_check.read_env_file(env_file)
        self.assertEqual(values, {'A': '1', 'B': 'two'})

    def test_master_key_resolved_from_config_reference(self):
        self.write_project(master_key='sk-config-key')
        config, error = smoke_check.load_config(self.root)
        self.assertIsNone(error)
        self.assertEqual(smoke_check.master_key_from_config(config, self.root), 'sk-config-key')

    def test_master_key_never_printed(self):
        self.write_project(master_key=SECRET)
        session = FakeSession(default_responses())
        code, printed = self.run_main(['--root', str(self.root), '--model', 'alias-a'],
                                      session=session)
        self.assertEqual(code, 0)
        self.assertNotIn(SECRET, printed)
        results_file = (self.root / 'logs' / 'smoke-results.json').read_text(encoding='utf-8')
        self.assertNotIn(SECRET, results_file)
        headers = [kwargs.get('headers') for _, _, kwargs in session.calls]
        self.assertTrue(any(h and h.get('Authorization') == 'Bearer ' + SECRET for h in headers))

    def test_missing_master_key_is_reported(self):
        self.write_project(master_key=None)
        os.environ.pop('LITELLM_MASTER_KEY', None)
        session = FakeSession(default_responses())
        with contextlib.redirect_stderr(io.StringIO()):
            code, _ = self.run_main(['--root', str(self.root)], session=session)
        self.assertEqual(code, 2)
        self.assertEqual(session.calls, [])


class HealthTests(TempRootCase):
    def test_health_urls(self):
        checker = smoke_check.SmokeChecker(base_url='http://127.0.0.1:8001',
                                           headroom_url='http://127.0.0.1:8787')
        urls = [url for _, url in checker.health_urls()]
        self.assertIn('http://127.0.0.1:8787/health', urls)
        self.assertIn('http://127.0.0.1:8001/health/liveliness', urls)
        self.assertIn('http://127.0.0.1:8001/v1/models', urls)

    def test_health_only_runs_no_inference(self):
        self.write_project()
        session = FakeSession(default_responses())
        code, _ = self.run_main(['--root', str(self.root), '--health-only'], session=session)
        self.assertEqual(code, 0)
        for url in session.urls('POST'):
            self.fail('health-only must not POST, got %s' % url)

    def test_failed_health_probe_is_reported(self):
        self.write_project()
        responses = default_responses()
        responses['http://127.0.0.1:8001/health/liveliness'] = FakeResponse(
            status_code=503, payload={'error': {'type': 'service_unavailable'}})
        session = FakeSession(responses)
        checker = smoke_check.SmokeChecker(base_url='http://127.0.0.1:8001', master_key='k',
                                           session=session)
        results = checker.run_health()
        liveliness = [r for r in results if r['check'] == 'litellm-liveliness'][0]
        self.assertFalse(liveliness['ok'])
        self.assertEqual(liveliness['status'], 503)
        self.assertEqual(liveliness['error_type'], 'service_unavailable')


class NonStreamTests(unittest.TestCase):
    def checker(self, responses=None, **kwargs):
        return smoke_check.SmokeChecker(base_url='http://127.0.0.1:8001',
                                       headroom_url='http://127.0.0.1:8787',
                                       master_key='k', session=FakeSession(responses or default_responses()),
                                       **kwargs)

    def test_messages_ok(self):
        result = self.checker().check_model('alias-a', 'messages')
        self.assertTrue(result['ok'])
        self.assertEqual(result['status'], 200)
        self.assertEqual(result['actual_model'], 'kimi-k3')
        self.assertFalse(result['stream'])

    def test_chat_ok(self):
        result = self.checker().check_model('alias-a', 'chat')
        self.assertTrue(result['ok'])
        self.assertEqual(result['actual_model'], 'kimi-k3')

    def test_http_error_reports_type_only(self):
        responses = default_responses()
        responses['http://127.0.0.1:8001/v1/messages'] = FakeResponse(
            status_code=401,
            payload={'error': {'type': 'authentication_error', 'message': 'key %s' % SECRET}})
        result = self.checker(responses).check_model('alias-a', 'messages')
        self.assertFalse(result['ok'])
        self.assertEqual(result['error_type'], 'authentication_error')
        self.assertNotIn(SECRET, json.dumps(result))

    def test_non_json_error_body(self):
        responses = default_responses()
        responses['http://127.0.0.1:8001/v1/messages'] = FakeResponse(
            status_code=500, payload=None, text='boom %s' % SECRET)
        result = self.checker(responses).check_model('alias-a', 'messages')
        self.assertFalse(result['ok'])
        self.assertEqual(result['error_type'], 'non_json_response')
        self.assertNotIn(SECRET, json.dumps(result))

    def test_empty_content_is_not_ok(self):
        responses = default_responses()
        responses['http://127.0.0.1:8001/v1/messages'] = FakeResponse(
            status_code=200, payload={'type': 'message', 'model': 'x', 'content': []})
        result = self.checker(responses).check_model('alias-a', 'messages')
        self.assertFalse(result['ok'])
        self.assertEqual(result['error_type'], 'empty_content')


class StreamTests(unittest.TestCase):
    def checker(self, lines):
        responses = default_responses()
        responses['http://127.0.0.1:8001/v1/messages'] = FakeResponse(status_code=200, lines=lines)
        responses['http://127.0.0.1:8001/v1/chat/completions'] = FakeResponse(status_code=200, lines=lines)
        return smoke_check.SmokeChecker(base_url='http://127.0.0.1:8001',
                                        headroom_url='http://127.0.0.1:8787', master_key='k',
                                        session=FakeSession(responses))

    def test_messages_stream_with_message_stop(self):
        lines = sse(
            ('message_start', json.dumps({'type': 'message_start', 'message': {'model': 'kimi-k3'}})),
            ('content_block_delta', json.dumps({'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': 'OK'}})),
            ('message_stop', json.dumps({'type': 'message_stop'})),
        )
        result = self.checker(lines).check_model('alias-a', 'messages', stream=True)
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['stream'])
        self.assertEqual(result['actual_model'], 'kimi-k3')

    def test_messages_stream_error_event_with_http_200(self):
        lines = sse(
            ('message_start', json.dumps({'type': 'message_start', 'message': {'model': 'kimi-k3'}})),
            ('error', json.dumps({'type': 'error', 'error': {'type': 'overloaded_error', 'message': SECRET}})),
        )
        result = self.checker(lines).check_model('alias-a', 'messages', stream=True)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error_type'], 'overloaded_error')
        self.assertNotIn(SECRET, json.dumps(result))

    def test_messages_stream_truncated(self):
        lines = sse(
            ('message_start', json.dumps({'type': 'message_start', 'message': {'model': 'kimi-k3'}})),
            ('content_block_delta', json.dumps({'type': 'content_block_delta', 'delta': {'text': 'partial'}})),
        )
        result = self.checker(lines).check_model('alias-a', 'messages', stream=True)
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'truncated_stream')

    def test_chat_stream_with_done(self):
        lines = sse(
            (None, json.dumps({'model': 'kimi-k3', 'choices': [{'delta': {'content': 'OK'}}]})),
            (None, '[DONE]'),
        )
        result = self.checker(lines).check_model('alias-a', 'chat', stream=True)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['actual_model'], 'kimi-k3')

    def test_chat_stream_with_finish_reason(self):
        lines = sse(
            (None, json.dumps({'model': 'kimi-k3', 'choices': [{'delta': {'content': 'OK'}, 'finish_reason': 'stop'}]})),
        )
        result = self.checker(lines).check_model('alias-a', 'chat', stream=True)
        self.assertTrue(result['ok'], result)

    def test_chat_stream_truncated(self):
        lines = sse((None, json.dumps({'model': 'kimi-k3', 'choices': [{'delta': {'content': 'OK'}}]})),)
        result = self.checker(lines).check_model('alias-a', 'chat', stream=True)
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'truncated_stream')

    def test_chat_stream_error_chunk(self):
        lines = sse((None, json.dumps({'error': {'type': 'rate_limit_error', 'message': SECRET}})),)
        result = self.checker(lines).check_model('alias-a', 'chat', stream=True)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error_type'], 'rate_limit_error')
        self.assertNotIn(SECRET, json.dumps(result))


class RequestErrorTests(unittest.TestCase):
    def test_request_error_is_captured(self):
        import requests as requests_module
        session = FakeSession(default_responses(),
                              errors={'/v1/messages': requests_module.ConnectionError('down')})
        checker = smoke_check.SmokeChecker(base_url='http://127.0.0.1:8001', master_key='k',
                                           session=session)
        result = checker.check_model('alias-a', 'messages')
        self.assertFalse(result['ok'])
        self.assertEqual(result['error_type'], 'ConnectionError')
        self.assertIsNone(result['status'])

    def test_one_failure_does_not_stop_the_others(self):
        import requests as requests_module
        session = FakeSession(default_responses(),
                              errors={'/v1/messages': requests_module.Timeout('slow')})
        checker = smoke_check.SmokeChecker(base_url='http://127.0.0.1:8001', master_key='k',
                                           session=session)
        results = checker.run(['alias-a', 'alias-b'], ['messages'])
        messages = [r for r in results if r['protocol'] == 'messages']
        self.assertEqual(len(messages), 2)
        self.assertTrue(all(not r['ok'] for r in messages))

    def test_run_covers_every_model_and_protocol(self):
        checker = smoke_check.SmokeChecker(base_url='http://127.0.0.1:8001', master_key='k',
                                           session=FakeSession(default_responses()))
        results = checker.run(['alias-a', 'alias-b'], ['messages', 'chat'])
        pairs = {(r['model'], r['protocol']) for r in results if r['protocol'] in ('messages', 'chat')}
        self.assertEqual(pairs, {('alias-a', 'messages'), ('alias-a', 'chat'),
                                 ('alias-b', 'messages'), ('alias-b', 'chat')})


class CompressionTests(TempRootCase):
    def test_payload_pairs_tool_call_and_result(self):
        payload = smoke_check.build_compression_payload('alias-a')
        roles = [m['role'] for m in payload['messages']]
        self.assertEqual(roles, ['user', 'assistant', 'tool', 'assistant', 'user'])
        assistant = payload['messages'][1]
        tool_call = assistant['tool_calls'][0]
        self.assertEqual(tool_call['type'], 'function')
        self.assertEqual(tool_call['function']['name'], 'read_log')
        self.assertEqual(payload['messages'][2]['role'], 'tool')
        self.assertEqual(payload['messages'][2]['tool_call_id'], tool_call['id'])
        self.assertEqual(payload['model'], 'alias-a')

    def test_payload_is_repetitive_and_carries_the_sentinel(self):
        payload = smoke_check.build_compression_payload('alias-a')
        tool_output = payload['messages'][2]['content']
        lines = tool_output.splitlines()
        self.assertGreater(len(lines), 100)
        self.assertLess(len(set(lines)), 40)
        self.assertTrue(all(line.startswith('20') for line in lines))
        self.assertIn(smoke_check.SENTINEL, payload['messages'][-1]['content'])

    def test_compression_ok(self):
        checker = smoke_check.SmokeChecker(base_url='http://127.0.0.1:8001',
                                           headroom_url='http://127.0.0.1:8787', master_key='k',
                                           session=FakeSession(default_responses()))
        result = checker.check_compression('alias-a')
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['protocol'], 'compress')
        self.assertFalse(result['stream'])
        self.assertEqual(result['tokens_before'], 4000)
        self.assertEqual(result['tokens_after'], 500)
        self.assertTrue(result['sentinel_preserved'])

    def test_compression_uses_the_compress_endpoint(self):
        session = FakeSession(default_responses())
        checker = smoke_check.SmokeChecker(base_url='http://127.0.0.1:8001',
                                           headroom_url='http://127.0.0.1:8787', master_key='k',
                                           session=session)
        checker.check_compression('alias-a')
        self.assertEqual(session.urls('POST'), ['http://127.0.0.1:8787/v1/compress'])

    def test_compression_rejects_unreduced_tokens(self):
        responses = default_responses()
        body = dict(COMPRESS_BODY)
        body['tokens_after'] = body['tokens_before']
        responses['http://127.0.0.1:8787/v1/compress'] = FakeResponse(status_code=200, payload=body)
        checker = smoke_check.SmokeChecker(base_url='http://127.0.0.1:8001',
                                           headroom_url='http://127.0.0.1:8787', master_key='k',
                                           session=FakeSession(responses))
        result = checker.check_compression('alias-a')
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'tokens_not_reduced')

    def test_compression_rejects_lost_sentinel(self):
        responses = default_responses()
        body = dict(COMPRESS_BODY)
        body['messages'] = [dict(m) for m in COMPRESS_BODY['messages']]
        body['messages'][-1] = {'role': 'user', 'content': 'summarise the failing steps.'}
        responses['http://127.0.0.1:8787/v1/compress'] = FakeResponse(status_code=200, payload=body)
        checker = smoke_check.SmokeChecker(base_url='http://127.0.0.1:8001',
                                           headroom_url='http://127.0.0.1:8787', master_key='k',
                                           session=FakeSession(responses))
        result = checker.check_compression('alias-a')
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'sentinel_lost')
        self.assertFalse(result['sentinel_preserved'])

    def test_compression_http_error(self):
        responses = default_responses()
        responses['http://127.0.0.1:8787/v1/compress'] = FakeResponse(
            status_code=404, payload={'error': {'type': 'not_found', 'message': SECRET}})
        checker = smoke_check.SmokeChecker(base_url='http://127.0.0.1:8001',
                                           headroom_url='http://127.0.0.1:8787', master_key='k',
                                           session=FakeSession(responses))
        result = checker.check_compression('alias-a')
        self.assertFalse(result['ok'])
        self.assertEqual(result['error_type'], 'not_found')
        self.assertNotIn(SECRET, json.dumps(result))

    def test_compression_runs_without_inference(self):
        self.write_project()
        session = FakeSession(default_responses())
        code, _ = self.run_main(['--root', str(self.root), '--health-only', '--compression'],
                                session=session)
        self.assertEqual(code, 0)
        self.assertEqual(session.urls('POST'), ['http://127.0.0.1:8787/v1/compress'])


class ResultsFileTests(TempRootCase):
    def test_write_results_creates_parent(self):
        target = self.root / 'deep' / 'nested' / 'smoke-results.json'
        smoke_check.write_results(target, [{'ok': True}])
        self.assertTrue(target.exists())
        self.assertEqual(json.loads(target.read_text(encoding='utf-8')), [{'ok': True}])

    def test_results_have_the_documented_fields(self):
        self.write_project()
        session = FakeSession(default_responses())
        code, _ = self.run_main(['--root', str(self.root)], session=session)
        self.assertEqual(code, 0)
        results = json.loads((self.root / 'logs' / 'smoke-results.json').read_text(encoding='utf-8'))
        self.assertTrue(results)
        for result in results:
            for field in ('model', 'protocol', 'stream', 'status', 'ok', 'actual_model', 'seconds'):
                self.assertIn(field, result)

    def test_failed_run_exits_nonzero_and_records_failure(self):
        self.write_project()
        responses = default_responses()
        responses['http://127.0.0.1:8001/v1/messages'] = FakeResponse(
            status_code=500, payload={'error': {'type': 'internal_server_error'}})
        session = FakeSession(responses)
        code, _ = self.run_main(['--root', str(self.root)], session=session)
        self.assertEqual(code, 1)
        results = json.loads((self.root / 'logs' / 'smoke-results.json').read_text(encoding='utf-8'))
        self.assertTrue(any(not r['ok'] for r in results))


class RealHttpTests(unittest.TestCase):
    """End to end checks against a local fake gateway over real HTTP."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), FakeGatewayHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = 'http://127.0.0.1:%d' % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def checker(self, **kwargs):
        return smoke_check.SmokeChecker(base_url=self.base, headroom_url=self.base,
                                        master_key='k', timeout=10, **kwargs)

    def test_health_over_http(self):
        results = self.checker().run_health()
        self.assertEqual(len(results), 3)
        self.assertTrue(all(r['ok'] for r in results), results)
        self.assertTrue(all(r['status'] == 200 for r in results))

    def test_non_stream_messages_over_http(self):
        result = self.checker().check_model('alias-a', 'messages')
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['actual_model'], 'kimi-k3')

    def test_non_stream_chat_over_http(self):
        result = self.checker().check_model('alias-a', 'chat')
        self.assertTrue(result['ok'], result)

    def test_stream_messages_over_http(self):
        result = self.checker().check_model('ok', 'messages', stream=True)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['actual_model'], 'kimi-k3')

    def test_stream_truncated_over_http(self):
        result = self.checker().check_model('truncated', 'messages', stream=True)
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'truncated_stream')

    def test_stream_error_event_over_http(self):
        result = self.checker().check_model('error-event', 'messages', stream=True)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error_type'], 'overloaded_error')

    def test_stream_chat_over_http(self):
        result = self.checker().check_model('chat-ok', 'chat', stream=True)
        self.assertTrue(result['ok'], result)

    def test_stream_chat_truncated_over_http(self):
        result = self.checker().check_model('chat-truncated', 'chat', stream=True)
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'truncated_stream')

    def test_http_error_over_http(self):
        result = self.checker().check_model('http-500', 'messages')
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 500)
        self.assertEqual(result['error_type'], 'internal_server_error')

    def test_compression_over_http(self):
        result = self.checker().check_compression('alias-a')
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['sentinel_preserved'])


if __name__ == '__main__':
    unittest.main()


class BoundaryTests(unittest.TestCase):
    def test_error_event_without_json_is_failure(self):
        verdict = smoke_check.analyze_stream([
            ('event', 'error'), ('data', 'not-json'), ('data', '[DONE]')], 'chat')
        self.assertFalse(verdict['ok'])

    def test_gateway_key_is_not_sent_to_headroom(self):
        from unittest.mock import Mock
        session = Mock()
        checker = smoke_check.SmokeChecker(master_key='private-gateway-key', session=session)
        checker._get(checker.headroom_url + '/health')
        self.assertNotIn('Authorization', session.get.call_args.kwargs['headers'])
        checker._post(checker.headroom_url + '/v1/compress', {})
        self.assertNotIn('Authorization', session.post.call_args.kwargs['headers'])
        checker._post(checker.base_url + '/v1/messages', {})
        self.assertEqual(session.post.call_args.kwargs['headers']['Authorization'],
                         'Bearer private-gateway-key')
