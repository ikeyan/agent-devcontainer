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

## ホストで flow を落とすアプリ (SELFCHECK_DOMAINS)

アプリによっては、対象ドメイン以外の flow を行ごと落とす。これは整理でなく防御である
ことが多い — ヘッダの redact は「知っている値だけ伏せる」denylist になりがちで、対象
ドメインの外を通すと未知の auth ヘッダが素通りする。したがって「行数 = 入力 flow 数」を
全アプリに強制して filter を消させるのは本末転倒になる。

そこでアプリ側に**受理する部分木の root ドメイン**を宣言させ、検査器がそれを使って
合成 flow を組み立てる:

    SELFCHECK_DOMAINS = ("anthropic.com", "claude.com")   # module 直下・リテラルで書く

宣言の意味は「この root 自身と、その subdomain を受理する」。代表ホスト (api.anthropic.com)
ではなく root を書くのが要点で、root が分かっていれば「どんな正しい allowlist でも必ず
落とすホスト」を公開接尾辞の知識なしに作れる (下記)。

宣言があるアプリには「受理ホストの flow N 本 + 落とされるべき数本」を流し、出力が
ちょうど N 行であることを要求する。これで
  - 取りこぼし / 増加の検出力 (== 判定) を緩めずに済む
  - filter そのものが検査対象になる (宣言だけして落としていなければ N+1 行で落ちる)
宣言が無いアプリは従来どおり、既定ホストの flow N 本に対して N 行を要求する。

受理側と非受理側は**別の実行に分ける** (同じ flows に混ぜない):
  - 受理: 宣言 root / 子 / 孫の各ホストで 3 本流し、3 行出ること
  - 非受理: sentinel と近傍を流し、0 行であること
分けると、差引きで辻褄の合う誤り (受理分を 1 本落として非対象を 1 本通す) が原理的に
隠せなくなる。混ぜて行数だけ見ていた頃はこれが素通りしていた。

分けたことで**出力に印を仕込む必要も無くなった** — 非受理側は「0 行であること」自体が
判定なので、redact に書き換えられうる URL や本文を手掛かりにしなくてよい。合成 flow の
method も受理側と非受理側で同じにしてある。非受理側だけ別の method にすると、host を一切
見ずにその method を落とすだけの filter が契約を満たしてしまい、第三者宛の GET/POST が
素通りする。**host 以外の手掛かりを残さない**のが要点。

宣言された受理ホストは**全て**検査する。先頭だけを試すと、host[0] は通すが他を落とす
filter を「成功」と report してしまう (部分成功を成功にしない)。

落とすべき flow は、無関係な sentinel だけでなく**宣言 root から導いた near-miss** も混ぜる。
sentinel を 1 本落とせるだけでは allowlist の境界の誤りを検出できない — たとえば
`host.endswith("anthropic.com")` は宣言 root を正しく受理しつつ `notanthropic.com` まで
受理してしまい、第三者の資格情報が出力に載る。

導出が root を要求する理由: 代表ホストからでは「登録可能ドメインの境界がどこか」が決まらない。
`api.anthropic.com` からラベル位置で近傍を作ろうとすると、`example.co.uk` のような複数ラベルの
公開接尾辞で破綻する (登録可能ラベルは labels[-2] ではない)。公開接尾辞リストを持ち込むのは
データ依存が増えるだけなので、代わりに root を宣言させて `not<root>` を作る — これは
`anthropic.com` でも `example.co.uk` でも一様に「別の登録可能ドメイン」になる。

宣言は `ast` で静的に読む (import も実行もしない) ので、検査器が consumer のコードを
自プロセスに取り込むことはない。

negative probe: 不正 JSON / 行の増加 / 宣言だけして落とさない / 受理分を落として非対象を
通す / 宣言 root の一部しか受理しない / ラベル境界を見ない (endswith、複数ラベル公開接尾辞
を含む) / 完全一致でしか通さない / 孫を落とす / host でなく method で落とすだけ / 宣言が
リテラルでない / 宣言の形が違う / 型注記つき宣言と注記と代入の分離を読めること — を流し、
検査器がそれぞれを検出/処理することを pin する (= この検査器自体が機能していることの担保)。

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
# SELFCHECK_DOMAINS を宣言したアプリが必ず落とすべきホスト。`.invalid` は RFC 2606 の予約 TLD
# なので、正当な allowlist がこれを含むことはない。
REJECTED_HOST = "selfcheck-rejected.invalid"
# 受理側と非受理側で**同じ method** を使う。非受理側だけ別 method (PATCH 等) にすると、
# host を一切見ずにその method を落とすだけの filter が契約を満たしてしまい、第三者宛の
# GET/POST が素通りする。host 以外に手掛かりを残さないのが要点。


# 宣言 root の subdomain が受理されることを確かめるためのラベル。
SUB_LABEL = "selfcheck-sub"
# アプリ側が使う宣言名。テンプレートと検査器で綴りがずれると、宣言したつもりのアプリが
# 「宣言なし」扱いになって既定ホストで検査され、行数 0 で落ちる (原因が読み取りにくい)。
# 単一の出所にして、テンプレートとの一致を negative probe で pin する。
DECL_NAME = "SELFCHECK_DOMAINS"
TEMPLATE = ENGINE / "redact_flow.template.py"


def accepted_hosts(domain: str) -> tuple[str, ...]:
    """宣言 root から「受理されねばならない」ホストを導く。

    宣言の意味が「この root 自身とその subdomain 全部」なので、root / 1 段下 / 2 段下を試す。
    root だけだと完全一致でしか通さない filter を、1 段だけだと「直下の子は通すが孫は落とす」
    filter を見逃す (宣言は部分木全体を約束している)。
    """
    return (domain, f"{SUB_LABEL}.{domain}", f"{SUB_LABEL}.{SUB_LABEL}.{domain}")


class DeclarationError(Exception):
    """DECL_NAME の宣言はあるが読めない (リテラルでない / 形が違う / 空)。

    「宣言なし」と同じ扱い (空 tuple) にすると fail-open になる — 検査器は既定ホストの
    flow しか流さなくなり、非対象ホストを落とせるかの検査が丸ごと無効化されるのに緑になる。
    宣言を書こうとして失敗している以上、黙って検査を降格せず明示的に落とす。
    """


def in_subtree(host: str, domain: str) -> bool:
    """host が domain 自身か、その subdomain か。"""
    return host == domain or host.endswith(f".{domain}")


def near_miss_hosts(domain: str) -> tuple[str, ...]:
    """宣言 root から「どんな正しい allowlist でも必ず落とす」近傍ホストを導く。

    root が分かっているので公開接尾辞の知識は要らない。`not<root>` は root が
    anthropic.com でも example.co.uk でも一様に「別の登録可能ドメイン」になる。
    """
    return (
        # 左境界を見ない実装 (endswith) が誤って受理する: anthropic.com -> notanthropic.com
        f"not{domain}",
        # 右端を anchor しない実装 (部分一致) が誤って受理する
        f"{domain}.{REJECTED_HOST}",
    )


def read_selfcheck_domains(path: Path) -> tuple[str, ...]:
    """redact_flow.py が module 直下で宣言する SELFCHECK_DOMAINS を静的に読む。

    import も実行もしない (検査器のプロセスに consumer のコードを持ち込まないため)。

    宣言が**無い**場合だけ空 tuple を返す (= このアプリは flow を落とさない)。宣言はあるが
    読めない場合は DeclarationError — 空 tuple に丸めると非対象ホストの検査が黙って
    無効化される (fail-open)。
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return ()  # 実行時にも同じ理由で落ちるので、そちらのエラーで報告される

    # module 直下の代入を**全て**集める。最初の 1 つで打ち切ると、後の代入で上書きされる
    # モジュールで実行時と違う値を読んでしまい、宣言したはずの root が未検査のまま緑になる。
    assigns = []
    for node in tree.body:
        if isinstance(node, ast.AugAssign):
            if isinstance(node.target, ast.Name) and node.target.id == DECL_NAME:
                raise DeclarationError(
                    f"{DECL_NAME} を += で組み立てないこと — 静的に読めない"
                )
            continue
        if isinstance(node, ast.Assign):
            targets, value_node = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            # 注記のみ (`X: tuple[str, ...]`) は宣言ではないので飛ばし、後続の代入を探す。
            if node.value is None:
                continue
            targets, value_node = [node.target], node.value
        else:
            continue
        if any(isinstance(t, ast.Name) and t.id == DECL_NAME for t in targets):
            assigns.append(value_node)

    if not assigns:
        return ()
    if len(assigns) > 1:
        raise DeclarationError(
            f"{DECL_NAME} が module 直下で {len(assigns)} 回代入されている"
            " — 実行時は最後の値になるが静的には曖昧なので 1 箇所にまとめること"
        )
    try:
        value = ast.literal_eval(assigns[0])
    except (ValueError, TypeError, SyntaxError) as e:
        raise DeclarationError(
            f"{DECL_NAME} がリテラルでないため静的に読めない"
            " — 検査器は import せず ast で読むので、module 直下にリテラルで書くこと"
        ) from e
    if isinstance(value, str):
        value = (value,)
    if not (
        isinstance(value, (list, tuple))
        and value
        and all(isinstance(v, str) and v for v in value)
    ):
        raise DeclarationError(
            f"{DECL_NAME} は空でない文字列の tuple で書くこと (実際: {value!r})"
        )
    return tuple(value)


def synth_flows(hosts: tuple[str, ...]) -> bytes:
    """秘密らしき値を含む HTTP flow を 1 つの flows バイト列にする。
    GET / POST(ヘッダ+body) / response 無し を各ホストにつき 1 本ずつ含め、to_dict の経路を
    広く通す。ホストごとに同じ 3 本を作るので、受理側と非受理側で method の差は無い。"""
    flows = []
    for host in hosts:
        flows += _flows_for(host)

    buf = io.BytesIO()
    w = mio.FlowWriter(buf)
    for f in flows:
        w.add(f)
    return buf.getvalue()


def _flows_for(host: str) -> list:
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
    return flows


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


def _run_lines(redact_path: Path, flows: bytes) -> tuple[list[str] | None, list[str]]:
    """redact を走らせ (出力行, エラー) を返す。実行自体が失敗したら行は None。

    行数の判定と JSON の判定は独立に report する — 不正 JSON があるときに行数の判定を
    抑えると、改行を混ぜて行を増やす redact (両方に違反する) を行数側で捕まえられない。"""
    p = run_redact(redact_path, flows)
    if p.returncode != 0:
        return None, [f"exit={p.returncode}: {p.stderr.decode(errors='replace').strip()[:400]}"]
    lines = p.stdout.decode().splitlines()
    errs = []
    for i, ln in enumerate(lines):
        try:
            json.loads(ln)
        except json.JSONDecodeError as e:
            errs.append(f"行 {i} が不正 JSON: {e}")
    return lines, errs


def _check_accepted(redact_path: Path, host: str) -> list[str]:
    """受理されるべきホストの flow が 1 本も落ちないこと。"""
    lines, errs = _run_lines(redact_path, synth_flows((host,)))
    if lines is not None and len(lines) != N_FLOWS:
        errs.append(f"行数 {len(lines)} != 受理されるべき flow 数 {N_FLOWS}")
    return errs


def _check_rejected(redact_path: Path, reject_hosts: tuple[str, ...]) -> list[str]:
    """非対象ホストの flow が 1 本も出ないこと。

    受理側と同じ method を使うので、host を見ない filter (特定 method を落とすだけ等) では
    この検査を満たせない。出力が 0 行であること自体が判定になるので、redact に書き換えられ
    うる URL や本文を印として使う必要もない。"""
    if not reject_hosts:
        return []
    lines, errs = _run_lines(redact_path, synth_flows(reject_hosts))
    if lines:
        named = [h for h in reject_hosts if any(h in ln for ln in lines)]
        which = ", ".join(named) if named else ", ".join(reject_hosts)
        errs.append(
            f"非対象ホスト ({which}) の flow を落としていない — {len(lines)} 行出力された"
        )
    return errs


def check_contract(redact_path: Path) -> list[str]:
    """エンジン契約違反のリストを返す (空なら OK)。

    root を宣言しているアプリは、宣言された全 root について
      - 受理: root / 子 / 孫 の各ホストで flow が 1 本も落ちないこと (ホストごとに別実行)
      - 非受理: sentinel と root から導いた近傍が 1 本も出ないこと (まとめて別実行)
    を検査する。受理と非受理を**別の実行に分ける**ので、差引きで辻褄が合う誤り (受理分を
    落として非対象を通す) は原理的に隠せず、印を仕込む必要も無い。"""
    try:
        domains = read_selfcheck_domains(redact_path)
    except DeclarationError as e:
        return [str(e)]
    if not domains:
        return _check_accepted(redact_path, DEFAULT_HOST)
    errs: list[str] = []
    for domain in domains:
        for host in accepted_hosts(domain):
            errs += [f"[{host}] {e}" for e in _check_accepted(redact_path, host)]
        # 宣言が重なっているとき (example.com と api.example.com)、後者の近傍
        # notapi.example.com は前者の部分木に入る = 正しい allowlist なら受理すべき。
        # 落ちることを要求すると正しいアプリを落とすので、宣言部分木に入る近傍は外す。
        rejects = tuple(
            h
            for h in (REJECTED_HOST, *near_miss_hosts(domain))
            if not any(in_subtree(h, d) for d in domains)
        )
        errs += [f"[{domain} 非受理] {e}" for e in _check_rejected(redact_path, rejects)]
    return errs


def write_temp_app(d: Path, body: str) -> Path:
    """任意の本体を持つ一時 redact_flow.py を書く (negative probe 用)。ENGINE を import path に通す。"""
    d.mkdir(parents=True, exist_ok=True)
    p = d / "redact_flow.py"
    p.write_text(f"import sys\nsys.path.insert(0, {str(ENGINE)!r})\n{body}")
    return p


def write_temp_redact(d: Path, redact_body: str, *, declare_domains: tuple[str, ...] = ()) -> Path:
    """core/redact/flows_to_ndjson を使う一時 redact_flow.py を作る (negative probe 用)。
    declare_domains を渡すと SELFCHECK_DOMAINS を宣言するが filter は実装しない
    (= 宣言と実装の食い違いを検査器が捕まえるかの probe になる)。"""
    decl = f"SELFCHECK_DOMAINS = {tuple(declare_domains)!r}\n" if declare_domains else ""
    return write_temp_app(
        d,
        "from flows_to_ndjson import flows_to_ndjson\n"
        f"{decl}"
        f"{redact_body}\n"
        'if __name__ == "__main__":\n'
        "    flows_to_ndjson(redact)\n",
    )


def _app_src(decl: str, cond: str, *, annotated: bool = False, split_annotation: bool = False) -> str:
    """negative probe 用に「宣言 + host filter」を持つ redact_flow の本体を組み立てる。"""
    if split_annotation:
        head = f"SELFCHECK_DOMAINS: tuple[str, ...]\nSELFCHECK_DOMAINS = {decl}\n"
    elif annotated:
        head = f"SELFCHECK_DOMAINS: tuple[str, ...] = {decl}\n"
    else:
        head = f"SELFCHECK_DOMAINS = {decl}\n"
    return (
        "import json\n"
        "from flows_to_ndjson import to_dict\n"
        "from mitmproxy import http, io\n"
        "def _subtree(h, d):\n"
        "    return h == d or h.endswith('.' + d)\n"
        f"{head}"
        'if __name__ == "__main__":\n'
        "    with sys.stdin.buffer as fp:\n"
        "        for f in io.FlowReader(fp).stream():\n"
        f"            if isinstance(f, http.HTTPFlow) and ({cond}):\n"
        "                print(json.dumps(to_dict(f), ensure_ascii=False))\n"
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
            domains = read_selfcheck_domains(rp)  # errs が空 = ここでは投げない
            note = (
                f" (SELFCHECK_DOMAINS={' '.join(domains)} / root/子/孫の受理と near-miss の drop も確認)"
                if domains
                else ""
            )
            print(f"ok  {rel}{note}")

    # 2) テンプレートと検査器の宣言名の一致 (ずれると新規アプリが「宣言なし」扱いになる)
    if TEMPLATE.exists():
        if DECL_NAME not in TEMPLATE.read_text(encoding="utf-8"):
            print(
                f"NG  {TEMPLATE.name}: 宣言名 {DECL_NAME} が書かれていない"
                " — テンプレートに従った新規アプリは宣言なし扱いになり行数 0 で落ちる",
                file=sys.stderr,
            )
            failed = True
    else:
        print(f"NG  {TEMPLATE} が無い", file=sys.stderr)
        failed = True

    # 3) negative probe: 検査器が契約違反を実際に検出することを pin する
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
        # (c) root を宣言しながら filter を実装しない redact → 非対象の素通りを検出するはず
        rp = write_temp_redact(
            root / "declared_no_filter",
            "def redact(s):\n    return s\n",
            declare_domains=("example.com",),
        )
        if not any("落としていない" in e for e in check_contract(rp)):
            print("negative: 宣言だけして落とさない redact を検出できなかった", file=sys.stderr)
            failed = True
        # (d) 受理分を 1 本落として非対象を 1 本通す filter。差引きで行数は N のまま合うので、
        #     同一性の検査だけが捕まえられる。
        rp = write_temp_app(
            root / "wrong_filter",
            _app_src(
                '("example.com",)',
                'f.request.method != "DELETE" and ('
                'f.request.pretty_host == "notexample.com" '
                'or f.request.pretty_host.endswith("example.com"))',
            ),
        )
        errs = check_contract(rp)
        if not (any("落としていない" in e for e in errs) and any("行数" in e for e in errs)):
            print(
                "negative: 受理分を落として非対象を通す filter を検出できなかった",
                file=sys.stderr,
            )
            failed = True
        # (d2) host を一切見ず、特定 method を落とすだけの filter。非受理側の flow に受理側と
        #      違う method を使っていると、これが契約を満たしてしまい第三者宛が素通りする。
        rp = write_temp_app(
            root / "method_only",
            _app_src('("example.com",)', 'f.request.method != "PATCH"'),
        )
        if not any("落としていない" in e for e in check_contract(rp)):
            print(
                "negative: host を見ず method だけで落とす filter を検出できなかった",
                file=sys.stderr,
            )
            failed = True
        # (e) 宣言した root のうち先頭しか受理しない filter。宣言の一部だけ通るのを成功と
        #     report しないことを pin する。
        rp = write_temp_app(
            root / "partial_domains",
            _app_src(
                '("a.example.com", "b.example.com")',
                '_subtree(f.request.pretty_host, "a.example.com")',
            ),
        )
        if not any(e.startswith("[b.example.com]") for e in check_contract(rp)):
            print(
                "negative: 宣言した root の一部しか受理しない filter を検出できなかった",
                file=sys.stderr,
            )
            failed = True
        # (f) DNS ラベル境界を見ない filter (endswith)。宣言 root は正しく受理するので、
        #     root から導いた near-miss を混ぜないと検出できない。
        rp = write_temp_app(
            root / "boundary",
            _app_src('("anthropic.com",)', 'f.request.pretty_host.endswith("anthropic.com")'),
        )
        if not any("落としていない" in e for e in check_contract(rp)):
            print("negative: ラベル境界を見ない filter を検出できなかった", file=sys.stderr)
            failed = True
        # (f2) 複数ラベルの公開接尾辞 (example.co.uk)。ラベル位置から近傍を作る導出だと
        #      登録可能ラベルを取り違えてこの誤りを素通りする。root 基準なら一様に効く。
        rp = write_temp_app(
            root / "boundary_psl",
            _app_src('("example.co.uk",)', 'f.request.pretty_host.endswith("example.co.uk")'),
        )
        if not any("落としていない" in e for e in check_contract(rp)):
            print(
                "negative: 複数ラベル公開接尾辞でラベル境界を見ない filter を検出できなかった",
                file=sys.stderr,
            )
            failed = True
        # (g) 部分木を宣言しながら完全一致でしか通さない filter → subdomain が受理されない
        rp = write_temp_app(
            root / "exact_only",
            _app_src('("example.com",)', 'f.request.pretty_host == "example.com"'),
        )
        if not any(e.startswith(f"[{SUB_LABEL}.example.com]") for e in check_contract(rp)):
            print(
                "negative: 部分木を宣言しながら完全一致でしか通さない filter を検出できなかった",
                file=sys.stderr,
            )
            failed = True
        # (i) 宣言が重なっている場合 (example.com と api.example.com)。後者の近傍は前者の
        #     部分木に入るので、除外しないと正しい union allowlist を leak と誤判定する。
        rp = write_temp_app(
            root / "overlapping_roots",
            _app_src(
                '("example.com", "api.example.com")',
                '_subtree(f.request.pretty_host, "example.com")',
            ),
        )
        errs = check_contract(rp)
        if errs:
            print(f"negative: 重なった宣言を leak と誤判定している ({errs[0]})", file=sys.stderr)
            failed = True
        # (j) root と直下の子までは通すが孫を落とす filter。部分木を約束した宣言に対して
        #     1 段しか試さないと、この部分的な対応を成功と report してしまう。
        rp = write_temp_app(
            root / "one_level_only",
            _app_src(
                '("example.com",)',
                'f.request.pretty_host == "example.com" or ('
                'f.request.pretty_host.endswith(".example.com") '
                'and f.request.pretty_host.count(".") == 2)',
            ),
        )
        if not any(e.startswith(f"[{SUB_LABEL}.{SUB_LABEL}.example.com]") for e in check_contract(rp)):
            print(
                "negative: 孫を落とす filter (部分木の一部だけ対応) を検出できなかった",
                file=sys.stderr,
            )
            failed = True
        # (k) 宣言はあるがリテラルでない (別名を参照) 形。「宣言なし」に丸めると既定ホストの
        #     flow しか流れず、非対象ホストの検査が黙って無効化されたまま緑になる (fail-open)。
        #     filter を実装していないアプリを使い、素通りが検出できることまで確かめる。
        rp = write_temp_app(
            root / "nonliteral_decl",
            "import json\n"
            "from flows_to_ndjson import to_dict\n"
            "from mitmproxy import http, io\n"
            '_DOMAINS = ("example.com",)\n'
            "SELFCHECK_DOMAINS = _DOMAINS\n"
            'if __name__ == "__main__":\n'
            "    with sys.stdin.buffer as fp:\n"
            "        for f in io.FlowReader(fp).stream():\n"
            "            if isinstance(f, http.HTTPFlow):\n"
            "                print(json.dumps(to_dict(f), ensure_ascii=False))\n",
        )
        if not any("リテラル" in e for e in check_contract(rp)):
            print(
                "negative: リテラルでない宣言を「宣言なし」に丸めている (fail-open)",
                file=sys.stderr,
            )
            failed = True
        # (k2) 宣言の形が違う (文字列の tuple でない) 場合も同様に明示的に落ちること
        rp = write_temp_redact(root / "bad_shape_decl", "def redact(s):\n    return s\n")
        rp.write_text(rp.read_text().replace("def redact", "SELFCHECK_DOMAINS = 42\ndef redact", 1))
        if not any("tuple" in e for e in check_contract(rp)):
            print("negative: 形の違う宣言を検出できなかった", file=sys.stderr)
            failed = True
        # (m) module 直下で 2 回代入されている形。最初の 1 つで打ち切ると実行時と違う値を
        #     読み、宣言したはずの root が未検査のまま緑になる。曖昧なので明示的に落とす。
        rp = write_temp_redact(root / "reassigned_decl", "def redact(s):\n    return s\n")
        rp.write_text(
            rp.read_text().replace(
                "def redact",
                'SELFCHECK_DOMAINS = ("a.example.com",)\n'
                'SELFCHECK_DOMAINS = ("a.example.com", "b.example.com")\n'
                "def redact",
                1,
            )
        )
        if not any("2 回代入" in e for e in check_contract(rp)):
            print("negative: 複数回代入された宣言を検出できなかった", file=sys.stderr)
            failed = True
        # (m2) += で組み立てる形も静的に読めないので落とす
        rp = write_temp_redact(root / "augassign_decl", "def redact(s):\n    return s\n")
        rp.write_text(
            rp.read_text().replace(
                "def redact",
                'SELFCHECK_DOMAINS = ("a.example.com",)\n'
                'SELFCHECK_DOMAINS += ("b.example.com",)\n'
                "def redact",
                1,
            )
        )
        if not any("+=" in e for e in check_contract(rp)):
            print("negative: += で組み立てた宣言を検出できなかった", file=sys.stderr)
            failed = True
        # (h) 型注記つきの宣言 (AnnAssign) を読めること。読み落とすと、正しく filter する
        #     アプリが既定ホストで検査されて「行数 0」で落ちる。
        rp = write_temp_app(
            root / "annassign",
            _app_src(
                '("example.com",)',
                '_subtree(f.request.pretty_host, "example.com")',
                annotated=True,
            ),
        )
        errs = check_contract(rp)
        if errs:
            print(f"negative: 型注記つき宣言を読めていない ({errs[0]})", file=sys.stderr)
            failed = True
        # (h2) 注記だけを先に書き、あとで実代入する形。注記の行で走査を打ち切ると宣言を
        #      読み落とし、正しく filter するアプリが「行数 0」で落ちる。
        rp = write_temp_app(
            root / "annassign_split",
            _app_src(
                '("example.com",)',
                '_subtree(f.request.pretty_host, "example.com")',
                split_annotation=True,
            ),
        )
        errs = check_contract(rp)
        if errs:
            print(f"negative: 注記と代入を分けた宣言を読めていない ({errs[0]})", file=sys.stderr)
            failed = True

    if failed:
        print("redact selfcheck FAILED", file=sys.stderr)
        return 1
    print("ok  redact selfcheck (engine contract + negative probe)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
