"""Socket-activated, credential-free affinity proxy. The selected worker authenticates every request."""
import asyncio
from collections import Counter, OrderedDict
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import time

from aiohttp import ClientError, DummyCookieJar, ClientSession, ClientTimeout, UnixConnector, web
from multidict import CIMultiDict

LIMIT = 12 << 20
SLOT = re.compile(r'lab(?:[1-9]|[1-5][0-9]|6[0-4])')
HOP = {'connection','keep-alive','proxy-authenticate','proxy-authorization','te','trailer','transfer-encoding','upgrade','proxy-connection','host'}


def headers(h):
    skip = HOP | {s.strip().lower() for v in h.getall('Connection', []) for s in v.split(',')}
    return CIMultiDict((k,v) for k,v in h.items() if k.lower() not in skip)


def digest(values):
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(',', ':')).encode()).digest()


def hint(value, prefix):
    m = re.fullmatch(prefix + rf'_({SLOT.pattern})_[0-9a-f]{{24}}', value) if isinstance(value,str) else None
    return m[1] if m else None


def shape(b):
    rows = b.get('messages', b.get('contents', b.get('input', [])))
    if isinstance(rows,str): rows = [{'role':'user','content':rows}]
    if not isinstance(rows,list): rows=[]
    system = b.get('system', b.get('instructions', b.get('systemInstruction')))
    first = None
    for r in rows:
        if not isinstance(r,dict):continue
        if system is None and r.get('role') in ('system','developer'):system=r.get('content')
        if first is None and r.get('role','user')=='user':first=r.get('content',r.get('parts'))
    last = rows[-1] if rows and isinstance(rows[-1],dict) else {}
    ids = [hint(b.get('previous_response_id'),'resp')]
    if last.get('role')=='tool':ids.append(hint(last.get('tool_call_id'),'call'))
    if last.get('type')=='function_call_output':ids.append(hint(last.get('call_id'),'call'))
    parts=last.get('parts',last.get('content'))
    for p in parts if isinstance(parts,list) else ():
        if not isinstance(p,dict):continue
        if p.get('type')=='tool_result':ids.append(hint(p.get('tool_use_id'),'call'))
        if isinstance(p.get('functionResponse'),dict):ids.append(hint(p['functionResponse'].get('id'),'call'))
    return set(filter(None,ids)), (system,first) if first is not None else None


class Reject(Exception):
    def __init__(self,status,message):self.status=status;super().__init__(message)


class Router:
    def __init__(self,root):
        self.root=Path(root);self.version=None;self.slots=();self.table=OrderedDict();self.active=Counter();self.cursor=0;self.clients={};self.readers=0

    def refresh(self):
        path=self.root/'pool.json'
        try:
            st=path.stat();version=(st.st_ino,st.st_mtime_ns)
            if version!=self.version:
                if st.st_size>8192:raise ValueError()
                slots=json.loads(path.read_bytes())['slots']
                if not isinstance(slots,list) or len(slots)>64 or not all(isinstance(s,str) and SLOT.fullmatch(s) for s in slots):raise ValueError()
                self.slots=tuple(dict.fromkeys(slots));self.version=version
        except (OSError,ValueError,KeyError,TypeError):self.slots=();self.version=None
        if not self.slots:raise Reject(503,'No healthy account slot')

    def select(self,req,b):
        self.refresh();ids,first=shape(b)
        tenant=[req.headers.get(k) for k in ('Authorization','x-api-key','x-goog-api-key','X-ACP-Session-Key')]+[req.query.get('key')]
        explicit=req.headers.get('X-ACP-Session');endpoint=req.path.removesuffix(':streamGenerateContent').removesuffix(':generateContent');key=digest([tenant,endpoint,explicit or first]) if explicit or first else None
        now=time.monotonic();t=self.table
        while t and next(iter(t.values()))[1]<=now:t.popitem(last=False)
        cached=t.get(key,(None,))[0]
        if explicit and cached:ids.add(cached)
        if len(ids)>1:raise Reject(409,'Conflicting continuation slots')
        if ids:
            slot=next(iter(ids))
            if slot not in self.slots:raise Reject(503,'Continuation slot offline')
        elif cached in self.slots:slot=cached
        elif explicit:slot=max(self.slots,key=lambda s:digest([key.hex(),s]))
        else:
            rotation=self.slots[self.cursor:]+self.slots[:self.cursor];slot=min(rotation,key=self.active.__getitem__);self.cursor=(self.slots.index(slot)+1)%len(self.slots)
        if key and (explicit or not ids):
            t[key]=(slot,now+1800);t.move_to_end(key)
            if len(t)>4096:t.popitem(last=False)
        return slot

    async def proxy(self,req):
        downstream=None;slot=None
        try:
            if req.headers.get('Content-Encoding','identity').lower()!='identity':raise Reject(415,'Encoded requests unsupported')
            if self.readers>=16:raise Reject(503,'Router intake busy')
            self.readers+=1
            reading=True
            def release():
                nonlocal reading
                if reading:
                    self.readers-=1;reading=False
            try:
                raw=bytearray()
                async with asyncio.timeout(15):
                    async for chunk in req.content.iter_chunked(65536):
                        if len(raw)+len(chunk)>LIMIT:raise Reject(413,'Request exceeds router limit')
                        raw.extend(chunk)
                b=json.loads(raw) if raw else {}
                if not isinstance(b,dict):raise Reject(400,'Expected JSON object')
                slot=self.select(req,b);self.active[slot]+=1;del b
                if slot not in self.clients:self.clients[slot]=ClientSession(connector=UnixConnector(path=str(self.root/(slot+'.sock')),limit=0),auto_decompress=False,timeout=ClientTimeout(total=None,sock_connect=5,sock_read=1150),trust_env=False,cookie_jar=DummyCookieJar())
                # Release intake capacity after upload, not after a long non-streamed generation.
                async def upload():
                    try:
                        for i in range(0,len(raw),65536):yield bytes(raw[i:i+65536])
                    finally:
                        raw.clear();release()
                data=upload() if raw else None
                if data is None:release()
                outgoing=headers(req.headers)
                if data is not None:outgoing.popall('Content-Length',None)
                response=await self.clients[slot].request(req.method,'http://localhost'+req.raw_path,data=data,headers=outgoing,allow_redirects=False)
            finally:release()
            async with response:
                downstream=web.StreamResponse(status=response.status,headers=headers(response.headers));await downstream.prepare(req)
                async for chunk in response.content.iter_any():await downstream.write(chunk)
                await downstream.write_eof();return downstream
        except Reject as e:status,message=e.status,str(e)
        except (ValueError,RecursionError):status,message=400,'Invalid JSON request'
        except web.HTTPRequestEntityTooLarge:status,message=413,'Request exceeds router limit'
        except TimeoutError:status,message=504,'Router timed out'
        except (ClientError,OSError):status,message=503,'Selected worker unavailable'
        finally:
            if slot:self.active[slot]-=1
        if downstream is not None and downstream.prepared:
            if req.transport:req.transport.abort()
            return downstream
        return web.json_response({'error':{'message':message}},status=status)

    async def cleanup(self,app):
        for c in self.clients.values():await c.close()


def make_app(root='/run/acp-gateway-sockets'):
    router=Router(root);app=web.Application(client_max_size=LIMIT,handler_args={'auto_decompress':False});app.router.add_route('*','/{path:.*}',router.proxy);app.on_cleanup.append(router.cleanup);return app


if __name__=='__main__':
    if os.getenv('LISTEN_PID')!=str(os.getpid()) or os.getenv('LISTEN_FDS')!='1':raise SystemExit('Requires acp-router.socket')
    web.run_app(make_app(),sock=socket.socket(fileno=3),handler_cancellation=True,access_log=None)
