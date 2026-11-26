"""Request-scoped client tool declarations and wire adapters. No tool execution."""
import json
import re
import uuid
from jsonschema import Draft202012Validator as Validator, SchemaError
from formats import Failure


class ClientCall(Exception):
    def __init__(self,name,args):
        super().__init__(name); self.name=name; self.arguments=args; self.id='call_'+uuid.uuid4().hex


def safe_schema_tree(s):
    if isinstance(s,dict):
        if any(k in s for k in ('$ref','$dynamicRef','$recursiveRef')):raise Failure(400,'Schema references not supported')
        for v in s.values():safe_schema_tree(v)
    elif isinstance(s,list):
        for v in s:safe_schema_tree(v)


def normalize_schema(s):
    if not isinstance(s,dict):raise Failure(400,'Expected object schema')
    s=dict(s)
    s.pop('$schema',None)  # Validate locally against the gateway dialect, never fetch a URL.
    if any(k in s for k in ('$ref','$dynamicRef','$recursiveRef')):raise Failure(400,'Schema references not supported')
    if isinstance(s.get('type'),str):s['type']=s['type'].lower()
    for k in ('properties','$defs','patternProperties','dependentSchemas'):
        if k in s and isinstance(s[k],dict):s[k]={n:normalize_schema(v) for n,v in s[k].items()}
    for k in ('items','additionalProperties','contains','not','if','then','else','propertyNames','unevaluatedProperties','unevaluatedItems'):
        if isinstance(s.get(k),dict):s[k]=normalize_schema(s[k])
    for k in ('oneOf','anyOf','allOf','prefixItems'):
        if isinstance(s.get(k),list):s[k]=[normalize_schema(v) for v in s[k]]
    if s.pop('nullable',False) and isinstance(s.get('type'),str):s['type']=[s['type'],'null']
    return s


def declarations(kind,b):
    raw=b.get('tools',[]); defs=[]
    if not isinstance(raw,list):raise Failure(400,'Expected tools array')
    if kind=='gemini':
        raw=[f for t in raw if isinstance(t,dict) for f in t.get('functionDeclarations',[])]
    for t in raw:
        if not isinstance(t,dict):raise Failure(400,'Expected tool declaration')
        if kind=='chat':t=t.get('function',{})
        if kind=='responses' and t.get('type')!='function':continue
        name=t.get('name');schema=t.get('parametersJsonSchema',t.get('parameters',t.get('input_schema',{'type':'object','properties':{}})))
        if not isinstance(name,str) or not re.fullmatch(r'[\w.-]{1,128}',name) or not isinstance(schema,dict):raise Failure(400,'Invalid function declaration')
        if any(d['name']==name for d in defs):raise Failure(400,'Duplicate client function name')
        safe_schema_tree(schema); schema=normalize_schema(schema)
        try:Validator.check_schema(schema)
        except SchemaError:raise Failure(400,'Invalid function schema: '+name)
        defs.append({'name':name,'description':str(t.get('description','')),'inputSchema':schema})
    choice=b.get('tool_choice'); mode=b.get('toolConfig',{}).get('functionCallingConfig',{}).get('mode') if kind=='gemini' else None
    if choice=='none' or isinstance(choice,dict) and choice.get('type')=='none' or mode=='NONE':return []
    from protocol_options import policy
    p=policy(kind,b)
    if p.mode=='specific':defs=[d for d in defs if d['name']==p.name]
    if kind=='gemini':
        allowed=b.get('toolConfig',{}).get('functionCallingConfig',{}).get('allowedFunctionNames',[])
        if allowed:defs=[d for d in defs if d['name'] in allowed]
    if p.mode in ('specific','required') and not defs:raise Failure(400,'No declared tool matches forced choice')
    return defs


def normalize_history(kind,b):
    key={'chat':'messages','responses':'input','anthropic':'messages','gemini':'contents'}[kind]; rows=b.get(key)
    if not isinstance(rows,list):return b
    out=[]
    def encoded(o):return json.dumps(o,sort_keys=True,ensure_ascii=False,separators=(',',':'))
    for m in rows:
        if not isinstance(m,dict):out.append(m);continue
        if kind=='responses' and m.get('type')=='reasoning':continue
        if kind=='chat' and m.get('role')=='assistant':m={k:v for k,v in m.items() if k!='reasoning_content'}
        if kind=='responses' and m.get('type') in ('function_call','function_call_output'):
            out.append({'role':'assistant' if m['type']=='function_call' else 'user','content':encoded(m)});continue
        if kind=='chat' and (m.get('role')=='tool' or m.get('tool_calls')):
            out.append({'role':'user' if m.get('role')=='tool' else 'assistant','content':encoded(m)});continue
        k='parts' if kind=='gemini' else 'content';blocks=m.get(k)
        if isinstance(blocks,list):
            if kind=='gemini' and blocks and all(isinstance(p,dict) and 'functionResponse' in p for p in blocks): m={**m,'role':'user'}
            if kind=='gemini' and m.get('role')=='model':
                blocks=[p for p in blocks if not isinstance(p,dict) or not p.get('thought')]
            if kind=='anthropic' and m.get('role')=='assistant':
                blocks=[p for p in blocks if not isinstance(p,dict) or p.get('type') not in ('thinking','redacted_thinking')]
            expanded=[]
            for p in blocks:
                nested=(p.get('functionResponse',{}).get('parts') if kind=='gemini' else p.get('content') if kind=='anthropic' and p.get('type')=='tool_result' else None) if isinstance(p,dict) else None
                if isinstance(nested,list):
                    from media_blocks import image
                    media=[part for part in nested if image(part,kind)]
                    if media:
                        rest=[part for part in nested if part not in media]
                        if kind=='gemini':p={**p,'functionResponse':{**p['functionResponse'],'parts':rest}}
                        else:p={**p,'content':rest}
                        expanded.extend([p,*media]);continue
                expanded.append(p)
            blocks=[({'text':encoded(p)} if kind=='gemini' else {'type':'text','text':encoded(p)}) if isinstance(p,dict) and ((kind=='gemini' and ('functionCall' in p or 'functionResponse' in p)) or (kind=='anthropic' and p.get('type') in ('tool_use','tool_result'))) else p for p in expanded];m={**m,k:blocks}
        out.append(m)
    return {**b,key:out}


def tool_response(r,c):
    text=''.join(''.join(v) for k,v in r.blocks if k=='delta')
    s=r.composed(text or None,'stop');k=r.kind
    if k=='gemini':
        parts=s['candidates'][0]['content']['parts']
        if not r.blocks:parts.clear()
        parts.append({'functionCall':{'name':c.name,'args':c.arguments,'id':c.id}})
    elif k=='anthropic':
        s['content'].append({'type':'tool_use','id':c.id,'name':c.name,'input':c.arguments});s['stop_reason']='tool_use'
    elif k=='responses':
        s['output'].append({'id':'fc_'+c.id,'type':'function_call','call_id':c.id,'name':c.name,'arguments':json.dumps(c.arguments,ensure_ascii=False),'status':'completed'})
    else:
        choice=s['choices'][0];choice['finish_reason']='tool_calls'
        choice['message']['tool_calls']=[{'id':c.id,'type':'function','function':{'name':c.name,'arguments':json.dumps(c.arguments,ensure_ascii=False)}}]
    return s


def tool_frames(r,c,previous=None):
    yield from r.started_frames()
    yield from r.finish_block()
    s=tool_response(r,c);k=r.kind;idx=len(r.blocks);args=json.dumps(c.arguments,ensure_ascii=False)
    if k=='gemini':
        parts=s['candidates'][0]['content']['parts'];parts[:]=parts[-1:];yield None,s
    elif k=='anthropic':
        yield r.event('content_block_start',index=idx,content_block={'type':'tool_use','id':c.id,'name':c.name,'input':{}})
        yield r.event('content_block_delta',index=idx,delta={'type':'input_json_delta','partial_json':args})
        yield r.event('content_block_stop',index=idx)
        yield r.event('message_delta',delta={'stop_reason':'tool_use','stop_sequence':None},usage=r.api_usage() or {'output_tokens':0})
        yield r.event('message_stop')
    elif k=='responses':
        item=s['output'][-1];base={'item_id':item['id'],'output_index':idx}
        yield r.event('response.output_item.added',output_index=idx,item={**item,'status':'in_progress','arguments':''})
        yield r.event('response.function_call_arguments.delta',**base,delta=args)
        yield r.event('response.function_call_arguments.done',**base,arguments=args)
        yield r.event('response.output_item.done',output_index=idx,item=item)
        yield r.event('response.completed',response=s)
    else:
        tool=s['choices'][0]['message']['tool_calls'][0]
        yield r.chat_delta({'tool_calls':[{'index':0,**tool}]})
        yield r.chat_delta({},'tool_calls');yield from r.chat_usage_frame();yield None,'[DONE]'
