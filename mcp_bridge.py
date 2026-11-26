"""Session-local MCP stdio adapter. Tool execution belongs to the HTTP client."""
import json
import socket
import sys
import threading

LIMIT = 4 * 1024 * 1024
CANCELLED = {'isError': True, 'content': [{'type': 'text', 'text': 'Client tool execution cancelled or timed out'}]}


def serve(catalog, path, token):
    names = {tool['name'] for tool in catalog}
    output_lock = threading.Lock()
    lock = threading.Lock()
    active = {}

    def reply(i, **payload):
        with output_lock:
            print(json.dumps({'jsonrpc': '2.0', 'id': i, **payload}, separators=(',', ':')), flush=True)

    def invoke(i, sock, cancelled, name, arguments):
        result = CANCELLED
        try:
            sock.settimeout(None)
            sock.connect(path)
            if cancelled.is_set():
                raise OSError('cancelled')
            packet = json.dumps(dict(token=token, rpc_id=i, bridge_name=name, arguments=arguments)).encode() + b'\n'
            if len(packet) <= LIMIT:
                sock.sendall(packet)
                with sock.makefile('rb') as reader:
                    line = reader.readline(16*1024*1024 + 1)
                if len(line) <= 16*1024*1024 and line.endswith(b'\n'):
                    data = json.loads(line)
                    if isinstance(data, dict) and isinstance(data.get('content'), list) and type(data.get('isError')) is bool:
                        result = {'content': data['content'], 'isError': data['isError']}
        except (ValueError, OSError, RecursionError):
            pass
        finally:
            with lock:
                active.pop(i, None)
            sock.close()
        reply(i, result=result)

    while line := sys.stdin.buffer.readline(LIMIT + 1):
        if len(line) > LIMIT:
            return  # Fail closed without accumulating an unbounded stdio frame.
        try:
            req = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(req, dict):
            continue
        i, method = req.get('id'), req.get('method')
        params = req.get('params', {})
        if not isinstance(params, dict):
            params = {}
        if i is None:
            target = params.get('requestId')
            if method == 'notifications/cancelled' and type(target) in (str, int):
                with lock:
                    entry = active.get(target)
                    if entry is not None:
                        sock, cancelled = entry
                        cancelled.set()
                        try:
                            sock.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
            continue
        if type(i) not in (str, int):
            continue
        if method == 'initialize':
            reply(i, result={'protocolVersion': params.get('protocolVersion'), 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'client-bridge', 'version': '2'}})
        elif method == 'tools/list':
            reply(i, result={'tools': catalog})
        elif method == 'ping':
            reply(i, result={})
        elif method == 'tools/call':
            name, args = params.get('name'), params.get('arguments', {})
            if not isinstance(name, str) or name not in names or not isinstance(args, dict):
                reply(i, error={'code': -32602, 'message': 'Invalid tool or arguments'})
                continue
            with lock:
                if i in active or len(active) >= 8:
                    reply(i, result=CANCELLED)
                    continue
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                cancelled = threading.Event()
                active[i] = sock, cancelled
            threading.Thread(target=invoke, args=(i, sock, cancelled, name, args), daemon=True).start()
        else:
            reply(i, error={'code': -32601, 'message': 'Method not found'})


if __name__ == '__main__':
    catalog_path, socket_path, config_path = sys.argv[1:]
    with open(config_path, encoding="ascii") as f:
        ipc_token = f.read().strip()
    with open(catalog_path, encoding='utf-8') as f:
        tools = json.load(f)
    serve(tools, socket_path, ipc_token)
