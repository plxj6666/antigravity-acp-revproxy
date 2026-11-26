import unittest
from aiohttp.test_utils import TestClient,TestServer
from gateway import make_app
from tool_bridge import ClientCall

class ToolContinuation(unittest.IsolatedAsyncioTestCase):
    async def test_large_pi_system_after_edit(self):
        system='system note. '*2700+'END_INSTRUCTIONS';target='ORIGINAL_TASK'; rows=[{'role':'user','parts':[{'text':target}]},{'role':'model','parts':[{'functionCall':{'name':'edit','args':{'content':'x'*18000}}}]},{'role':'user','parts':[{'functionResponse':{'name':'edit','response':{'output':'edit ok'}}}]}]
        rows[0]['parts'][0]['text']='Old task'
        rows[1:1]=[{'role':'user','parts':[{'functionResponse':{'name':'lookup','response':{'output':'ok'}}},{'text':target}]}]
        async def backend(model,prompt,*a,archive,system_text):
            self.assertEqual(system,system_text);self.assertNotIn(system,prompt);self.assertIn('Latest user task before tool execution:\n'+target,prompt);self.assertIn('edit ok',prompt);return 'done','stop',{}
        async with TestClient(TestServer(make_app('abc',backend))) as client:
            r=await client.post('/v1/models/demo:generateContent',json={'systemInstruction':{'parts':[{'text':system}]},'contents':rows},headers={'x-goog-api-key':'abc'});self.assertEqual(r.status,200,await r.text())

    def test_acp_display_metadata(self):
        from acp import public_tool_label
        generic={'name':'mcp_client_pi_edit_0','title':'Run mcp_client_pi_edit_0'}
        self.assertEqual(public_tool_label({**generic,'title':'修正车轮位置'}),'修正车轮位置')
        self.assertEqual(public_tool_label({**generic,'rawInput':{'toolSummary':'private argument'}}),'')

    async def test_public_tool_label_before_function_call(self):
        async def backend(model,prompt,ready,piece,*args):
            if ready:await ready()
            call=ClientCall('edit',{});call.label='Align wheels';raise call
        async with TestClient(TestServer(make_app('abc',backend))) as client:
            for action in ('generateContent','streamGenerateContent?alt=sse'):
                r=await client.post('/v1/models/demo:'+action,json={'contents':[{'parts':[{'text':'fix wheels'}]}]},headers={'x-goog-api-key':'abc'});wire=await r.text()
                self.assertEqual(r.status,200);self.assertLess(wire.index('Align wheels'),wire.index('functionCall'));self.assertNotIn('"thought"',wire)

    async def test_tool_only_has_labeled_status_when_requested(self):
        async def backend(model,prompt,ready,piece,*args):
            if ready:await ready()
            raise ClientCall('write',{})
        async with TestClient(TestServer(make_app('abc',backend))) as client:
            for stream in (False,True):
                for show in (False,True):
                    action='streamGenerateContent?alt=sse' if stream else 'generateContent';r=await client.post('/v1/models/demo:'+action,json={'generationConfig':{'thinkingConfig':{'includeThoughts':show}},'contents':[{'parts':[{'text':'create page'}]}]},headers={'x-goog-api-key':'abc'});wire=await r.text();self.assertEqual(r.status,200,wire);self.assertEqual('Gateway: requesting client tool write.' in wire,show); self.assertIn('functionCall',wire)
