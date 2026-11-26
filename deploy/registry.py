import asyncio,os,re,socket
ADMIN_ORIGIN=os.environ['ACP_ADMIN_ORIGIN']
from pathlib import Path
from aiohttp import web
lock=asyncio.Lock()
async def catalog(r):
    slots=[p.stem[6:] for p in Path('/run/acp-gateway-sockets').glob('admin-lab*.sock') if re.fullmatch(r'admin-lab[1-9][0-9]*',p.stem)]
    return web.json_response(sorted(slots,key=lambda n:int(n[3:])))
async def allocate(r):
    if r.headers.get('Origin')!=ADMIN_ORIGIN or r.content_type!='application/json': raise web.HTTPForbidden()
    if lock.locked(): raise web.HTTPConflict(text='另一次创建正在进行')
    async with lock:
        p=await asyncio.create_subprocess_exec('/usr/bin/sudo','-n','/usr/local/sbin/acp-add-slot',stdin=-3,stdout=-1,stderr=-3)
        out,_=await p.communicate()
        if p.returncode==3: raise web.HTTPConflict(text='已达到服务器容量限制（64 个账号）')
        if p.returncode or not re.fullmatch(rb'lab[1-9][0-9]*\n',out): raise web.HTTPServiceUnavailable(text='未能创建账号槽位，请联系管理员')
        return web.json_response({'slot':out.decode().strip()},status=201)
app=web.Application(client_max_size=1024)
app.router.add_get('/accounts',catalog);app.router.add_post('/accounts',allocate)
web.run_app(app,sock=socket.socket(fileno=3),access_log=None,print=None)
