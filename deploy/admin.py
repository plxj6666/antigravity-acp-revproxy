"""One unprivileged, socket-activated credential enrollment service per slot."""
import asyncio, json, os, re, secrets, signal, socket, tempfile
from pathlib import Path
from aiohttp import web
from quota import snapshot

AGY='/opt/acp-gateway/bin/agy'
CRED=Path('.gemini/antigravity-cli/antigravity-oauth-token')
ORIGIN=os.environ['ACP_ADMIN_ORIGIN']
lock=asyncio.Lock()
pending=None

def failure(msg):
    raise web.HTTPBadRequest(text=msg)

def credential(value):
    if value.startswith('{'):
        try: obj=json.loads(value)
        except ValueError: failure('JSON 格式错误')
        token=obj.get('token',obj) if isinstance(obj,dict) else None
        if not isinstance(token,dict) or obj.get('auth_method','consumer') not in ('consumer',''): failure('仅支持个人账号凭据')
        fields={k:v for k,v in token.items() if k in ('access_token','refresh_token','expiry','token_type') and isinstance(v,str)}
    elif value.startswith('1//'):
        obj={}; fields={'refresh_token':value,'token_type':'Bearer'}
    else: failure('请使用 Antigravity refresh token 或凭据 JSON')
    if not (fields.get('access_token') or fields.get('refresh_token')): failure('缺少 token')
    return {'token':fields,'auth_method':'consumer',**{k:obj[k] for k in ('id_token','id_token_email') if isinstance(obj.get(k),str)}}

async def spawn(home,*args):
    return await asyncio.create_subprocess_exec(AGY,*args,cwd=home,env={'HOME':home,'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'},stdin=-1,stdout=-1,stderr=-2,start_new_session=True)

async def kill(p):
    if p.returncode is None:
        try: os.killpg(p.pid,signal.SIGKILL)
        except ProcessLookupError: pass
    await p.wait()

async def validate(home):
    p=await spawn(home,'models')
    try:
        await asyncio.wait_for(p.communicate(),35)
        if p.returncode: failure('agy 未能验证账号。请检查凭据、账号权限或重试；原账号未变更。')
    finally: await kill(p)
    src=Path(home)/CRED
    if not src.is_file(): failure('未找到可保存的 CLI 凭据，原账号未变更')
    dst=Path.home()/CRED; dst.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=dst.parent,delete=False) as tmp:
        try: tmp.write(src.read_bytes()); tmp.flush(); os.replace(tmp.name,dst)
        finally: Path(tmp.name).unlink(missing_ok=True)

async def cleanup():
    global pending
    if pending:
        p,d,_,timer=pending; pending=None; timer.cancel(); await kill(p); d.cleanup()

async def expire(ticket):
    async with lock:
        if pending and pending[2]==ticket: await cleanup()

async def handle(r):
    global pending
    if r.headers.get('Origin')!=ORIGIN or r.content_type!='application/json': raise web.HTTPForbidden()
    try: body=await r.json()
    except ValueError: failure('请求不是 JSON')
    if not isinstance(body,dict): failure('请求格式错误')
    value=body.get('value',''); op=r.match_info['op']
    if not isinstance(value,str) or len(value)>32000: failure('输入无效')
    async with lock:
        try:
            if op=='start':
                await cleanup(); d=tempfile.TemporaryDirectory(); ticket=secrets.token_urlsafe(32)
                try: p=await spawn(d.name,'acp','--login')
                except BaseException: d.cleanup(); raise
                timer=asyncio.get_running_loop().call_later(300,lambda:asyncio.create_task(expire(ticket)))
                pending=(p,d,ticket,timer)
                try:
                    async with asyncio.timeout(12):
                        while line:=await p.stdout.readline():
                            url=line.decode(errors='replace').strip()
                            if re.fullmatch(r'https://accounts\.google\.com/o/oauth2/[^\s]+',url): return web.json_response({'url':url,'ticket':ticket})
                        failure('无法发起 Google 授权')
                except BaseException: await cleanup(); raise
            if op=='finish':
                if not pending or pending[2]!=body.get('ticket'): failure('授权已过期，请重新开始')
                if not value or '\n' in value or '\r' in value: failure('请输入单行授权码或回调地址')
                p,d,_,_=pending
                try:
                    await asyncio.wait_for(p.communicate((value+'\n').encode()),35)
                    if p.returncode: failure('Google 授权未完成，请重新开始')
                    await validate(d.name)
                finally: await cleanup()
            elif op=='import':
                doc=credential(value.strip()); await cleanup()
                with tempfile.TemporaryDirectory() as d:
                    f=Path(d)/CRED; f.parent.mkdir(parents=True); f.write_text(json.dumps(doc)); await validate(d)
            else: raise web.HTTPNotFound()
        except TimeoutError: failure('验证超时，请稍后重试')
    return web.json_response({'ok':True})

async def quotas(r): return web.json_response(await snapshot())

async def status(r): return web.json_response({'configured':(Path.home()/CRED).is_file()})

if __name__=='__main__':
    os.umask(0o077); app=web.Application(client_max_size=40000); app.router.add_get('/status',status); app.router.add_get('/quota',quotas); app.router.add_post('/{op:start|finish|import}',handle)
    web.run_app(app,sock=socket.socket(fileno=3),access_log=None,print=None)
