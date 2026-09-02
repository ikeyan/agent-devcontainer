"""extract/*/redact_flow.py のエンジン契約を合成 flow で検査する (make -C .devcontainer/core check-redact-selfcheck)。

各 redact_flow.py に合成 flow (複数の HTTP flow) を round-trip させ、
  - 出力が valid NDJSON (各行が JSON として読める)
  - 行数が入力 flow 数と一致する (flow を取りこぼさない / 増やさない)
  - 例外なく完了する
を assert する。これはアプリ非依存に常に成り立つべき「エンジン契約」。

例外は「host で flow を丸ごと落とす」アプリ。module トップレベルに
    SELFCHECK_DOMAINS = ("anthropic.com", "claude.com")
と受理 subtree の root を宣言すると (実行せず ast で読む)、行数一致の代わりに境界契約を検査する:
root ごとに受理されるべき host (root 自身と probe.<root>) には全部の形 (GET+resp / 秘密らしき
値を積んだ POST / response 無し) を流し、正しい filter なら必ず落ちる近傍 — dot 境界破りが拾う
not<root> / <root>.probe.invalid、および実接続先が域外で Host ヘッダだけ受理 host を騙る偽装
flow (pretty_host で filter する実装が通す) — には秘密形を流して、「受理 flow を全部・それだけ、
1 flow = 1 つの JSON object、url の host が読める形で出力する」ことを (host, method) 単位の
Counter で assert する (host 単位だと同一 host 内の形の差し替え = 取りこぼし+重複の相殺が見えない)。
filter の実装は engine の iter_flow_dicts / flows_to_ndjson(keep_domains=) を宣言と同じ tuple で
使う (core/redact/flows_to_ndjson.py) — 宣言と実装が構造的にずれない。宣言の再束縛 (実行時は
最後が勝つのに検査は 1 つしか読めない) や root の重複・入れ子・probe 名前空間との衝突は
宣言エラーとして拒否する (fail-closed)。

各アプリ固有の秘密を実際に伏せているか (網羅性) はここでは検査しない。合成 flow では
各アプリの実秘密を再現できず、アプリごとに伏せる対象も違うため、汎用に「生トークンが
残っていないか」を assert すると新規アプリで偽陽性になる。秘密の網羅性は、子 claude が
本物の flow を見ながら対話的に確かめる領分 (それがサンドボックスの存在意義)。

negative probe: 契約違反の各 class (不正 JSON / 行の増減 / substring の境界破り / pretty_host
filter / 全落とし / 不正・入れ子・再束縛・probe 衝突の宣言 / 秘密形だけで壊れる redact / JSON
object でない行・host が読めない url / 非 UTF-8 出力 / syntax error のアプリ) を流して検出を
pin し、正しい宣言アプリが緑になることも positive control として pin する (= この検査器自体が
機能していることの担保)。

副作用を残さない: 一時物は TemporaryDirectory に隔離し、subprocess には
PYTHONDONTWRITEBYTECODE=1 を渡して __pycache__ を残さない。
"""

from __future__ import annotations

import ast
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
from collections import Counter
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
sys.path.insert(0, str(ENGINE))
from flows_to_ndjson import host_in_domains  # noqa: E402  (probe 導出とアプリ実装で同じ matcher を使う)

SECRET_SHAPE = 1  # 秘密らしき値 (auth ヘッダ・body・set-cookie) を積んだ形
SHAPE_METHOD = {0: "GET", SECRET_SHAPE: "POST", 2: "DELETE"}  # 形 → method (出力の照合キー)
N_SHAPES = len(SHAPE_METHOD)

# SELFCHECK_DOMAINS の root として許す形 (小文字の dot 区切り)。probe host を機械合成するので絞る。
DOMAIN_RE = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+")

# plan の 1 要素: (実接続先 host, 形, 偽装 Host ヘッダ値 | None)
Plan = list[tuple[str, int, str | None]]


def synth_flow(host: str, shape: int, host_header: str | None = None):
    """形 shape の flow を 1 本作る。0: GET+resp / 1: POST(秘密らしきヘッダ+body)+resp /
    2: response 無し — to_dict の経路を広く通す 3 形。host_header を与えると実接続先 (host) と
    別の Host ヘッダを積む (偽装 flow — pretty_host はヘッダ側を返す)。"""
    hdr = [(b"host", host_header.encode())] if host_header else []
    if shape == 0:
        return tflow.tflow(
            req=tutils.treq(host=host, method=b"GET", path=b"/api/items?q=1", headers=hdr),
            resp=tutils.tresp(content=b'{"items":[1,2,3]}'),
        )
    if shape == SECRET_SHAPE:
        return tflow.tflow(
            req=tutils.treq(
                host=host,
                method=b"POST",
                path=b"/api/login",
                headers=hdr
                + [(b"authorization", b"Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.SIG_secret")],
                content=b'{"deviceToken":"id123:APA91bSECRET_fcm"}',
            ),
            resp=tutils.tresp(
                headers=[(b"set-cookie", b"session=TOPSECRET; Path=/")],
                content=b'{"ok":true}',
            ),
        )
    # response 無しの flow (to_dict が response 系キーを出さない経路)
    f = tflow.tflow(req=tutils.treq(host=host, method=b"DELETE", path=b"/api/items/9", headers=hdr))
    f.response = None
    return f


def synth_flows(plan: Plan) -> bytes:
    """plan をそのまま flows バイト列にする。どの host にどの形を流すかは plan が明示する
    (位置依存の巡回だと、宣言アプリで秘密形が受理 host に載らない並びが作れてしまう)。"""
    buf = io.BytesIO()
    w = mio.FlowWriter(buf)
    for host, shape, host_header in plan:
        w.add(synth_flow(host, shape, host_header))
    return buf.getvalue()


def default_plan() -> Plan:
    """宣言なしアプリ用: 1 形 1 flow (期待行数 = len(plan) — 定数を別に持たない)。"""
    return [("address", s, None) for s in range(N_SHAPES)]


def declared_domains(redact_path: Path) -> tuple[str, ...] | None:
    """module トップレベルの SELFCHECK_DOMAINS 宣言を実行せずに ast で読む。
    無ければ None。あるのに解釈できなければ ValueError (fail-closed — 黙って通常契約に
    落とすと、filter するアプリが「行数」の紛らわしい赤になる)。"""
    bindings: list[tuple[ast.stmt, bool]] = []  # (束縛文, 単独の Name 代入か)
    for node in ast.parse(redact_path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        else:
            continue
        plain = any(isinstance(t, ast.Name) and t.id == "SELFCHECK_DOMAINS" for t in targets)
        # 展開代入 (SELFCHECK_DOMAINS, x = ...) 等、target の内側に埋まった束縛も拾って拒否する
        # (見逃すと宣言が無い扱い → filter するアプリが紛らわしい「行数」の赤になる)。
        anywhere = any(
            isinstance(n, ast.Name) and n.id == "SELFCHECK_DOMAINS"
            for t in targets
            for n in ast.walk(t)
        )
        if anywhere:
            bindings.append((node, plain))
    if not bindings:
        return None
    # 実行時は最後の束縛が勝つが、検査が読めるのは静的な 1 つだけ。複数あると検査した宣言と
    # 実効 filter がずれ得る (fail-open) ので束縛は 1 回に限る。累積代入・値の無い注釈・
    # 展開代入も拒否する。
    if len(bindings) > 1:
        raise ValueError("SELFCHECK_DOMAINS がトップレベルで複数回束縛されている (1 回だけ代入する)")
    node, plain = bindings[0]
    if not plain:
        raise ValueError("SELFCHECK_DOMAINS が展開代入 (tuple unpacking 等) で束縛されている (単独の代入にする)")
    if isinstance(node, ast.AugAssign):
        raise ValueError("SELFCHECK_DOMAINS への累積代入 (+= 等) は静的に読めない")
    if isinstance(node, ast.AnnAssign) and node.value is None:
        raise ValueError("SELFCHECK_DOMAINS が注釈だけで値が無い")
    try:
        val = ast.literal_eval(node.value)
    except ValueError as e:
        raise ValueError(f"リテラルとして評価できない: {e}") from e
    # tuple 限定 (list 不可): list は宣言後に .append 等で実行時だけ広げられ、検査した宣言と
    # 実効 filter がずれる (fail-open)。tuple なら変異は AttributeError で必ず露呈する。
    if not isinstance(val, tuple) or not val or not all(
        isinstance(d, str) and DOMAIN_RE.fullmatch(d) for d in val
    ):
        raise ValueError(f"小文字 dot 区切りドメインの空でない tuple でない (list も不可 — 変異可能): {val!r}")
    domains = val
    # root 同士の重複・入れ子を拒否する。入れ子 (example.com と sub.example.com) だと棄却
    # probe notsub.example.com が親 root の受理域に正当に入り、正しい filter が常に赤になる。
    # 親 root の宣言だけで子 subtree は受理済み。
    for i, a in enumerate(domains):
        for b in domains[:i]:
            if a == b or a.endswith("." + b) or b.endswith("." + a):
                raise ValueError(f"root が重複または入れ子: {a!r} と {b!r} (親 root だけ宣言する)")
    return domains


def boundary_plan(domains: tuple[str, ...]) -> tuple[Plan, list[tuple[str, str]]]:
    """宣言された root から (流す flow の plan, 受理されるべき (host, method) 列) を導く。
    受理 host (root 自身と probe.<root>) には全形を流す — 秘密形が受理側の redact/serialize
    経路を必ず通る。棄却側は秘密形で流す — 秘密を積んだ flow が確実に落ちる方向で検査する:
    not<root> (dot 境界無視の suffix マッチが拾う)、<root>.probe.invalid (前方 substring
    マッチが拾う)、無関係 host、そして実接続先が域外で Host ヘッダだけ受理 host を騙る偽装
    flow (pretty_host で filter する実装が拾う)。
    棄却 probe の host が別の宣言 root の受理域に入る組 (例: "evil.example" と
    "notevil.example") は正しい filter が偽赤になるため宣言エラーで拒否する。"""
    plan: Plan = []
    accept: list[tuple[str, str]] = []
    rejects: list[str] = []
    for d in domains:
        for h in (f"probe.{d}", d):
            for s in range(N_SHAPES):
                plan.append((h, s, None))
                accept.append((h, SHAPE_METHOD[s]))
        for h in (f"not{d}", f"{d}.probe.invalid"):
            plan.append((h, SECRET_SHAPE, None))
            rejects.append(h)
        plan.append(("forged-host.probe.invalid", SECRET_SHAPE, f"probe.{d}"))
        rejects.append("forged-host.probe.invalid")
    plan.append(("unrelated.probe.invalid", SECRET_SHAPE, None))
    rejects.append("unrelated.probe.invalid")
    # 棄却の「意図」で流す host が受理域に入っていないことを、accept 集合との重複を含めて検証する
    # (例: ("evil.example", "notevil.example") では not<root> probe が他方の root そのもの)。
    for h in rejects:
        if host_in_domains(h, domains):
            raise ValueError(f"棄却 probe の host {h!r} が宣言 root の受理域に入る (この root の組は検査不能)")
    return plan, accept


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


def check_contract(redact_path: Path, plan: Plan, accept: list[tuple[str, str]] | None = None) -> list[str]:
    """エンジン契約違反のリストを返す (空なら OK)。accept が None なら「行数 = 入力 flow 数」、
    宣言アプリなら「受理 flow を全部・それだけ出力し、URL の host と method で照合できる」。
    アプリ出力は敵対的でもここでは例外を出さない (per-line/per-app のエラーに封じ込める)。"""
    errs: list[str] = []
    p = run_redact(redact_path, synth_flows(plan))
    if p.returncode != 0:
        errs.append(f"exit={p.returncode}: {p.stderr.decode(errors='replace').strip()[:400]}")
        return errs
    lines = p.stdout.decode(errors="replace").splitlines()  # 非 UTF-8 は置換され JSON 検査で赤になる
    if accept is None and len(lines) != len(plan):
        errs.append(f"行数 {len(lines)} != flow 数 {len(plan)}")
    out_pairs: list[tuple[str, str]] = []
    for i, ln in enumerate(lines):
        try:
            d = json.loads(ln)
        except json.JSONDecodeError as e:
            errs.append(f"行 {i} が不正 JSON: {e}")
            continue
        if accept is not None:
            url = d.get("url") if isinstance(d, dict) else None
            host = None
            if isinstance(url, str):
                try:
                    host = urllib.parse.urlsplit(url).hostname
                except ValueError:  # 例: host を "[REDACTED]" に書き換えた url は urlsplit が raise する
                    host = None
            if not host:
                errs.append(f"行 {i} の url から host を読めない (宣言アプリは 1 flow を 1 つの JSON object にし url の host を残す契約)")
            else:
                method = d.get("method")
                out_pairs.append((host, method if isinstance(method, str) else "?"))
    if accept is not None:
        # (host, method) 単位の Counter で照合する。host 単位の set だと「1 flow を取りこぼし
        # 別の flow を重複出力」や「同一 host 内で形を差し替え」が相殺で緑になる。
        want = Counter(accept)
        got = Counter(out_pairs)
        want_hosts = {h for h, _ in want}
        got_hosts = {h for h, _ in got}
        for h in sorted(want_hosts - got_hosts):
            errs.append(f"受理されるべき host が出力に無い: {h}")
        for h in sorted(got_hosts - want_hosts):
            errs.append(f"落とすべき host が出力に残っている: {h}")
        for key in sorted(set(want) | set(got)):
            h, m = key
            if h in want_hosts and h in got_hosts and got[key] != want[key]:
                errs.append(f"host {h} の {m} flow 行数 {got[key]} != {want[key]} (取りこぼし/重複/形の差し替え)")
    return errs


def check_app(redact_path: Path) -> list[str]:
    """宣言の有無で契約を選んで検査する (アプリ本体と negative probe の共通入口)。"""
    try:
        domains = declared_domains(redact_path)
        plan_accept = boundary_plan(domains) if domains is not None else None
    except ValueError as e:
        return [f"SELFCHECK_DOMAINS 宣言が不正: {e}"]
    except Exception as e:  # SyntaxError (ast.parse) や読取り失敗もこのアプリの NG に封じ込める
        # (checker ごと落とすと残りのアプリと negative probe が未検査のまま終わる)
        return [f"redact_flow を解析できない: {type(e).__name__}: {e}"]
    if plan_accept is None:
        return check_contract(redact_path, default_plan())
    plan, accept = plan_accept
    return check_contract(redact_path, plan, accept=accept)


def write_temp_redact(d: Path, body: str, run: str = "flows_to_ndjson(redact)") -> Path:
    """core/redact/flows_to_ndjson を使う一時 redact_flow.py を作る (negative probe 用)。"""
    d.mkdir(parents=True, exist_ok=True)
    src = (
        "import sys\n"
        f"sys.path.insert(0, {str(ENGINE)!r})\n"
        "from flows_to_ndjson import flows_to_ndjson\n"
        f"{body}\n"
        'if __name__ == "__main__":\n'
        f"    {run}\n"
    )
    p = d / "redact_flow.py"
    p.write_text(src)
    return p


DECL = 'SELFCHECK_DOMAINS = ("probe-kept.example",)\n'
KEEP_RUN = "flows_to_ndjson(redact, keep_domains=SELFCHECK_DOMAINS)"


def filter_app_body(keep_expr: str) -> str:
    """engine を使わず host filter を自作したアプリの最小形 (境界や host の取り方を誤る
    negative probe 用。正しいアプリは engine の iter_flow_dicts / keep_domains= を使う)。
    keep_expr は pretty_host 文字列 h に対する式。"""
    return (
        DECL
        + "import json\n"
        "from mitmproxy import http, io\n"
        "from flows_to_ndjson import to_dict\n"
        "def filter_main():\n"
        "    for f in io.FlowReader(sys.stdin.buffer).stream():\n"
        "        if not isinstance(f, http.HTTPFlow):\n"
        "            continue\n"
        "        h = f.request.pretty_host\n"
        f"        if {keep_expr}:\n"
        "            print(json.dumps(to_dict(f), ensure_ascii=False))\n"
    )


def main() -> int:
    failed = False

    # 1) 本検査: 各 extract/*/redact_flow.py がエンジン契約を満たす。check_app 内で封じ切れない
    #    想定外の例外も per-app NG に落とす backstop を敷く (1 アプリの異常で checker ごと死ぬと
    #    残りのアプリと negative probe が「未検査のまま緑に見える」最悪の形になるため)。
    apps = sorted((REPO / "extract").glob("*/redact_flow.py"))
    if not apps:
        print("note: extract/*/redact_flow.py が無い (検査対象なし)")
    for rp in apps:
        try:
            errs = check_app(rp)
        except Exception as e:
            errs = [f"checker 内部例外: {type(e).__name__}: {e}"]
        rel = rp.relative_to(REPO)
        if errs:
            failed = True
            print(f"NG  {rel}", file=sys.stderr)
            for e in errs:
                print(f"      {e}", file=sys.stderr)
        else:
            print(f"ok  {rel}")

    # 2) negative probe: 検査器が契約違反を実際に検出することを pin する。
    #    全 probe をアプリ本体と同じ入口 check_app に通す (宣言の解釈から契約選択までを含めて検査)。
    def probe(name: str, expect_err: str | None, body: str, run: str = "flows_to_ndjson(redact)") -> None:
        nonlocal failed
        rp = write_temp_redact(probe_root / name, body, run=run)
        errs = check_app(rp)
        if expect_err is None:
            if errs:
                print(f"positive probe {name}: 契約を満たすはずが違反になった: {errs}", file=sys.stderr)
                failed = True
        elif not any(expect_err in e for e in errs):
            print(f"negative probe {name}: 期待した検出 '{expect_err}' が出ない (errs={errs})", file=sys.stderr)
            failed = True

    with tempfile.TemporaryDirectory() as td:
        probe_root = Path(td)
        # 不正 JSON を出す redact
        probe("bad_json", "不正 JSON", "def redact(s):\n    return 'NOT_JSON'\n")
        # 改行を混ぜて行を増やす redact
        probe("extra_line", "行数", 'def redact(s):\n    return s + "\\nINJECTED"\n')
        # positive control: engine の keep_domains= を宣言と同じ tuple で使う正しいアプリ
        # (engine matcher の dot 境界や host の取り方が壊れたらここが赤になる)
        probe("good_filter", None, DECL + "def redact(s):\n    return s\n", run=KEEP_RUN)
        # dot 境界を破る substring filter (自作 __main__)
        probe("substr_filter", "落とすべき host", filter_app_body('"probe-kept.example" in h'), run="filter_main()")
        # dot 境界は正しいが pretty_host (Host ヘッダ優先 = 偽装可能) で filter する自作 __main__
        # → 偽装 flow を通してしまい受理 host の POST が 1 本多くなるはず
        probe(
            "pretty_host_filter",
            "flow 行数",
            filter_app_body('h == "probe-kept.example" or h.endswith(".probe-kept.example")'),
            run="filter_main()",
        )
        # 宣言したのに全部落とす filter
        probe("drop_all", "受理されるべき host", filter_app_body("False"), run="filter_main()")
        # 秘密形 (auth ヘッダ) の flow だけで壊れる redact — 受理 host に秘密形が載らない検査だと素通しになる
        probe(
            "secret_shape_crash",
            "exit=",
            DECL + 'def redact(s):\n    if "authorization" in s:\n        raise RuntimeError("boom")\n    return s\n',
            run=KEEP_RUN,
        )
        # JSON object でない行 (配列) を出す宣言アプリ — host 照合できない契約違反
        probe(
            "array_line",
            "host を読めない",
            DECL + "import json\ndef redact(s):\n    return json.dumps([s])\n",
            run=KEEP_RUN,
        )
        # url の host を括弧付き placeholder に書き換える宣言アプリ (urlsplit が raise する形)
        probe(
            "bracket_host",
            "host を読めない",
            DECL + 'import json\ndef redact(s):\n    d = json.loads(s)\n    d["url"] = "http://[REDACTED]/x"\n    return json.dumps(d)\n',
            run=KEEP_RUN,
        )
        # url を文字列以外に書き換える宣言アプリ (str 前提で urlsplit すると checker が落ちる形)
        probe(
            "int_url",
            "host を読めない",
            DECL + 'import json\ndef redact(s):\n    d = json.loads(s)\n    d["url"] = 123\n    return json.dumps(d)\n',
            run=KEEP_RUN,
        )
        # 非 UTF-8 バイト列を stdout に吐くアプリ → decode で checker が落ちず JSON 検査で赤になるはず
        probe(
            "binary_stdout",
            "不正 JSON",
            DECL + 'def bad_main():\n    sys.stdout.buffer.write(b"\\xff\\xfe\\n")\n',
            run="bad_main()",
        )
        # 不正な SELFCHECK_DOMAINS 宣言 (str) → 紛らわしい "行数" でなく宣言エラーになるはず
        probe("bad_decl", "宣言が不正", 'SELFCHECK_DOMAINS = "probe-kept.example"\ndef redact(s):\n    return s\n')
        # root の入れ子宣言 → 棄却 probe が親の受理域に入る構成なので宣言段階で拒否するはず
        probe(
            "nested_decl",
            "宣言が不正",
            'SELFCHECK_DOMAINS = ("probe-kept.example", "sub.probe-kept.example")\ndef redact(s):\n    return s\n',
        )
        # 宣言の再束縛 (実行時は 2 つ目が勝つのに検査は 1 つしか読めない) → 拒否するはず
        probe(
            "rebind_decl",
            "宣言が不正",
            DECL + 'SELFCHECK_DOMAINS = ("probe-kept.example", "evil.example")\ndef redact(s):\n    return s\n',
        )
        # 棄却 probe (notevil.example) が別の宣言 root と衝突する組 → 偽赤でなく宣言エラーになるはず
        probe(
            "probe_collision_decl",
            "宣言が不正",
            'SELFCHECK_DOMAINS = ("evil.example", "notevil.example")\ndef redact(s):\n    return s\n',
        )
        # list での宣言 → .append 等の実行時変異で宣言とずれ得るので tuple 限定を pin
        probe("list_decl", "宣言が不正", 'SELFCHECK_DOMAINS = ["probe-kept.example"]\ndef redact(s):\n    return s\n')
        # 展開代入での束縛 → 宣言が無い扱い (紛らわしい行数の赤) でなく宣言エラーになるはず
        probe(
            "unpack_decl",
            "宣言が不正",
            'SELFCHECK_DOMAINS, _OTHER = ("probe-kept.example",), 1\ndef redact(s):\n    return s\n',
        )
        # syntax error のアプリ → checker ごと落ちず、このアプリの NG に封じ込めるはず
        probe("broken_syntax", "解析できない", "def redact(s:\n")

    if failed:
        print("redact selfcheck FAILED", file=sys.stderr)
        return 1
    print("ok  redact selfcheck (engine contract + negative probe)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
