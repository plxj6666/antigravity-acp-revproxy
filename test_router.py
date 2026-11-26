"""End-to-end: fresh HTTP connections -> router -> two isolated Unix worker processes -> fake ACP."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from aiohttp import ClientSession, TCPConnector
from aiohttp.test_utils import TestServer
from deploy.acp_router import make_app


class RouterIntegration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp=tempfile.TemporaryDirectory(dir='/tmp');self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
        for slot in ('lab1','lab2'):
            path=self.root/(slot+'.sock');env=dict(os.environ,ACP_SLOT=slot,AGY_BIN=str(Path('fixtures/fake_agy.py').resolve()),ACP_TEST_TRACE=str(self.root/slot),GATEWAY_API_KEYS_JSON='{"a":"keyA"}')
            p=await asyncio.create_subprocess_exec(sys.executable,'-c',"from gateway import make_app; from aiohttp import web; import sys; web.run_app(make_app('shared'),path=sys.argv[1],access_log=None,handler_cancellation=True)",str(path),env=env,stdout=-3,stderr=-3)
            async def stop(proc):
                if proc.returncode is None:proc.terminate()
                await proc.wait()
            self.addAsyncCleanup(stop,p)
            for _ in range(300):
                if path.exists():break
                if p.returncode is not None:self.fail('worker exited')
                await asyncio.sleep(.02)
            self.assertTrue(path.exists())
        self.pool('lab1','lab2');self.server=TestServer(make_app(self.root));await self.server.start_server();self.addAsyncCleanup(self.server.close)
        self.c=ClientSession(connector=TCPConnector(force_close=True),headers={'Authorization':'Bearer keyA'});self.addAsyncCleanup(self.c.close)

    def pool(self,*slots):
        tmp=self.root/'manifest.tmp';tmp.write_text(json.dumps({'slots':slots}));tmp.replace(self.root/'pool.json')

    async def post(self,rows):
        b=dict(messages=rows,tools=[{'type':'function','function':{'name':'read','parameters':{'type':'object'}}}]);raw=json.dumps(b).encode()
        async def upload():yield raw[:5];yield raw[5:]
        async with self.c.post(self.server.make_url('/v1/chat/completions'),data=upload(),headers={'Content-Type':'application/json'}) as r:return r.status,await r.json()

    async def test_gemini_action_affinity(self):
        for action in ('generateContent','streamGenerateContent?alt=sse'):
            url=self.server.make_url('/v1/models/antigravity-default:'+action)
            async with self.c.post(url,json={'contents':[{'role':'user','parts':[{'text':'first'}]}]}) as r:
                self.assertEqual(r.status,200);await r.read()
        self.assertTrue((self.root/'lab1').exists());self.assertFalse((self.root/'lab2').exists())

    async def test_manifest_slot_bounds(self):
        import runpy
        valid=runpy.run_path('deploy/acp-pool-sync')['valid_slot']
        self.assertEqual([s for s in ('lab1','lab0','lab64','lab65','lab123') if valid(s)],['lab1','lab64'])

    async def test_pending_owner_and_offline(self):
        rows=[dict(role='user',content='ONE')];status,b=await self.post(rows);self.assertEqual(status,200);m=b['choices'][0]['message'];cid=m['tool_calls'][0]['id'];self.assertTrue(cid.startswith('call_lab1_'));rows.extend([m,dict(role='tool',tool_call_id=cid,content='proof')]);self.pool('lab2');self.assertEqual((await self.post(rows))[0],503);self.assertFalse((self.root/'lab2').exists());self.pool('lab2','lab1');status,b=await self.post(rows);self.assertEqual(status,200);self.assertEqual(b['choices'][0]['message']['content'],'proof')

    async def test_fingerprint_hot_pool_and_previous(self):
        async def send(**b):
            async with self.c.post(self.server.make_url('/v1/responses'),json=b) as r:return r.status,await r.json()
        rows=[{'role':'user','content':'first'}];_,first=await send(input=rows);self.assertTrue(first['id'].startswith('resp_lab1_'));rows+= [dict(role='assistant',content='OK'),dict(role='user',content='again')];_,third=await send(input=rows);self.assertTrue(third['id'].startswith('resp_lab1_'));_,second=await send(input='other');self.assertTrue(second['id'].startswith('resp_lab2_'));status,last=await send(previous_response_id=second['id'],input='next');self.assertEqual(status,200);self.assertTrue(last['id'].startswith('resp_lab2_'));self.pool();r=await self.c.get(self.server.make_url('/v1/models'));self.assertEqual(r.status,503);await r.read();self.pool('lab2');_,new=await send(input='fresh');self.assertTrue(new['id'].startswith('resp_lab2_'))
