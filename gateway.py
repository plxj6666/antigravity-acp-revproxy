"""Loopback HTTP gateway. See README.md for supported protocol subsets."""
import asyncio
from continuity import Leases
from session_manager import SessionManager, incoming_result
from history_archive import preview
import hmac
import inspect
import uuid
import hashlib
import json
import os
from contextlib import asynccontextmanager
from aiohttp import web

from tool_bridge import ClientCall, declarations, normalize_history, tool_response, tool_frames
from acp import upstream, OUTPUT_LIMIT, request_diag
import time
from formats import Failure, Reply, error_body, parse, output_limit, render_history, normalize_acp_usage

from protocol_options import Contract,policy,budget
class StopMatched(Exception):
    pass

from media_blocks import split_rows,attach

LIMIT = 12 * 1024 * 1024
ACTIVE_PER_SLOT = 100
QUEUE_PER_SLOT = 32


async def read_body(req):
    data = bytearray()
    if req.content_length is not None and req.content_length > LIMIT:
        raise Failure(413, 'Request too large')
    async for chunk in req.content.iter_chunked(16384):
        data.extend(chunk)
        if len(data) > LIMIT:
            raise Failure(413, 'Request too large')
    return json.loads(data),len(data)


def make_app(token, backend=upstream, *, heartbeat=10):
    app = web.Application(client_max_size=LIMIT)
    leases=Leases()
    manager=SessionManager() if backend is upstream else None
    if manager:app.cleanup_ctx.append(manager.lifecycle)
    tenant_keys=json.loads(os.getenv('GATEWAY_API_KEYS_JSON','{}'))
    slots = asyncio.Semaphore(ACTIVE_PER_SLOT)
    outstanding = 0
    session_keys=json.loads(os.environ.get('ACP_SESSION_KEYS_JSON','{}'))
    ingest=asyncio.Semaphore(4); long_slots=asyncio.Semaphore(2)
    parsed=web.RequestKey('parsed',bool);heavy=web.RequestKey('large-request',bool)

    @asynccontextmanager
    async def admission(listing):
        nonlocal outstanding
        if outstanding >= ACTIVE_PER_SLOT + QUEUE_PER_SLOT:
            raise Failure(503,'Gateway queue full; retry later')
        outstanding += 1
        try:
            try:
                await asyncio.wait_for(slots.acquire(), 10 if listing else 110)
            except asyncio.TimeoutError:
                raise Failure(503,'Gateway queue wait timed out; retry later')
            try:
                yield
            finally:
                slots.release()
        finally:
            outstanding -= 1

    async def handle(req):
        clock=asyncio.get_running_loop().time;began=clock()
        metrics=dict(req_id=uuid.uuid4().hex[:12],kind=None,model=None,stream=False,slot=os.getenv('ACP_SLOT','local'),session_hit=False,queue_s=None,ready_s=None,ttft_thought_s=None,ttft_text_s=None,tool_calls=0,status=500,stop_reason=None,usage=None,context_used=None,context_size=None,thought_chunks=0,denied_native_tools=0,last_acp_update_s=None)
        diag_token=request_diag.set((metrics,time.monotonic()))
        headers={'X-Gateway-Request-Id':metrics['req_id']}
        try:
            if ingest.locked():
                metrics['status']=503
                return web.json_response({'error':{'message':'Upload/parser busy; retry later'}},status=503,headers=headers)
            await ingest.acquire()
            try:
                resp=await _handle(req,metrics,headers,began)
                if metrics['status']==500:metrics['status']=resp.status
                return resp
            finally:
                if not req.get(parsed):ingest.release()
                if req.get(heavy):long_slots.release()
        except asyncio.CancelledError:
            metrics['status']=499;raise
        finally:
            request_diag.reset(diag_token)
            metrics['total_s']=round(clock()-began,3)
            print(json.dumps(metrics,separators=(',',':')),flush=True)

    async def _handle(req,metrics,notes,began):
        name = req.match_info.route.name
        kind = {'chat':'chat','responses':'responses','anthropic':'anthropic','models':'chat'}.get(name,'gemini')
        output = None
        array = False
        first = True
        metrics['kind']=kind

        loop=asyncio.get_running_loop();last_event=loop.time()
        async def frame(event, obj):
            nonlocal first,last_event
            last_event=loop.time()
            data = obj if isinstance(obj,str) else json.dumps(obj, separators=(',', ':'))
            if array:
                wire = ('' if first else ',') + data
                first = False
            else:
                wire = (f'event: {event}\n' if event else '') + f'data: {data}\n\n'
            await asyncio.wait_for(output.write(wire.encode()), 10)

        async def start(text_start=True):
            nonlocal output
            output = web.StreamResponse(headers={'Content-Type':'application/json' if array else 'text/event-stream',
                                                 'Cache-Control':'no-cache', 'X-Accel-Buffering':'no', **notes})
            await output.prepare(req)
            if array:
                await output.write(b'[')
            for event in reply.frames('start') if text_start else (): 
                await frame(*event)

        async def delta(piece):
            for event in reply.frames('delta',piece):
                await frame(*event)

        try:
            keys = [req.headers.get('Authorization','')]
            if kind=='anthropic': keys.append('Bearer '+req.headers.get('x-api-key',''))
            if kind=='gemini': keys.append('Bearer '+req.headers.get('x-goog-api-key',req.query.get('key','')))
            tenant=next((('api',t) for t,v in tenant_keys.items() if v and any(hmac.compare_digest(k.encode(),('Bearer '+v).encode()) for k in keys)),('shared',))
            if tenant==('shared',) and not any(hmac.compare_digest(k.encode(), ('Bearer '+token).encode()) for k in keys):
                raise Failure(401,'Invalid gateway key')
            listing = name in ('models','gemini_models')
            if not listing:
                try:
                    body,wire_size = await asyncio.wait_for(read_body(req),15)
                except asyncio.TimeoutError:
                    raise Failure(408,'Upload timed out')
                except (ValueError, UnicodeError, RecursionError):
                    raise Failure(400,'Invalid JSON')
                previous=body.get('previous_response_id') if isinstance(body,dict) and manager and kind=='responses' else None
                if previous is not None:
                    if not isinstance(previous,str):raise Failure(400,'Invalid previous_response_id')
                    body={k:v for k,v in body.items() if k!='previous_response_id'}
                history=[]
                model,prompt,stream = parse(kind,normalize_history(kind,body),req.match_info.get('model'),history_sink=history)
                nonce=req.headers.get('X-ACP-Session');seq=req.headers.get('X-ACP-Turn');archive=None
                principal=None
                if nonce is not None or seq is not None or req.headers.get('X-ACP-Session-Key'):
                    supplied=req.headers.get('X-ACP-Session-Key','')
                    principal=next((p for p,k in session_keys.items() if k and hmac.compare_digest(k.encode(),supplied.encode())),None)
                    if principal is None:raise Failure(403,'Dedicated session key required')
                    tenant=(*tenant,'session',principal)
                system_text='\n'.join(m['content'] for m in history if m['role'] in ('system','developer'))
                conversation=[m for m in history if m['role'] not in ('system','developer')]
                plain,images=split_rows(history)
                if wire_size-sum(len(p['data']) for p in images)>2<<20:raise Failure(413,'Text request too large')
                cold_size=len(render_history(plain).encode())
                prompt=render_history(conversation)
                if not manager and cold_size>48000:
                    if long_slots.locked():raise Failure(503,'Archive capacity reached; retry later')
                    await long_slots.acquire();req[heavy]=True
                    last_task=None
                    if kind=='gemini' and any('functionResponse' in p for p in body['contents'][-1]['parts']):
                        for m in reversed(body['contents']):
                            if m.get('role','user')!='user':continue
                            last_task='\n'.join(p['text'] for p in m['parts'] if isinstance(p.get('text'),str) and not p.get('thought'))
                            if last_task:break
                    try:
                        prompt,archive=await asyncio.to_thread(preview,split_rows(conversation)[0],last_task);prompt=attach(prompt,images)
                    except ValueError as exc:raise Failure(413,str(exc))
                    notes['X-ACP-History-Mode']='searchable-archive'
                if kind=='gemini':
                    stream = req.match_info['action']=='streamGenerateContent'
                    alt = req.query.get('alt','json')
                    if alt not in ('sse','json'):
                        raise Failure(400,'Unsupported alt format')
                    array = stream and alt=='json'
                tools=declarations(kind,body);choice=policy(kind,body);contract=Contract(kind,body)
                forced=choice.mode in ('required','specific')
                base_key=hashlib.sha256(json.dumps([system_text,contract.instruction,contract.stops,body.get('tools',[])],sort_keys=True).encode()).digest()
                system_text+=contract.instruction
                if forced:system_text+='\nYou must invoke '+('the client tool '+choice.name if choice.name else 'at least one available client tool')+' before returning text.'
                if body.get('store'):
                    if not manager:raise Failure(400,'Response storage requires managed sessions')
                    notes['X-ACP-Response-Storage']=f'worker-memory; idle-ttl={manager.idle}'
                if not manager and (nonce is not None or seq is not None) and (tools or archive or (seq!='1' and len(history)!=1)):
                    raise Failure(400,'Explicit sessions accept text increments only; omit session headers for full history or tools')
                metrics.update(stream=stream,model='sha256:'+hashlib.sha256(model.encode()).hexdigest()[:16])
                reply = Reply(kind,model)
                if manager and kind=='responses':reply.id=manager.id('resp')
                if kind=='chat':reply.include_usage=body.get('stream_options',{}).get('include_usage',False)
                cap = min(output_limit(kind,body) or OUTPUT_LIMIT, OUTPUT_LIMIT)
                if kind=='gemini' and ('generationConfig' in body or 'safetySettings' in body):
                    notes['X-Gateway-Compatibility']='ACP uses provider safety and sampling defaults; client sampling hints not applied'
                level=body.get('reasoning_effort') if kind=='chat' else None
                if kind=='responses': level=body.get('reasoning',{}).get('effort')
                if kind=='anthropic': level=body.get('output_config',{}).get('effort')
                if kind=='anthropic' and body.get('thinking',{}).get('type')=='enabled':
                    level=budget(body['thinking'].get('budget_tokens'))
                    notes['X-Gateway-Compatibility']='thinking='+level
                if level is not None and level not in ('minimal','low','medium','high','xhigh','max'):
                    raise Failure(400,'Unsupported ACP reasoning effort')
                if kind=='gemini' and 'thinkingConfig' in body.get('generationConfig',{}):
                    thinking=body['generationConfig']['thinkingConfig']
                    if thinking.get('thinkingBudget',-1)!=-1:
                        level=budget(thinking['thinkingBudget'])
                    if thinking.get('thinkingLevel','THINKING_LEVEL_UNSPECIFIED')!='THINKING_LEVEL_UNSPECIFIED':
                        level=thinking['thinkingLevel'].lower()
                    notes['X-Gateway-Compatibility']+='; thinking='+str(level or 'provider-default')+'; optional ACP summary events'
                def compatibility(value):
                    key='X-Gateway-Compatibility'
                    notes[key]='; '.join(filter(None,(notes.get(key),value)))
                if hasattr(level,'confirm'):
                    def confirmed(actual):
                        compatibility('negotiated-thinking='+actual)
                    level.confirm=confirmed
                if kind=='gemini' and ('tools' in body or 'toolConfig' in body):
                    key='X-Gateway-Compatibility'
                    notes[key]=(notes[key]+'; ' if key in notes else '')+'client functions via MCP catalog'
                if kind=='responses' and any(k in body for k in ('service_tier','truncation')):
                    compatibility('service tier and truncation use provider defaults')
                if any(k in body for k in ('temperature','top_p','top_k')):
                    compatibility('sampling hints not applied')
                notes['X-Gateway-Limit-Policy'] = f'{cap} tokens; local o200k_base and available ACP turn output counts; context occupancy excluded'
                if kind=='responses' and any(k in body for k in ('reasoning','include','prompt_cache_key','tools')):
                    compatibility('client functions via MCP catalog; optional ACP summary events; no explicit cache controls')
            ingest.release();req[parsed]=True
            queued=asyncio.get_running_loop().time()
            async with admission(listing):
                if listing:
                    result = await backend()
                    if name=='gemini_models':
                        result = {'models':[{'name':'models/'+m['id'],'displayName':m['id'],'supportedGenerationMethods':['generateContent','streamGenerateContent']} for m in result['data']]}
                else:
                    think=body.get('thinking',{})
                    show_plan=(kind=='gemini' and body.get('generationConfig',{}).get('thinkingConfig',{}).get('includeThoughts',False)
                        or kind=='responses' and 'reasoning' in body and body['reasoning'].get('summary')!='none'
                        or kind=='anthropic' and bool(think) and think.get('type')!='disabled' and think.get('display')!='omitted'
                        or kind=='chat' and bool(level))
                    async def emit(phase,s):
                        if not s:return
                        if stream and output is None:await start(False)
                        for event in reply.frames(phase,s):
                            if stream:await frame(*event)
                    async def thought(s):
                        if metrics['ttft_thought_s'] is None:metrics['ttft_thought_s']=round(loop.time()-began,3)
                        await emit('summary',s)
                    async def timed_piece(s):
                        if metrics['ttft_text_s'] is None:metrics['ttft_text_s']=round(loop.time()-began,3)
                        if not forced or notes.get('X-ACP-Session-State')=='tool':
                            await emit('delta',contract.feed(s))
                            if contract.stopped:raise StopMatched
                    def context_update(used,size):
                        for key,val in [('used',used),('size',size)]:
                            if type(val) is int and val>=0:
                                metrics['context_'+key]=val
                                if output is None:notes['X-ACP-Context-'+key.title()]=str(val)
                    metrics['queue_s']=round(loop.time()-queued,3)
                    async def ready():
                        metrics['ready_s']=round(loop.time()-began,3)
                        if output is None:await start(True)
                    async def generate():
                        if manager:
                            async def prepare(rows,cold):
                                plain,images=split_rows(rows);p=render_history(plain)
                                if cold and len(p.encode())>48000:
                                    if long_slots.locked():raise Failure(503,'Archive capacity reached; retry later')
                                    await long_slots.acquire();req[heavy]=True
                                    notes['X-ACP-History-Mode']='searchable-archive'
                                    try:
                                        p,a=await asyncio.to_thread(preview,plain);return attach(p,images),a
                                    except ValueError as e:raise Failure(413,str(e))
                                return attach(p,images),None
                            return await manager.run(tenant=tenant,kind=kind,model=model,level=level,system=system_text,tools=tools,policy=(choice.mode,choice.name,contract.stops),base_key=base_key,contract=contract,rows=conversation,tool_result=incoming_result(kind,body),nonce=nonce,seq=seq,previous=previous,response=reply.id,cap=cap,ready=ready if stream else None,text=timed_piece,thought=thought if show_plan else None,context=context_update,prepare=prepare,metrics=metrics,headers=notes)
                        with leases.acquire(nonce,seq,model,kind,principal) as lease:
                            if kind=='gemini' and stream and not array:
                                await start(False)
                                await asyncio.wait_for(output.write(b': connected\n\n'),10)
                            params=inspect.signature(backend).parameters
                            ext=any(p.kind==p.VAR_KEYWORD for p in params.values())
                            optional={'context_update':context_update,'thought_delta':thought if show_plan else None,'system_text':system_text}
                            extra={k:v for k,v in optional.items() if k in params or ext}
                            metrics['session_hit']=bool(lease and lease.sid)
                            if lease is not None:extra['lease']=lease
                            if archive:extra['archive']=archive
                            return await backend(model,prompt,ready if stream else None,timed_piece if stream or backend is upstream or 'thought_delta' in extra else None,cap,level,tools,**extra)
                    try:
                        if stream and not array:
                            # Comments keep the transport live without inventing model content.
                            task=asyncio.create_task(generate())
                            try:
                                while not (await asyncio.wait({task},timeout=heartbeat))[0]:
                                    if loop.time()-last_event>=heartbeat:
                                        if output is None:await start(False)
                                        await asyncio.wait_for(output.write(b': keepalive\n\n'),10)
                                        last_event=loop.time()
                                answer,reason,usage=task.result()
                            finally:
                                task.cancel(); await asyncio.gather(task,return_exceptions=True)
                        else:answer,reason,usage=await generate()
                    except StopMatched:
                        answer=''.join(''.join(v) for k,v in reply.blocks if k=='delta');reason='stop';usage={}
                    except ClientCall as invocation:
                        metrics.update(tool_calls=1,stop_reason='tool_call')
                        try:
                            await emit('delta',contract.feed('',True))
                            has_text=any(k=='delta' for k,_ in reply.blocks)
                            if not has_text:
                                prefix=getattr(invocation,'public_text','')
                                label=getattr(invocation,'label','')
                                if not prefix and kind=='gemini':
                                    prefix=('ACP · '+label) if label else ('Gateway: requesting client tool '+invocation.name+'.' if show_plan and not reply.blocks else '')
                                if prefix:await timed_piece(prefix)
                            if manager:
                                wire=tool_response(reply,invocation)
                                if kind=='chat':sent=[wire['choices'][0]['message']]
                                elif kind=='responses':sent=wire['output']
                                elif kind=='gemini':sent=[wire['candidates'][0]['content']]
                                else:sent=[dict(role='assistant',content=wire['content'])]
                                field={'chat':'messages','responses':'input','anthropic':'messages','gemini':'contents'}[kind]
                                dummy={'role':'user','parts':[{'text':'end'}]} if kind=='gemini' else {'role':'user','content':'end'}
                                projection=[]
                                try:parse(kind,normalize_history(kind,{field:[*sent,dummy]}),model,history_sink=projection)
                                except Failure:invocation.record(None)
                                else:invocation.record(projection[:-1])
                            if not stream:
                                resp=web.json_response(tool_response(reply,invocation),headers=notes)
                                if manager:await resp.prepare(req);await resp.write_eof()
                                return resp
                            if output is None:await start(False)
                            for event in tool_frames(reply,invocation):await frame(*event)
                            if array:await output.write(b']')
                            await output.write_eof()
                            return output
                        except BaseException:
                            if manager:await manager.abort(invocation.id)
                            raise
                    if forced and notes.get('X-ACP-Session-State')!='tool':raise Failure(502,'Model did not satisfy forced tool_choice')
                    if not stream:contract.validate(answer[:min((answer.index(x) for x in contract.stops if x in answer),default=len(answer))])
                    if metrics['ttft_text_s'] is None and answer:
                        try:await timed_piece(answer)
                        except StopMatched:pass
                    await emit('delta',contract.feed('',True))
                    if contract.stopped:
                        reply.stop_sequence=contract.matched
                        answer=''.join(''.join(v) for k,v in reply.blocks if k=='delta');reason='stop'
                    metrics.update(stop_reason=reason,usage=normalize_acp_usage(usage))
                    reply.usage=usage
            if not listing:
                try:
                    if stream:
                        if output is None:await start(False)
                        for event in reply.frames('end',answer,reason):await frame(*event)
                        if array:await output.write(b']')
                        resp=output
                    else:resp=web.json_response(reply.composed(answer,reason),headers=notes)
                    if manager:
                        if not stream:await resp.prepare(req)
                        await resp.write_eof();await manager.delivered(reply.id)
                    return resp
                except BaseException:
                    if manager:await manager.retire_response(reply.id)
                    raise
            return web.json_response(result, headers=notes)
        except Failure as exc:
            if manager and 'reply' in locals():await manager.retire_response(reply.id)
            status, message = exc.status, str(exc)
        except asyncio.TimeoutError:
            status, message = 504,'ACP request timed out'
        except (OSError, KeyError, ValueError, AttributeError, TypeError):
            status, message = 502,'Invalid ACP response or unavailable agy executable'
        metrics.update(status=status,stop_reason='error')
        err = error_body(kind,status,message)
        if output is None:
            return web.json_response(err,status=status,headers=notes)
        if kind=='responses':
            await frame(*reply.event('error',code='server_error',message=message,param=None))
        else:
            await frame('error' if kind=='anthropic' else None,err)
        if array: await output.write(b']')
        return output

    app.router.add_get('/v1/models',handle,name='models')
    app.router.add_post('/v1/chat/completions',handle,name='chat')
    app.router.add_post('/v1/responses',handle,name='responses')
    app.router.add_post('/v1/messages',handle,name='anthropic')
    for version in ('v1','v1beta'):
        # /v1/models is already the OpenAI discovery endpoint.
        if version=='v1beta': app.router.add_get('/v1beta/models',handle,name='gemini_models')
        app.router.add_post('/'+version+'/models/{model}:'+'{action:generateContent|streamGenerateContent}',handle)
    return app


if __name__ == '__main__':
    token = os.environ.get('GATEWAY_API_KEY','')
    if not token.isascii() or len(token)<24 or any(c.isspace() for c in token):
        raise SystemExit('Set GATEWAY_API_KEY to a random ASCII token (at least 24 characters)')
    if os.getenv('LISTEN_PID') == str(os.getpid()) and os.getenv('LISTEN_FDS') == '1':
        import socket
        binding = {'sock': socket.socket(fileno=3)}
    else:
        binding = {'host':'127.0.0.1','port':int(os.getenv('PORT','8787'))}
    web.run_app(make_app(token),**binding,handler_cancellation=True,access_log=None,shutdown_timeout=3)
