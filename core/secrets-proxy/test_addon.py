"""addon.py の単体/property テスト。mitmproxy の test ヘルパで hook を直接駆動する
(実ネットワーク不要)。

方針: 不変条件ごとに 1 つの property test を置き、値は hypothesis で生成し、離散軸
(機構 / Content-Type / into 種別 / source / encode / bind / 捕獲元 …) は for で掛け合わせる。
1 ケース 1 assert ではなく、軸の直積 × ランダム値で網羅して回帰検出力を上げる。

  実行: python3 test_addon.py            (proxy イメージ内 = mitmproxy あり)
"""
import base64
import contextlib
import json
import os
import tempfile
import urllib.parse as _url

from hypothesis import HealthCheck, given, settings, strategies as st
from mitmproxy.test import tflow

import addon

_TMP = tempfile.mkdtemp(prefix="sp-test-")
_SETTINGS = settings(max_examples=25, deadline=None,
                     suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much])

# 印字可能 ASCII から区切り/クォート/バックスラッシュ/@ を除いた安全域。JSON・ヘッダ・
# url-encode に素で乗り、redaction の byte 単位 replace が一致し、placeholder(@@…@@)と衝突しない。
_SAFE = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126),
    min_size=8, max_size=48,
).filter(lambda s: not (set('"\\@') & set(s)))
_JUNK = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126),
    min_size=0, max_size=24,
).filter(lambda s: not (set('"\\@') & set(s)))
# Set-Cookie の値は ; , (属性/複数 cookie の区切り) を含めない (RFC 6265)。捕獲元に
# set-cookie を含む test はこちらを使う。
_COOKIE_SAFE = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126),
    min_size=8, max_size=48,
).filter(lambda s: not (set('"\\@;,') & set(s)))


def _proxy(rules_yaml):
    """rules_yaml から SecretsProxy を load して返す (RULES_PATH をモジュールごと差し替え)。"""
    path = os.path.join(_TMP, "rules.yaml")
    with open(path, "w") as f:
        f.write(rules_yaml)
    addon.RULES_PATH = path
    sp = addon.SecretsProxy()
    sp.load(None)
    return sp


@contextlib.contextmanager
def _env(**kv):
    old = {k: os.environ.get(k) for k in kv}
    os.environ.update(kv)
    try:
        yield
    finally:
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


@contextlib.contextmanager
def _nullctx():
    yield


def _source(kind, value):
    """secret 参照の YAML 断片と、解決に要る文脈 (env セット / file 書込) を返す。"""
    if kind == "env":
        return "{ env: SP_SECRET }", _env(SP_SECRET=value)
    if kind == "file":
        p = os.path.join(_TMP, "secret.txt")
        with open(p, "w") as fh:
            fh.write(value)
        return "{ file: %s }" % p, _nullctx()
    raise ValueError(kind)


def _reqflow(host, *, method="POST", path="/p", headers=None, ct=None, body=None):
    f = tflow.tflow()
    f.request.host = host
    f.request.headers["host"] = host
    f.request.method = method
    f.request.path = path
    for k, v in (headers or {}).items():
        f.request.headers[k] = v
    if ct is not None:
        f.request.headers["content-type"] = ct
    if body is not None:
        f.request.set_text(body)
    return f


def _respflow(host, *, ct=None, body=None, headers=None):
    f = tflow.tflow(resp=True)
    f.request.host = host
    f.request.headers["host"] = host
    if ct is not None:
        f.response.headers["content-type"] = ct
    for k, v in (headers or {}).items():
        f.response.headers[k] = v
    if body is not None:
        f.response.set_text(body)
    return f


def _guard(sp, f):
    sp.responseheaders(f)
    sp.response(f)
    return f


# into 種別ごとの (name, place(host, s)=その場所に s を置いた request, get(flow)=その場所の値)。
# place は「宣言場所へ placeholder を置く」用途と、空文字を置いて inject に上書きさせる用途を兼ねる。
_INTO = [
    ("header", "x-tok",
     lambda h, s: _reqflow(h, headers={"x-tok": s}),
     lambda f: f.request.headers.get("x-tok")),
    ("query", "tok",
     lambda h, s: _reqflow(h, path="/p?tok=" + _url.quote(s, safe="")),
     lambda f: f.request.query.get("tok")),
    ("form", "field",
     lambda h, s: _reqflow(h, ct="application/x-www-form-urlencoded", body=_url.urlencode({"field": s})),
     lambda f: f.request.urlencoded_form.get("field")),
    ("json", "field",
     lambda h, s: _reqflow(h, ct="application/json", body=json.dumps({"field": s})),
     lambda f: json.loads(f.request.get_text() or "{}").get("field")),
]


# ===================== load 時 fail-closed / 正規化 =====================

def test_load_rejects_invalid_rules():
    """不正な rules は load で raise する (fail-closed)。理由文字列も検証する。"""
    H = 'hosts:\n  "x.example.com":\n'
    cases = [
        ("capture into 欠落",
         H + "    capture:\n      - { from: { type: set-cookie, name: s }, store: s }\n", "into"),
        ("capture.from string 形",
         H + "    capture:\n      - from: set-cookie\n        store: s\n"
             "        into: { type: cookie, name: s }\n", "from"),
        ("capture.into encode",
         H + "    capture:\n      - from: { type: json, name: t }\n        store: s\n"
             "        into: { type: header, name: x, encode: base64 }\n", "encode"),
        ("capture store 重複",
         H + "    capture:\n      - from: { type: set-cookie, name: a }\n        store: dup\n"
             "        into: { type: cookie, name: a }\n"
             "      - from: { type: set-cookie, name: b }\n        store: dup\n"
             "        into: { type: cookie, name: b }\n", "衝突"),
        ("substitute placeholder 重複",
         H + "    substitute:\n      - placeholder: \"P\"\n        secret: { env: A }\n"
             "        into: { type: header, name: h1 }\n"
             "      - placeholder: \"P\"\n        secret: { env: B }\n"
             "        into: { type: header, name: h2 }\n", "衝突"),
    ]
    for label, rules, expect in cases:
        try:
            _proxy(rules)
        except Exception as e:
            assert expect in str(e), (label, str(e))
        else:
            raise AssertionError(label + ": load が raise しなかった")


def test_load_normalizes_sources_and_forms():
    """inject/substitute の source (bw/env/file) と name/placeholder 形の正規化を構造で検証。"""
    sp = _proxy(
        'block_unlisted: true\n'
        'hosts:\n'
        '  "api.example.com":\n'
        '    inject:\n'
        '      - secret: { item: "Example API", field: password }\n'
        '        into: { type: header, name: Authorization, value: "Bearer ${secret}" }\n'
        '      - secret: { env: TOK }\n'
        '        into: { type: header, name: x-e }\n'
        '      - secret: { file: /nonexistent }\n'
        '        into: { type: header, name: x-f }\n'
        '    substitute:\n'
        '      - name: acct1\n'
        '        secret: { item: ACCT1 }\n'
        '        fields:\n'
        '          - { field: username, into: { type: form, name: email } }\n'
        '          - { field: password, into: { type: form, name: pw } }\n'
        '      - placeholder: "LIT"\n'
        '        secret: { env: T2 }\n'
        '        into: { type: json, name: buser, encode: base64 }\n'
    )
    injs = sp.inject["api.example.com"]
    assert injs[0]["source"] == ("bw", "Example API", "password")
    assert injs[0]["value_tmpl"] == "Bearer ${secret}"
    assert injs[1]["source"] == ("env", "TOK")
    assert injs[2]["source"] == ("file", "/nonexistent")
    subs = {s["ph"]: s for s in sp.substitute["api.example.com"]}
    assert set(subs) == {"@@SECRET_acct1_username@@", "@@SECRET_acct1_password@@", "LIT"}, subs
    assert subs["@@SECRET_acct1_username@@"]["source"] == ("bw", "ACCT1", "username")
    assert subs["@@SECRET_acct1_username@@"]["into"] == {"type": "form", "name": "email", "encode": None}
    assert subs["LIT"]["source"] == ("env", "T2")
    assert subs["LIT"]["into"]["encode"] == "base64"


# ===================== block / block_unlisted =====================

def test_block_rules_and_unlisted():
    """block ルール (host/methods/path の各省略可) と block_unlisted の直積を検証。"""
    sp = _proxy(
        'block_unlisted: true\n'
        'block:\n'
        '  - host: "*.example.com"\n'
        '    methods: [POST, DELETE]\n'
        '    path: "^/danger"\n'
        '    reason: "r1"\n'
        '  - path: "^/admin"\n'          # host/method 省略 = 全ホスト/全メソッド
        '    reason: "r2"\n'
        'allow_hosts: ["a.example.com", "b.example.com"]\n'
    )
    # (method, host, path, blocked であるべきか)
    cases = [
        ("POST", "a.example.com", "/danger", True),    # r1
        ("DELETE", "a.example.com", "/danger", True),  # r1
        ("GET", "a.example.com", "/danger", False),    # r1 method 不一致 → 素通し
        ("POST", "b.example.com", "/danger", True),    # r1 host 一致
        ("GET", "a.example.com", "/admin", True),      # r2 (host 省略)
        ("GET", "a.example.com", "/safe", False),      # どの rule も不一致 → allow
        ("GET", "evil.other.net", "/safe", True),      # block_unlisted (未許可)
    ]
    for method, host, path, want in cases:
        f = _reqflow(host, method=method, path=path)
        sp.request(f)
        blocked = f.response is not None and f.response.status_code == 403
        assert blocked == want, (method, host, path, f.response and f.response.status_code)


# ===================== inject =====================

@given(value=_SAFE)
@_SETTINGS
def test_inject_places_value(value):
    """inject: source(env/file) × encode(none/base64) × into(header/query/form/json) の直積で、
    宣言した場所に期待どおりの値 (encode 済み) が載る。"""
    host = "x.example.com"
    for kind in ("env", "file"):
        for encode in (None, "base64"):
            expected = base64.b64encode(value.encode()).decode() if encode else value
            for into_type, name, place, get in _INTO:
                sec, ctx = _source(kind, value)
                enc = (", encode: %s" % encode) if encode else ""
                rules = (
                    'block_unlisted: true\n'
                    'hosts:\n'
                    '  "%s":\n' % host +
                    '    inject:\n'
                    '      - secret: %s\n' % sec +
                    '        into: { type: %s, name: %s%s }\n' % (into_type, name, enc)
                )
                with ctx:
                    sp = _proxy(rules)
                    f = place(host, "")      # 空を置いて inject に上書きさせる
                    sp.request(f)
                assert f.response is None, (kind, encode, into_type, f.response and f.response.status_code)
                assert get(f) == expected, (kind, encode, into_type, get(f), expected)
    # value テンプレート (${secret} ラッパ) の展開 (header で 1 度)
    with _env(SP_SECRET=value):
        sp = _proxy(
            'block_unlisted: true\n'
            'hosts:\n'
            '  "%s":\n' % host +
            '    inject:\n'
            '      - secret: { env: SP_SECRET }\n'
            '        into: { type: header, name: x-tok, value: "Bearer ${secret}" }\n'
        )
        f = _reqflow(host)
        sp.request(f)
    assert f.request.headers.get("x-tok") == "Bearer " + value


# ===================== substitute (placeholder -> 本物) =====================

@given(value=_SAFE, junk=_JUNK)
@_SETTINGS
def test_substitute_restores_only_bound_and_declared_location(value, junk):
    """substitute: 束縛ホスト かつ 宣言 into 位置 の placeholder だけを本物 (encode 済み) へ戻す。
    非束縛ホスト / 別フィールド では戻さず placeholder のまま (= 本物が漏れない)。into × encode で掛ける。"""
    host, other, PH = "x.example.com", "other.example.com", "PH0001"
    for into_type, name, place, get in _INTO:
        for encode in (None, "base64"):
            enc = (", encode: %s" % encode) if encode else ""
            expected = base64.b64encode(value.encode()).decode() if encode else value
            rules = (
                'block_unlisted: true\n'
                'detect_leaks: false\n'            # 置換スコープのみ隔離 (leak は別 test)
                'allow_hosts: ["%s"]\n' % other +
                'hosts:\n'
                '  "%s":\n' % host +
                '    substitute:\n'
                '      - placeholder: "%s"\n' % PH +
                '        secret: { env: SP_SECRET }\n'
                '        into: { type: %s, name: %s%s }\n' % (into_type, name, enc)
            )
            with _env(SP_SECRET=value):
                sp = _proxy(rules)
                sp.running()
                # 束縛ホスト + 宣言位置 → 本物 (encode 済み) へ復元
                f1 = place(host, PH)
                sp.request(f1)
                assert f1.response is None, (into_type, encode)
                assert get(f1) == expected, ("restore", into_type, encode, get(f1))
                # 束縛ホスト + 別フィールド (bio) → 触らない (byte 不変)
                f2 = _reqflow(host, ct="application/json", body=json.dumps({"bio": PH + junk}))
                sp.request(f2)
                assert json.loads(f2.request.get_text())["bio"] == PH + junk, ("abuse", into_type, encode)
                # 非束縛ホスト + 宣言位置 → 復元しない (placeholder のまま)
                f3 = place(other, PH)
                sp.request(f3)
                assert f3.response is None, (into_type, encode)
                assert get(f3) == PH, ("non-bound", into_type, encode, get(f3))


def test_unresolved_substitute_fails_closed():
    """解決不能 substitute: placeholder が宣言位置に来たら 502、来なければ素通し。into 種別で掛ける。"""
    host, PH = "x.example.com", "PHUNRES"
    os.environ.pop("SP_MISSING", None)
    for into_type, name, place, get in _INTO:
        sp = _proxy(
            'block_unlisted: true\n'
            'hosts:\n'
            '  "%s":\n' % host +
            '    substitute:\n'
            '      - placeholder: "%s"\n' % PH +
            '        secret: { env: SP_MISSING }\n'
            '        into: { type: %s, name: %s }\n' % (into_type, name)
        )
        sp.running()      # warm 失敗 → unresolved sentinel
        f = place(host, PH)
        sp.request(f)
        assert f.response is not None and f.response.status_code == 502, \
            (into_type, f.response and f.response.status_code)
        clear = _reqflow(host)
        sp.request(clear)
        assert clear.response is None, into_type


# ===================== leak 検知 =====================

@given(value=_SAFE)
@_SETTINGS
def test_leak_detection_blocks_real_value(value):
    """dev が本物の捕獲済み値を送ると 403。leak scan は raw byte (ヘッダ値 / textual 本文) を
    見る (url-decode しない tripwire) ので、生値が素で現れる場所で掛ける。単独/部分文字列も。"""
    host = "x.example.com"
    with _env(SP_SECRET=value):
        sp = _proxy(
            'block_unlisted: true\n'
            'detect_leaks: true\n'
            'hosts:\n'
            '  "%s":\n' % host +
            '    substitute:\n'
            '      - placeholder: "PHLEAK"\n'
            '        secret: { env: SP_SECRET }\n'
            '        into: { type: header, name: x-api-key }\n'
        )
        sp.running()
        makers = [
            lambda: _reqflow(host, headers={"x-any": value}),                        # ヘッダ単独
            lambda: _reqflow(host, headers={"x-two": "pre-" + value + "-post"}),      # ヘッダ内の部分文字列
            lambda: _reqflow(host, ct="application/json", body=json.dumps({"a": {"b": value}})),  # 入れ子 JSON 本文
        ]
        for i, mk in enumerate(makers):
            f = mk()
            sp.request(f)
            assert f.response is not None and f.response.status_code == 403, \
                (i, f.response and f.response.status_code)


# ===================== capture (応答 -> placeholder) + 復元 =====================

@given(value=_COOKIE_SAFE)
@_SETTINGS
def test_capture_roundtrip_and_bind(value):
    """capture: 捕獲元 (set-cookie/header/json/regex) で掛け、応答から本物を捕獲 → 応答を
    redact → 以降 placeholder を送ると束縛ホスト宛にだけ本物へ復元する。"""
    host, other = "x.example.com", "other.example.com"
    sources = [
        ("set-cookie", "c", lambda f: f.response.headers.__setitem__("set-cookie", "c=" + value)),
        ("header", "x-src", lambda f: f.response.headers.__setitem__("x-src", value)),
        ("json", "tok", lambda f: f.response.set_text(json.dumps({"tok": value}))),
        ("regex", '"tok":"([^"]+)"', lambda f: f.response.set_text('{"tok":"' + value + '"}')),
    ]
    for frm, name, setup in sources:
        sp = _proxy(
            'block_unlisted: true\n'
            'allow_hosts: ["%s"]\n' % other +
            'hosts:\n'
            '  "%s":\n' % host +
            '    capture:\n'
            '      - from: { type: %s, name: %r }\n' % (frm, name) +
            '        store: s\n'
            '        bind: ["%s"]\n' % host +
            '        into: { type: header, name: x-auth }\n'
        )
        f = _respflow(host, ct="application/json")
        setup(f)
        _guard(sp, f)
        assert sp.cap_store["s"]["value"] == value, (frm, sp.cap_store.get("s"))
        assert value not in (f.response.get_text() or ""), (frm, "body")
        for hv in f.response.headers.values():
            assert value not in hv, (frm, "hdr", hv)
        ph = sp.cap_store["s"]["ph"]
        r = _reqflow(host, headers={"x-auth": ph})
        sp.request(r)
        assert r.response is None and r.request.headers["x-auth"] == value, (frm, "restore")
        r2 = _reqflow(other, headers={"x-auth": ph})
        sp.request(r2)
        assert r2.request.headers["x-auth"] == ph, (frm, "non-bound")


def test_capture_reissue_keeps_real_value_before_redaction():
    """再発行応答でも redaction 前に抽出するので cap_store に本物が入り、正規 placeholder は
    leak 誤検知されず本物へ復元される (順序の回帰 pin)。"""
    sp = _proxy(
        'block_unlisted: true\n'
        'hosts:\n'
        '  "x.example.com":\n'
        '    capture:\n'
        '      - from: { type: json, name: token }\n'
        '        store: tok\n'
        '        bind: ["x.example.com"]\n'
        '        into: { type: header, name: x-auth }\n'
    )

    def do_response(tokval):
        f = _respflow("x.example.com", ct="application/json", body=json.dumps({"token": tokval}))
        return _guard(sp, f)

    do_response("REALTOKEN999")
    assert sp.cap_store["tok"]["value"] == "REALTOKEN999"
    f2 = do_response("REALTOKEN999")   # 同 token 再発行 (cap_store に既存) でも本物のまま
    assert sp.cap_store["tok"]["value"] == "REALTOKEN999", sp.cap_store["tok"]
    ph = sp.cap_store["tok"]["ph"]
    body = f2.response.get_text()
    assert "REALTOKEN999" not in body and ph in body, body
    req = _reqflow("x.example.com", headers={"x-auth": ph})
    sp.request(req)
    assert req.response is None and req.request.headers["x-auth"] == "REALTOKEN999"


# ===================== 応答 redaction の網羅性 (crown-jewel の不変条件) =====================

@given(value=_SAFE)
@_SETTINGS
def test_response_redaction_is_complete(value):
    """既知の秘密 (substitute値 / inject生値 / inject符号化値 / capture値 / bind先) が応答に現れたら、
    Content-Type や置き場所 (本文 / ヘッダ) に依らず dev に返らない。機構 × Content-Type で掛ける。"""
    host = "x.example.com"
    b64 = base64.b64encode(value.encode()).decode()

    def subst(h, into_type, name, bind=None):
        bl = ('        bind: [%s]\n' % ", ".join('"%s"' % x for x in bind)) if bind else ""
        return (
            'block_unlisted: true\n'
            + (('allow_hosts: ["%s"]\n' % host) if bind else "")
            + 'hosts:\n  "%s":\n' % h +
            '    substitute:\n'
            '      - placeholder: "PHRED"\n'
            '        secret: { env: SP_SECRET }\n'
            '        into: { type: %s, name: %s }\n' % (into_type, name) + bl
        )

    def inject(into_type, name, encode=None):
        enc = (", encode: %s" % encode) if encode else ""
        return (
            'block_unlisted: true\n'
            'hosts:\n  "%s":\n' % host +
            '    inject:\n'
            '      - secret: { env: SP_SECRET }\n'
            '        into: { type: %s, name: %s%s }\n' % (into_type, name, enc)
        )

    capture = (
        'block_unlisted: true\n'
        'hosts:\n  "%s":\n' % host +
        '    capture:\n'
        '      - from: { type: json, name: tok }\n'
        '        store: s\n        bind: ["%s"]\n' % host +
        '        into: { type: header, name: x-auth }\n'
    )

    # (label, rules, warm 要否, on_wire=応答に現れる秘密, body(dict))
    scenarios = [
        ("substitute", subst(host, "header", "x-api-key"), True, value, {"echo": value}),
        ("inject_raw", inject("header", "x-api-key"), True, value, {"echo": value}),
        ("inject_b64", inject("json", "buser", "base64"), True, b64, {"echo": b64}),
        ("capture", capture, False, value, {"tok": value}),
        ("subst_bind_target", subst("a.example.com", "header", "x-api-key", bind=[host]),
         True, value, {"echo": value}),
    ]
    for label, rules, warm, on_wire, body in scenarios:
        for ct in ("application/json", "application/octet-stream"):
            with _env(SP_SECRET=value):
                sp = _proxy(rules)
                if warm:
                    sp.running()
            f = _respflow(host, ct=ct, body=json.dumps(body), headers={"x-echo": on_wire})
            _guard(sp, f)
            got = f.response.get_text() or ""
            assert on_wire not in got, (label, ct, "body", got)
            for hv in f.response.headers.values():
                assert on_wire not in hv, (label, ct, "hdr", hv)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok:", fn.__name__)
    print("ALL PASS")
