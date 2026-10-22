"""Live check: health, configured model aliases and Anthropic messages API."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import os
import sys
import time
import requests
import yaml

BASE='http://127.0.0.1:8001'
cfg=yaml.safe_load(Path(__file__).with_name('litellm-config.yaml').read_text(encoding='utf-8'))
ENV_FILE=Path(__file__).with_name('.env')


def read_env_file(path):
    """Parse NAME=value pairs from a .env file; blanks and # comments are skipped."""
    values = {}
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
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


def resolve(value):
    """Config holds os.environ/NAME references; read the variable, never the literal.

    A variable that is unset falls back to the .env file next to this script:
    after bat.bat the key lives only in the child process, not in a fresh shell.
    """
    if isinstance(value, str) and value.startswith('os.environ/'):
        name = value[len('os.environ/'):]
        if name in os.environ:
            return os.environ[name]
        return read_env_file(ENV_FILE).get(name, '')
    return value or ''


master = resolve(cfg['general_settings']['master_key'])
if not master:
    sys.exit('LITELLM_MASTER_KEY is not set: export it or put it in .env before running the smoke check.')

HEADERS={'anthropic-version':'2023-06-01','Authorization':'Bearer '+master}

def check(model):
    started = time.monotonic()
    r=requests.post(BASE+'/v1/messages',headers=HEADERS,json={
        'model':model,'max_tokens':128,'messages':[{'role':'user','content':'Reply with OK only.'}]
    },timeout=330)
    result={'model':model,'status':r.status_code,'guardrail':r.headers.get('x-litellm-applied-guardrails')}
    if r.ok:
        d=r.json()
        result['type']=d.get('type')
        result['actual_model']=d.get('model')
        result['text']=' '.join(p.get('text','') for p in d.get('content',[]))[:120]
        result['ok']=d.get('type')=='message' and bool(d.get('content'))
    else:
        # Do not echo provider error bodies: they can contain credentials.
        result['ok']=False
        try: result['error_type']=r.json().get('error',{}).get('type')
        except ValueError: result['error_type']='non-JSON response'
    result['seconds']=round(time.monotonic()-started, 1)
    print(json.dumps(result,ensure_ascii=True),flush=True)
    return result

if __name__=='__main__':
    for url in ['http://127.0.0.1:8787/health',BASE+'/health/liveliness',BASE+'/v1/models']:
        r=requests.get(url,headers=HEADERS,timeout=10)
        print(url, r.status_code,flush=True)
        r.raise_for_status()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(check, sorted({m['model_name'] for m in cfg['model_list']})))
    Path(__file__).with_name('logs').joinpath('smoke-results.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
    raise SystemExit(0 if all(r['ok'] for r in results) else 1)
