import asyncio
import io
import json
import unittest
from aiohttp import TCPConnector
from aiohttp.test_utils import TestClient, TestServer
from gateway import make_app, LIMIT
from formats import Failure


CASES = [('/v1/chat/completions', {'messages':[{'role':'user','content':'hi'}]}, 'Authorization','Bearer abc'),
         ('/v1/responses', {'input':'hi'}, 'Authorization','Bearer abc'),
         ('/v1/messages', {'messages':[{'role':'user','content':[{'type':'text','text':'hi'}]}],'max_tokens':128}, 'x-api-key','abc'),
         ('/v1beta/models/demo:generateContent', {'contents':[{'parts':[{'text':'hi'}]}]}, 'x-goog-api-key','abc')]


class Routes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.gate=asyncio.Event(); self.caps=[]; self.levels=[]
        async def fake(model,prompt,start,delta,cap,level,tools):
            self.levels.append(level)
            self.caps.append(cap)
            if prompt=='bad': raise Failure(400,'bad model')
            if start:
                await start(); await delta('一'); await self.gate.wait()
                if prompt=='fail': raise Failure(502,'failed')
                await delta('二')
            return '一二','stop',{'cachedReadTokens':80,'inputTokens':100,'thoughtTokens':4,'outputTokens':7,'totalTokens':107}
        self.client=TestClient(TestServer(make_app('abc',fake)),connector=TCPConnector(limit=150))
        await self.client.start_server()

    async def asyncTearDown(self):
        self.gate.set(); await self.client.close()

    async def test_sync(self):
        for url,b,k,v in CASES:
            r=await self.client.post(url,json=b,headers={k:v}); self.assertEqual(r.status,200)
            obj=await r.json(); self.assertIn('一二',str(obj))

    async def test_live_sse_chunked_input(self):
        for i,(url,b,k,v) in enumerate(CASES):
            self.gate.clear()
            if i==3: url=url.replace('generateContent','streamGenerateContent?alt=sse')
            else: b={**b,'stream':True}
            async def upload():
                raw=json.dumps(b).encode(); yield raw[:5]; yield raw[5:]
            r=await self.client.post(url,data=upload(),headers={k:v})
            self.assertEqual(r.status,200)
            # First delta must arrive BEFORE allowing the upstream to finish.
            while True:
                line=await asyncio.wait_for(r.content.readline(),2)
                self.assertTrue(line)
                if line.startswith(b'data:') and '一' in str(json.loads(line[5:])): break
            self.gate.set(); tail=await r.read()
            self.assertIn([b'[DONE]',b'response.completed',b'message_stop',b'"finishReason":"STOP"'][i],tail)

    async def test_pi_responses(self):
        self.gate.set()
        url,_,k,v=CASES[1]
        body={'input':[{'role':'developer','content':'Be concise.'},{'role':'user','content':[{'type':'input_text','text':'hi'}]}],
              'tools':[{'type':'function','name':'read','parameters':{'type':'object'}}], 'store':False,
              'include':['reasoning.encrypted_content'],'reasoning':{'effort':'low','summary':'auto'},
              'prompt_cache_key':'demo','max_output_tokens':4096,'stream':True}
        for turn in range(2):
            r=await self.client.post(url,json=body,headers={k:v}); wire=await r.text()
            self.assertEqual(r.status,200,wire)
            final=json.loads(wire.split('event: response.completed\ndata: ')[1])['response']
            self.assertEqual(final['output'][0]['content'][0]['text'],'一二')
            body['input'] += final['output'] + [{'role':'user','content':'hi again'}]
        for change in [{'store':True},{'tool_choice':'unknown'},{'max_output_tokens':False}]:
            r=await self.client.post(url,json={**body,**change},headers={k:v}); self.assertEqual(r.status,400)

    async def test_output_ceiling(self):
        from acp import clip_chunk
        for url,b,k,v in CASES:
            r=await self.client.post(url,json=b,headers={k:v})
            self.assertEqual(self.caps[-1], b.get('max_tokens',100000))
        url,b,k,v=CASES[0]
        await self.client.post(url,json={**b,'max_tokens':128000},headers={k:v})
        self.assertEqual(self.caps[-1],100000)
        parts=list(clip_chunk(' hello'*100001,100000))
        self.assertTrue(parts[-1][2])
        self.assertEqual(sum(n for _,n,_ in parts),100000)
        self.assertGreater(len(''.join(s for s,_,_ in parts)),100000)

    async def test_client_fields(self):
        for i,extra in [(0,{'max_tokens':50}),(3,{'generationConfig':{'maxOutputTokens':50,'temperature':0.7},'safetySettings':[{'category':'HARM_CATEGORY_HARASSMENT','threshold':'BLOCK_NONE'}]})]:
            with self.subTest(i=i):
                url,b,k,v=CASES[i]; r=await self.client.post(url,json={**b,**extra},headers={k:v}); self.assertEqual(r.status,200,await r.text()); self.assertIn('X-Gateway-Limit-Policy',r.headers)

                self.assertEqual(self.caps[-1],50)
        url,b,k,v=CASES[-1]
        for extra in [{'generationConfig':None},{'generationConfig':{'maxOutputTokens':False}},{'safetySettings':{}},{'safetySettings':[{'category':'HARM_CATEGORY_HARASSMENT','threshold':'BLOCK_LOW_AND_ABOVE'}]}]:
            r=await self.client.post(url,json={**b,**extra},headers={k:v}); self.assertEqual(r.status,400)

    async def test_gemini_silent_before_ready(self):
        async def silent(model,prompt,ready,piece,*_):
            await self.gate.wait(); await ready(); await piece('ok'); return 'ok','stop',{}
        async with TestClient(TestServer(make_app('abc',silent,heartbeat=.02))) as client:
            url,b,k,v=CASES[-1]
            try:
                r=await asyncio.wait_for(client.post(url.replace('generateContent','streamGenerateContent?alt=sse'),json=b,headers={k:v}),1)
                for _ in range(2):
                    self.assertTrue((await asyncio.wait_for(r.content.readline(),1)).startswith(b':')); self.assertEqual(await r.content.readline(),b'\n')
                self.gate.set(); self.assertIn('finishReason',await r.text())
            finally:self.gate.set()

    async def test_pi_eve_gemini(self):
        self.gate.set()
        url='/v1/models/gemini-4-neon:streamGenerateContent?alt=sse'
        b={'contents':[{'role':'user','parts':[{'text':'hi'}]}],'systemInstruction':{'parts':[{'text':'Be concise'}]},'tools':[{'functionDeclarations':[{'name':'read'}]}],'toolConfig':{'functionCallingConfig':{'mode':'VALIDATED'}},'generationConfig':{'maxOutputTokens':64000,'thinkingConfig':{'includeThoughts':True,'thinkingLevel':'MEDIUM'}}}
        r=await self.client.post(url,json=b,headers={'x-goog-api-key':'abc'})
        wire=await r.text(); self.assertEqual(r.status,200,wire); self.assertIn('finishReason',wire); self.assertNotIn('"thought":true',wire); self.assertIn('thinking',r.headers['X-Gateway-Compatibility'])

        self.assertEqual(self.levels[-1],'medium'); self.assertIn('"cachedContentTokenCount":80',wire); self.assertIn('"candidatesTokenCount":3',wire)

    async def test_gemini_optional_tools(self):
        url,b,k,v=CASES[-1]
        tools=[{'functionDeclarations':[{'name':'lookup'}]}]
        for mode in ('AUTO','NONE','ANY'):
            body={**b,'tools':tools,'toolConfig':{'functionCallingConfig':{'mode':mode}}}
            r=await self.client.post(url,json=body,headers={k:v})
            self.assertEqual(r.status,502 if mode=='ANY' else 200,await r.text())
            if mode!='ANY': self.assertIn('client functions',r.headers['X-Gateway-Compatibility'])

    async def test_concurrent_queue(self):
        url,b,k,v=CASES[2]; headers={k:v}; stream={**b,'stream':True}
        active=[await asyncio.wait_for(self.client.post(url,json=stream,headers=headers),2) for _ in range(100)]
        extra=asyncio.create_task(self.client.post(url,json=b,headers=headers)); await asyncio.sleep(.05); self.assertEqual(len(self.caps),100); self.assertFalse(extra.done()); self.gate.set()
        r=await asyncio.wait_for(extra,2); self.assertEqual(r.status,200,await r.text()); await asyncio.gather(*(s.read() for s in active))

    async def test_errors(self):
        url,b,k,v=CASES[1]; hdr={k:v}
        for body,status,headers in [(b,401,{}),({'input':'bad','stream':True},400,hdr),({'input':'x'*(LIMIT+1)},413,hdr),({'input':[{'role':'user','content':[{'type':'image'}]}]},400,hdr)]:
            with io.BytesIO(json.dumps(body).encode()) as upload:
                r=await self.client.post(url,data=upload,headers={**headers,'Content-Type':'application/json'}); self.assertEqual(r.status,status); await r.read()
        self.gate.set(); r=await self.client.post(url,json={'input':'fail','stream':True},headers=hdr)
        tail=await r.text(); self.assertIn('event: error',tail); self.assertNotIn('event: response.completed',tail)

    async def test_gemini_stream_array(self):
        self.gate.set(); url,b,k,v=CASES[-1]; r=await self.client.post(url.replace('generateContent','streamGenerateContent'),json=b,headers={k:v})
        data=await r.json(); self.assertEqual(data[-1]['candidates'][0]['finishReason'],'STOP')


if __name__=='__main__': unittest.main()
