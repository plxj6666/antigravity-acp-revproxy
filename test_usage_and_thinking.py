import asyncio
import io
import json
from contextlib import redirect_stdout
from tool_bridge import ClientCall
import unittest
from aiohttp.test_utils import TestClient, TestServer
from formats import Reply, normalize_acp_usage
from gateway import make_app
from test_gateway import CASES


class UsageAndHeartbeat(unittest.IsolatedAsyncioTestCase):
    def test_counts_and_chat_finish(self):
        usage=dict(inputTokens=11,cachedReadTokens=100,cachedWriteTokens=5,outputTokens=24,thoughtTokens=4)
        for kind in ('chat','responses','anthropic','gemini'):
            r=Reply(kind,'demo');r.usage=usage
            obj=r.result('ok','stop');u=r.api_usage()
            if kind=='chat':
                self.assertEqual(obj['usage']['prompt_tokens'],116)
                r.include_usage=True;frames=list(r.frames('end','ok','stop'))
                self.assertEqual(frames[-2][1]['choices'],[]);self.assertEqual(frames[-2][1]['usage'],u);self.assertEqual(frames[-1][1],'[DONE]')
            elif kind=='gemini':self.assertEqual(obj['usageMetadata'],dict(promptTokenCount=116,cachedContentTokenCount=100,candidatesTokenCount=20,thoughtsTokenCount=4,totalTokenCount=140))
            elif kind=='responses':self.assertEqual(u['input_tokens']-u['input_tokens_details']['cached_tokens'],16)
            else:self.assertEqual(u['input_tokens'],11)
        self.assertIsNone(normalize_acp_usage({'used':100,'size':1048576}))

    async def test_native_summary_before_text_or_tool(self):
        opts=[{'reasoning_effort':'high'},{'reasoning':{}},{'thinking':{'type':'adaptive'}},{'generationConfig':{'thinkingConfig':{'includeThoughts':True}}}]
        for i,(url,body,k,v) in enumerate(CASES):
            for tool in (False,True):
                for stream in (False,True):
                    gate=asyncio.Event();log=io.StringIO()
                    if not stream:gate.set()
                    async def backend(model,prompt,ready,piece,*args,thought_delta,context_update,system_text):
                        self.assertNotIn('<brief>',prompt);context_update(12345,98765)
                        await thought_delta('SYNTHETIC_SUMMARY');await gate.wait();await piece('PUBLIC_TEXT')
                        if tool:raise ClientCall('lookup',{})
                        return 'PUBLIC_TEXT','stop',{'inputTokens':3,'outputTokens':2}
                    path=url;b={**body,**opts[i]}
                    if stream and i==3:path=url.replace('generateContent','streamGenerateContent?alt=sse')
                    elif stream:b['stream']=True
                    with redirect_stdout(log):
                        async with TestClient(TestServer(make_app('abc',backend))) as client:
                            try:
                                res=await client.post(path,json=b,headers={k:v});self.assertEqual(res.status,200)
                                if not stream:
                                    self.assertEqual(res.headers['X-ACP-Context-Used'],'12345');obj=await res.json();wire=json.dumps(obj)
                                else:
                                    lines=[]
                                    while True:
                                        line=await asyncio.wait_for(res.content.readline(),1);self.assertTrue(line);lines.append(line.decode())
                                        if b'SYNTHETIC_SUMMARY' in line:break
                                    gate.set();wire=''.join(lines)+await res.text()
                                if i==0 and not stream:self.assertEqual(obj['choices'][0]['message']['reasoning_content'],'SYNTHETIC_SUMMARY')
                                else:self.assertLess(wire.index('SYNTHETIC_SUMMARY'),wire.index('PUBLIC_TEXT'))
                                if tool:
                                    self.assertLess(wire.index('PUBLIC_TEXT'),wire.index('lookup'));self.assertNotIn('12345',wire)
                                    if not stream:
                                        if i==0:self.assertNotIn('usage',obj)
                                        if i==1:self.assertIsNone(obj['usage'])
                                        if i==2:self.assertEqual(obj['usage'],{'input_tokens':0,'output_tokens':0})
                                        if i==3:self.assertNotIn('usageMetadata',obj)
                                if stream and i==2:
                                    frames=[json.loads(l[6:]) for l in wire.splitlines() if l.startswith('data: ')]
                                    for evt in ('content_block_start','content_block_stop'):
                                        self.assertEqual([x['index'] for x in frames if x['type']==evt],list(range(2+int(tool))))
                                if stream and i==1:
                                    frames=[json.loads(l[6:]) for l in wire.splitlines() if l.startswith('data: ')]
                                    self.assertEqual([x['type'] for x in frames[-1]['response']['output']],['reasoning','message']+(['function_call'] if tool else []))
                                    self.assertEqual([x['sequence_number'] for x in frames],list(range(len(frames))))
                            finally:gate.set()
                    metric=json.loads(log.getvalue());self.assertEqual(metric['context_used'],12345);self.assertEqual(metric['context_size'],98765);self.assertEqual(metric['req_id'],res.headers['X-Gateway-Request-Id']);self.assertNotIn('SYNTHETIC_SUMMARY',log.getvalue());self.assertNotIn('PUBLIC_TEXT',log.getvalue())

    def test_responses_partial_only_last_item(self):
        r=Reply('responses','demo')
        events=[x for phase,s in [('summary','a'),('delta','b')] for _,x in r.frames(phase,s)]
        events.extend(x for _,x in r.frames('end','b','length'))
        done=[x['item'] for x in events if x['type']=='response.output_item.done']
        self.assertEqual(done,events[-1]['response']['output'])
        self.assertEqual([x['status'] for x in done],['completed','incomplete'])

    async def test_post_start_error_all_sse(self):
        from formats import Failure
        async def backend(model,prompt,ready,piece,*args):
            await ready();await piece('visible');raise Failure(502,'upstream failed')
        async with TestClient(TestServer(make_app('abc',backend))) as client:
            for i,(url,b,k,v) in enumerate(CASES):
                if i==3:url=url.replace('generateContent','streamGenerateContent?alt=sse')
                else:b={**b,'stream':True}
                res=await client.post(url,json=b,headers={k:v});wire=await res.text()
                rows=[json.loads(l[6:]) for l in wire.splitlines() if l.startswith('data: ')]
                err=rows[-1]
                self.assertEqual(err['message'] if i==1 else err['error']['message'],'upstream failed')
                self.assertNotIn(('[DONE]','response.completed','message_stop','"STOP"')[i],wire)

    async def test_silent_backend_all_sse(self):
        gate=asyncio.Event()
        async def backend(model,prompt,ready,piece,*args):
            await gate.wait();await ready();await piece('ok');return 'ok','stop',{}
        async with TestClient(TestServer(make_app('abc',backend,heartbeat=.02))) as client:
            for url,b,k,v in CASES:
                gate.clear()
                if 'generateContent' in url:url=url.replace('generateContent','streamGenerateContent?alt=sse')
                else:b={**b,'stream':True}
                try:
                    resp=await asyncio.wait_for(client.post(url,json=b,headers={k:v}),2)
                    self.assertIn((await asyncio.wait_for(resp.content.readline(),1)).strip(),(b': keepalive',b': connected'))
                finally:gate.set()
                self.assertIn('ok',await resp.text())
