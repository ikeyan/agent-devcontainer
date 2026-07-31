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

宣言があるアプリには「受理ホストの flow N 本 + 落とされるべき数本」を流し、出力が
ちょうど N 行であることを要求する。これで
  - 取りこぼし / 増加の検出力 (== 判定) を緩めずに済む
  - filter そのものが検査対象になる (宣言だけして落としていなければ N+1 行で落ちる)
宣言が無いアプリは従来どおり、既定ホストの flow N 本に対して N 行を要求する。

行数だけでは足りない: 「受理分を 1 本落として非対象を 1 本通す」filter (host でなく
method で誤判定した等) は差引きで N 行に収まってしまい、非対象の秘密が出力に載ったまま
素通りする。そこで行数に加えて**どの flow が出たか**を照合する。

同一性は **HTTP method** で見る。URL や本文は「値を伏せる」正当な redact に書き換えられうる
(数値や query を <num> にする等) ので印にできない — それを印にすると、正しいアプリが CI で
落ちる。method は enum で秘密を持たないため redact 対象にならない。合成 flow は method が
互いに重ならないようにしてあり、非対象 flow だけが PATCH を持つ。

宣言された受理ホストは**全て**検査する。先頭だけを試すと、host[0] は通すが他を落とす
filter を「成功」と report してしまう (部分成功を成功にしない)。

落とすべき flow は、無関係な sentinel だけでなく**宣言ホストから導いた near-miss** も混ぜる。
sentinel を 1 本落とせるだけでは allowlist の境界の誤りを検出できない — たとえば
`host.endswith("anthropic.com")` は `api.anthropic.com` を正しく受理しつつ `apianthropic.com`
まで受理してしまい、第三者の資格情報が出力に載る。導出は near_miss_hosts() を参照。

宣言は `ast` で静的に読む (import も実行もしない) ので、検査器が consumer のコードを
自プロセスに取り込むことはない。

negative probe: 「不正 JSON を出す redact」「改行を混ぜて行を増やす redact」「受理ホストを
宣言しているのに落とさない redact」「受理分を落として非対象を通す filter (行数は一致)」
「宣言ホストの一部しか受理しない filter」「DNS ラベル境界を見ない filter (endswith)」
「型注記つき宣言 (AnnAssign) を読めること」を流し、検査器がそれぞれを検出/処理することを
pin する (= この検査器自体が機能していることの担保)。

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
# flow の同一性は HTTP method で見る。URL や本文は「値を伏せる」正当な redact に書き換え
# られうる (数値や query を <num> にする等) ので印にできないが、method は enum で秘密を
# 持たないため redact 対象にならない。合成 flow は method が互いに重ならないようにしてある。
ACCEPT_METHODS = ("GET", "POST", "DELETE")
REJECT_METHOD = "PATCH"
# 非対象 flow の取りこぼしを method 以外からも拾う保険 (これらが消されても method で捕まる)。
REJECT_MARKERS = (REJECTED_HOST, "/should-be-dropped", "THIRD_PARTY_SECRET")


def near_miss_hosts(host: str) -> tuple[str, ...]:
    """宣言ホストから「allowlist の境界を間違えた実装だけが受理する」ホストを導く。

    無関係な sentinel (REJECTED_HOST) を 1 本落とせるだけでは、境界の誤りは検出できない。
    たとえば `host.endswith("anthropic.com")` は `api.anthropic.com` を正しく受理しつつ
    `apianthropic.com` も受理してしまい、第三者の資格情報が出力に載る。
    """
    out = []
    if "." in host:
        # 左境界 (DNS ラベルの区切り) を見ない実装が誤って受理する: api.anthropic.com -> apianthropic.com
        head, _, rest = host.partition(".")
        if head and rest:
            out.append(f"{head}{rest}")
    # 右端を anchor しない実装 (部分一致) が誤って受理する: <host>.selfcheck-rejected.invalid
    out.append(f"{host}.{REJECTED_HOST}")
    return tuple(out)


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
        # `X = ...` と `X: tuple[str, ...] = ...` (AnnAssign) の両方を拾う。型注記付きで書かれた
        # 宣言を黙って見落とすと、正しく filter するアプリが「行数 0」で落ちる。
        if isinstance(node, ast.Assign):
            targets, value_node = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value_node = [node.target], node.value
        else:
            continue
        if not any(isinstance(t, ast.Name) and t.id == "SELFCHECK_HOSTS" for t in targets):
            continue
        if value_node is None:  # 注記のみ (`X: tuple[str, ...]`) は宣言ではない
            return ()
        try:
            value = ast.literal_eval(value_node)
        except ValueError:
            return ()
        if isinstance(value, str):
            value = (value,)
        if isinstance(value, (list, tuple)) and value and all(isinstance(v, str) and v for v in value):
            return tuple(value)
        return ()
    return ()


def synth_flows(host: str, *, reject_hosts: tuple[str, ...] = ()) -> bytes:
    """秘密らしき値を含む複数の HTTP flow を 1 つの flows バイト列にする。
    GET / POST(ヘッダ+body) / response 無し をそれぞれ 1 本ずつ含め、to_dict の経路を広く通す。
    全て `host` 宛。`reject_hosts` の各ホストについて「落とされるべき」flow を 1 本ずつ足す。"""
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

    for i, rh in enumerate(reject_hosts):
        flows.append(
            tflow.tflow(
                req=tutils.treq(
                    host=rh,
                    method=REJECT_METHOD.encode(),
                    path=f"/should-be-dropped/{i}".encode(),
                ),
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


def _check_one(redact_path: Path, host: str, *, reject_hosts: tuple[str, ...] = ()) -> list[str]:
    """1 つの受理ホストについて契約を検査する。reject_hosts の flow は全て落ちねばならない。"""
    errs: list[str] = []
    flows = synth_flows(host, reject_hosts=reject_hosts)
    p = run_redact(redact_path, flows)
    if p.returncode != 0:
        return [f"exit={p.returncode}: {p.stderr.decode(errors='replace').strip()[:400]}"]

    out = p.stdout.decode()
    lines = out.splitlines()
    methods: list[str] = []
    for i, ln in enumerate(lines):
        try:
            methods.append(json.loads(ln).get("method"))
        except json.JSONDecodeError as e:
            errs.append(f"行 {i} が不正 JSON: {e}")

    if len(lines) != N_FLOWS:
        if reject_hosts and not lines:
            errs.append(f"行数 0 — 宣言された受理ホスト ({host}) を filter が受理していない")
        else:
            errs.append(f"行数 {len(lines)} != 受理されるべき flow 数 {N_FLOWS}")

    # 行数だけでは「受理分を 1 本落として非対象を 1 本通す」誤った filter が素通りする
    # (差引きが合ってしまう)。どの flow が出たかを method で照合する。
    missing = [m for m in ACCEPT_METHODS if m not in methods]
    if missing:
        errs.append(f"受理されるべき flow が出力に無い (method: {', '.join(missing)})")
    if reject_hosts:
        leaked_hosts = [h for h in reject_hosts if h in out]
        leaked_marks = [m for m in REJECT_MARKERS if m in out]
        if REJECT_METHOD in methods or leaked_hosts or leaked_marks:
            which = leaked_hosts or list(reject_hosts)
            found = f"method {REJECT_METHOD}" if REJECT_METHOD in methods else repr((leaked_marks or which)[0])
            errs.append(
                f"非対象ホスト ({', '.join(which)}) の flow を落としていない — 出力に {found} が残っている"
            )
    return errs


def check_contract(redact_path: Path) -> list[str]:
    """エンジン契約違反のリストを返す (空なら OK)。

    受理ホストを宣言しているアプリは**宣言された全ホスト**について検査する (一部だけ通る
    filter を成功と report しないため)。各回、非対象ホストの flow を 1 本混ぜ、それが落ちた
    上で受理分がちょうど N_FLOWS 行出ることを要求する。"""
    hosts = read_selfcheck_hosts(redact_path)
    if not hosts:
        return _check_one(redact_path, DEFAULT_HOST)
    errs: list[str] = []
    for host in hosts:
        # 無関係な sentinel に加えて、この host の境界を間違えた実装だけが受理するホストを混ぜる。
        rejects = (REJECTED_HOST, *near_miss_hosts(host))
        errs += [f"[{host}] {e}" for e in _check_one(redact_path, host, reject_hosts=rejects)]
    return errs


def write_temp_app(d: Path, body: str) -> Path:
    """任意の本体を持つ一時 redact_flow.py を書く (negative probe 用)。ENGINE を import path に通す。"""
    d.mkdir(parents=True, exist_ok=True)
    p = d / "redact_flow.py"
    p.write_text(f"import sys\nsys.path.insert(0, {str(ENGINE)!r})\n{body}")
    return p


def write_temp_redact(d: Path, redact_body: str, *, declare_hosts: tuple[str, ...] = ()) -> Path:
    """core/redact/flows_to_ndjson を使う一時 redact_flow.py を作る (negative probe 用)。
    declare_hosts を渡すと SELFCHECK_HOSTS を宣言するが filter は実装しない
    (= 宣言と実装の食い違いを検査器が捕まえるかの probe になる)。"""
    decl = f"SELFCHECK_HOSTS = {tuple(declare_hosts)!r}\n" if declare_hosts else ""
    return write_temp_app(
        d,
        "from flows_to_ndjson import flows_to_ndjson\n"
        f"{decl}"
        f"{redact_body}\n"
        'if __name__ == "__main__":\n'
        "    flows_to_ndjson(redact)\n",
    )


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
            note = (
                f" (SELFCHECK_HOSTS={' '.join(hosts)} / 各ホストで sentinel + near-miss の drop も確認)"
                if hosts
                else ""
            )
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
        # (d) 受理分を 1 本落として非対象を 1 本通す filter (host でなく method で誤判定した等)。
        #     差引きで行数は N のまま合ってしまうので、同一性の検査だけが捕まえられる。
        rp = write_temp_app(
            root / "wrong_filter",
            "import json\n"
            "from flows_to_ndjson import to_dict\n"
            "from mitmproxy import http, io\n"
            'SELFCHECK_HOSTS = ("api.example.com",)\n'
            'if __name__ == "__main__":\n'
            "    with sys.stdin.buffer as fp:\n"
            "        for f in io.FlowReader(fp).stream():\n"
            "            if isinstance(f, http.HTTPFlow) and f.request.method != 'DELETE':\n"
            "                print(json.dumps(to_dict(f), ensure_ascii=False))\n",
        )
        errs = check_contract(rp)
        if not (any("落としていない" in e for e in errs) and any("出力に無い" in e for e in errs)):
            print(
                "negative: 受理分を落として非対象を通す filter (行数は一致) を検出できなかった",
                file=sys.stderr,
            )
            failed = True
        # (e) 宣言した受理ホストのうち先頭しか受理しない filter。宣言の一部だけ通るのを
        #     成功と report しないことを pin する。
        rp = write_temp_app(
            root / "partial_hosts",
            "import json\n"
            "from flows_to_ndjson import to_dict\n"
            "from mitmproxy import http, io\n"
            'SELFCHECK_HOSTS = ("a.example.com", "b.example.com")\n'
            'if __name__ == "__main__":\n'
            "    with sys.stdin.buffer as fp:\n"
            "        for f in io.FlowReader(fp).stream():\n"
            '            if isinstance(f, http.HTTPFlow) and f.request.pretty_host == "a.example.com":\n'
            "                print(json.dumps(to_dict(f), ensure_ascii=False))\n",
        )
        if not any(e.startswith("[b.example.com]") for e in check_contract(rp)):
            print(
                "negative: 宣言した受理ホストの一部しか受理しない filter を検出できなかった",
                file=sys.stderr,
            )
            failed = True
        # (f) DNS ラベル境界を見ない filter (endswith)。宣言ホストは正しく受理するので、
        #     宣言ホストから導いた near-miss を混ぜないと検出できない。
        rp = write_temp_app(
            root / "boundary",
            "import json\n"
            "from flows_to_ndjson import to_dict\n"
            "from mitmproxy import http, io\n"
            'SELFCHECK_HOSTS = ("api.anthropic.com",)\n'
            'if __name__ == "__main__":\n'
            "    with sys.stdin.buffer as fp:\n"
            "        for f in io.FlowReader(fp).stream():\n"
            '            if isinstance(f, http.HTTPFlow) and f.request.pretty_host.endswith("anthropic.com"):\n'
            "                print(json.dumps(to_dict(f), ensure_ascii=False))\n",
        )
        if not any("落としていない" in e for e in check_contract(rp)):
            print("negative: ラベル境界を見ない filter を検出できなかった", file=sys.stderr)
            failed = True
        # (g) 型注記つきの宣言 (AnnAssign) を読めること。読み落とすと、正しく filter する
        #     アプリが既定ホストで検査されて「行数 0」で落ちる。
        rp = write_temp_app(
            root / "annassign",
            "import json\n"
            "from flows_to_ndjson import to_dict\n"
            "from mitmproxy import http, io\n"
            'SELFCHECK_HOSTS: tuple[str, ...] = ("api.example.com",)\n'
            'if __name__ == "__main__":\n'
            "    with sys.stdin.buffer as fp:\n"
            "        for f in io.FlowReader(fp).stream():\n"
            '            if isinstance(f, http.HTTPFlow) and f.request.pretty_host == "api.example.com":\n'
            "                print(json.dumps(to_dict(f), ensure_ascii=False))\n",
        )
        errs = check_contract(rp)
        if errs:
            print(f"negative: 型注記つき宣言を読めていない ({errs[0]})", file=sys.stderr)
            failed = True

    if failed:
        print("redact selfcheck FAILED", file=sys.stderr)
        return 1
    print("ok  redact selfcheck (engine contract + negative probe)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
