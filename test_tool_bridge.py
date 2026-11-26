import unittest
from formats import Reply,Failure,parse
from tool_bridge import ClientCall,tool_response,tool_frames,declarations,normalize_history

class Bridge(unittest.TestCase):
    def test_event_arguments(self):
        c=ClientCall('lookup',{'id':3})
        for kind in ['chat','responses','anthropic','gemini']:
            r=Reply(kind,'demo'); self.assertTrue(tool_response(r,c)); frames=list(tool_frames(r,c));self.assertTrue(frames)
            if kind=='gemini':self.assertEqual(frames[0][1]['candidates'][0]['content']['parts'][0]['functionCall']['args'],{'id':3})
    def test_quoted_model_side_result(self):
        body={'contents':[{'role':'model','parts':[{'functionResponse':{'name':'lookup','response':{'output':'blue'}}}]}]}
        self.assertIn('blue',parse('gemini',normalize_history('gemini',body),'demo')[1])
    def test_allowlist(self):
        b={'tools':[{'functionDeclarations':[{'name':'a'},{'name':'b'}]}],'toolConfig':{'functionCallingConfig':{'allowedFunctionNames':['b']}}}
        self.assertEqual([d['name'] for d in declarations('gemini',b)],['b']);b['tools'][0]['functionDeclarations'][1]['parameters']={'items':{'$ref':'https://example.test/schema'}}
        with self.assertRaises(Failure):declarations('gemini',b)
