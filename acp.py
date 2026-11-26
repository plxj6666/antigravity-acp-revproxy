"""One sandboxed ACP subprocess per request."""
import asyncio
from contextlib import nullcontext
import json
import os
import signal
import time
from contextvars import ContextVar

request_diag=ContextVar('request_diag',default=None)

def stamp(diag,key):
    if diag:
        metrics,start=diag
        metrics[key]=round(time.monotonic()-start,3)

def log_args(diag):
    directory=os.getenv('ACP_LOG_DIR')
    if not directory or not diag:return []
    folder=Path(directory)
    folder.mkdir(mode=0o700,parents=True,exist_ok=True)
    st=folder.lstat()
    if not folder.is_dir() or folder.is_symlink() or st.st_uid!=os.getuid() or st.st_mode & 0o077:
        raise Failure(500,'ACP_LOG_DIR must be a private owner-only directory')
    rid=diag[0]['req_id']
    # A failed resume can spawn a second process in this HTTP request.
    launch=diag[0].get('acp_log_launch',0)+1
    diag[0]['acp_log_launch']=launch
    path=folder/(rid+('' if launch==1 else '-'+str(launch))+'.log')
    fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
    return ['--log-file',str(path.resolve())]
import re
import sys
from tool_bridge import ClientCall, Validator
from referencing import Registry
import tempfile
import unicodedata
from pathlib import Path

import tiktoken

# Pre-fetch the verified o200k_base vocabulary here before starting offline workers.
os.environ["TIKTOKEN_CACHE_DIR"] = str(Path(__file__).resolve().parent / "vocab")
ENC = tiktoken.get_encoding("o200k_base")
OUTPUT_LIMIT = 100_000


def clip_chunk(piece, remaining):
    for i in range(0, len(piece), 4096):
        part = piece[i:i+4096]
        ids = ENC.encode_ordinary(part)
        if len(ids) > remaining:
            yield ENC.decode_bytes(ids[:remaining]).decode("utf-8", "ignore"), remaining, True
            return
        remaining -= len(ids)
        yield part, len(ids), False

from formats import ALIAS, Failure


def model_options(session):
    for cfg in session.get('configOptions') or []:
        if cfg.get('category') == 'model' or cfg.get('id') == 'model':
            values = [o['value'] for group in cfg.get('options', [])
                      for o in group.get('options', [group]) if 'value' in o]
            return cfg['id'], values
    legacy = session.get('models') or {}
    return None, [o['modelId'] for o in legacy.get('availableModels', [])]


def bridge_name(i, declaration):
    # ACP flattens server + MCP tool names. Keep the client verb recognizable
    # while ensuring every projected name is unique and ASCII-safe.
    verb=re.sub('[^A-Za-z0-9_]', '_', declaration['name'])[:45]
    return f'pi_{verb}_{i}'


def public_tool_label(update):
    """Only ACP display metadata, never tool payloads or thought chunks."""
    def clean(s):
        if not isinstance(s,str):return ''
        return ' '.join(''.join(ch if not unicodedata.category(ch).startswith('C') else ' ' for ch in s[:1024]).split())[:240]
    title=clean(update.get('title'));name=update.get('name','')
    # Some agy versions use the wire name as a placeholder rather than a title.
    if title and title.casefold() not in {str(name).casefold(),'run '+str(name).casefold()}:return title
    return ''


async def exchange(proc, cwd, model, prompt, ready, delta, token_cap, level=None, tools=(), servers=(), lease=None, *, system_text='', thought_delta=None, context_update=None, managed=None):
    serial, chunks, thought_size, text_size = getattr(managed,"serial",0), [], 0, 0
    sid = None
    used = 0
    upstream_base=0

    class LocalLimit(Exception):
        pass

    async def send(msg):
        proc.stdin.write((json.dumps({'jsonrpc': '2.0', **msg}) + '\n').encode())
        await proc.stdin.drain()

    async def call(method, params):
        nonlocal serial, thought_size, text_size, used, upstream_base
        serial += 1
        if managed:managed.serial=serial
        await send({'id': serial, 'method': method, 'params': params})
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                raise Failure(502, 'agy stopped before replying; check CLI installation and sign-in')
            m = json.loads(raw)
            if 'method' in m:
                if 'id' in m:
                    reply = ({'result': {'outcome': {'outcome': 'cancelled'}}}
                             if m['method'] == 'session/request_permission' else
                             {'error': {'code': -32601, 'message': 'Client method not supported'}})
                    if m['method']=='session/request_permission' and method=='session/prompt':
                        params=m.get('params') or {}; tc=params.get('toolCall') or {}
                        tool_name=tc.get('name')
                        allowed=(tool_name=='mcp_history_lookup' and any(s['name']=='history' for s in servers)) or (managed and tool_name in {'mcp_client_'+bridge_name(i,d) for i,d in enumerate(tools)})
                        if params.get('sessionId')==sid and allowed:
                            once=next((o for o in params.get('options',[]) if o.get('kind')=='allow_once'),None)
                            if once:reply={'result':{'outcome':{'outcome':'selected','optionId':once['optionId']}}}
                    diag=getattr(managed,'diag',None) if managed else request_diag.get()
                    if diag and m['method']=='session/request_permission' and reply.get('result',{}).get('outcome',{}).get('outcome')=='cancelled':
                        diag[0]['denied_native_tools']+=1
                    await send({'id': m['id'], **reply})
                elif m['method'] == 'session/update' and method == 'session/prompt':
                    p = m.get('params', {})
                    u = p.get('update', {})
                    c = u.get('content', {})
                    if p.get('sessionId')!=sid:continue
                    diag=getattr(managed,'diag',None) if managed else request_diag.get()
                    if diag:
                        metrics=diag[0]
                        stamp(diag,'last_acp_update_s')
                        if 'tool_result_s' in metrics:metrics.setdefault('post_result_update_s',metrics['last_acp_update_s'])
                        if u.get('sessionUpdate')=='agent_thought_chunk':metrics['thought_chunks']+=1
                    if u.get('sessionUpdate')=='usage_update' and context_update:
                        context_update(u.get('used'),u.get('size'))
                    if u.get('sessionUpdate')=='usage_update' and type(u.get('outputTokens')) is int:
                        # Only an explicit output counter is comparable with a turn cap.
                        count=u['outputTokens']
                        if managed and managed.counter_epoch!=getattr(managed,'seen_epoch',None):
                            upstream_base=managed.last_count;managed.seen_epoch=managed.counter_epoch
                        if managed:managed.last_count=count
                        if count-upstream_base >= (managed.request_cap if managed else token_cap):raise LocalLimit
                    if u.get('sessionUpdate')=='agent_thought_chunk' and c.get('type')=='text':
                        thought_size+=len(c['text'].encode())
                        if thought_size>2**21:raise Failure(502,'Agent response too large')
                        if thought_delta is None:continue
                        for piece,n,limited in clip_chunk(c['text'],managed.remaining if managed else token_cap-used):
                            used+=n
                            if managed:managed.remaining-=n
                            if piece and thought_delta:await thought_delta(piece)
                            if limited:raise LocalLimit
                    if u.get('sessionUpdate')=='tool_call' and not managed:
                        for i,d in enumerate(tools):
                            if u.get('name')=='mcp_client_'+bridge_name(i,d):
                                args=u.get('rawInput'); props=d['inputSchema'].get('properties',{})
                                if not isinstance(args,dict):raise Failure(502,'Malformed ACP tool arguments')
                                args=dict(args)
                                label=public_tool_label(u)
                                if 'toolSummary' not in props:args.pop('toolSummary',None)
                                if not Validator(d['inputSchema'],registry=Registry()).is_valid(args):
                                    raise Failure(502,'Model supplied invalid arguments for client tool '+d['name'])
                                if len(ENC.encode_ordinary(json.dumps(args,ensure_ascii=False)))>token_cap-used:raise LocalLimit
                                invocation=ClientCall(d['name'],args);invocation.public_text=''.join(chunks);invocation.label=label
                                raise invocation
                    if p.get('sessionId') == sid and u.get('sessionUpdate') == 'agent_message_chunk' and c.get('type') == 'text':
                        text_size += len(c['text'].encode())
                        if text_size > 2**20:
                            raise Failure(502, 'Agent response too large')
                        for piece, n, limited in clip_chunk(c['text'], managed.remaining if managed else token_cap-used):
                            used += n
                            if managed:managed.remaining-=n
                            if piece:
                                chunks.append(piece)
                                if delta:
                                    await delta(piece)
                            if limited:
                                raise LocalLimit
            elif m.get('id') == serial:
                if 'error' in m:
                    if m['error'].get('code') == -32000:
                        raise Failure(503, 'Antigravity login required: run agy acp --login on the host')
                    raise Failure(502, f"ACP {method} failed (code {m['error'].get('code')})")
                return m['result']

    if managed and managed.initialized:
        sid=managed.sid
        if hasattr(level,'confirm'):level.confirm(getattr(managed,'actual_level',str(level)))
    else:
        init = await call('initialize', {'protocolVersion': 1, 'clientCapabilities': {}, 'clientInfo': {'name': 'lab-chat', 'version': '1.0'}})
        if init['protocolVersion'] != 1:
            raise Failure(502, 'Unsupported ACP version')
        params={'cwd':cwd,'mcpServers':list(servers)}
        resumed=lease is not None and lease.sid is not None
        if resumed:
            sid=lease.sid
            await call('session/resume',{**params,'sessionId':sid})
            session={}
        else:
            if system_text and prompt is not None:
                blocks=list(prompt) if isinstance(prompt,list) else [{'type':'text','text':prompt}]
                prefix=system_text+'\n\n--- Current request ---\n'
                if blocks and blocks[0]['type']=='text':
                    blocks[0]={**blocks[0],'text':prefix+blocks[0]['text']}
                else:blocks.insert(0,{'type':'text','text':prefix})
                prompt=blocks
            session=await call('session/new',params);sid=session['sessionId']
            if lease is not None:lease.sid=sid
        key, ids = model_options(session)
        if prompt is None:
            return {'object': 'list', 'data': [{'id': x, 'object': 'model', 'owned_by': 'antigravity'} for x in dict.fromkeys([ALIAS, *ids])]}
        if model != ALIAS and not resumed:
            if model not in ids:
                raise Failure(400, 'Unknown model; see GET /v1/models')
            if key:
                selected = await call('session/set_config_option', {'sessionId': sid, 'configId': key, 'value': model})
                session = {**session, **selected}
            else:
                await call('session/set_model', {'sessionId': sid, 'modelId': model})
        if level and resumed and hasattr(level,'confirm'):level.confirm(getattr(lease,'actual_level',str(level)))
        if level and resumed and level!=lease.level:raise Failure(409,'Reasoning level changed in active session')
        if lease is not None and not resumed:lease.level=level
        if level and not resumed:
            config=next((c for c in session.get('configOptions',[]) if c.get('id')=='thought_level'),{})
            available=[o.get('value') for o in config.get('options',[])]
            if hasattr(level,'select'):
                requested=level;level=requested.select(available)
                if lease is not None:lease.actual_level=level
                requested.confirm(level)
            if level not in available:
                raise Failure(400,'Model does not advertise native thinking level: '+level)
            selected=await call('session/set_config_option',{'sessionId':sid,'configId':'thought_level','value':level})
            if not any(c.get('id')=='thought_level' and c.get('currentValue')==level for c in selected.get('configOptions',[])):
                raise Failure(502,'ACP did not confirm requested thinking level')
        if managed:managed.initialized=True
    if ready:
        await ready()
    try:
        result = await call('session/prompt', {'sessionId': sid, 'prompt': prompt if isinstance(prompt,list) else [{'type': 'text', 'text': prompt}]})
    except LocalLimit:
        if lease is not None:lease.incomplete=True
        return ''.join(chunks), 'length', {}
    reasons = {'end_turn': 'stop', 'max_tokens': 'length', 'max_turn_requests': 'length', 'refusal': 'content_filter'}
    if result.get('stopReason') not in reasons:
        raise Failure(502, 'Agent turn interrupted')
    if not ''.join(chunks).strip() and result.get('stopReason')=='end_turn':raise Failure(502,'Upstream ended turn without visible content or client tool call')
    return ''.join(chunks), reasons[result['stopReason']], result.get('usage') or {}


async def active_timeout(session, coro, seconds):
    import time
    def active():
        return (session.wait_start if session.wait_start is not None else time.monotonic())-session.waited
    deadline=active()+seconds
    task=asyncio.create_task(coro)
    try:
        while not task.done():
            left=deadline-active()
            if left<=0:raise asyncio.TimeoutError
            await asyncio.wait({task},timeout=left)
        return task.result()
    finally:
        task.cancel();await asyncio.gather(task,return_exceptions=True)


async def upstream(model=None, prompt=None, ready=None, delta=None, token_cap=OUTPUT_LIMIT, level=None, tools=(), *, archive=None, lease=None, system_text='', thought_delta=None, context_update=None, managed=None):
    with (nullcontext(lease.directory.name) if lease else tempfile.TemporaryDirectory(prefix='acp-chat-')) as cwd, (nullcontext(managed.private.name) if managed else tempfile.TemporaryDirectory(prefix='acp-catalog-')) as meta:
        servers=[]
        if archive:
            hist=Path(meta)/'history.json';hist.write_text(json.dumps(archive))
            servers.append({'name':'history','command':sys.executable,'args':['-I',str(Path(__file__).resolve().with_name('history_archive.py')),str(hist)],'env':[]})
        if tools:
            path=Path(meta)/'tools.json'
            catalog=[]
            for i,d in enumerate(tools):
                # agy injects toolSummary itself. Avoid duplicate required keys when
                # the requesting client is also agy; retain it on the outbound call.
                schema=dict(d['inputSchema'])
                if 'properties' in schema:
                    schema['properties']={k:v for k,v in schema['properties'].items() if k!='toolSummary'}
                if 'required' in schema:schema['required']=[k for k in schema['required'] if k!='toolSummary']
                catalog.append({**d,'name':bridge_name(i,d),'description':d['name']+': '+d['description'],'inputSchema':schema})
            path.write_text(json.dumps(catalog))
            servers.append({'name':'client','command':sys.executable,'args':['-I',str(Path(__file__).resolve().with_name('mcp_bridge.py' if managed else 'mcp_catalog.py')),str(path),*([managed.socket_path,managed.config_path] if managed else [])],'env':[]})
            system_text += "\nAccess the client's workspace only via the client MCP pi_* tools. Do not use native host tools or bypass denials. Respect the user's testing, format and language instructions."
        env = os.environ.copy()
        for secret in ('GATEWAY_API_KEY','GATEWAY_API_KEYS_JSON','ACP_SESSION_KEYS_JSON'):env.pop(secret,None)
        existing=managed.proc if managed else None
        diag=request_diag.get()
        if managed and diag and os.getenv('ACP_LOG_DIR'):
            if not existing:managed.log_req_id=diag[0]['req_id']
            diag[0]['acp_log_req_id']=managed.log_req_id
        proc = existing or await asyncio.create_subprocess_exec(os.environ.get('AGY_BIN', 'agy'), 'acp', '--sandbox', '--include-default-customizations=false', *log_args(request_diag.get()),
            stdin=-1, stdout=-1, stderr=-3, cwd=cwd, env=env, start_new_session=True, limit=4*1024*1024)
        if managed:
            managed.proc=proc
            return await active_timeout(managed,exchange(proc,cwd,model,prompt,ready,delta,token_cap,level,tools,servers,lease,system_text=system_text,thought_delta=thought_delta,context_update=context_update,managed=managed),1000)
        try:
            return await asyncio.wait_for(exchange(proc, cwd, model, prompt, ready, delta, token_cap, level, tools, servers, lease,system_text=system_text,thought_delta=thought_delta,context_update=context_update), timeout=1000 if prompt is not None else 120)
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.communicate(), 2)
            except asyncio.TimeoutError:
                # asyncio Process exposes no public close for its stdout pipe.
                proc._transport.close()
                await asyncio.wait_for(proc.wait(), 2)


