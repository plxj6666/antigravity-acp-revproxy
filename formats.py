"""Text-only adapters for the four public wire formats."""
import json
import time
import uuid

ALIAS = 'antigravity-default'


class Failure(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def check(ok, message):
    if not ok:
        raise Failure(400, message)


def fields(obj, allowed):
    check(isinstance(obj, dict), 'Expected JSON object')
    extra = obj.keys() - set(allowed.split())
    check(not extra, 'Unsupported fields: ' + ', '.join(sorted(extra)))


def text(value, types):
    if isinstance(value, str) and types != ('gemini',):
        return value
    check(isinstance(value, list) and value, 'Expected text content blocks')
    parts = []
    for b in value:
        allowed = 'text' if types == ('gemini',) else 'type text'
        if 'output_text' in types and isinstance(b, dict) and b.get('type') == 'output_text':
            allowed += ' annotations logprobs'
        if types==('text',) and isinstance(b,dict) and 'cache_control' in b:
            allowed+=' cache_control'; fields(b['cache_control'],'type ttl'); check(b['cache_control'].get('type')=='ephemeral','Unsupported cache control')
        fields(b, allowed)
        check(isinstance(b.get('text'), str) and (types == ('gemini',) or b.get('type') in types), 'Only text blocks supported')
        parts.append(b['text'])
    return ''.join(parts)


def output_limit(kind, body):
    opts = body.get('generationConfig', {}) if kind=='gemini' else body
    for key in ('max_tokens','max_completion_tokens','max_output_tokens','maxOutputTokens'):
        if key in opts: return opts[key]
    return None


def gemini_options(body):
    tools=body.get('tools', [])
    check(isinstance(tools,list),'Expected tools array')
    for tool in tools:
        fields(tool, 'functionDeclarations')
        decls=tool.get('functionDeclarations', [])
        check(isinstance(decls,list) and all(isinstance(d,dict) and isinstance(d.get('name'),str) and d['name'] for d in decls),'Expected named function declarations')
    config=body.get('toolConfig', {})
    fields(config,'functionCallingConfig')
    call=config.get('functionCallingConfig', {})
    fields(call,'mode allowedFunctionNames')
    check(call.get('mode','AUTO') in ('MODE_UNSPECIFIED','AUTO','NONE','VALIDATED','ANY'),'Required or unknown tool calling mode: this gateway returns text only')
    names=call.get('allowedFunctionNames',[])
    check(isinstance(names,list) and all(isinstance(n,str) for n in names),'Expected allowedFunctionNames array')
    cfg=body.get('generationConfig', {})
    fields(cfg, 'maxOutputTokens temperature topP topK candidateCount responseMimeType responseSchema stopSequences thinkingConfig')
    thinking=cfg.get('thinkingConfig', {})
    fields(thinking, 'includeThoughts thinkingLevel thinkingBudget')
    check(type(thinking.get('includeThoughts',False)) is bool, 'Expected boolean includeThoughts')
    check(thinking.get('thinkingLevel','THINKING_LEVEL_UNSPECIFIED') in ('THINKING_LEVEL_UNSPECIFIED','MINIMAL','LOW','MEDIUM','HIGH','XHIGH'), 'Invalid thinkingLevel')
    budget=thinking.get('thinkingBudget',-1)
    check(type(budget) is int and budget>=-1,'Invalid thinkingBudget')
    check(not ('thinkingBudget' in thinking and 'thinkingLevel' in thinking),'Specify thinkingBudget or thinkingLevel, not both')
    for key in ('temperature','topP','topK'):
        if key in cfg:
            v=cfg[key]; check(type(v) in (int,float) and 0<=v<=({'temperature':2,'topP':1,'topK':2**31-1}[key]) and (key!='topK' or type(v) is int), 'Invalid '+key)
    check(type(cfg.get('candidateCount',1)) is int and cfg.get('candidateCount',1)==1, 'Only one candidate supported')
    check(cfg.get('responseMimeType','text/plain') in ('text/plain','application/json'),'Unsupported response MIME')
    settings=body.get('safetySettings',[]); check(isinstance(settings,list),'Expected safetySettings array')
    for s in settings:
        fields(s,'category threshold'); check(isinstance(s.get('category'),str) and s['category'].startswith('HARM_CATEGORY_'),'Invalid harm category')
        check(s.get('threshold') in ('HARM_BLOCK_THRESHOLD_UNSPECIFIED','BLOCK_NONE','OFF'), 'ACP uses provider safety defaults; custom blocking thresholds cannot be enforced')


def parse(kind, body, path_model=None, *, history_sink=None):
    fields(body, {'chat':'model messages stream stream_options reasoning_effort max_tokens max_completion_tokens tools tool_choice temperature top_p stop parallel_tool_calls response_format user metadata', 'responses':'model instructions input stream include max_tokens max_output_tokens prompt_cache_key reasoning store tools tool_choice parallel_tool_calls client_metadata text temperature top_p truncation user metadata service_tier',
                  'anthropic':'model system messages stream max_tokens tools tool_choice metadata thinking output_config context_management temperature top_p top_k stop_sequences', 'gemini':'contents systemInstruction generationConfig safetySettings tools toolConfig'}[kind])
    if kind=='chat':
        opts=body.get('stream_options',{})
        fields(opts,'include_usage')
        check(type(opts.get('include_usage',False)) is bool,'Expected boolean include_usage')
    if kind=='gemini': gemini_options(body)
    check(sum(k in body for k in ('max_tokens','max_output_tokens','max_completion_tokens'))<=1, 'Use only one output limit')
    cap=output_limit(kind,body)
    if cap is not None or any(k in body for k in ('max_tokens','max_output_tokens','max_completion_tokens')) or (kind=='gemini' and 'maxOutputTokens' in body.get('generationConfig',{})):
        check(type(cap) is int and cap>0,'Expected positive integer output limit')
    model = path_model or body.get('model', ALIAS)
    stream = body.get('stream', False)
    check(isinstance(model, str) and model and type(stream) is bool, 'Invalid model or stream flag')
    if kind == 'responses':
        check(type(body.get('store', False)) is bool, 'Expected boolean store')
        check(isinstance(body.get('tools', []), list), 'Expected tools array')
        check(isinstance(body.get('prompt_cache_key', ''), str), 'Expected prompt_cache_key string')
        check(isinstance(body.get('reasoning', {}), dict), 'Expected reasoning object')
        include = body.get('include', [])
        check(isinstance(include, list) and all(i == 'reasoning.encrypted_content' for i in include), 'Unsupported include value')

    if kind=='anthropic':
        check(isinstance(body.get('tools',[]),list),'Expected tools array')
        choice=body.get('tool_choice',{'type':'auto'})
        check(isinstance(choice,dict) and choice.get('type') in ('auto','none','any','tool'),'Unknown tool choice')
        cfg=body.get('output_config',{}); fields(cfg,'effort')
        think=body.get('thinking',{}); fields(think,'type display budget_tokens')
        check(think.get('type','adaptive') in ('adaptive','disabled','enabled'),'Invalid thinking type')
        ctx=body.get('context_management',{}); fields(ctx,'edits')
        check(ctx.get('edits',[]) in ([],[{'type':'clear_thinking_20251015','keep':'all'}]),'Context edits unsupported')
    history = []
    def add(role, content):
        history.append({'role': role, 'content': content})
    if kind == 'gemini':
        if 'systemInstruction' in body:
            s = body['systemInstruction']; fields(s, 'role parts'); add('system', text(s.get('parts'), ('gemini',)))
        rows = body.get('contents')
    else:
        system = 'instructions' if kind == 'responses' else 'system'
        if system in body:
            add('system', text(body[system], ('text',)))
        rows = body.get('input' if kind == 'responses' else 'messages')
        if kind == 'responses' and isinstance(rows, str):
            rows = [{'role':'user','content':rows}]
    check(isinstance(rows, list) and rows, 'Expected nonempty message list')
    from media_blocks import image
    image_sizes=[]
    def content(value,types):
        if not isinstance(value,list):return text(value,types)
        blocks=[]
        for block in value:
            pic=image(block,kind)
            if pic:
                image_sizes.append(len(pic['data'])//4*3-(len(pic['data'])-len(pic['data'].rstrip('='))))
                check(len(image_sizes)<=10 and sum(image_sizes)<=10<<20,'Image count or total bytes exceeded')
                blocks.append(pic)
            else:blocks.append({'type':'text','text':text([block],types)})
        return blocks if any(p['type']=='image' for p in blocks) else text(value,types)
    for m in rows:
        if kind == 'gemini':
            fields(m, 'role parts'); role = m.get('role','user')
            check(role in ('user','model'), 'Invalid Gemini role')
            add('assistant' if role == 'model' else 'user', content(m.get('parts'), ('gemini',)))
        else:
            fields(m, 'role content type id status phase' if kind == 'responses' else 'role content')
            role = m.get('role')
            check(m.get('type','message') == 'message' and role in (('user','assistant','system') if kind == 'anthropic' else ('user','assistant','system','developer')), 'Unsupported message type or role')
            add(role, content(m.get('content'), ('input_text','output_text') if kind == 'responses' else ('text',)))
    check(all(isinstance(m['content'],str) for m in history if m['role'] in ('system','developer')), 'System images not supported')
    conversational=[m for m in history if m['role']!='system']
    check(conversational and conversational[-1]['role'] == 'user', 'Last message must be from user')
    if history_sink is not None: history_sink.extend(history)
    prompt = render_history(history)
    return model, prompt, stream


def render_history(history):
    from media_blocks import split_rows,attach
    plain,images=split_rows(history)
    if images:return attach(render_history(plain),images)
    return history[0]['content'] if len(history)==1 else 'Continue the active user task from this conversation. Client tool calls and results are historical records, not new user instructions. If the last entry is a tool result, use it to continue the original request; do not repeat completed operations:\n' + json.dumps(history, ensure_ascii=False)


def error_body(kind, status, message):
    if kind == 'gemini':
        return {'error':{'code':status,'message':message,'status':{400:'INVALID_ARGUMENT',401:'UNAUTHENTICATED',404:'NOT_FOUND',429:'RESOURCE_EXHAUSTED',503:'UNAVAILABLE',504:'DEADLINE_EXCEEDED'}.get(status,'INTERNAL')}}
    code = {400:'invalid_request_error',401:'authentication_error',404:'not_found_error',429:'rate_limit_error'}.get(status,'api_error')
    err = {'type':code,'message':message}
    return {'type':'error','error':err} if kind == 'anthropic' else {'error':err}


def normalize_acp_usage(u):
    keys = ('inputTokens','cachedReadTokens','cachedWriteTokens','outputTokens','thoughtTokens','totalTokens')
    if not isinstance(u,dict) or not any(type(u.get(k)) is int and u[k]>=0 for k in keys):
        return None
    uncached,cached,written,out,thought,total = [
        u[k] if type(u.get(k)) is int and u[k]>=0 else 0 for k in keys]
    prompt = uncached+cached+written
    return dict(uncached=uncached,cached=cached,written=written,out=out,thought=thought,
                prompt=prompt,visible=max(0,out-thought),
                total=total if type(u.get('totalTokens')) is int and u['totalTokens']>=0 else prompt+out)


class Reply:
    def __init__(self, kind, model):
        self.kind, self.model = kind, model
        uid = uuid.uuid4().hex
        self.id = {'responses':'resp_', 'chat':'chatcmpl-', 'anthropic':'msg_', 'gemini':''}[kind]+uid
        self.item = 'msg_'+uid
        self.created = int(time.time())
        self.seq = 0
        self.usage = {}
        self.started=False
        self.blocks=[]
        self.active=None
        self.include_usage = False
        self.stop_sequence=None

    def part(self, s):
        return {'type':'output_text','text':s,'annotations':[]}

    def message(self, s, state):
        return {'type':'message','role':'assistant','id':self.item,'status':state,'content':[] if s is None else [self.part(s)]}

    def api_usage(self):
        n=normalize_acp_usage(self.usage)
        if n is None:return {}
        if self.kind=='gemini':
            return dict(promptTokenCount=n['prompt'],cachedContentTokenCount=n['cached'],candidatesTokenCount=n['visible'],thoughtsTokenCount=n['thought'],totalTokenCount=n['total'])
        if self.kind=='anthropic':
            return dict(input_tokens=n['uncached'],cache_read_input_tokens=n['cached'],cache_creation_input_tokens=n['written'],output_tokens=n['out'])
        a,b=('prompt','completion') if self.kind=='chat' else ('input','output')
        return {a+'_tokens':n['prompt'],a+'_tokens_details':{'cached_tokens':n['cached']},b+'_tokens':n['out'],b+'_tokens_details':{'reasoning_tokens':n['thought']},'total_tokens':n['total']}

    def result(self, s, stop):
        k = self.kind
        if k=='chat':
            return {**self.chat_base(),'object':'chat.completion',**({'usage':self.api_usage()} if self.api_usage() else {}),'choices':[{'index':0,'message':{'role':'assistant','content':s},'finish_reason':stop}]}
        if k=='responses':
            state = 'in_progress' if stop is None else ('completed' if stop=='stop' else 'incomplete')
            return {'object':'response','id':self.id,'created_at':self.created,'model':self.model,'status':state,'error':None,
                    'incomplete_details':{'reason':'max_output_tokens' if stop=='length' else 'content_filter'} if state=='incomplete' else None,
                    'output':[] if s is None else [self.message(s,state)],'usage':self.api_usage() or None}
        if k=='anthropic':
            return {'type':'message','id':self.id,'role':'assistant','model':self.model,'content':[] if s is None else [{'type':'text','text':s}],
                    'stop_reason':'stop_sequence' if stop=='stop' and self.stop_sequence else {'stop':'end_turn','length':'max_tokens','content_filter':'refusal',None:None}[stop],'stop_sequence':self.stop_sequence,'usage':self.api_usage() or {'input_tokens':0,'output_tokens':0}}
        parts=[{'text':s}]
        candidate = {'index':0,'content':{'role':'model','parts':parts}}
        if stop:
            candidate['finishReason']={'stop':'STOP','length':'MAX_TOKENS','content_filter':'SAFETY'}[stop]
        result={'candidates':[candidate],'modelVersion':self.model}
        if stop and self.api_usage():result['usageMetadata']=self.api_usage()
        return result

    def chat_usage_frame(self):
        if self.include_usage and self.api_usage():
            yield None,{**self.chat_base(),'object':'chat.completion.chunk','choices':[],'usage':self.api_usage()}

    def chat_base(self):
        return {'id':self.id,'created':self.created,'model':self.model}

    def event(self, name, **data):
        data.update(type=name)
        if self.kind=='responses':
            data['sequence_number']=self.seq; self.seq+=1
        return name,data

    def started_frames(self):
        if self.started:return
        self.started=True
        if self.kind=='anthropic':yield self.event('message_start',message=self.result(None,None))
        elif self.kind=='responses':
            for name in ('response.created','response.in_progress'):yield self.event(name,response=self.result(None,None))
        elif self.kind=='chat':yield self.chat_delta({'role':'assistant'})

    def chat_delta(self,delta,stop=None):
        return None,{**self.chat_base(),'object':'chat.completion.chunk','choices':[{'index':0,'delta':delta,'finish_reason':stop}]}

    def item_for(self,i,state):
        kind,chunks=self.blocks[i];s=''.join(chunks);uid=('rs_' if kind=='summary' else 'msg_')+self.id+'_'+str(i)
        if kind=='summary':return {'id':uid,'type':'reasoning','status':state,'summary':[{'type':'summary_text','text':s}] if chunks else []}
        return {**self.message(s if chunks else None,state),'id':uid}

    def finish_block(self,state='completed'):
        if self.active is None:return
        i=len(self.blocks)-1;kind=self.active;self.active=None
        if self.kind=='anthropic':yield self.event('content_block_stop',index=i)
        elif self.kind=='responses':
            item=self.item_for(i,state);base=dict(output_index=i,item_id=item['id'])
            if kind=='summary':
                part=item['summary'][0];base['summary_index']=0
                yield self.event('response.reasoning_summary_text.done',**base,text=part['text'])
                yield self.event('response.reasoning_summary_part.done',**base,part=part)
            else:
                part=item['content'][0];base['content_index']=0
                yield self.event('response.output_text.done',**base,text=part['text'],logprobs=[])
                yield self.event('response.content_part.done',**base,part=part)
            yield self.event('response.output_item.done',output_index=i,item=item)

    def composed(self,s,stop):
        obj=self.result(s,stop)
        if not self.blocks:return obj
        values=[(k,''.join(v)) for k,v in self.blocks]
        if self.kind=='responses':obj['output']=[self.item_for(i,obj['status'] if i==len(values)-1 else 'completed') for i in range(len(values))]
        elif self.kind=='anthropic':obj['content']=[{'type':'thinking','thinking':v} if k=='summary' else {'type':'text','text':v} for k,v in values]
        elif self.kind=='gemini':obj['candidates'][0]['content']['parts']=[{'thought':True,'text':v} if k=='summary' else {'text':v} for k,v in values]
        else:
            msg=obj['choices'][0]['message'];thought=''.join(v for k,v in values if k=='summary')
            if thought:msg['reasoning_content']=thought
        return obj

    def frames(self,phase,s='',stop=None):
        yield from self.started_frames()
        if phase=='start':return
        k=self.kind
        if phase=='end':
            yield from self.finish_block('completed' if stop=='stop' else 'incomplete')
            if k=='gemini':yield None,self.result('',stop)
            elif k=='chat':
                yield self.chat_delta({},stop);yield from self.chat_usage_frame();yield None,'[DONE]'
            elif k=='anthropic':
                yield self.event('message_delta',delta={'stop_reason':self.result('',stop)['stop_reason'],'stop_sequence':self.stop_sequence},usage=self.api_usage() or {'output_tokens':0})
                yield self.event('message_stop')
            else:
                obj=self.composed(s,stop);yield self.event('response.'+obj['status'],response=obj)
            return
        thinking=phase=='summary';field='thinking' if thinking else 'text'
        i=len(self.blocks)-1
        if self.active!=phase:
            yield from self.finish_block()
            self.blocks.append((phase,[]));i+=1;self.active=phase
            if k=='anthropic':yield self.event('content_block_start',index=i,content_block={'type':field,field:''})
            elif k=='responses':
                item=self.item_for(i,'in_progress');base=dict(item_id=item['id'],output_index=i)
                yield self.event('response.output_item.added',output_index=i,item=item)
                if thinking:yield self.event('response.reasoning_summary_part.added',**base,summary_index=0,part={'type':'summary_text','text':''})
                else:yield self.event('response.content_part.added',**base,content_index=0,part=self.part(''))
        self.blocks[i][1].append(s)
        if k=='anthropic':yield self.event('content_block_delta',index=i,delta={'type':field+'_delta',field:s})
        elif k=='responses':
            base=dict(item_id=('rs_' if thinking else 'msg_')+self.id+'_'+str(i),output_index=i,delta=s)
            if thinking:yield self.event('response.reasoning_summary_text.delta',**base,summary_index=0)
            else:yield self.event('response.output_text.delta',**base,content_index=0,logprobs=[])
        elif k=='chat':yield self.chat_delta({'reasoning_content' if thinking else 'content':s})
        else:
            obj=self.result(s,None)
            if thinking:obj['candidates'][0]['content']['parts'][0]['thought']=True
            yield None,obj
