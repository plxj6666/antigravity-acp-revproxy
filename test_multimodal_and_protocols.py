import asyncio
import os
import io
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import test_session_bridge as harness
from formats import Failure,parse
from protocol_options import Contract,budget,policy
from tool_bridge import declarations,normalize_history
from session_manager import incoming_result
from acp import active_timeout

PIC={'type':'image','data':'YWJj','mimeType':'image/png'}
URI='data:image/png;base64,YWJj'
BLOCK={'chat':{'type':'image_url','image_url':{'url':URI}},'responses':{'type':'input_image','image_url':URI},'anthropic':{'type':'image','source':{'type':'base64','media_type':'image/png','data':'YWJj'}},'gemini':{'inlineData':{'mimeType':'image/png','data':'YWJj'}}}
ROUTES={'responses':'/v1/responses','anthropic':'/v1/messages','gemini':'/v1/models/antigravity-default:generateContent','chat':'/v1/chat/completions'}

def body(k,block):
    if k=='gemini':return {'contents':[{'parts':[block]}]}
    return {'input' if k=='responses' else 'messages':[{'role':'user','content':[block]}]}

class Http(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=harness.ManagedIntegration.asyncSetUp
    records=harness.ManagedIntegration.records
    chat=harness.ManagedIntegration.chat
    async def post(self,k,b):return await self.client.post(ROUTES[k],data=io.BytesIO(json.dumps(b).encode()),headers={'Authorization':'Bearer keyA','Content-Type':'application/json'})

    async def test_four_images_to_acp(self):
        for k in ROUTES:
            r=await self.post(k,body(k,BLOCK[k]));self.assertEqual(r.status,200,await r.text());self.assertEqual(self.records()[-1]['images'],[PIC])

    async def test_invalid_inputs(self):
        for block in [{'type':'input_image','image_url':'https://example.invalid/x'}, {'type':'input_image','image_url':URI.replace('png','svg+xml')},{'type':'input_audio','input_audio':{}},{'type':'input_image','image_url':URI+'@'}]:
            self.assertEqual((await self.post('responses',body('responses',block))).status,400)
        b=body('gemini',BLOCK['gemini']);b['contents'][0]['parts']*=11;self.assertEqual((await self.post('gemini',b)).status,400)
        self.assertEqual((await self.post('responses',{'input':'a'*(2<<20)})).status,413)

    async def test_correction_is_bounded(self):
        with patch.dict(os.environ,ACP_TEST_IGNORE_TOOLS='1'):
            r=await self.chat([harness.user('go')],[harness.fn('read')],tool_choice='required');self.assertEqual(r.status,502);self.assertIn('forced tool_choice',await r.text());self.assertFalse(self.manager.sessions)
        log=self.records();ids=[x['rpc_id'] for x in log if 'rpc_id' in x];self.assertEqual(ids,sorted(set(ids)));self.assertEqual(sum(x.get('method')=='session/prompt' for x in log),2);self.assertEqual(len({x['pid'] for x in log}),1)

    async def test_specific_and_continuation_cap(self):
        tools=[harness.fn('one'),harness.fn('two')];r=await self.chat([harness.user('go')],tools,tool_choice={'type':'function','function':{'name':'two'}});self.assertEqual((await r.json())['choices'][0]['message']['tool_calls'][0]['function']['name'],'two')
        rows=[harness.user('test')];tools=[harness.fn('one')];r=await self.chat(rows,tools,max_tokens=2);m=(await r.json())['choices'][0]['message'];rows.extend([m,dict(role='tool',tool_call_id=m['tool_calls'][0]['id'],content='hello world')]);r=await self.chat(rows,tools,max_tokens=100);self.assertEqual((await r.json())['choices'][0]['message']['content'],'hello world')

    async def test_store_stop_invalid_json(self):
        r=await self.post('responses',{'input':'test','store':True});self.assertIn('idle-ttl',r.headers['X-ACP-Response-Storage']);i=(await r.json())['id'];r=await self.post('responses',{'input':'next','previous_response_id':i});self.assertEqual(r.headers['X-ACP-Session-State'],'previous')
        r=await self.chat([harness.user('go')],stop='K');self.assertEqual((await r.json())['choices'][0]['message']['content'],'O')
        r=await self.chat([harness.user('go')],response_format={'type':'json_object'});self.assertEqual(r.status,502)

    async def test_forced_result_can_finish(self):
        tools=[harness.fn('read'),harness.fn('other')];specific={'type':'function','function':{'name':'read'}}
        for follow in ('auto',specific):
            rows=[harness.user('go')];r=await self.chat(rows,tools,tool_choice=specific);m=(await r.json())['choices'][0]['message'];rows.extend([m,dict(role='tool',tool_call_id=m['tool_calls'][0]['id'],content='done')])
            r=await self.chat(rows,tools,tool_choice=follow);self.assertEqual((await r.json())['choices'][0]['message']['content'],'done')

    async def test_tool_stop_boundary(self):
        for stream in (True,False):
            r=await self.chat([harness.user('PRETOOL')],[harness.fn('read')],stop=['king'],stream=stream);wire=await r.text();self.assertIn('Chec',wire);self.assertNotIn('tool_calls',wire)
        rows=[harness.user('PRETOOL')];tools=[harness.fn('read')];r=await self.chat(rows,tools,stop=['missing']);m=(await r.json())['choices'][0]['message'];self.assertEqual(m['content'],'Checking')
        rows.extend([m,dict(role='tool',tool_call_id=m['tool_calls'][0]['id'],content='done')]);r=await self.chat(rows,tools,stop=['missing']);self.assertEqual((await r.json())['choices'][0]['message']['content'],'done')

    async def test_terminal_delivery_failure(self):
        from aiohttp import web
        real=web.StreamResponse.write;ids=[]
        async def write(resp,data):
            if data.startswith(b'event: response.completed'):
                ids.append(json.loads(data.split(b'data: ',1)[1])['response']['id']);raise ConnectionResetError()
            return await real(resp,data)
        with patch.object(web.StreamResponse,'write',write):
            r=await self.post('responses',{'input':'hi','stream':True});await r.read()
        self.assertTrue(ids);self.assertEqual((await self.post('responses',{'input':'next','previous_response_id':ids[0]})).status,409)

    async def test_bad_arguments_not_cancelled(self):
        t=harness.fn('read');t['function']['parameters'].update(required=['x']);r=await self.chat([harness.user('go')],[t]);self.assertIn('input schema validation failed',(await r.json())['choices'][0]['message']['content'])

    async def test_paused_deadline(self):
        s=SimpleNamespace(waited=0.,wait_start=None)
        async def tool():
            s.wait_start=time.monotonic();await asyncio.sleep(.1);s.waited+=time.monotonic()-s.wait_start;s.wait_start=None;return True
        self.assertTrue(await active_timeout(s,tool(),.03));
        with self.assertRaises(asyncio.TimeoutError):await active_timeout(s,asyncio.sleep(.1),.02)

class Options(unittest.TestCase):
    def test_choice_matrix(self):
        for k in ROUTES:
            for mode in ('none','auto','required','specific'):
                t={'name':'read'};b={'tools':[{'functionDeclarations':[t]} if k=='gemini' else {'type':'function','function':t} if k=='chat' else {'type':'function',**t}]}
                if k=='gemini':b['toolConfig']={'functionCallingConfig':{'mode':{'none':'NONE','auto':'AUTO','required':'ANY','specific':'ANY'}[mode],**({'allowedFunctionNames':['read']} if mode=='specific' else {})}}
                elif k=='anthropic':b['tool_choice']={'type':{'required':'any','specific':'tool'}.get(mode,mode),'name':'read'}
                else:b['tool_choice']=mode if mode!='specific' else {'type':'function',**({'function':t} if k=='chat' else t)}
                self.assertEqual(policy(k,b).mode,mode);self.assertEqual(len(declarations(k,b)),int(mode!='none'))

    def test_fields_and_nested_media(self):
        for k,b in [('gemini',{'generationConfig':{'thinkingConfig':{'thinkingLevel':'XHIGH'},'responseMimeType':'application/json'}}),('anthropic',{'thinking':{'type':'enabled','budget_tokens':4096}}),('chat',{'stream_options':{'include_usage':True},'response_format':{'type':'json_object'}})]:
            parse(k,{**body(k,BLOCK[k]),**b});Contract(k,b).validate('{"x":1}')
        self.assertEqual(budget(4096),'medium');b=body('gemini',{'functionResponse':{'id':'x','name':'read','response':{},'parts':[BLOCK['gemini']]}});self.assertEqual(incoming_result('gemini',b)[1]['content'][-1],PIC);self.assertEqual(parse('gemini',normalize_history('gemini',b))[1][-1],PIC)

    def test_split_stop(self):
        c=Contract('chat',{'stop':['END']});self.assertEqual(c.feed('beforeE')+c.feed('NDafter')+c.feed('',True),'before')
