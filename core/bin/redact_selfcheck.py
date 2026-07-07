"""extract/*/redact_flow.py のエンジン契約を合成 flow で検査する (make -C .devcontainer/core check-redact-selfcheck)。

各 redact_flow.py に合成 flow (複数の HTTP flow) を round-trip させ、
  - 出力が valid NDJSON (各行が JSON として読める)
  - 行数が入力 flow 数と一致する (flow を取りこぼさない / 増やさない)
  - 例外なく完了する
を assert する。これはアプリ非依存に常に成り立つべき「エンジン契約」。

各アプリ固有の秘密を実際に伏せているか (網羅性) はここでは検査しない。合成 flow では
各アプリの実秘密を再現できず、アプリごとに伏せる対象も違うため、汎用に「生トークンが
残っていないか」を assert すると新規アプリで偽陽性になる。秘密の網羅性は、子 claude が
本物の flow を見ながら対話的に確かめる領分 (それがサンドボックスの存在意義)。

negative probe: 「不正 JSON を出す redact」「改行を混ぜて行を増やす redact」を流し、
検査器がそれぞれを検出することを pin する (= この検査器自体が機能していることの担保)。

副作用を残さない: 一時物は TemporaryDirectory に隔離し、subprocess には
PYTHONDONTWRITEBYTECODE=1 を渡して __pycache__ を残さない。
"""

from __future__ import annotations

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


def synth_flows() -> bytes:
    """秘密らしき値を含む複数の HTTP flow を 1 つの flows バイト列にする。
    GET / POST(ヘッダ+body) / response 無し をそれぞれ 1 本ずつ含め、to_dict の経路を広く通す。"""
    flows = [
        tflow.tflow(
            req=tutils.treq(method=b"GET", path=b"/api/items?q=1"),
            resp=tutils.tresp(content=b'{"items":[1,2,3]}'),
        ),
        tflow.tflow(
            req=tutils.treq(
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
    f3 = tflow.tflow(req=tutils.treq(method=b"DELETE", path=b"/api/items/9"))
    f3.response = None
    flows.append(f3)

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


def check_contract(redact_path: Path, flows: bytes) -> list[str]:
    """エンジン契約違反のリストを返す (空なら OK)。"""
    errs: list[str] = []
    p = run_redact(redact_path, flows)
    if p.returncode != 0:
        errs.append(f"exit={p.returncode}: {p.stderr.decode(errors='replace').strip()[:400]}")
        return errs
    lines = p.stdout.decode().splitlines()
    if len(lines) != N_FLOWS:
        errs.append(f"行数 {len(lines)} != flow 数 {N_FLOWS}")
    for i, ln in enumerate(lines):
        try:
            json.loads(ln)
        except json.JSONDecodeError as e:
            errs.append(f"行 {i} が不正 JSON: {e}")
    return errs


def write_temp_redact(d: Path, redact_body: str) -> Path:
    """core/redact/flows_to_ndjson を使う一時 redact_flow.py を作る (negative probe 用)。"""
    d.mkdir(parents=True, exist_ok=True)
    src = (
        "import sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(ENGINE)!r})\n"
        "from flows_to_ndjson import flows_to_ndjson\n"
        f"{redact_body}\n"
        'if __name__ == "__main__":\n'
        "    flows_to_ndjson(redact)\n"
    )
    p = d / "redact_flow.py"
    p.write_text(src)
    return p


def main() -> int:
    flows = synth_flows()
    failed = False

    # 1) 本検査: 各 extract/*/redact_flow.py がエンジン契約を満たす
    apps = sorted((REPO / "extract").glob("*/redact_flow.py"))
    if not apps:
        print("note: extract/*/redact_flow.py が無い (検査対象なし)")
    for rp in apps:
        errs = check_contract(rp, flows)
        rel = rp.relative_to(REPO)
        if errs:
            failed = True
            print(f"NG  {rel}", file=sys.stderr)
            for e in errs:
                print(f"      {e}", file=sys.stderr)
        else:
            print(f"ok  {rel}")

    # 2) negative probe: 検査器が契約違反を実際に検出することを pin する
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # (a) 不正 JSON を出す redact → "不正 JSON" を検出するはず
        rp = write_temp_redact(root / "bad_json", "def redact(s):\n    return 'NOT_JSON'\n")
        if not any("不正 JSON" in e for e in check_contract(rp, flows)):
            print("negative: 不正 JSON を出す redact を検出できなかった", file=sys.stderr)
            failed = True
        # (b) 改行を混ぜて行を増やす redact → "行数" のズレを検出するはず
        rp = write_temp_redact(root / "extra_line", 'def redact(s):\n    return s + "\\nINJECTED"\n')
        if not any("行数" in e for e in check_contract(rp, flows)):
            print("negative: 改行混入で行を増やす redact を検出できなかった", file=sys.stderr)
            failed = True

    if failed:
        print("redact selfcheck FAILED", file=sys.stderr)
        return 1
    print("ok  redact selfcheck (engine contract + negative probe)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
