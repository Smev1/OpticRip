#!/usr/bin/env python3
"""Log-only HTTP proxy: records request lines / CONNECT targets, returns 502.

Used to observe which endpoints MakeMKV contacts and over which scheme.
"""
import socket, threading, sys, time, os

LOG = sys.argv[2] if len(sys.argv) > 2 else 'proxy.log'
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8888

def log(msg):
    with open(LOG, 'a', encoding='utf-8') as f:
        f.write('%s %s\n' % (time.strftime('%H:%M:%S'), msg))

def handle(c, addr):
    try:
        c.settimeout(5)
        data = b''
        while b'\r\n\r\n' not in data and len(data) < 65536:
            chunk = c.recv(4096)
            if not chunk:
                break
            data += chunk
        head = data.split(b'\r\n\r\n')[0].decode('latin1', 'replace')
        first = head.split('\r\n')[0] if head else '(empty)'
        log('FROM %s:%d %s' % (addr[0], addr[1], first))
        for line in head.split('\r\n')[1:]:
            if line.lower().startswith(('host:', 'user-agent:', 'proxy-connection:')):
                log('   ' + line)
        c.sendall(b'HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
    except Exception as e:
        log('ERR %s' % e)
    finally:
        try: c.close()
        except Exception: pass

srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(('127.0.0.1', PORT))
srv.listen(16)
log('listening on %d' % PORT)
while True:
    c, a = srv.accept()
    threading.Thread(target=handle, args=(c, a), daemon=True).start()
