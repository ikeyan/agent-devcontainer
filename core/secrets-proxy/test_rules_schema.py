"""rules.schema.json の substitute / into 分岐を fixture で gate する。

check-rules は実体の rules.yaml だけを検証するが、rules.yaml が踏まない分岐 (substituteEntry の
oneOf の全枝、into 必須化・capture.into の encode 非対応などの negative) はそこでは exercise されない。
スキーマを壊しても気づけない穴になるため、positive (通るべき) / negative (弾くべき) の最小 fixture を
ここで pin する。

特に negative probe は、代理トークンを宣言外フィールドへ逃がす exfil 対策 (into 必須) が
スキーマ層でも崩れていないことを保証する。

依存は jsonschema + PyYAML 不要 (dict を直接検証)。実行: python3 test_rules_schema.py
"""
import json
import os
import sys

import jsonschema

HERE = os.path.dirname(os.path.abspath(__file__))


def _validator():
    with open(os.path.join(HERE, "rules.schema.json")) as f:
        schema = json.load(f)
    cls = jsonschema.validators.validator_for(schema)
    cls.check_schema(schema)
    return cls(schema)


def _doc(host_spec):
    """hostSpec を 1 ホスト分の完全な rules ドキュメントに包む。"""
    return {"hosts": {"api.example.com": host_spec}}


V = _validator()


def _ok(label, host_spec):
    errs = sorted(V.iter_errors(_doc(host_spec)), key=lambda e: list(e.path))
    if errs:
        msgs = "; ".join(f"/{'/'.join(map(str, e.path))}: {e.message}" for e in errs)
        raise AssertionError(f"{label}: 通るべき fixture が弾かれた: {msgs}")


def _bad(label, host_spec):
    if V.is_valid(_doc(host_spec)):
        raise AssertionError(f"{label}: 弾くべき fixture が通った")


# --- substitute: name 形 (糖衣 @@SECRET_<name>_<field>@@) ---

def test_substitute_name_form_ok():
    _ok("name 形", {
        "substitute": [{
            "name": "acct1",
            "secret": {"item": "ACCT1"},
            "fields": [
                {"field": "username", "into": {"type": "form", "name": "emailAddress"}},
                {"field": "password", "into": {"type": "form", "name": "password"}},
            ],
        }],
    })


def test_substitute_name_form_field_missing_into_is_rejected():
    # into 必須 (exfil 対策)。field 直下に into が無ければ弾く。
    _bad("name 形 into 欠落", {
        "substitute": [{
            "name": "acct1",
            "secret": {"item": "ACCT1"},
            "fields": [{"field": "password"}],
        }],
    })


def test_substitute_name_form_secret_with_field_is_rejected():
    # name 形の secret は item のみ (field は fields[] 側)。secret.field は additionalProperties で弾く。
    _bad("name 形 secret.field", {
        "substitute": [{
            "name": "acct1",
            "secret": {"item": "ACCT1", "field": "password"},
            "fields": [{"field": "password", "into": {"type": "form", "name": "password"}}],
        }],
    })


def test_substitute_name_form_totp_field_is_rejected():
    # substitute では totp を field に取れない (pattern から除外済み)。
    _bad("name 形 totp", {
        "substitute": [{
            "name": "acct1",
            "secret": {"item": "ACCT1"},
            "fields": [{"field": "totp", "into": {"type": "form", "name": "otp"}}],
        }],
    })


# --- substitute: placeholder 形 (任意リテラル fake) ---

def test_substitute_placeholder_form_ok():
    _ok("placeholder 形", {
        "substitute": [{
            "placeholder": "sk-ant-FAKExxxx",
            "secret": {"item": "ACCT1", "field": "password"},
            "into": {"type": "header", "name": "x-api-key"},
            "bind": ["api.anthropic.com"],
        }],
    })


def test_substitute_placeholder_form_missing_into_is_rejected():
    _bad("placeholder 形 into 欠落", {
        "substitute": [{
            "placeholder": "sk-ant-FAKExxxx",
            "secret": {"item": "ACCT1", "field": "password"},
        }],
    })


def test_substitute_unknown_key_is_rejected():
    _bad("未知キー", {
        "substitute": [{
            "placeholder": "fake",
            "secret": {"env": "T"},
            "into": {"type": "header", "name": "x"},
            "bogus": 1,
        }],
    })


# --- capture: from={type,name} 形 + into 必須化 (回帰) ---

def test_capture_with_into_ok():
    _ok("capture into あり", {
        "capture": [{
            "from": {"type": "set-cookie", "name": "sid"},
            "store": "sid",
            "into": {"type": "cookie", "name": "sid"},
        }],
    })


def test_capture_without_into_is_rejected():
    _bad("capture into 欠落", {
        "capture": [{"from": {"type": "set-cookie", "name": "sid"}, "store": "sid"}],
    })


def test_capture_from_string_form_is_rejected():
    # 旧形式 (from が string + 兄弟 name) はもう無効。
    _bad("capture from string", {
        "capture": [{"from": "set-cookie", "name": "sid", "store": "sid",
                     "into": {"type": "cookie", "name": "sid"}}],
    })


def test_capture_from_missing_name_is_rejected():
    _bad("capture from name 欠落", {
        "capture": [{"from": {"type": "json"}, "store": "t",
                     "into": {"type": "header", "name": "x"}}],
    })


def test_capture_into_encode_is_rejected():
    # capture.into は captureInto ($def) で encode 非対応 (addon の fail-closed と一致)。encode 付きは弾く。
    _bad("capture into encode", {
        "capture": [{
            "from": {"type": "json", "name": "t"},
            "store": "t",
            "into": {"type": "header", "name": "x-auth", "encode": "base64"},
        }],
    })


# --- into.encode ---

def test_substitute_into_encode_base64_ok():
    _ok("encode base64", {
        "substitute": [{
            "placeholder": "fakeb64",
            "secret": {"env": "T"},
            "into": {"type": "json", "name": "buser", "encode": "base64"},
        }],
    })


def test_into_encode_unknown_value_is_rejected():
    _bad("encode 未知値", {
        "substitute": [{
            "placeholder": "fakeb64",
            "secret": {"env": "T"},
            "into": {"type": "json", "name": "buser", "encode": "rot13"},
        }],
    })


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print("ok  rules_schema (%d fixtures)" % len(fns))


if __name__ == "__main__":
    try:
        _run_all()
    except AssertionError as ex:
        sys.stderr.write(f"rules schema fixture 失敗: {ex}\n")
        sys.exit(1)
