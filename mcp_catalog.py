"""Read-only MCP catalog. Never executes tools, even if permission is granted."""
import json
import sys

if __name__=='__main__':
    with open(sys.argv[1]) as f: catalog=json.load(f)
    for line in sys.stdin:
        req=json.loads(line); i=req.get('id'); method=req.get('method')
        if i is None:continue
        if method=='initialize':result={'protocolVersion':req['params']['protocolVersion'],'capabilities':{'tools':{}},'serverInfo':{'name':'client-catalog','version':'1'}}
        elif method=='tools/list':result={'tools':catalog}
        elif method=='ping':result={}
        else:
            print(json.dumps({'jsonrpc':'2.0','id':i,'error':{'code':-32601,'message':'No server-side tool execution'}}),flush=True);continue
        print(json.dumps({'jsonrpc':'2.0','id':i,'result':result}),flush=True)
