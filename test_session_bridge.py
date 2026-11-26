import asyncio
import json
import os
from pathlib import Path
import signal
import tempfile
import time
import unittest
from unittest.mock import patch
from aiohttp.test_utils import TestClient, TestServer
from gateway import make_app
from session_manager import SessionManager


def user(s):return {'role':'user','content':s}
def fn(name):return {'type':'function','function':{'name':name,'parameters':{'type':'object'}}}


class ManagedIntegration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.trace=Path(self.tmp.name)/'trace'
        env=patch.dict(os.environ,AGY_BIN=str(Path(__file__).with_name('fixtures')/'fake_agy.py'),ACP_TEST_TRACE=str(self.trace),GATEWAY_API_KEYS_JSON='{"a":"keyA","b":"keyB"}',ACP_SLOT='test1',ACP_SESSION_KEYS_JSON='{"one":"dedicated"}')
        env.start();self.addCleanup(env.stop)
        self.manager=SessionManager()
        with patch('gateway.SessionManager',return_value=self.manager):app=make_app('shared')
        self.client=TestClient(TestServer(app,handler_cancellation=True));await self.client.start_server();self.addAsyncCleanup(self.client.close)

    async def chat(self,rows,tools=None,key='keyA',headers=None,**kwargs):
        return await self.client.post('/v1/chat/completions',headers={'Authorization':'Bearer '+key,**(headers or {})},json=dict(messages=rows,tools=tools or [],**kwargs))

    def records(self):return [json.loads(l) for l in self.trace.read_text().splitlines()]

    async def step(self,n,prompt):
        rows=[user(prompt)];tools=[fn(str(i)) for i in range(n)];r=await self.chat(rows,tools);seen=set()
        for i in range(n):
            self.assertEqual(r.status,200);body=await r.json();self.assertNotIn('usage',body)
            msg=body['choices'][0]['message'];c=msg['tool_calls'][0];self.assertTrue(c['id'].startswith('call_test1_'));seen.add(c['function']['name'])
            rows.extend([msg,dict(role='tool',tool_call_id=c['id'],content='v'+c['function']['name'])]);snapshot=rows[:]
            self.assertEqual((await self.chat(snapshot,tools,key='keyB')).status,409)
            r=await self.chat(snapshot,tools);self.assertEqual(r.headers.get('X-ACP-Session-State'),'tool');self.assertNotIn('X-ACP-History-Mode',r.headers)
            self.assertEqual((await self.chat(snapshot,tools)).status,409)
        body=await r.json();self.assertEqual(seen,{str(i) for i in range(n)});self.assertEqual(body['choices'][0]['message']['content'],' '.join('v'+str(i) for i in range(n)));self.assertEqual(body['usage']['total_tokens'],6)
        log=self.records();self.assertEqual(len({m['pid'] for m in log}),1);self.assertEqual([m['prompt_bytes'] for m in log if 'prompt_bytes' in m],[len(prompt)])

    async def test_redacted_diag(self):
        directory=Path(self.tmp.name)/'logs'
        with patch.dict(os.environ,ACP_LOG_DIR=str(directory)),patch('builtins.print') as log:
            r=await self.chat([user('QA_DIAG')]);self.assertEqual(r.status,200);await r.read()
            rid=r.headers['X-Gateway-Request-Id']
            m=next(json.loads(args[0]) for args,_ in log.call_args_list if rid in str(args))
            self.assertEqual((m['thought_chunks'],m['denied_native_tools']),(1,1))
            self.assertLessEqual(m['last_acp_update_s'],m['total_s']+.001)
            self.assertNotIn('private sentinel',str(m))
            self.assertEqual((directory/(rid+'.log')).stat().st_mode&0o777,0o600)

    async def test_single(self):await self.step(1,'ONE')
    async def test_parallel(self):await self.step(2,'TWO')
    async def test_large(self):await self.step(1,'LARGE')

    async def test_system_prefix_once_across_warm_and_resume(self):
        rows=[dict(role='system',content='unique system directive'),user('first')]
        for turn in range(3):
            r=await self.chat(rows);self.assertEqual(r.status,200);await r.read()
            if turn==1:
                s=next(iter(self.manager.sessions));s.at-=301;await self.manager.sweep()
            rows.extend([dict(role='assistant',content='OK'),user(str(turn))])
        prefixes=[v['system_prefix'] for v in self.records() if 'system_prefix' in v]
        self.assertIn('unique system directive',prefixes[0]);self.assertEqual(prefixes[1:],['',''])

    async def test_clipping(self):
        r=await self.chat([user('THINK')],max_tokens=1);b=await r.json();self.assertEqual(r.status,200);self.assertEqual(b['choices'][0]['message']['content'],'OK')
        r=await self.chat([user('LARGE')],[fn('read')],max_tokens=2);self.assertEqual((await r.json())['choices'][0]['finish_reason'],'length')

    async def test_expiry(self):
        tools=[fn('read')];rows=[user('ONE')];r=await self.chat(rows,tools);msg=(await r.json())['choices'][0]['message'];rows.extend([msg,dict(role='tool',tool_call_id=msg['tool_calls'][0]['id'],content='x')]);s=next(iter(self.manager.sessions));s.at-=301;await self.manager.sweep();self.assertEqual((await self.chat(rows,tools)).status,409)

    async def test_isolation_branch_resume(self):
        rows=[user('first')];await self.chat(rows);await self.chat(rows,key='keyB');self.assertEqual(len(self.manager.sessions),2)
        s=next(s for s in self.manager.sessions if s.key[0]==('api','a'));s.at-=301;await self.manager.sweep();self.assertEqual(s.state,'SUSPENDED');self.assertIsNone(s.proc)
        extended=rows+[dict(role='assistant',content='OK'),user('again')];r=await self.chat(extended);self.assertEqual(r.headers['X-ACP-Session-State'],'prefix');self.assertEqual(self.records()[-1]['prompt_bytes'],5);self.assertTrue(any(x.get('method')=='session/resume' for x in self.records()))
        extended[1]['content']='edited';r=await self.chat(extended);self.assertEqual(r.headers['X-ACP-Session-State'],'cold');self.assertEqual(len(self.manager.sessions),3)

    async def test_implicit_resume_failure_cold_replay(self):
        rows=[user('first')];await self.chat(rows)
        s=next(iter(self.manager.sessions));s.at-=301;await self.manager.sweep()
        with patch.dict(os.environ,ACP_TEST_RESUME_FAIL='1'):
            r=await self.chat(rows+[dict(role='assistant',content='OK'),user('again')])
        self.assertEqual(r.status,200);self.assertEqual(r.headers['X-ACP-Session-State'],'cold-replay')
        self.assertGreater(self.records()[-1]['prompt_bytes'],len('again'))

    async def test_pending_crash_and_config(self):
        rows=[user('ONE')];tools=[fn('read')];r=await self.chat(rows,tools);msg=(await r.json())['choices'][0]['message'];s=next(iter(self.manager.sessions))
        self.assertEqual(Path(s.config_path).stat().st_mode&0o777,0o600)
        self.assertEqual(Path(s.private.name).stat().st_mode&0o777,0o700)
        self.assertLess(len(os.fsencode(s.socket_path)),80)
        os.kill(s.proc.pid,signal.SIGKILL)
        for _ in range(100):
            if not self.manager.sessions:break
            await asyncio.sleep(.02)
        rows.extend([msg,dict(role='tool',tool_call_id=msg['tool_calls'][0]['id'],content='x')])
        self.assertFalse(self.manager.sessions);self.assertEqual((await self.chat(rows,tools)).status,409)

    async def test_gemini_prefix_write_failure_aborts_pending(self):
        from aiohttp import web
        real=web.StreamResponse.write
        async def write(resp,data):
            if b'Gateway: requesting client tool' in data:raise ConnectionResetError('simulated handoff failure')
            return await real(resp,data)
        with patch.object(web.StreamResponse,'write',write):
            r=await self.client.post('/v1/models/antigravity-default:streamGenerateContent?alt=sse',headers={'x-goog-api-key':'keyA'},json={'contents':[{'parts':[{'text':'ONE'}]}],'generationConfig':{'thinkingConfig':{'includeThoughts':True}},'tools':[{'functionDeclarations':[{'name':'read','parameters':{'type':'object'}}]}]});await r.read()
        self.assertFalse(self.manager.sessions)

    async def test_edit_during_tool_cannot_seed_prefix(self):
        rows=[user('ONE')];tools=[fn('read')];r=await self.chat(rows,tools);m=(await r.json())['choices'][0]['message'];rows=[user('edited'),m,dict(role='tool',tool_call_id=m['tool_calls'][0]['id'],content='x')]
        r=await self.chat(rows,tools);self.assertEqual(r.status,200);self.assertEqual(next(iter(self.manager.sessions)).hashes,[])
        r=await self.chat(rows+[dict(role='assistant',content='x'),user('next')],tools);self.assertEqual(r.headers['X-ACP-Session-State'],'cold')

    async def test_idle_crash_does_not_suspend(self):
        h={'X-ACP-Session':'c'*64,'X-ACP-Turn':'1','X-ACP-Session-Key':'dedicated'}
        await self.chat([user('hello')],headers=h);s=next(iter(self.manager.sessions));os.kill(s.proc.pid,signal.SIGKILL);await s.proc.wait();s.at-=301;await self.manager.sweep();self.assertFalse(self.manager.sessions)
        h['X-ACP-Turn']='2';self.assertEqual((await self.chat([user('again')],headers=h)).status,409)

    async def test_orphan_not_task(self):
        self.manager.cold_replay=True;i='call_test1_'+'a'*24
        result=lambda x:dict(role='user',content=[dict(type='tool_result',tool_use_id=x,content='x')]);rows=[result('orphan'),dict(role='assistant',content=[dict(type='tool_use',id=i,name='read',input={})]),result(i)]
        r=await self.client.post('/v1/messages',headers={'x-api-key':'keyA'},json={'messages':rows,'max_tokens':100});self.assertEqual(r.status,409);self.assertFalse(self.manager.sessions)

    async def test_explicit_tool_turn(self):
        h={'X-ACP-Session':'b'*64,'X-ACP-Turn':'1','X-ACP-Session-Key':'dedicated'}
        r=await self.chat([user('ONE')],[fn('read')],headers=h);msg=(await r.json())['choices'][0]['message']
        rows=[dict(role='tool',tool_call_id=msg['tool_calls'][0]['id'],content='result')]
        self.assertEqual((await self.chat(rows,[fn('read')],headers=h)).status,409)
        h['X-ACP-Turn']='2';r=await self.chat(rows,[fn('read')],headers=h)
        self.assertEqual((await r.json())['choices'][0]['message']['content'],'result')

    async def test_crash_and_disconnect(self):
        headers={'X-ACP-Session':'a'*64,'X-ACP-Turn':'1','X-ACP-Session-Key':'dedicated'};r=await self.chat([user('CRASH')],headers=headers);self.assertEqual(r.status,502);self.assertEqual((await self.chat([user('again')],headers=headers)).status,409)
        r=await self.chat([user('WAIT')],stream=True);s=next(iter(self.manager.sessions));pid=s.proc.pid;r.close()
        for _ in range(100):
            if not self.manager.sessions:break
            await asyncio.sleep(.02)
        self.assertFalse(self.manager.sessions)
        with self.assertRaises(ProcessLookupError):os.kill(pid,0)

    async def test_orphan_and_previous(self):
        h={'Authorization':'Bearer keyA'};url='/v1/responses';r=await self.client.post(url,headers=h,json={'input':[{'type':'function_call_output','call_id':'call_unknown','output':'x'}]});self.assertEqual(r.status,409)
        r=await self.client.post(url,headers=h,json={'input':'hello'});prev=(await r.json())['id'];self.assertTrue(prev.startswith('resp_test1_'))
        r=await self.client.post(url,headers=h,json={'previous_response_id':prev,'input':'new'});self.assertEqual(r.headers['X-ACP-Session-State'],'previous');self.assertEqual(self.records()[-1]['prompt_bytes'],3)
