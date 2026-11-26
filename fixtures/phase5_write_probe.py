"""Local Gemini HIGH control: fixed bash -> write -> final. No arbitrary tool execution."""
import asyncio,json,os,secrets,sys,tempfile,time
from pathlib import Path
from aiohttp import ClientTimeout
from aiohttp.test_utils import TestServer,TestClient
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from gateway import make_app

async def main():
    root=Path(tempfile.mkdtemp(prefix='phase5-write-'));os.environ['ACP_LOG_DIR']=str(root/'native');print('QA directory',root,flush=True)
    key=secrets.token_hex(24);os.environ['GATEWAY_API_KEYS_JSON']=json.dumps({'probe':key})
    async with TestClient(TestServer(make_app(secrets.token_hex(24)),handler_cancellation=True),timeout=ClientTimeout(total=940)) as client:
        for i in range(6):
            large=i%2==1; spec=('a detailed, self-contained interactive HTML game, with original inline JS, CSS and SVG, 50–65 KB of actual source in the content argument. No padding, external resources or placeholders.' if large else 'a minimal HTML page containing only a title and heading, under 200 bytes.')
            tools=[dict(name=n,description=d,parameters=dict(type='object',required=list(props),properties={p:{'type':'string'} for p in props})) for n,d,props in [('bash','Run pwd', ['command']),('write','Write the complete HTML file',['path','content'])]]
            rows=[{'role':'user','parts':[{'text':f'Trial {i}. Use client tools in exactly this order: call bash once with pwd, then call write once with path scene.html, then say DONE without any more tools. Write '+spec}]}];start=time.monotonic()
            try:
                for turn,expected in enumerate(('bash','write',None),1):
                    body=dict(contents=rows,tools=[{'functionDeclarations':tools}],generationConfig={'maxOutputTokens':64000,'thinkingConfig':{'thinkingLevel':'HIGH','includeThoughts':True}})
                    async with client.post('/v1beta/models/gemini-4-neon:generateContent',json=body,headers={'Authorization':'Bearer '+key}) as r:
                        rid=r.headers.get('X-Gateway-Request-Id'); assert r.status==200,r.status
                        data=await r.json(); content=data['candidates'][0]['content'];calls=[p['functionCall'] for p in content['parts'] if 'functionCall' in p];assert [c['name'] for c in calls]==([expected] if expected else [])
                        n=0
                        if expected:
                            c=calls[0];value='/workspace'
                            if expected=='write':
                                raw=c['args']['content'].encode();n=len(raw);(root/f'{i}.html').write_bytes(raw);value='written'
                            rows.extend([content,{'role':'user','parts':[{'functionResponse':{'id':c['id'],'name':expected,'response':{'output':value}}}]}])
                        else:assert any(p.get('text','').strip() and not p.get('thought') for p in content['parts'])
                        print('TRIAL',i,turn,rid,n,round(time.monotonic()-start,3),flush=True)
            except Exception as e:print('FAILED',i,turn,type(e).__name__,str(e)[:60] if type(e)==AssertionError else '',flush=True)

if __name__=='__main__':asyncio.run(main())
