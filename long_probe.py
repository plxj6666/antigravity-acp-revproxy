"""Manual test, billed requests, requires a logged-in agy. Does not print secrets."""
import asyncio,json,os,secrets
from aiohttp.test_utils import TestClient,TestServer
from gateway import make_app

async def main():
    key=secrets.token_hex(32);os.environ['ACP_SESSION_KEYS_JSON']=json.dumps({'tester':key});model='gemini-4-neon';marker=secrets.token_hex(8)
    rows=[]
    for i in range(110):rows.extend([{'role':'user','content':f'Entry {i} '+('historical sample record '*130)+(f' ORCHID label={marker}' if i==51 else '')},{'role':'assistant','content':'Recorded.'}])
    rows.append({'role':'user','content':'Return the ORCHID label only.'});paths=['/v1/chat/completions','/v1/responses','/v1/messages',f'/v1beta/models/{model}:generateContent'];base={'Authorization':'Bearer test'}
    async with TestClient(TestServer(make_app('test'))) as c:
        for i,path in enumerate(paths):
            b=({'model':model,'messages':rows} if i<3 else {'contents':[{'role':'model' if r['role']=='assistant' else 'user','parts':[{'text':r['content']}]} for r in rows]});
            if i==1:b['input']=b.pop('messages')
            if i==2:b['max_tokens']=600
            r=await c.post(path,json=b,headers=base);t=await r.text();ok=r.status==200 and marker in t; print(path,len(json.dumps(b)),r.status,ok,flush=True);assert ok
        h={**base,'X-ACP-Session':secrets.token_hex(32),'X-ACP-Session-Key':key};m=secrets.token_hex(8)
        for n in range(1,11):
            q='Memorize label '+m+'. Reply OK.' if n==1 else 'Return the original label only.' if n==10 else f'Record note {n}. Reply OK.';h['X-ACP-Turn']=str(n);r=await c.post(paths[0],json={'model':model,'messages':[{'role':'user','content':q}]},headers=h);v=await r.json();assert r.status==200,(n,r.status);answer=v['choices'][0]['message']['content'];print('increment',n,bool(answer.strip()),flush=True)
        assert m in answer
if __name__=='__main__':asyncio.run(main())
