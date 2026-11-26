import asyncio
import json
from pathlib import Path
import secrets
import sys
import tempfile
import unittest


class Bridge(unittest.IsolatedAsyncioTestCase):
    async def test_pending_bridge_does_not_block_discovery(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as td:
            catalog = Path(td) / 'tools.json'
            catalog.write_text('[{"name":"pi_read_0","inputSchema":{"type":"object"}}]')
            token = secrets.token_hex(32)
            config = Path(td) / 'secret'; config.touch(mode=0o600); config.write_text(token)
            path = td + '/ipc.sock'
            incoming = asyncio.Queue()
            server = await asyncio.start_unix_server(lambda r,w: incoming.put_nowait((r,w)),path=path)
            proc = await asyncio.create_subprocess_exec(sys.executable,'-I',str(Path(__file__).with_name('mcp_bridge.py')),str(catalog),path,str(config),stdin=-1,stdout=-1)
            async def send(i,method,**params):
                proc.stdin.write((json.dumps(dict(id=i,method=method,params=params))+'\n').encode());await proc.stdin.drain()
            async def recv():return json.loads(await asyncio.wait_for(proc.stdout.readline(),2))
            try:
                for i,(method,key) in enumerate([('initialize','serverInfo'),('tools/list','tools')]):
                    await send(i,method,protocolVersion='test');self.assertIn(key,(await recv())['result'])
                for i in [2,3]:
                    await send(i,'tools/call',name='pi_read_0',arguments={'x':1})
                    reader,writer=await asyncio.wait_for(incoming.get(),2)
                    try:
                        self.assertEqual(json.loads(await reader.readline()),dict(token=token,rpc_id=i,bridge_name='pi_read_0',arguments={'x':1}))
                        await send(4,'server/discover');self.assertEqual((await recv())['error']['code'],-32601)
                        await send(5,'ping');self.assertEqual((await recv())['result'],{})
                        if i==2:
                            result={'isError':False,'content':[{'type':'text','text':'from client'}]}
                            writer.write(json.dumps(result).encode()+b'\n');await writer.drain();self.assertEqual((await recv())['result'],result)
                        else:
                            await send(None,'notifications/cancelled',requestId=i);self.assertTrue((await recv())['result']['isError'])
                    finally:writer.close();await writer.wait_closed()
            finally:
                proc.stdin.close();await proc.stdin.wait_closed();await asyncio.wait_for(proc.wait(),2)
                server.close();await server.wait_closed()
