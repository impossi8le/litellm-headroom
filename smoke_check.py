"""Live diagnostics for a LiteLLM proxy and its Headroom sidecar.

Importing this module has no side effects: the configuration, the credentials
and every network call happen inside :func:`main` (or when a caller drives
:class:`SmokeChecker` directly). Nothing is printed and no file is written at
import time, so the module is safe to import from tests.

Diagnostics covered:

* health of the Headroom sidecar, the LiteLLM liveness probe and the model list;
* a small inference call per configured alias, over the Anthropic Messages
  protocol and/or the OpenAI Chat Completions protocol, optionally streaming;
* the Headroom ``/v1/compress`` endpoint, using synthetic repetitive tool
  output only - it never calls a paid model.

Credentials are never printed and provider error bodies are never echoed:
error bodies can contain keys, so only sanitized error *types* are reported.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
import yaml

DEFAULT_BASE_URL = 'http://127.0.0.1:8001'
DEFAULT_HEADROOM_URL = 'http://127.0.0.1:8787'
DEFAULT_TIMEOUT = 330
DEFAULT_WORKERS = 2
DEFAULT_PROTOCOL = 'messages'
PROTOCOLS = ('messages', 'chat')
CONFIG_NAME = 'litellm-config.yaml'
ENV_NAME = '.env'
RESULTS_NAME = 'smoke-results.json'
# Sentinel that must survive compression untouched.
SENTINEL = 'SENTINEL-7f3a91c2-KEEP-ME'
_SAFE_TOKEN = re.compile(r'[^A-Za-z0-9_.:-]')


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def safe_token(value, default='unknown'):
    """Reduce a provider supplied type string to a safe, printable token.

    Only a type name is reported, never a message or a body: provider errors
    can embed credentials, request ids or even parts of the prompt.
    """
    if not isinstance(value, str):
        return default
    token = _SAFE_TOKEN.sub('_', value.strip()).strip('_')
    if not token:
        return default
    return token[:64]


def read_env_file(path):
    """Parse NAME=value pairs from a .env file; blanks and # comments are skipped."""
    values = {}
    try:
        lines = Path(path).read_text(encoding='utf-8').splitlines()
    except OSError:
        return values
    for line in lines:
        trimmed = line.strip()
        if not trimmed or trimmed.startswith('#'):
            continue
        if trimmed.startswith('export '):
            trimmed = trimmed[len('export '):].lstrip()
        name, sep, value = trimmed.partition('=')
        name = name.strip()
        if not sep or not name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        values[name] = value
    return values


def resolve_env_ref(value, env=None, env_file=None):
    """Resolve an ``os.environ/NAME`` reference to the variable's value.

    A variable that is unset falls back to the .env file next to the config:
    after bat.bat the key lives only in the child process, not in a fresh
    shell. A literal value is returned unchanged.
    """
    if isinstance(value, str) and value.startswith('os.environ/'):
        name = value[len('os.environ/'):]
        environ = os.environ if env is None else env
        if name in environ:
            return environ[name]
        if env_file is not None:
            return read_env_file(env_file).get(name, '')
        return ''
    return value or ''


def load_config(root):
    """Return (config, error). A missing config is not fatal: models can be given on the CLI."""
    path = Path(root) / CONFIG_NAME
    if not path.exists():
        return {}, None
    try:
        data = yaml.safe_load(path.read_text(encoding='utf-8'))
    except OSError as exc:
        return {}, 'config unreadable: %s' % type(exc).__name__
    except yaml.YAMLError as exc:
        return {}, 'config not valid YAML: %s' % type(exc).__name__
    if data is None:
        return {}, None
    if not isinstance(data, dict):
        return {}, 'config is not a mapping'
    return data, None


def config_models(config):
    """Alias names from the rendered config, sorted and de-duplicated."""
    names = []
    for entry in config.get('model_list') or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get('model_name')
        if name and name not in names:
            names.append(name)
    return sorted(names)


def config_headroom_url(config):
    """Headroom base URL from the guardrail config, else the default sidecar URL."""
    for guardrail in config.get('guardrails') or []:
        if not isinstance(guardrail, dict):
            continue
        params = guardrail.get('litellm_params') or {}
        if not isinstance(params, dict):
            continue
        if str(params.get('guardrail', '')).lower() == 'headroom' and params.get('api_base'):
            return str(params['api_base']).rstrip('/')
    return DEFAULT_HEADROOM_URL


def master_key_from_config(config, root):
    """Resolve the master key referenced by the config; never logged or printed."""
    settings = config.get('general_settings') or {}
    reference = settings.get('master_key') if isinstance(settings, dict) else None
    if not reference:
        return ''
    return str(resolve_env_ref(reference, env_file=Path(root) / ENV_NAME) or '')


# --------------------------------------------------------------------------- #
# protocol helpers
# --------------------------------------------------------------------------- #
def build_headers(master_key):
    """Headers for the proxy. The key is only ever sent, never displayed."""
    headers = {'anthropic-version': '2023-06-01', 'content-type': 'application/json'}
    if master_key:
        headers['Authorization'] = 'Bearer ' + master_key
    return headers


def request_payload(model, protocol, stream):
    """The smallest possible inference request for the given protocol."""
    payload = {
        'model': model,
        'max_tokens': 128,
        'messages': [{'role': 'user', 'content': 'Reply with OK only.'}],
    }
    if stream:
        payload['stream'] = True
    return payload


def endpoint_for(base_url, protocol):
    path = '/v1/messages' if protocol == 'messages' else '/v1/chat/completions'
    return base_url.rstrip('/') + path


def http_error_type(response):
    """Sanitized error type of a failed HTTP response (no body, no message)."""
    try:
        payload = response.json()
    except ValueError:
        return 'non_json_response'
    if isinstance(payload, dict):
        error = payload.get('error')
        if isinstance(error, dict):
            return safe_token(error.get('type'), 'http_error')
        if error is not None:
            return 'http_error'
        if payload.get('type'):
            return safe_token(payload.get('type'), 'http_error')
    return 'http_error'


def parse_sse_events(lines):
    """Turn raw SSE lines into (kind, value) pairs.

    ``kind`` is ``event`` for ``event:`` fields and ``data`` for ``data:``
    fields; comments and blank lines are dropped.
    """
    events = []
    for raw in lines or []:
        if raw is None:
            continue
        if isinstance(raw, bytes):
            line = raw.decode('utf-8', 'replace')
        else:
            line = str(raw)
        line = line.strip()
        if not line or line.startswith(':'):
            continue
        if line.startswith('event:'):
            events.append(('event', line[len('event:'):].strip()))
        elif line.startswith('data:'):
            events.append(('data', line[len('data:'):].strip()))
    return events


def _payload(text):
    """Parse an SSE data payload as JSON, or return None."""
    if not text or text == '[DONE]':
        return None
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def analyze_stream(events, protocol):
    """Judge a stream.

    A stream is only good when it carries a terminal marker: ``message_stop``
    for the Messages protocol, ``[DONE]`` or a ``finish_reason`` for Chat
    Completions. Error events are rejected even when the HTTP status was 200,
    and a stream that stops before its terminal marker is truncated, not fine.

    Returns a dict with ``ok``, ``reason``, ``error_type``, ``actual_model``
    and the collected text.
    """
    error_type = None
    reason = None
    actual_model = None
    text_parts = []
    terminal = False
    saw_event = False

    for kind, value in events:
        if kind == 'event':
            saw_event = True
            if value == 'error':
                error_type = 'stream_error'
            if value == 'message_stop':
                terminal = True
            continue
        if value == '[DONE]':
            terminal = True
            continue
        payload = _payload(value)
        if payload is None:
            continue
        if payload.get('model') and not actual_model:
            actual_model = str(payload['model'])
        message = payload.get('message')
        if isinstance(message, dict) and message.get('model'):
            actual_model = str(message['model'])
        if payload.get('type') == 'error' or payload.get('error') not in (None, False):
            error = payload.get('error')
            if isinstance(error, dict):
                error_type = safe_token(error.get('type'), 'stream_error')
            else:
                error_type = 'stream_error'
            continue
        if payload.get('type') == 'message_stop':
            terminal = True
        choices = payload.get('choices')
        if isinstance(choices, list) and choices:
            choice = choices[0]
            if isinstance(choice, dict):
                if choice.get('finish_reason'):
                    terminal = True
                delta = choice.get('delta') or {}
                if isinstance(delta, dict) and delta.get('content'):
                    text_parts.append(str(delta['content']))
        delta = payload.get('delta')
        if isinstance(delta, dict) and delta.get('text'):
            text_parts.append(str(delta['text']))

    if error_type:
        reason = 'stream_error'
        ok = False
    elif not terminal:
        reason = 'truncated_stream' if (saw_event or text_parts) else 'no_terminal_event'
        ok = False
    else:
        reason = 'ok'
        ok = True
    return {
        'ok': ok,
        'reason': reason,
        'error_type': error_type,
        'actual_model': actual_model,
        'text': ''.join(text_parts)[:120],
    }


def check_non_stream_payload(payload, protocol):
    """Judge a non-streaming response body. Returns (ok, error_type)."""
    if not isinstance(payload, dict):
        return False, 'non_json_response'
    error = payload.get('error')
    if error is not None:
        if isinstance(error, dict):
            return False, safe_token(error.get('type'), 'http_error')
        return False, 'http_error'
    if protocol == 'messages':
        if payload.get('type') != 'message':
            return False, safe_token(payload.get('type'), 'unexpected_response')
        content = payload.get('content')
        if not isinstance(content, list) or not content:
            return False, 'empty_content'
        return True, None
    choices = payload.get('choices')
    if not isinstance(choices, list) or not choices:
        return False, 'empty_content'
    return True, None


# --------------------------------------------------------------------------- #
# compression payload
# --------------------------------------------------------------------------- #
def build_compression_payload(model):
    """Synthetic conversation with repetitive tool output and a protected sentinel.

    The tool output is a repetitive build log on purpose: that is what the
    compression service is meant to shrink (its log compressor only kicks in
    for log shaped text), and it is a plain string, so the service does not
    exclude it as a protected tool blob. The assistant tool call and the tool
    result are paired correctly (matching ``tool_call_id``) so the service
    sees a valid transcript. The last user message carries the sentinel, which
    must survive compression untouched: user messages are never compressed.
    """
    log_lines = ['2026-10-20T10:%02d:%02dZ INFO build step %d of 30 completed'
                 % (index // 60, index % 60, index) for index in range(30)]
    repetitive = '\n'.join(log_lines[index % 30] for index in range(300))
    messages = [
        {'role': 'user', 'content': 'Read the build log and list the failing steps.'},
        {
            'role': 'assistant',
            'content': '',
            'tool_calls': [{
                'id': 'call_smoke_1',
                'type': 'function',
                'function': {'name': 'read_log', 'arguments': '{"path": "build.log"}'},
            }],
        },
        {'role': 'tool', 'tool_call_id': 'call_smoke_1', 'content': repetitive},
        {'role': 'assistant', 'content': 'The build log was read.'},
        {'role': 'user', 'content': SENTINEL + ' Summarise the failing steps.'},
    ]
    return {'messages': messages, 'model': model or 'unknown'}


def message_texts(messages):
    """Flatten the text of a message list (strings and content block lists)."""
    texts = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        content = message.get('content')
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and isinstance(block.get('text'), str):
                    texts.append(block['text'])
    return texts


def last_user_text(messages):
    """Text of the last user message, or '' when there is none."""
    for message in reversed(messages or []):
        if isinstance(message, dict) and message.get('role') == 'user':
            texts = message_texts([message])
            return '\n'.join(texts)
    return ''


# --------------------------------------------------------------------------- #
# checker
# --------------------------------------------------------------------------- #
class SmokeChecker:
    """Runs the diagnostics against one proxy deployment."""

    def __init__(self, base_url=DEFAULT_BASE_URL, headroom_url=DEFAULT_HEADROOM_URL,
                 master_key='', timeout=DEFAULT_TIMEOUT, workers=DEFAULT_WORKERS,
                 session=None):
        self.base_url = base_url.rstrip('/')
        self.headroom_url = headroom_url.rstrip('/')
        self.master_key = master_key or ''
        self.timeout = timeout
        self.workers = max(1, int(workers or 1))
        self.session = session if session is not None else requests.Session()
        self.headers = build_headers(self.master_key)

    # -- low level ---------------------------------------------------------- #
    def _get(self, url):
        headers = self.headers if url.startswith(self.base_url + '/') else {'content-type': 'application/json'}
        return self.session.get(url, headers=headers, timeout=min(self.timeout, 10))

    def _post(self, url, payload, stream=False):
        headers = self.headers if url.startswith(self.base_url + '/') else {'content-type': 'application/json'}
        return self.session.post(url, headers=headers, json=payload,
                                 timeout=self.timeout, stream=stream)

    # -- checks ------------------------------------------------------------- #
    def health_urls(self):
        return [
            ('headroom', self.headroom_url + '/health'),
            ('litellm-liveliness', self.base_url + '/health/liveliness'),
            ('litellm-models', self.base_url + '/v1/models'),
        ]

    def run_health(self):
        """Probe the health endpoints. No inference, no cost."""
        results = []
        for name, url in self.health_urls():
            started = time.monotonic()
            result = {
                'model': None,
                'protocol': 'health',
                'stream': False,
                'status': None,
                'ok': False,
                'actual_model': None,
                'seconds': 0.0,
                'check': name,
            }
            try:
                response = self._get(url)
                result['status'] = response.status_code
                result['ok'] = bool(response.status_code == 200)
                if not result['ok']:
                    result['reason'] = 'http_%s' % response.status_code
                    result['error_type'] = http_error_type(response)
            except Exception as exc:  # noqa: BLE001 - report, never crash the run
                result['error_type'] = safe_token(type(exc).__name__, 'request_error')
                result['reason'] = 'request_failed'
            result['seconds'] = round(time.monotonic() - started, 2)
            results.append(result)
        return results

    def check_model(self, model, protocol, stream=False):
        """One inference probe. Failures are reported, never raised."""
        started = time.monotonic()
        result = {
            'model': model,
            'protocol': protocol,
            'stream': bool(stream),
            'status': None,
            'ok': False,
            'actual_model': None,
            'seconds': 0.0,
        }
        url = endpoint_for(self.base_url, protocol)
        payload = request_payload(model, protocol, stream)
        try:
            response = self._post(url, payload, stream=stream)
            result['status'] = response.status_code
            if response.status_code != 200:
                result['reason'] = 'http_%s' % response.status_code
                result['error_type'] = http_error_type(response)
            elif stream:
                events = parse_sse_events(response.iter_lines())
                verdict = analyze_stream(events, protocol)
                result['ok'] = verdict['ok']
                result['reason'] = verdict['reason']
                result['error_type'] = verdict['error_type']
                result['actual_model'] = verdict['actual_model']
            else:
                try:
                    body = response.json()
                except ValueError:
                    body = None
                ok, error_type = check_non_stream_payload(body, protocol)
                result['ok'] = ok
                result['error_type'] = error_type
                if ok:
                    result['actual_model'] = (body or {}).get('model')
                    result['reason'] = 'ok'
                else:
                    result['reason'] = 'invalid_response'
        except Exception as exc:  # noqa: BLE001 - one broken model must not stop the run
            result['error_type'] = safe_token(type(exc).__name__, 'request_error')
            result['reason'] = 'request_failed'
        finally:
            if 'response' in locals() and hasattr(response, 'close'):
                response.close()
        result['seconds'] = round(time.monotonic() - started, 2)
        return result

    def check_compression(self, model):
        """Ask Headroom to compress a synthetic transcript. No model is called.

        Verifies that the service answers, that the token count went down and
        that the protected last user sentinel survived.
        """
        started = time.monotonic()
        result = {
            'model': model or 'headroom',
            'protocol': 'compress',
            'stream': False,
            'status': None,
            'ok': False,
            'actual_model': None,
            'seconds': 0.0,
            'tokens_before': None,
            'tokens_after': None,
            'tokens_saved': None,
            'sentinel_preserved': False,
        }
        url = self.headroom_url + '/v1/compress'
        payload = build_compression_payload(model)
        try:
            response = self._post(url, payload)
            result['status'] = response.status_code
            if response.status_code != 200:
                result['reason'] = 'http_%s' % response.status_code
                result['error_type'] = http_error_type(response)
            else:
                try:
                    body = response.json()
                except ValueError:
                    body = None
                if not isinstance(body, dict):
                    result['reason'] = 'invalid_response'
                    result['error_type'] = 'non_json_response'
                else:
                    result['reason'] = 'ok'
                    result['actual_model'] = body.get('model')
                    before = body.get('tokens_before')
                    after = body.get('tokens_after')
                    result['tokens_before'] = before
                    result['tokens_after'] = after
                    result['tokens_saved'] = body.get('tokens_saved')
                    messages = body.get('messages')
                    sentinel_text = last_user_text(messages)
                    if not sentinel_text:
                        sentinel_text = '\n'.join(message_texts(messages))
                    result['sentinel_preserved'] = SENTINEL in sentinel_text
                    if not isinstance(before, int) or not isinstance(after, int):
                        result['ok'] = False
                        result['reason'] = 'invalid_response'
                    elif after >= before:
                        result['ok'] = False
                        result['reason'] = 'tokens_not_reduced'
                    elif not result['sentinel_preserved']:
                        result['ok'] = False
                        result['reason'] = 'sentinel_lost'
                    else:
                        result['ok'] = True
        except Exception as exc:  # noqa: BLE001
            result['error_type'] = safe_token(type(exc).__name__, 'request_error')
            result['reason'] = 'request_failed'
        result['seconds'] = round(time.monotonic() - started, 2)
        return result

    # -- orchestration ------------------------------------------------------ #
    def run(self, models, protocols, stream=False, health_only=False, compression=False):
        """Run every requested check and return the result list."""
        results = self.run_health()
        if health_only:
            if compression:
                results.append(self.check_compression(models[0] if models else ''))
            return results
        tasks = [(model, protocol) for model in models for protocol in protocols]
        if tasks:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                futures = [pool.submit(self.check_model, model, protocol, stream)
                           for model, protocol in tasks]
                for future in futures:
                    results.append(future.result())
        if compression:
            results.append(self.check_compression(models[0] if models else ''))
        return results


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #
def build_parser():
    parser = argparse.ArgumentParser(
        prog='smoke_check.py',
        description='Live diagnostics for a LiteLLM proxy and its Headroom sidecar.')
    parser.add_argument('--root', default=None,
                        help='project root holding litellm-config.yaml and .env '
                             '(default: the directory of this script)')
    parser.add_argument('--base-url', default=DEFAULT_BASE_URL,
                        help='LiteLLM proxy base URL (default: %(default)s)')
    parser.add_argument('--headroom-url', default=None,
                        help='Headroom base URL (default: the guardrail api_base '
                             'from the config, else %s)' % DEFAULT_HEADROOM_URL)
    parser.add_argument('--model', action='append', default=None, metavar='NAME',
                        help='model alias to check; repeatable (default: every alias '
                             'in the config)')
    parser.add_argument('--protocol', choices=PROTOCOLS + ('both',), default=DEFAULT_PROTOCOL,
                        help='protocol to exercise (default: %(default)s)')
    parser.add_argument('--stream', action='store_true',
                        help='use streaming requests and verify the terminal event')
    parser.add_argument('--health-only', action='store_true',
                        help='only run the health probes, no inference')
    parser.add_argument('--workers', type=int, default=DEFAULT_WORKERS,
                        help='parallel checks (default: %(default)s)')
    parser.add_argument('--timeout', type=float, default=DEFAULT_TIMEOUT,
                        help='per request timeout in seconds (default: %(default)s)')
    parser.add_argument('--compression', action='store_true',
                        help='also verify the Headroom /v1/compress endpoint '
                             '(synthetic data, no paid inference)')
    return parser


def write_results(path, results):
    """Write the result list as JSON, creating the parent directory first."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(results, indent=2, ensure_ascii=True), encoding='utf-8')
    return path


def main(argv=None, session=None):
    """Entry point. Returns the process exit code.

    ``session`` is an injection seam for tests: it must quack like
    ``requests.Session``. When omitted a real session is used.
    """
    args = build_parser().parse_args(argv)
    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parent

    config, config_error = load_config(root)
    if config_error:
        print('warning: %s' % config_error, file=sys.stderr)

    models = list(dict.fromkeys(args.model)) if args.model else config_models(config)
    if not models and not args.health_only:
        print('error: no model aliases found; pass --model or fix %s' % CONFIG_NAME,
              file=sys.stderr)
        return 2

    protocols = list(PROTOCOLS) if args.protocol == 'both' else [args.protocol]

    master_key = master_key_from_config(config, root)
    if not master_key:
        if args.health_only:
            print('warning: master key not found, continuing without it', file=sys.stderr)
        else:
            print('error: master key not set: export it or put it in .env before '
                  'running the smoke check.', file=sys.stderr)
            return 2

    headroom_url = args.headroom_url or config_headroom_url(config)
    checker = SmokeChecker(base_url=args.base_url, headroom_url=headroom_url,
                            master_key=master_key, timeout=args.timeout,
                            workers=args.workers, session=session)
    results = checker.run(models, protocols, stream=args.stream,
                           health_only=args.health_only, compression=args.compression)
    for result in results:
        print(json.dumps(result, ensure_ascii=True), flush=True)

    results_path = write_results(root / 'logs' / RESULTS_NAME, results)
    print('results: %s' % results_path)
    return 0 if all(result.get('ok') for result in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
