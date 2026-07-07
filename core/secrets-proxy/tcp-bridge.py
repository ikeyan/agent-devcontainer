#!/usr/bin/env python3
"""stdio <-> TCP ブリッジ (container 側)。

host 側トンネル (../proxy-web-tunnel.py) から `docker compose exec -T secrets-proxy
python /app/tcp-bridge.py 127.0.0.1 8081` として呼ばれ、この container 内の loopback
(127.0.0.1:8081 = mitmweb の Web UI) と、exec の stdin/stdout を双方向に中継する。

mitmweb は container の loopback にしか bind していない (dev から到達不可) ので、host は
publish ではなくこの docker exec 経由でだけ Web UI に届く。dev は docker socket を持たず
exec もできないので、この経路も使えない。
"""
import socket
import sys
import threading


def main() -> None:
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8081
    sock = socket.create_connection((host, port))

    def upstream() -> None:
        # exec の stdin (= host からのバイト列) を mitmweb へ流す。
        try:
            while True:
                chunk = sys.stdin.buffer.read1(65536)
                if not chunk:
                    break
                sock.sendall(chunk)
        except OSError:
            pass
        finally:
            try:
                sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    threading.Thread(target=upstream, daemon=True).start()

    # mitmweb からの応答を exec の stdout (= host) へ流す。
    try:
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
    except OSError:
        pass
    finally:
        sock.close()


if __name__ == "__main__":
    main()
