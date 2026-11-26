#!/usr/bin/env python3
"""Offline ACP peer for managed gateway integration tests. No network."""
import json
import os
import subprocess
import sys


def emit(**m):print(json.dumps(dict(jsonrpc='2.0',**m)),flush=True)

def trace(**fields):
    with open(os.environ['ACP_TEST_TRACE'],'a') as f:f.write(json.dumps(dict(pid=os.getpid(),**fields))+'\n')

sid='fake-session'; bridge=None
for line in sys.stdin:
    m=json.loads(line);method=m['method'];p=m['params'];result={};trace(method=method,rpc_id=m['id'])
    if method=='initialize':result={'protocolVersion':1}
    if method=='session/resume' and os.environ.get('ACP_TEST_RESUME_FAIL'):
        emit(id=m['id'],error={'code':-1,'message':'test'});continue
    if method in ('session/new','session/resume'):
        assert '_meta' not in p
        result={'sessionId':sid};server=next((s for s in p['mcpServers'] if s['name']=='client'),None)
        if server:
            bridge=subprocess.Popen([server['command'],*server['args']],stdin=-1,stdout=-1,text=True)
    if method=='session/prompt':
        prompt=p['prompt'][0]['text'];prefix,sep,tail=prompt.partition('\n\n--- Current request ---\n');prompt=tail if sep else prompt;trace(system_prefix=prefix if sep else '',prompt_bytes=len(prompt.encode()),images=[b for b in p['prompt'] if b['type']=='image']);answer='OK'
        if prompt=='CRASH':sys.exit(2)
        def chunk(kind,text):emit(method='session/update',params={'sessionId':sid,'update':{'sessionUpdate':kind,'content':{'type':'text','text':text}}})
        if prompt=='THINK':chunk('agent_thought_chunk','a'*1100000)
        if prompt=='QA_DIAG':
            chunk('agent_thought_chunk','private sentinel')
            emit(method='session/request_permission',id='perm',params={'sessionId':sid,'toolCall':{'name':'native'}})
            assert json.loads(sys.stdin.readline())['result']['outcome']['outcome']=='cancelled'
        if prompt=='WAIT':
            import time
            time.sleep(60)
        if prompt=='PRETOOL':chunk('agent_message_chunk','Checking')
        if bridge and not os.getenv('ACP_TEST_IGNORE_TOOLS'):
            def rpc(i,method,params):bridge.stdin.write(json.dumps(dict(id=i,method=method,params=params))+'\n');bridge.stdin.flush()
            rpc(0,'tools/list',{});tools=json.loads(bridge.stdout.readline())['result']['tools'];values={}
            for i,t in enumerate(tools):rpc(i+1,'tools/call',dict(name=t['name'],arguments={'html':'x'*65536} if prompt=='LARGE' else {}))
            for _ in tools:
                r=json.loads(bridge.stdout.readline());values[r['id']]=r['result']['content'][0]['text']
            answer=' '.join(values[i] for i in sorted(values))
        chunk('agent_message_chunk',answer);result={'stopReason':'end_turn','usage':{'inputTokens':2,'outputTokens':1,'cachedReadTokens':3,'totalTokens':6}}
    emit(id=m['id'],result=result)
