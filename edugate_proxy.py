"""Host-only HTTP CONNECT proxy for Edugate.

Docker/Coolify traffic to edugate.ksu.edu.sa is reset (curl 56).
Run this on the host (not in Docker) so the container can tunnel through
your home network stack:

    python3 edugate_proxy.py

Then the bot auto-tries http://172.17.0.1:18080 and host.docker.internal.
"""
import argparse
import socket
import threading

ALLOW_HOSTS = {"edugate.ksu.edu.sa"}


def _pump(src, dst):
    try:
        while True:
            buf = src.recv(65536)
            if not buf:
                break
            dst.sendall(buf)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _handle(conn):
    try:
        header = b""
        while b"\r\n\r\n" not in header:
            chunk = conn.recv(4096)
            if not chunk:
                return
            header += chunk
            if len(header) > 8192:
                return
        line = header.split(b"\r\n", 1)[0].decode("ascii", "replace")
        parts = line.split()
        if len(parts) < 2 or parts[0].upper() != "CONNECT":
            conn.sendall(b"HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\n\r\n")
            return
        host, _, port = parts[1].partition(":")
        port = int(port or "443")
        if host not in ALLOW_HOSTS or port != 443:
            conn.sendall(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
            return
        remote = socket.create_connection((host, port), timeout=20)
        conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        leftover = header.split(b"\r\n\r\n", 1)[1]
        if leftover:
            remote.sendall(leftover)
        thread = threading.Thread(target=_pump, args=(conn, remote), daemon=True)
        thread.start()
        _pump(remote, conn)
        thread.join(timeout=60)
        remote.close()
    except OSError:
        try:
            conn.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
        except OSError:
            pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def main():
    parser = argparse.ArgumentParser(description="Host CONNECT proxy for Edugate")
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=18080)
    args = parser.parse_args()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.bind, args.port))
    sock.listen(32)
    print(f"edugate proxy listening on {args.bind}:{args.port} (edugate.ksu.edu.sa only)")
    while True:
        conn, _addr = sock.accept()
        threading.Thread(target=_handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
