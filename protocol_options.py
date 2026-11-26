"""Validated per-request output contracts shared by the wire adapters."""
import json
from dataclasses import dataclass
from referencing import Registry
from formats import check,Failure
from tool_bridge import Validator,SchemaError,normalize_schema,safe_schema_tree

@dataclass(frozen=True)
class ToolPolicy:
    mode:str
    name:str|None=None


def policy(kind,b):
    c=b.get('tool_choice','auto')
    if kind=='gemini':
        cfg=b.get('toolConfig',{}).get('functionCallingConfig',{});m=cfg.get('mode','AUTO');names=cfg.get('allowedFunctionNames',[])
        if m=='ANY' and len(names)==1:return ToolPolicy('specific',names[0])
        c={'ANY':'required','NONE':'none','AUTO':'auto','VALIDATED':'auto','MODE_UNSPECIFIED':'auto'}.get(m)
    elif isinstance(c,dict):
        t=c.get('type')
        if t==('tool' if kind=='anthropic' else 'function'):
            f=c.get('function') if kind=='chat' else c
            check(isinstance(f,dict) and isinstance(f.get('name'),str),'Expected tool name');return ToolPolicy('specific',f['name'])
        c='required' if kind=='anthropic' and t=='any' else t if kind=='anthropic' else None
    check(c in ('none','auto','required'),'Invalid tool choice');return ToolPolicy(c)


class BudgetLevel(str):
    def __new__(cls,value):
        obj=super().__new__(cls,value);obj.confirm=lambda _:None;return obj

    def select(self,available):
        ranks=['minimal','low','medium','high','xhigh'];target=ranks.index(str(self))
        supported=[v for v in ranks if v in available]
        check(supported,'Model does not advertise a numeric thinking budget mapping')
        return min(supported,key=lambda v:abs(ranks.index(v)-target))


def budget(n):
    check(type(n) is int and n>=0,'Invalid thinking budget');return BudgetLevel('minimal' if n==0 else 'low' if n<=2048 else 'medium' if n<=8192 else 'high' if n<=32768 else 'xhigh')


class Contract:
    def __init__(self,kind,b):
        for key,maximum in [('temperature',2),('top_p',1),('top_k',2**31-1)]:
            if key in b:check(type(b[key]) in (int,float) and 0<=b[key]<=maximum and (key!='top_k' or type(b[key]) is int),'Invalid '+key)
        if 'parallel_tool_calls' in b:check(type(b['parallel_tool_calls']) is bool,'Expected boolean parallel_tool_calls')
        if 'metadata' in b:check(isinstance(b['metadata'],dict),'Expected metadata object')
        if 'user' in b:check(isinstance(b['user'],str),'Expected user string')
        if 'truncation' in b:check(b['truncation'] in ('auto','disabled'),'Invalid truncation')
        if 'service_tier' in b:check(isinstance(b['service_tier'],str),'Invalid service_tier')
        cfg=b.get('generationConfig',{});self.format='text';self.schema=None;self.instruction='';self.pending='';self.stopped=False
        stops=cfg.get('stopSequences',b.get('stop_sequences',b.get('stop',[])))
        if isinstance(stops,str):stops=[stops]
        check(isinstance(stops,list) and len(stops)<=5 and all(isinstance(s,str) and 0<len(s)<=4096 for s in stops),'Invalid stop sequences');self.stops=stops
        f=b.get('response_format',b.get('text',{}).get('format',{}) if isinstance(b.get('text',{}),dict) else None)
        check(isinstance(f,dict),'Invalid response format');self.format=f.get('type','text')
        if cfg.get('responseMimeType')=='application/json':self.format='json_schema' if 'responseSchema' in cfg else 'json'
        check('responseSchema' not in cfg or self.format=='json_schema','responseSchema requires JSON output')
        check(self.format in ('text','json_object','json_schema','json'),'Invalid response format')
        if self.format!='text':self.instruction='\nReturn only valid JSON'+(' object' if self.format=='json_object' else '')+'.'
        if self.format=='json_schema':
            spec=f.get('json_schema',f);schema=cfg.get('responseSchema',spec.get('schema') if isinstance(spec,dict) else None);safe_schema_tree(schema);self.schema=normalize_schema(schema)
            try:Validator.check_schema(self.schema)
            except SchemaError:raise Failure(400,'Invalid response schema')
            self.instruction+=' Conform to: '+json.dumps(self.schema,ensure_ascii=False,sort_keys=True)

    def validate(self,text):
        if self.format=='text':return
        try:v=json.loads(text,parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except ValueError:raise Failure(502,'Model did not return valid JSON')
        if self.format=='json_object' and not isinstance(v,dict) or self.schema is not None and not Validator(self.schema,registry=Registry()).is_valid(v):raise Failure(502,'Model output violates response schema')

    def feed(self,text,final=False):
        if self.stopped:return ''
        s=self.pending+text;self.pending='';i=min((s.index(x) for x in self.stops if x in s),default=len(s))
        if i<len(s):self.stopped=True;self.matched=next(x for x in self.stops if s.startswith(x,i));return s[:i]
        if not final and self.stops:
            k=max(len(x) for x in self.stops)-1
            if k:self.pending=s[-k:];s=s[:-k]
        return s
