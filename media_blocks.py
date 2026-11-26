"""Inline-only media normalization. Never resolve a URL or pass media to the history tool."""
import base64
import binascii
import re
from formats import Failure

MIMES={'image/png','image/jpeg','image/webp','image/gif'}

def image(block,kind):
    if not isinstance(block,dict):return None
    typ=block.get('type')
    if kind in ('chat','responses') and typ==('image_url' if kind=='chat' else 'input_image'):
        url=block.get('image_url');url=url.get('url') if isinstance(url,dict) else url
        match=re.fullmatch(r'data:([^;,]+);base64,(.*)',url or '') if isinstance(url,str) else None
        if not match:raise Failure(400,'Only inline base64 data URI images are supported; external URLs are disabled for SSRF protection')
        mime,data=match.groups()
    elif kind=='anthropic' and typ=='image':
        source=block.get('source');source=source if isinstance(source,dict) else {}
        if source.get('type')!='base64':raise Failure(400,'Only inline base64 images are supported')
        mime,data=source.get('media_type'),source.get('data')
    elif kind=='gemini' and 'inlineData' in block:
        src=block['inlineData'];mime,data=(src.get('mimeType'),src.get('data')) if isinstance(src,dict) else (None,None)
    else:return None
    if not isinstance(mime,str) or mime not in MIMES:raise Failure(400,'Unsupported ACP media type; only PNG/JPEG/WebP/GIF images supported')
    if not isinstance(data,str) or not data:raise Failure(400,'Expected base64 image')
    if len(data)>4*((5*2**20+2)//3):raise Failure(413,'Image too large')
    try:n=len(base64.b64decode(data,validate=True))
    except (ValueError,binascii.Error):raise Failure(400,'Invalid base64 image')
    if n>5*2**20:raise Failure(413,'Image too large')
    return {'type':'image','mimeType':mime,'data':data}


def split_rows(rows):
    plain=[];images=[]
    for row in rows:
        content=row['content']
        if isinstance(content,list):
            parts=[]
            for p in content:
                if p['type']=='image':images.append(p);parts.append('[attached image %d]' % len(images))
                else:parts.append(p['text'])
            content=''.join(parts)
        plain.append({**row,'content':content})
    return plain,images


def attach(text,images):return [{'type':'text','text':text},*images] if images else text
