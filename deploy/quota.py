"""Read-only quota lookup; never return upstream bodies or credential values."""
import asyncio, json, time
from datetime import datetime, timezone
from pathlib import Path
import aiohttp

CRED=Path.home()/'.gemini/antigravity-cli/antigravity-oauth-token'
BASE='https://daily-cloudcode-pa.googleapis.com/v1internal:'
cache=None
lock=asyncio.Lock()

def quota_rows(summary,models):
    rows=[]
    def add(kind,key,label,q):
        if not isinstance(q,dict) or not isinstance(key,str):return
        f=q.get('remainingFraction'); reset=q.get('resetTime')
        remaining=round(f*100,1) if type(f) in (int,float) and 0<=f<=1 else None
        if remaining is None and not isinstance(reset,str):return
        rows.append({'kind':kind,'name':key[:120], 'label':label[:160] if isinstance(label,str) else '',
                     'remaining':remaining,'reset':reset if isinstance(reset,str) else None})
    if isinstance(summary,dict):
        for g in summary.get('groups') or []:
            for b in g.get('buckets') or []:
                label=' · '.join(s for s in (g.get('displayName'),b.get('displayName')) if isinstance(s,str))
                add('bucket',b.get('bucketId') or b.get('displayName'),label,b)
    if isinstance(models,dict):
        for key,m in sorted((models.get('models') or {}).items()):
            add('model',key,m.get('displayName'),m.get('quotaInfo'))
    return rows

async def snapshot():
    global cache
    async with lock:
        if not CRED.is_file(): return {'configured':False,'rows':[]}
        key=CRED.stat().st_mtime_ns
        if cache and cache[0]==key and time.monotonic()<cache[1]: return cache[2]
        result={'configured':True,'rows':[],'updated':int(time.time())}
        try:
            doc=json.loads(CRED.read_bytes()); result['email']=str(doc.get('id_token_email') or '')[:200]
            # Let the installed CLI handle OAuth refresh; never transmit a refresh token ourselves.
            expiry=doc['token'].get('expiry')
            if expiry and datetime.fromisoformat(expiry.replace('Z','+00:00'))<=datetime.now(timezone.utc):
                p=await asyncio.create_subprocess_exec('/opt/acp-gateway/bin/agy','models',cwd=Path.home(),stdin=-3,stdout=-3,stderr=-3)
                try: await asyncio.wait_for(p.wait(),12)
                finally:
                    if p.returncode is None: p.kill(); await p.wait()
                doc=json.loads(CRED.read_bytes())
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10),headers={'User-Agent':'antigravity/1.5.17 linux/amd64 (acp-gateway quota)','Authorization':'Bearer '+doc['token']['access_token']}) as client:
                async def post(method,payload):
                    async with client.post(BASE+method,json=payload,allow_redirects=False) as r:
                        if r.status!=200: return {}
                        return await r.json(content_type=None)
                project=doc.get('project_id'); body={'project':project} if project else {}
                summary,models=await asyncio.gather(post('retrieveUserQuotaSummary',body),post('fetchAvailableModels',body),return_exceptions=True)
                result['rows']=quota_rows(summary,models)
        except (TimeoutError,aiohttp.ClientError,ValueError,TypeError,KeyError,AttributeError,OSError): pass
        cache=(key,time.monotonic()+(60 if result['rows'] else 15),result); return result
