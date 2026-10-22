"""Live check: health, configured model aliases and Anthropic messages API."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import time
import requests
import yaml

BASE='http://127.0.0.1:8001'
cfg=yaml.safe_load(Path(__file__).with_name('litellm-config.yaml').read_text(encoding='utf-8'))
HEADERS={'anthropic-version':'2023-06-01','Authorization':'Bearer '+cfg['general_settings']['master_key']}

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
        results=list(pool.map(check,[m['model_name'] for m in cfg['model_list']]))
    Path(__file__).with_name('logs').joinpath('smoke-results.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
    raise SystemExit(0 if all(r['ok'] for r in results) else 1)
