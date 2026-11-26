"""Bounded, fail-closed ACP continuation leases. Bearer nonce never logged."""
import hashlib
import re
import tempfile
import time
from contextlib import contextmanager
from formats import Failure

class Lease:
    def __init__(self, model, kind):
        self.directory=tempfile.TemporaryDirectory(prefix='acp-thread-'); self.sid=None
        self.model=model; self.kind=kind; self.turn=0; self.busy=False; self.at=time.monotonic()

class Leases:
    def __init__(self): self.items={};self.failed={}
    def remove(self,k):self.items.pop(k).directory.cleanup()
    def sweep(self):
        for k,v in list(self.items.items()):
            if not v.busy and time.monotonic()-v.at>1800:self.remove(k)
    @contextmanager
    def acquire(self,nonce,seq,model,kind,principal=None):
        if nonce is None and seq is None:yield None;return
        if not re.fullmatch('[0-9a-f]{64}',nonce or '') or not re.fullmatch('[1-9][0-9]{0,8}',seq or ''):raise Failure(400,'Expected random 64-hex X-ACP-Session and positive X-ACP-Turn')
        k=(principal,hashlib.sha256(nonce.encode()).digest());self.sweep();v=self.items.get(k)
        self.failed={a:t for a,t in self.failed.items() if time.monotonic()-t<1800}
        if k in self.failed:raise Failure(409,'Session incomplete; start with a fresh nonce and history')
        if not v:
            if seq!='1':raise Failure(409,'Session expired or worker changed; restart explicitly with history')
            if len(self.failed)>=1024 or len(self.items)>=32:raise Failure(503,'Session capacity reached')
            v=self.items[k]=Lease(model,kind)
        if v.busy or v.turn+1!=int(seq) or (v.model,v.kind)!=(model,kind):raise Failure(409,'Session busy, mismatched or unexpected turn')
        v.busy=True
        try:yield v
        except BaseException:
            self.failed[k]=time.monotonic();self.remove(k);raise
        else:
            if getattr(v,'incomplete',False):self.failed[k]=time.monotonic();self.remove(k)
            else:v.busy=False;v.at=time.monotonic();v.turn+=1
