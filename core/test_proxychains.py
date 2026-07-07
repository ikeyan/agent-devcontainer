#!/usr/bin/env python3
"""proxychains の透過ルーティング挙動を hermetic に検証する回帰テスト。

外部ネット不要。ローカルに「最初の request line を記録するだけ」の最小 HTTP proxy を立て、
proxychains4 でラップした curl を走らせ、proxy が `CONNECT <hostname>:443`(IP でなくホスト名)を
受け取ったことを assert する。これは secrets-proxy が rules.yaml を「ホスト名」で照合・注入できる前提
(= redsocks の SO_ORIGINAL_DST=IP 落ちに対する proxychains の優位点) を pin する。

- 宛先は `test.invalid`(RFC 6761 で必ず解決不能)。proxy_dns が捏造 IP を返し、connect 時にホスト名へ
  復元して CONNECT に載せる。実ネットワークには一切出ない (curl の接続先は 127.0.0.1 のテスト proxy)。
- HTTP(S)_PROXY を明示的に環境から外して curl を走らせる: proxy env を見せると curl 自身が
  secrets-proxy へ張ってしまい「proxychains が捕まえた」ことの検証にならないため。
- negative probe: proxychains 無しなら (test.invalid が解決できず) テスト proxy に接続は来ない
  = 観測された CONNECT が proxychains 由来であることの帰属を担保する。
"""
import os
import socket
import subprocess
import sys
import tempfile
import threading

TARGET_HOST = "test.invalid"


class Recorder:
    def __init__(self):
        self.line = None


def serve_once(sock, rec, timeout):
    """1 接続だけ受け、最初の request line を rec.line に記録する。"""
    sock.settimeout(timeout)
    try:
        conn, _ = sock.accept()
    except socket.timeout:
        return
    with conn:
        conn.settimeout(timeout)
        buf = b""
        try:
            while b"\r\n" not in buf and len(buf) < 4096:
                chunk = conn.recv(1024)
                if not chunk:
                    break
                buf += chunk
        except socket.timeout:
            pass
        rec.line = buf.split(b"\r\n", 1)[0].decode("latin1", "replace")
        try:
            conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        except OSError:
            pass


def curl_env():
    """curl 自身が proxy env を使わないよう proxy 系変数を除いた環境を返す。"""
    env = dict(os.environ)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
              "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        env.pop(k, None)
    return env


def run_curl(port, use_proxychains):
    conf = tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False)
    conf.write(
        "strict_chain\nproxy_dns\nremote_dns_subnet 224\n"
        "tcp_connect_time_out 8000\ntcp_read_time_out 15000\nquiet_mode\n"
        f"[ProxyList]\nhttp 127.0.0.1 {port}\n"
    )
    conf.close()
    curl = ["curl", "-s", "-o", "/dev/null", "--max-time", "5", f"https://{TARGET_HOST}/"]
    cmd = (["proxychains4", "-f", conf.name] + curl) if use_proxychains else curl
    try:
        subprocess.run(cmd, capture_output=True, timeout=20, env=curl_env())
    except subprocess.TimeoutExpired:
        pass
    finally:
        os.unlink(conf.name)


def probe(use_proxychains, accept_timeout):
    rec = Recorder()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    t = threading.Thread(target=serve_once, args=(srv, rec, accept_timeout))
    t.start()
    run_curl(port, use_proxychains)
    t.join(accept_timeout + 5)
    srv.close()
    return rec.line


def main():
    # positive: proxychains 経由ならテスト proxy が CONNECT test.invalid:443 (ホスト名) を受ける。
    line = probe(use_proxychains=True, accept_timeout=10)
    want = f"CONNECT {TARGET_HOST}:443"
    if not line or want not in line:
        print(f"FAIL proxychains: proxy が期待の CONNECT を受けていない "
              f"(got={line!r}, want に含む={want!r})", file=sys.stderr)
        sys.exit(1)

    # negative probe: proxychains 無しなら test.invalid は解決できず proxy に接続が来ないはず。
    line2 = probe(use_proxychains=False, accept_timeout=3)
    if line2 is not None:
        print(f"FAIL negative: proxychains 無しでテスト proxy に接続が来た (got={line2!r}) "
              f"→ 観測の帰属が壊れている", file=sys.stderr)
        sys.exit(1)

    print("ok  proxychains (hostname preserved: CONNECT test.invalid:443 / negative clean)")


if __name__ == "__main__":
    main()
