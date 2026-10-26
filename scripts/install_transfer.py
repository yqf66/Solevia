"""Meter child installer traffic without decrypting TLS or changing machine settings.

Only the child receives an authenticated loopback proxy. Bytes include TLS overhead;
they are network traffic, never a claimed archive size or install percentage.
"""
import base64
import http.server
import os
import secrets
import select
import socket
import subprocess
import sys
import threading
import time
import urllib.parse


class Traffic:
    def __init__(self):
        self.received = 0
        self.lock = threading.Lock()

    def add(self, size):
        with self.lock:
            self.received += size


class Tunnel(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass

    def do_CONNECT(self):
        self.forward(True)

    def do_GET(self):
        self.forward(False)

    def do_HEAD(self):
        self.forward(False)

    def forward(self, tunnel):
        if not secrets.compare_digest(self.headers.get('Proxy-Authorization', ''), self.server.authorization):
            self.send_error(407)
            return
        upstream = None
        try:
            uri = urllib.parse.urlsplit('//' + self.path if tunnel else self.path)
            host, port = uri.hostname, uri.port or (443 if tunnel else 80)
            if not host or port not in (80, 443):
                raise ValueError('Unsupported destination')
            proxy = self.server.upstream
            upstream = socket.create_connection((proxy.hostname, proxy.port or 80) if proxy else (host, port), timeout=45)
            if tunnel:
                if proxy:
                    auth = ''
                    if proxy.username:
                        credentials = urllib.parse.unquote(proxy.username) + ':' + urllib.parse.unquote(proxy.password or '')
                        auth = 'Proxy-Authorization: Basic ' + base64.b64encode(credentials.encode()).decode() + '\r\n'
                    upstream.sendall(f'CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n{auth}\r\n'.encode())
                    response = b''
                    while not response.endswith(b'\r\n\r\n') and len(response) < 32768:
                        part = upstream.recv(1)
                        if not part:
                            break
                        response += part
                    if b' 200 ' not in response.split(b'\r\n', 1)[0]:
                        raise OSError('Upstream proxy refused connection')
                self.send_response(200, 'Connection established')
                self.end_headers()
            else:
                target = self.path if proxy else urllib.parse.urlunsplit(('', '', uri.path or '/', uri.query, ''))
                headers = ''.join(f'{key}: {value}\r\n' for key, value in self.headers.items()
                                  if key.lower() not in ('proxy-authorization', 'proxy-connection', 'connection'))
                upstream.sendall(f'{self.command} {target} HTTP/1.1\r\n{headers}Connection: close\r\n\r\n'.encode())
            peers = [self.connection, upstream]
            while True:
                readable, _, _ = select.select(peers, [], [], 90)
                if not readable:
                    break
                for source in readable:
                    data = source.recv(65536)
                    if not data:
                        return
                    if source is upstream:
                        self.server.traffic.add(len(data))
                    (upstream if source is self.connection else self.connection).sendall(data)
        except (OSError, ValueError):
            self.close_connection = True
        finally:
            if upstream:
                upstream.close()


def run(command):
    env = os.environ.copy()
    proxy = env.get('HTTPS_PROXY') or env.get('https_proxy') or env.get('HTTP_PROXY') or env.get('http_proxy')
    upstream = urllib.parse.urlsplit(proxy) if proxy else None
    if upstream and (upstream.scheme != 'http' or not upstream.hostname):
        print('[网速提示] 当前代理无法计量传输流量，保留原网络配置继续安装。', flush=True)
        return subprocess.call(command, env=env)
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Tunnel)
    server.daemon_threads = True
    server.traffic = Traffic()
    server.upstream = upstream
    password = secrets.token_hex(20)
    server.authorization = 'Basic ' + base64.b64encode(('solevia:' + password).encode()).decode()
    url = urllib.parse.urlunsplit(('http', f'solevia:{password}@127.0.0.1:{server.server_port}', '', '', ''))
    for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'npm_config_proxy', 'npm_config_https_proxy'):
        env[key] = url
    for key in ('ALL_PROXY', 'all_proxy'):
        env.pop(key, None)
    env['NO_PROXY'] = env['no_proxy'] = 'localhost,127.0.0.1,::1'
    # urllib proxy support must be used rather than the optional Rust transfer engine.
    env['HF_HUB_DISABLE_XET'] = '1'
    threading.Thread(target=server.serve_forever, daemon=True).start()
    previous, stamp = 0, time.monotonic()
    process = subprocess.Popen(command, env=env)
    try:
        while process.poll() is None:
            time.sleep(1)
            now, current = time.monotonic(), server.traffic.received
            print(f'[NETWORK_PROGRESS] bytes={current} total=-1 speed={int((current-previous)/(now-stamp))}', flush=True)
            previous, stamp = current, now
        print(f'[NETWORK_PROGRESS] bytes={server.traffic.received} total=-1 speed=0', flush=True)
        return process.returncode
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    sys.exit(run(sys.argv[1:]))
