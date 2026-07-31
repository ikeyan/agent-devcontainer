#!/usr/bin/env python3
"""host 側踏み台トンネル。

127.0.0.1:<port> (host loopback) で待ち受け、接続ごとに
  <COMPOSE> exec -T secrets-proxy python /app/tcp-bridge.py 127.0.0.1 8081
を起動して、socket <-> その exec プロセスの stdio を中継する。

secrets-proxy の mitmweb は container の loopback にしか bind していない (dev から到達不可)。
publish は張らないので LAN/dev への露出はゼロ。host は docker exec を打てる (docker socket を
持つ) からこの踏み台で届くが、dev は docker socket を持たないので届かない。

要件は host の python3 と docker/compose のみ (socat 不要)。COMPOSE env に `-f` 一式 (project 層
先頭 + core, 例: `docker compose -f ../project/compose.yaml -f compose.yaml`) を含めて渡すこと —
project 名 (top-level `name:`) は project/compose.yaml が供給するため、それを欠くと `exec` が別 project
namespace を見て secrets-proxy を見つけられない (canon: facts/docker/compose-project-directory-and-env-resolution)。カレントは .devcontainer/core を想定。
Ctrl-C で閉じる。secrets-proxy 本体は動いたまま。
"""
import os
import shlex
import socket
import subprocess
import sys
import threading

HOST = "127.0.0.1"
PORT = int(os.environ.get("PROXY_WEB_TUNNEL_PORT", "8081"))
# ambient な COMPOSE_PROJECT_NAME は file の name: を黙って上書きして exec が別 project を指し、
# COMPOSE_ENV_FILES は補間に使う .env を余所へ差し替える (core/Makefile の COMPOSE と同じ隔離。
# make 経由でなく直接実行した場合もここで揃える)。
os.environ.pop("COMPOSE_PROJECT_NAME", None)
os.environ.pop("COMPOSE_ENV_FILES", None)
COMPOSE = shlex.split(os.environ.get("COMPOSE", "docker compose -f ../project/compose.yaml -f compose.yaml"))
EXEC_CMD = COMPOSE + [
    "exec", "-T", "secrets-proxy",
    "python", "/app/tcp-bridge.py", "127.0.0.1", "8081",
]


def handle(conn: socket.socket) -> None:
    with conn:
        try:
            proc = subprocess.Popen(EXEC_CMD, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        except OSError as exc:
            sys.stderr.write(f"[tunnel] exec 起動失敗: {exc}\n")
            return

        def upstream() -> None:
            # host socket -> exec stdin (-> container の mitmweb)
            try:
                while True:
                    data = conn.recv(65536)
                    if not data:
                        break
                    proc.stdin.write(data)
                    proc.stdin.flush()
            except OSError:
                pass
            finally:
                try:
                    proc.stdin.close()
                except OSError:
                    pass

        thread = threading.Thread(target=upstream, daemon=True)
        thread.start()
        try:
            # exec stdout (<- mitmweb) -> host socket
            while True:
                data = proc.stdout.read1(65536)
                if not data:
                    break
                conn.sendall(data)
        except OSError:
            pass
        finally:
            proc.terminate()


def main() -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind((HOST, PORT))
    except OSError as exc:
        sys.stderr.write(f"[tunnel] {HOST}:{PORT} を bind できません ({exc})。既に開いていませんか?\n")
        sys.exit(1)
    srv.listen(64)
    sys.stderr.write(
        f"[tunnel] http://{HOST}:{PORT}/?token=<上記 token> を開く"
        " (Ctrl-C で閉じる; secrets-proxy は動いたまま)\n"
    )
    try:
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=handle, args=(conn,), daemon=True).start()
    except KeyboardInterrupt:
        sys.stderr.write("\n[tunnel] 閉じました\n")
    finally:
        srv.close()


if __name__ == "__main__":
    main()
