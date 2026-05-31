#!/usr/bin/env python3
"""Share the loopback FLEX viewer on the LAN, and make it mobile-friendly,
WITHOUT restarting the viewer.

Forwards <LAN-IP>:8732 -> 127.0.0.1:8732 as a TCP relay. For the main page
(GET /) it injects a viewport meta tag + responsive CSS into <head> on the
way through; SSE (/stream), /labels, and everything else pass through raw and
unbuffered. The running viewer is never touched.

  python3 ~/flex-lan-share.py            # bind LAN IP : 8732
  LAN_PORT=8080 python3 ~/flex-lan-share.py
"""
import os
import re
import socket
import threading

TARGET = ("127.0.0.1", 8732)
LISTEN_PORT = int(os.environ.get("LAN_PORT", "8732"))

# Injected before </head> on the GET / response. Media query => desktop view
# is unchanged; only narrow (phone) screens get the overrides. The 16px input
# font stops iOS from zooming when the filter box is focused.
INJECT = (
    b'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
    b'<style id="lan-responsive">'
    b'html{-webkit-text-size-adjust:100%}'
    b'@media (max-width:640px){'
    b'header{padding:10px 14px;gap:8px 12px;flex-wrap:wrap}'
    b'h1{font-size:11px}'
    b'.filter-wrap{margin-left:0;width:100%;order:9}'
    b'input[type=text]{width:100%;font-size:16px}'
    b'.chips{flex-wrap:wrap}.chip{padding:5px 8px 5px 11px}'
    b'main{padding:2px 12px 84px}'
    b'.page{margin:0 -12px;padding:14px 12px 16px}'
    b'.meta{gap:6px 10px;font-size:12px}.channel{margin-left:0}'
    b'.body{font-size:13px;padding-left:10px}'
    b'.label-edit{width:60vw}'
    b'}</style>\n'
)


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def read_head(sock):
    """Read up to (and incl.) the \\r\\n\\r\\n header terminator.
    Returns (head_bytes, leftover_bytes_after_headers)."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
        if len(buf) > 65536:
            break
    i = buf.find(b"\r\n\r\n")
    if i == -1:
        return buf, b""
    return buf[: i + 4], buf[i + 4:]


def header_value(head, name):
    m = re.search(rb"(?im)^" + re.escape(name) + rb":[ \t]*([^\r\n]*)\r\n", head)
    return m.group(1) if m else b""


def set_content_length(head, n):
    repl = ("Content-Length: %d\r\n" % n).encode()
    new, count = re.subn(rb"(?im)^content-length:[^\r\n]*\r\n", repl, head, count=1)
    return new if count else head[:-2] + repl + b"\r\n"


def pump(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def pump_both(a, b):
    t = threading.Thread(target=pump, args=(a, b), daemon=True)
    t.start()
    pump(b, a)
    t.join()


def handle(client):
    up = None
    try:
        head, extra = read_head(client)
        if not head:
            return
        try:
            line = head.split(b"\r\n", 1)[0].decode("latin1").split(" ")
            method, path = line[0], line[1]
        except Exception:
            method, path = "", ""
        up = socket.create_connection(TARGET, timeout=5)
        up.sendall(head + extra)

        if method == "GET" and path in ("/", "/index.html"):
            try:
                rhead, rbody = read_head(up)
                ctype = header_value(rhead, b"Content-Type")
                cl = header_value(rhead, b"Content-Length")
                if cl.isdigit():
                    need = int(cl)
                    while len(rbody) < need:
                        chunk = up.recv(65536)
                        if not chunk:
                            break
                        rbody += chunk
                if b"text/html" in ctype.lower() and b"</head>" in rbody:
                    rbody = rbody.replace(b"</head>", INJECT + b"</head>", 1)
                    rhead = set_content_length(rhead, len(rbody))
                client.sendall(rhead + rbody)
            except Exception:
                pass  # injection failed; the connection just continues raw below
        # Pass through anything else (and any keep-alive follow-ups) raw.
        pump_both(client, up)
    except OSError:
        pass
    finally:
        for s in (client, up):
            try:
                if s:
                    s.close()
            except OSError:
                pass


def main():
    ip = lan_ip()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((ip, LISTEN_PORT))
    srv.listen(128)
    print("FLEX viewer shared on your LAN (mobile-responsive):")
    print("  http://%s:%d/" % (ip, LISTEN_PORT))
    print("  injects viewport+responsive CSS on GET / ; SSE/labels pass raw")
    print("  running viewer (127.0.0.1:%d) untouched. Ctrl-C to stop." % TARGET[1])
    try:
        while True:
            client, _ = srv.accept()
            threading.Thread(target=handle, args=(client,), daemon=True).start()
    except KeyboardInterrupt:
        print("\nStopped sharing.")
    finally:
        srv.close()


if __name__ == "__main__":
    main()
