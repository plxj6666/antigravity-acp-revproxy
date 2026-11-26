"""Exercise installed CLI binaries against a local gateway, with disposable homes.
Print only request shapes, statuses and whether the client received the marker.
Set PROBE_BASE and GATEWAY_API_KEY to test a live gateway via the loopback relay.
"""
import asyncio, json, os, shutil, tempfile
from pathlib import Path
from aiohttp import web, ClientSession

async def main():
    base=os.getenv('PROBE_BASE'); names=os.getenv('CLIENTS','codex claude gemini agy').split()
    async with ClientSession() as client:
        async def relay(req):
            b=await req.json(); start=asyncio.get_running_loop().time()
            effort=os.getenv('PROBE_EFFORT')
            if effort and req.path.endswith('/responses'):b.setdefault('reasoning',{})['effort']=effort
            if effort and req.path.endswith('/messages'):b.setdefault('output_config',{})['effort']=effort
            print('request',req.path,'effort', b.get('reasoning',{}).get('effort') or b.get('output_config',{}).get('effort'),flush=True)
            if not base:return web.json_response({'error':{'message':'probe'}},status=400)
            async with client.post(base+req.path_qs,json=b,headers={'Authorization':'Bearer '+os.environ['GATEWAY_API_KEY']}) as r:
                downstream=web.StreamResponse(status=r.status,headers={'Content-Type':r.headers.get('Content-Type','application/json')})
                await downstream.prepare(req);n=0
                async for chunk in r.content.iter_any():
                    if not n:print('first bytes',req.path,round(asyncio.get_running_loop().time()-start,2),flush=True)
                    n+=len(chunk);await downstream.write(chunk)
                await downstream.write_eof()
                print('complete',req.path,r.status,n,round(asyncio.get_running_loop().time()-start,2),flush=True)
                return downstream
        app=web.Application(client_max_size=4<<20);app.router.add_post('/{p:.*}',relay);runner=web.AppRunner(app);await runner.setup();site=web.TCPSite(runner,'127.0.0.1',0);await site.start();url='http://127.0.0.1:'+str(site._server.sockets[0].getsockname()[1]); model='gemini-4-neon'; marker=os.urandom(12).hex(); prompt=('Use your local client file reading tool to read marker.txt from the user workspace. Reply with only the value from that file.' if os.getenv('TEST_CLIENT_TOOLS') else 'Respond exactly: CLI_CHECK_OK. No tools.')
        async def run(name):
            exe=shutil.which(name); print('Testing',name,flush=True)
            if not exe:return 'not installed'
            with tempfile.TemporaryDirectory() as home:
                h=Path(home); (h/'work').mkdir();(h/'work/marker.txt').write_text(marker);(h/'.codex').mkdir(); (h/'.gemini/antigravity-cli').mkdir(parents=True); (h/'.gemini/antigravity-cli/settings.json').write_text('{"modelProvider":"gemini"}');(h/'.gemini/settings.json').write_text('{"security":{"auth":{"selectedType":"gemini-api-key"}}}')
                env=dict(PATH=os.environ['PATH'],HOME=home,CODEX_HOME=home+'/.codex',TMPDIR=tempfile.gettempdir(),ANTHROPIC_API_KEY='dummy',ANTHROPIC_BASE_URL=url,GOOGLE_GEMINI_BASE_URL=url,GEMINI_API_KEY='dummy',GW_KEY='dummy',CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1',CI='1',GEMINI_CLI_TRUST_WORKSPACE='true')
                if os.getenv('TEST_CLIENT_TOOLS'): prompt_local=prompt.replace('marker.txt',str(h/'work/marker.txt'))
                else:prompt_local=prompt
                args={'codex':['exec','--ephemeral','--skip-git-repo-check','-s','read-only','-c','model_provider="gateway"','-c','model_providers.gateway.name="gateway"','-c',f'model_providers.gateway.base_url="{url}/v1"','-c','model_providers.gateway.env_key="GW_KEY"','-m',model,prompt], 'claude':['-p','--model',model,'--no-session-persistence',prompt], 'gemini':['-m',model,'-p',prompt], 'agy':['--sandbox','--model',model,'-p',prompt]}[name]
                proc=await asyncio.create_subprocess_exec(exe,*(prompt_local if a==prompt else a for a in args),cwd=h/'work',env=env,stdin=-3,stdout=-1,stderr=-1,start_new_session=True)
                try:out,err=await asyncio.wait_for(proc.communicate(),int(os.getenv('PROBE_TIMEOUT','90')));return (proc.returncode,(marker if os.getenv('TEST_CLIENT_TOOLS') else 'CLI_CHECK_OK') in out.decode(errors='replace'),len(out),len(err))
                except asyncio.TimeoutError:os.killpg(proc.pid,9);await proc.communicate();return 'timeout'
        try:
            for n in names: print(n,await run(n),flush=True)
        finally:await runner.cleanup()
if __name__=='__main__':asyncio.run(main())
