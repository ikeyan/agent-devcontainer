"""extract/*/redact_flow.py のエンジン契約を合成 flow で検査する (make -C .devcontainer/core check-redact-selfcheck)。

各 redact_flow.py に合成 flow (複数の HTTP flow) を round-trip させ、
  - 出力が valid NDJSON (各行が JSON として読める)
  - 行数が「受理されるべき flow 数」と一致する (flow を取りこぼさない / 増やさない)
  - 例外なく完了する
を assert する。これはアプリ非依存に常に成り立つべき「エンジン契約」。

各アプリ固有の秘密を実際に伏せているか (網羅性) はここでは検査しない。合成 flow では
各アプリの実秘密を再現できず、アプリごとに伏せる対象も違うため、汎用に「生トークンが
残っていないか」を assert すると新規アプリで偽陽性になる。秘密の網羅性は、子 claude が
本物の flow を見ながら対話的に確かめる領分 (それがサンドボックスの存在意義)。

## ホストで flow を落とすアプリ (SELFCHECK_HOSTS)

アプリによっては、対象ドメイン以外の flow を行ごと落とす。これは整理でなく防御である
ことが多い — ヘッダの redact は「知っている値だけ伏せる」denylist になりがちで、対象
ドメインの外を通すと未知の auth ヘッダが素通りする。したがって「行数 = 入力 flow 数」を
全アプリに強制して filter を消させるのは本末転倒になる。

そこでアプリ側に受理ホストを宣言させ、検査器はそれを使って合成 flow を組み立てる:

    SELFCHECK_HOSTS = ("api.anthropic.com",)   # module 直下・リテラルで書く

宣言があるアプリには「受理ホストの flow N 本 + 落とされるべき 1 本」を流し、出力が
ちょうど N 行であることを要求する。これで
  - 取りこぼし / 増加の検出力 (== 判定) を緩めずに済む
  - filter そのものが検査対象になる (宣言だけして落としていなければ N+1 行で落ちる)
宣言が無いアプリは従来どおり、既定ホストの flow N 本に対して N 行を要求する。

宣言は `ast` で静的に読む (import も実行もしない) ので、検査器が consumer のコードを
自プロセスに取り込むことはない。

negative probe: 「不正 JSON を出す redact」「改行を混ぜて行を増やす redact」「受理ホストを
宣言しているのに落とさない redact」を流し、検査器がそれぞれを検出することを pin する
(= この検査器自体が機能していることの担保)。

副作用を残さない: 一時物は TemporaryDirectory に隔離し、subprocess には
PYTHONDONTWRITEBYTECODE=1 を渡して __pycache__ を残さない。
"""

from __future__ import annotations

import ast
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from mitmproxy import io as mio
from mitmproxy.test import tflow, tutils

# ENGINE はこのスクリプトの位置基準 (kit root 直下 core/ でも consumer の .devcontainer/core/ でも合う)。
# REPO (extract/*/redact_flow.py の走査 root) は git toplevel — consumer ではリポジトリ root、
# kit repo では extract/ が無く「検査対象なし」の note になる (negative probe は常に走る)。
REPO = Path(
    subprocess.run(
        ["git", "-C", str(Path(__file__).resolve().parent), "rev-parse", "--show-toplevel"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
)
ENGINE = Path(__file__).resolve().parents[1] / "redact"
N_FLOWS = 3
# tutils.treq の既定ホスト。実在せず、どのアプリの allowlist にも入らない。
DEFAULT_HOST = "address"
# SELFCHECK_HOSTS を宣言したアプリが必ず落とすべきホスト。`.invalid` は RFC 2606 の予約 TLD
# なので、正当な allowlist がこれを含むことはない。
REJECTED_HOST = "selfcheck-rejected.invalid"


def read_selfcheck_hosts(path: Path) -> tuple[str, ...]:
    """redact_flow.py が module 直下で宣言する SELFCHECK_HOSTS を静的に読む。

    import も実行もしない (検査器のプロセスに consumer のコードを持ち込まないため)。
    宣言が無い / リテラルでない / 空 の場合は空 tuple = 「このアプリは flow を落とさない」。
    宣言を誤った場合は行数の不一致として本検査が落ちるので、ここでは黙って空を返してよい。
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return ()  # 実行時にも同じ理由で落ちるので、そちらのエラーで報告される
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "SELFCHECK_HOSTS" for t in node.targets):
            continue
        try:
            value = ast.literal_eval(node.value)
        except ValueError:
            return ()
        if isinstance(value, str):
            value = (value,)
        if isinstance(value, (list, tuple)) and value and all(isinstance(v, str) and v for v in value):
            return tuple(value)
        return ()
    return ()


def synth_flows(host: str, *, reject_host: str | None = None) -> bytes:
    """秘密らしき値を含む複数の HTTP flow を 1 つの flows バイト列にする。
    GET / POST(ヘッダ+body) / response 無し をそれぞれ 1 本ずつ含め、to_dict の経路を広く通す。
    全て `host` 宛。`reject_host` を渡すと「落とされるべき」flow を 1 本だけ末尾に足す。"""
    flows = [
        tflow.tflow(
            req=tutils.treq(host=host, method=b"GET", path=b"/api/items?q=1"),
            resp=tutils.tresp(content=b'{"items":[1,2,3]}'),
        ),
        tflow.tflow(
            req=tutils.treq(
                host=host,
                method=b"POST",
                path=b"/api/login",
                headers=[(b"authorization", b"Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.SIG_secret")],
                content=b'{"deviceToken":"id123:APA91bSECRET_fcm"}',
            ),
            resp=tutils.tresp(
                headers=[(b"set-cookie", b"session=TOPSECRET; Path=/")],
                content=b'{"ok":true}',
            ),
        ),
    ]
    # response 無しの flow (to_dict が response 系キーを出さない経路)
    f3 = tflow.tflow(req=tutils.treq(host=host, method=b"DELETE", path=b"/api/items/9"))
    f3.response = None
    flows.append(f3)

    if reject_host is not None:
        flows.append(
            tflow.tflow(
                req=tutils.treq(host=reject_host, method=b"GET", path=b"/should-be-dropped"),
                resp=tutils.tresp(content=b'{"leaked":"THIRD_PARTY_SECRET"}'),
            )
        )

    buf = io.BytesIO()
    w = mio.FlowWriter(buf)
    for f in flows:
        w.add(f)
    return buf.getvalue()


def run_redact(redact_path: Path, flows: bytes) -> subprocess.CompletedProcess:
    """redact_flow.py を、いま mitmproxy が import できているこの Python で実行する
    (uv 経由なら uv の Python、システムなら system python3 — どちらも sys.executable)。"""
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run(
        [sys.executable, str(redact_path)],
        input=flows,
        capture_output=True,
        env=env,
    )


def check_contract(redact_path: Path) -> list[str]:
    """エンジン契約違反のリストを返す (空なら OK)。

    受理ホストを宣言しているアプリには非対象ホストの flow を 1 本混ぜ、それが落ちた上で
    受理分がちょうど N_FLOWS 行出ることを要求する。"""
    errs: list[str] = []
    hosts = read_selfcheck_hosts(redact_path)
    if hosts:
        flows = synth_flows(hosts[0], reject_host=REJECTED_HOST)
    else:
        flows = synth_flows(DEFAULT_HOST)

    p = run_redact(redact_path, flows)
    if p.returncode != 0:
        errs.append(f"exit={p.returncode}: {p.stderr.decode(errors='replace').strip()[:400]}")
        return errs
    lines = p.stdout.decode().splitlines()
    if len(lines) != N_FLOWS:
        if hosts and len(lines) == N_FLOWS + 1:
            errs.append(
                f"行数 {len(lines)} — SELFCHECK_HOSTS を宣言しているのに "
                f"{REJECTED_HOST} 宛の flow を落としていない"
            )
        elif hosts and not lines:
            errs.append(
                f"行数 0 — 宣言された SELFCHECK_HOSTS[0] ({hosts[0]}) を filter が受理していない"
            )
        else:
            errs.append(f"行数 {len(lines)} != 受理されるべき flow 数 {N_FLOWS}")
    for i, ln in enumerate(lines):
        try:
            json.loads(ln)
        except json.JSONDecodeError as e:
            errs.append(f"行 {i} が不正 JSON: {e}")
    return errs


def write_temp_redact(d: Path, redact_body: str, *, declare_hosts: tuple[str, ...] = ()) -> Path:
    """core/redact/flows_to_ndjson を使う一時 redact_flow.py を作る (negative probe 用)。
    declare_hosts を渡すと SELFCHECK_HOSTS を宣言するが filter は実装しない
    (= 宣言と実装の食い違いを検査器が捕まえるかの probe になる)。"""
    d.mkdir(parents=True, exist_ok=True)
    decl = f"SELFCHECK_HOSTS = {tuple(declare_hosts)!r}\n" if declare_hosts else ""
    src = (
        "import sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(ENGINE)!r})\n"
        "from flows_to_ndjson import flows_to_ndjson\n"
        f"{decl}"
        f"{redact_body}\n"
        'if __name__ == "__main__":\n'
        "    flows_to_ndjson(redact)\n"
    )
    p = d / "redact_flow.py"
    p.write_text(src)
    return p


def main() -> int:
    failed = False

    # 1) 本検査: 各 extract/*/redact_flow.py がエンジン契約を満たす
    apps = sorted((REPO / "extract").glob("*/redact_flow.py"))
    if not apps:
        print("note: extract/*/redact_flow.py が無い (検査対象なし)")
    for rp in apps:
        errs = check_contract(rp)
        rel = rp.relative_to(REPO)
        if errs:
            failed = True
            print(f"NG  {rel}", file=sys.stderr)
            for e in errs:
                print(f"      {e}", file=sys.stderr)
        else:
            hosts = read_selfcheck_hosts(rp)
            note = f" (SELFCHECK_HOSTS={hosts[0]} / 非対象 1 本の drop も確認)" if hosts else ""
            print(f"ok  {rel}{note}")

    # 2) negative probe: 検査器が契約違反を実際に検出することを pin する
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # (a) 不正 JSON を出す redact → "不正 JSON" を検出するはず
        rp = write_temp_redact(root / "bad_json", "def redact(s):\n    return 'NOT_JSON'\n")
        if not any("不正 JSON" in e for e in check_contract(rp)):
            print("negative: 不正 JSON を出す redact を検出できなかった", file=sys.stderr)
            failed = True
        # (b) 改行を混ぜて行を増やす redact → "行数" のズレを検出するはず
        rp = write_temp_redact(root / "extra_line", 'def redact(s):\n    return s + "\\nINJECTED"\n')
        if not any("行数" in e for e in check_contract(rp)):
            print("negative: 改行混入で行を増やす redact を検出できなかった", file=sys.stderr)
            failed = True
        # (c) 受理ホストを宣言しながら落とさない redact → 非対象 flow の素通りを検出するはず
        rp = write_temp_redact(
            root / "declared_no_filter",
            "def redact(s):\n    return s\n",
            declare_hosts=("api.example.com",),
        )
        if not any("落としていない" in e for e in check_contract(rp)):
            print("negative: 宣言だけして落とさない redact を検出できなかった", file=sys.stderr)
            failed = True

    if failed:
        print("redact selfcheck FAILED", file=sys.stderr)
        return 1
    print("ok  redact selfcheck (engine contract + negative probe)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
