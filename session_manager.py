"""Worker-local sessions. One ACP inference producer, sequential HTTP consumers."""
import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import signal
import tempfile
import time
import uuid
from pathlib import Path
from referencing import Registry
from acp import bridge_name, upstream, ENC, request_diag, stamp
from formats import Failure, render_history
from tool_bridge import ClientCall, Validator


def contract_valid(contract,answer):
    try:contract.validate(answer);return True
    except Failure:return False


def digest(x):
    return hashlib.sha256(json.dumps(x, sort_keys=True, separators=(',', ':')).encode()).digest()


def incoming_result(kind, body):
    rows = body.get({'responses':'input','chat':'messages','gemini':'contents','anthropic':'messages'}[kind])
    if not isinstance(rows,list) or not rows:return None
    row=rows[-1]; entries=[]
    if kind=='responses' and row.get('type')=='function_call_output':entries=[(row.get('call_id'),row.get('output'),False)]
    if kind=='chat' and row.get('role')=='tool':entries=[(row.get('tool_call_id'),row.get('content'),False)]
    if kind=='anthropic' and isinstance(row.get('content'),list):entries=[(p.get('tool_use_id'),p.get('content'),p.get('is_error',False)) for p in row['content'] if p.get('type')=='tool_result']
    if kind=='gemini':entries=[(p['functionResponse'].get('id'),p['functionResponse'].get('response'),False) for p in row['parts'] if 'functionResponse' in p]
    if not entries:return None
    if len(entries)!=1:raise Failure(409,'Expected one pending tool result')
    i,value,err=entries[0]
    if not isinstance(i,str) or not i:raise Failure(409,'Missing tool result ID')
    # A cold replay needs a real prior user task and the exact historical invocation.
    def matches(m):
        if kind=='responses':return m.get('type')=='function_call' and m.get('call_id')==i
        if kind=='chat':return m.get('role')=='assistant' and any(c.get('id')==i for c in m.get('tool_calls',[]))
        if kind=='anthropic':return m.get('role')=='assistant' and isinstance(m.get('content'),list) and any(c.get('type')=='tool_use' and c.get('id')==i for c in m['content'])
        return m.get('role')=='model' and any(c.get('functionCall',{}).get('id')==i for c in m.get('parts',[]))
    def task(m):
        if m.get('role','user')!='user' or m.get('type','message')!='message':return False
        value=m.get('parts' if kind=='gemini' else 'content')
        return bool(value.strip()) if isinstance(value,str) else isinstance(value,list) and any(
            isinstance(p,dict) and p.get('type','text') in ('text','input_text') and isinstance(p.get('text'),str) and p['text'].strip() for p in value)
    has_task=False;replayable=False
    for m in rows[:-1]:
        if matches(m) and has_task:replayable=True
        has_task=has_task or task(m)
    pure=(kind not in ('anthropic','gemini') or len(row.get('content' if kind=='anthropic' else 'parts',[]))==1)
    from media_blocks import image
    media=[]
    if kind=='gemini':
        parts=next(p['functionResponse'].get('parts',[]) for p in row['parts'] if 'functionResponse' in p)
        for p in parts:
            pic=image(p,kind)
            if pic:media.append(pic)
    elif kind=='anthropic' and isinstance(value,list):
        media=[pic for p in value if (pic:=image(p,kind))]
        value=[p for p in value if p.get('type')!='image']
    return i,{'isError':bool(err),'content':[{'type':'text','text':value if isinstance(value,str) else json.dumps(value,ensure_ascii=False)},*media]},replayable,pure


class Session:
    def __init__(self,key,explicit,tools):
        self.directory=tempfile.TemporaryDirectory(prefix='acp-work-')
        self.private=tempfile.TemporaryDirectory(dir='/tmp',prefix='ag-')
        self.socket_path=self.private.name+'/s';self.config_path=self.private.name+'/key'
        self.token=secrets.token_hex(32)
        with os.fdopen(os.open(self.config_path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600),'w') as f:f.write(self.token)
        self.tools={bridge_name(i,t):t for i,t in enumerate(tools)}
        self.key=key;self.explicit=explicit;self.turn=0;self.hashes=[];self.response=None
        self.sid=self.proc=self.task=self.server=self.pending=None;self.initialized=False
        self.serial=0;self.waited=0.;self.wait_start=None
        self.state='RUNNING';self.at=time.monotonic();self.events=asyncio.Queue(64);self.handlers=set();self.context=(None,None)

    async def ipc(self,r,w):
        task=asyncio.current_task();self.handlers.add(task)
        try:
            if len(self.handlers)>8:return
            line=await asyncio.wait_for(r.readline(),5);msg=json.loads(line)
            if self.state not in ('RUNNING','WAITING_TOOL') or not hmac.compare_digest(msg['token'],self.token):return
            d=self.tools[msg['bridge_name']];raw=msg.get('arguments');args=dict(raw) if isinstance(raw,dict) else {}
            if 'toolSummary' not in d['inputSchema'].get('properties',{}):args.pop('toolSummary',None)
            if not isinstance(raw,dict) or not Validator(d['inputSchema'],registry=Registry()).is_valid(args):
                w.write(json.dumps({'isError':True,'content':[{'type':'text','text':'Invalid tool arguments: input schema validation failed'}]}).encode()+b'\n')
                await w.drain();return
            n=len(ENC.encode_ordinary(json.dumps(args,ensure_ascii=False)))
            if n>self.remaining:
                await self.events.put(('limit',None));return
            self.remaining-=n
            f=asyncio.get_running_loop().create_future();c=ClientCall(d['name'],args)
            await self.events.put(('tool',(c,f)))
            eof=asyncio.create_task(r.read(1))
            try:
                await asyncio.wait({f,eof},return_when=asyncio.FIRST_COMPLETED)
                if not f.done():
                    await self.invalidate(Failure(409,'Tool bridge disconnected'));return
                result=f.result()
            finally:
                eof.cancel();await asyncio.gather(eof,return_exceptions=True)
            w.write(json.dumps(result).encode()+b'\n');await w.drain()
            stamp(getattr(self,'diag',None),'tool_result_s')
        except (ValueError,KeyError,TypeError,OSError,TimeoutError):pass
        finally:
            self.handlers.discard(task);w.close();await w.wait_closed()

    async def stop(self,final):
        self.state='CLOSED' if final else 'SUSPENDED'
        tasks=[t for t in [self.task,*self.handlers] if t and t is not asyncio.current_task()];self.task=None
        for t in tasks:t.cancel()
        if self.proc:
            try:os.killpg(self.proc.pid,signal.SIGKILL)
            except ProcessLookupError:pass
        await asyncio.gather(*tasks,return_exceptions=True)
        if self.proc:
            try:await asyncio.wait_for(self.proc.communicate(),2)
            except asyncio.TimeoutError:
                self.proc._transport.close();await asyncio.wait_for(self.proc.wait(),2)
            self.proc=None
        self.initialized=False
        if final:
            if self.server:self.server.close();await self.server.wait_closed()
            self.directory.cleanup();self.private.cleanup()


class SessionManager:
    def __init__(self):
        self.sessions=set();self.dead={};self.guard=asyncio.Lock()
        self.cold_replay=os.getenv('ACP_ALLOW_COLD_TOOL_REPLAY')=='1'
        self.slot=os.getenv('ACP_SLOT','');self.slot=self.slot if re.fullmatch('[a-z0-9][a-z0-9-]{0,15}',self.slot) else ''
        self.warm,self.total,self.ttl,self.idle,self.wait=[max(1,int(os.getenv(k,v))) for k,v in [('MAX_WARM_SESSIONS',32),('MAX_TOTAL_SESSIONS',128),('SESSION_WARM_TTL',300),('SESSION_IDLE_TTL',1800),('TOOL_WAIT_TIMEOUT',300)]]

    def id(self,prefix):return prefix+'_'+(self.slot+'_'+uuid.uuid4().hex[:24] if self.slot else uuid.uuid4().hex)

    def remember(self,key):
        self.dead[key]=time.monotonic()
        if len(self.dead)>32768:self.dead.pop(next(iter(self.dead)))

    async def drop(self,s):
        self.sessions.discard(s)
        if s.pending:self.remember(s.pending[0]);s.pending[1].cancel();s.pending=None
        if s.explicit:self.remember(s.explicit)
        await s.stop(True)

    async def invalidate(self,s,e):
        async with self.guard:
            if s not in self.sessions:return
            if s.state=='WAITING_TOOL':await self.drop(s)
            else:s.failure=e
        if s.state=='RUNNING':await s.events.put(('error',e))

    async def delivered(self,response):
        async with self.guard:
            for s in self.sessions:
                if s.response==response and s.state=='DELIVERING':s.state='IDLE';return

    async def retire_response(self,response):
        async with self.guard:
            s=next((s for s in self.sessions if s.response==response),None)
            if s:await self.drop(s)

    async def abort(self,call):
        async with self.guard:
            s=next((s for s in self.sessions if s.pending and s.pending[0]==call),None)
            if s:await self.drop(s)

    async def suspend(self,s):
        if s.proc is None or s.proc.returncode is not None:await self.drop(s)
        else:await s.stop(False)

    async def sweep(self):
        async with self.guard:
            now=time.monotonic();self.dead={k:t for k,t in self.dead.items() if now-t<self.idle}
            for s in list(self.sessions):
                age=now-s.at
                if (s.state=='WAITING_TOOL' and age>=self.wait or s.state in ('SUSPENDED','IDLE') and age>=self.idle):await self.drop(s)
                elif s.state=='IDLE' and (age>=self.ttl or s.proc.returncode is not None):await self.suspend(s)

    async def lifecycle(self,app):
        async def reap():
            while True:await asyncio.sleep(30);await self.sweep()
        task=asyncio.create_task(reap())
        try:yield
        finally:
            task.cancel();await asyncio.gather(task,return_exceptions=True)
            for s in list(self.sessions):await self.drop(s)

    async def run(self,*,tenant,kind,model,level,system,tools,policy,base_key,contract,rows,tool_result,nonce,seq,previous,response,cap,ready,text,thought,context,prepare,metrics,headers):
        key=(tenant,kind,model,level,digest([system,policy]),digest(tools));hashes=[digest(m) for m in rows];explicit=None
        if nonce is not None or seq is not None:
            if not re.fullmatch('[0-9a-f]{64}',nonce or '') or not re.fullmatch('[1-9][0-9]{0,8}',seq or ''):raise Failure(400,'Invalid explicit session headers')
            explicit=(tenant,digest(nonce))
        async with self.guard:
            s=None;mode='cold';call=tool_result[0] if tool_result else None
            if call:
                s=next((x for x in self.sessions if x.pending and x.pending[0]==call),None)
                if s:
                    if s.state!='WAITING_TOOL' or time.monotonic()-s.at>=self.wait or (s.key!=key and not (policy[0]=='auto' and s.base_key==base_key and s.key[:4]==key[:4])) or explicit!=s.explicit or previous and previous!=s.response:raise Failure(409,'Tool continuation mismatch or expired')
                    mode='tool'
                elif (not self.cold_replay or call in self.dead or explicit or previous or not tool_result[2]
                      or not re.fullmatch('call_'+(re.escape(self.slot)+'_[0-9a-f]{24}' if self.slot else '[0-9a-f]{32}'),call)):
                    raise Failure(409,'Unknown, expired or already delivered tool call')
                else:headers['X-ACP-Session-State']='cold-replay';metrics['session_fallback']='cold_replay'
            if not s and explicit:
                s=next((x for x in self.sessions if x.explicit==explicit),None)
                if not s and (seq!='1' or explicit in self.dead):raise Failure(409,'Explicit session expired')
                if s:mode='explicit'
            if not s and previous:
                s=next((x for x in self.sessions if x.key[0]==tenant and x.response==previous),None)
                if not s:raise Failure(409,'Unknown previous response')
                mode='previous'
            if not s and not tool_result and not explicit and tenant!=('shared',) and len(rows)>1:
                matches=[x for x in self.sessions if x.key==key and not x.explicit and x.state in ('IDLE','SUSPENDED') and x.hashes and len(x.hashes)<len(hashes) and hashes[:len(x.hashes)]==x.hashes]
                if len(matches)==1:s=matches[0];mode='prefix'
            if s and ((mode!='tool' and s.key!=key) or s.explicit!=explicit or (mode!='tool' and s.state not in ('IDLE','SUSPENDED')) or (explicit and int(seq)!=s.turn+1) or previous and previous!=s.response):raise Failure(409,'Session busy or incompatible')
            if not s or s.state=='SUSPENDED':
                if not s and len(self.sessions)>=self.total:
                    victims=sorted((x for x in self.sessions if x.state in ('IDLE','SUSPENDED')),key=lambda x:x.at)
                    if not victims:raise Failure(503,'Session pool full')
                    await self.drop(victims[0])
                if sum(x.state!='SUSPENDED' for x in self.sessions)>=self.warm:
                    victims=sorted((x for x in self.sessions if x.state=='IDLE'),key=lambda x:x.at)
                    if not victims:raise Failure(503,'Warm session pool full')
                    await self.suspend(victims[0])
            if s is None:
                s=Session(key,explicit,tools);s.base_key=base_key;self.sessions.add(s);s.invalidate=lambda e:self.invalidate(s,e)
            s.diag=request_diag.get()
            if s.diag and getattr(s,'log_req_id',None):metrics['acp_log_req_id']=s.log_req_id
            s.state='RUNNING';s.at=time.monotonic();s.turn+=1
            if mode=='tool':
                s.waited+=time.monotonic()-s.wait_start;s.wait_start=None;s.remaining=cap
                self.remember(call);s.pending[1].set_result(tool_result[1]);s.pending=None
            if mode=='tool':
                if not tool_result[3] or hashes[:-1]!=s.observed:s.observed=None
                elif s.observed is not None:s.observed.append(hashes[-1])
            elif mode in ('explicit','previous'):s.observed=s.hashes+hashes
            else:s.observed=list(hashes)
            s.response=response;s.request_cap=cap;s.counter_epoch=s.turn
            if mode!='tool':s.last_count=0;s.invoked=False
        try:
            metrics['session_hit']=mode!='cold';headers.setdefault('X-ACP-Session-State',mode)
            if mode!='tool':
                if tools and not s.server:s.server=await asyncio.start_unix_server(s.ipc,path=s.socket_path,limit=4<<20)
                delta_rows=rows[len(s.hashes):] if mode=='prefix' else rows
                prompt,archive=await prepare(delta_rows,mode=='cold')
                async def put(k,v):await s.events.put((k,v))
                def update(u,n):s.context=(u,n)
                async def inference(p,a):
                    return await upstream(model,p,lambda:put('ready',None),lambda v:put('text',v),cap,level,tools,archive=a,lease=s,system_text=system,thought_delta=(lambda v:put('thought',v)) if thought else None,context_update=update,managed=s)
                async def produce():
                    try:
                        try:result=await inference(prompt,archive)
                        except Failure as e:
                            if mode!='prefix' or 'ACP session/resume failed' not in str(e):raise
                            async with self.guard:
                                await s.stop(False);s.state='RUNNING';s.task=asyncio.current_task()
                                s.directory.cleanup();s.directory=tempfile.TemporaryDirectory(prefix='acp-work-')
                                s.sid=None;s.hashes=[];s.context=(None,None)
                            metrics.update(session_hit=False,session_fallback='cold_replay');headers['X-ACP-Session-State']='cold-replay'
                            result=await inference(*await prepare(rows,True))
                        if policy[0] in ('specific','required') and result[1]=='stop' and not s.invoked:
                            result=await inference('Invoke the required client tool now. Do not answer with text.',None)
                            if not s.invoked:raise Failure(502,'Model did not satisfy forced tool_choice')
                        await put('end',result)
                    except Exception as e:await self.invalidate(s,e)
                s.remaining=cap;s.task=asyncio.create_task(produce())
            else:
                if ready:await ready()
            pieces=[]
            while True:
                k,v=await s.events.get();context(*s.context)
                if getattr(s,'failure',None):raise s.failure
                if k=='ready' and ready:await ready()
                elif k=='text':pieces.append(v);await text(v)
                elif k=='thought' and thought:await thought(v)
                elif k=='error':raise v
                elif k=='limit':
                    await self.drop(s);return '', 'length', {}
                elif k=='tool':
                    stamp(s.diag,'complete_tool_s')
                    s.invoked=True;c,f=v;c.id=self.id('call');s.pending=(c.id,f);s.at=time.monotonic();s.state='WAITING_TOOL';s.wait_start=s.at
                    def record(rows):
                        if rows is None:s.observed=None
                        elif s.observed is not None:s.observed.extend(digest(m) for m in rows)
                    c.record=record;raise c
                elif k=='end':
                    _,stop,usage=v;answer=''.join(pieces);v=answer,stop,usage
                    if any(x in answer for x in contract.stops) or contract.format!='text' and not contract_valid(contract,answer) or stop!='stop' or getattr(s,'incomplete',False):await self.drop(s)
                    else:
                        s.hashes=[] if s.observed is None else [*s.observed,digest(dict(role='assistant',content=answer))]
                        s.at=time.monotonic();s.state='DELIVERING'
                    return v
        except ClientCall:raise
        except BaseException:
            await self.drop(s);raise
