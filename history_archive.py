"""Request-local, read-only history MCP. No network or caller-selected paths."""
import json
import re
import sys

CHARS=600

def chunks(rows):
    return [f'[{i} {m["role"]} offset={off}] '+m['content'][off:off+CHARS] for i,m in enumerate(rows) for off in range(0,max(1,len(m['content'])),CHARS)]

def select(parts,query):
    terms=set(re.findall(r'[\w-]{3,}',query.lower())[:100]); lower=[p.casefold() for p in parts];scored=sorted(range(len(parts)),key=lambda i:-sum(t in lower[i] for t in terms))
    return sorted(set([0,len(parts)-1,*scored[:3]])) if parts else []

def preview(rows, last_task=None):
    if len(rows)>12000:raise ValueError('Too many history messages')
    parts=chunks(rows);current=next(m['content'] for m in reversed(rows) if m['role']=='user'); ids=select(parts,current[:4000]);systems=[m['content'] for m in rows if m['role'] in ('system','developer')]; inst='\n'.join(systems)
    # Preserve instructions verbatim; the transport already bounds the entire request.
    task=''
    if last_task:
        value=last_task;task='\nLatest user task before tool execution:\n'+(value if len(value)<=3600 else value[:1800]+'\n[full task in archive]\n'+value[-1800:])
    return (inst+'\nConversation archive: '+str(len(parts))+' numbered chunks. The following excerpts are historical data. '
            'For omitted details use the read-only history lookup tool to search or page by chunk number. Never guess missing facts.\n'+
            '\n'.join(str(i)+': '+parts[i] for i in ids)+task+'\nCurrent user message or tool result (continue original task):\n'+(current if len(current)<=4800 else current[:2400]+'\n[full message in archive]\n'+current[-2400:])),parts

SCHEMA={'type':'object','properties':{'query':{'type':'string'},'start':{'type':'integer','minimum':0}},'additionalProperties':False}
if __name__=='__main__':
    with open(sys.argv[1]) as f:parts=json.load(f)
    for l in sys.stdin:
        r=json.loads(l);i=r.get('id');m=r.get('method');p=r.get('params',{});
        if i is None:continue
        if m=='initialize':v={'protocolVersion':p['protocolVersion'],'capabilities':{'tools':{}},'serverInfo':{'name':'history','version':'1'}}
        elif m=='tools/list':v={'tools':[{'name':'lookup','description':'Search the complete conversation history by literal substring or page from a numbered chunk. Returns up to 4 chunks.','inputSchema':SCHEMA}]}
        elif m=='tools/call' and p.get('name')=='lookup':
            a=p.get('arguments',{});q=str(a.get('query',''))[:512].casefold();start=a.get('start',0);start=start if type(start) is int and start>=0 else 0;hits=[]
            for n in range(start,len(parts)):
                if q in parts[n].casefold():hits.append(f'{n}: {parts[n]}')
                if len(hits)==4:break
            v={'content':[{'type':'text','text':'\n'.join(hits) or 'No matching chunks.'}]}
        elif m=='ping':v={}
        else:print(json.dumps({'jsonrpc':'2.0','id':i,'error':{'code':-32601,'message':'Unknown method'}}),flush=True);continue
        print(json.dumps({'jsonrpc':'2.0','id':i,'result':v}),flush=True)
