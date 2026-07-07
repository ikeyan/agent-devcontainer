"""rules_bw.needs_bw / CLI の単体テスト。

依存は PyYAML だけ (mitmproxy 不要) なので、mitmproxy の無い `make check` でも走る。
entrypoint.sh が「rules に Vaultwarden(item) 源がある時だけ unlock を要求する」挙動の回帰 pin。

  実行: python3 test_rules_bw.py
"""
import os
import subprocess
import sys
import tempfile

import yaml

import rules_bw

HERE = os.path.dirname(os.path.abspath(__file__))


def _needs(yaml_text):
    return rules_bw.needs_bw(yaml.safe_load(yaml_text) or {})


def test_item_source_needs_bw():
    assert _needs(
        "hosts:\n"
        "  api.example.com:\n"
        "    inject:\n"
        "      - secret: {item: 'Example API', field: password}\n"
        "        into: {type: header, name: Authorization}\n"
    ) is True


def test_env_only_does_not_need_bw():
    assert _needs(
        "hosts:\n"
        "  api.example.com:\n"
        "    inject:\n"
        "      - secret: {env: REDACT_TOKEN}\n"
        "        into: {type: header, name: Authorization}\n"
    ) is False


def test_file_only_shortform_does_not_need_bw():
    # 短縮形: hostSpec が injection の配列。
    assert _needs(
        "hosts:\n"
        "  api.example.com:\n"
        "    - secret: {file: /run/secret}\n"
        "      into: {type: header, name: Authorization}\n"
    ) is False


def test_mixed_env_and_item_needs_bw():
    assert _needs(
        "hosts:\n"
        "  a.example.com:\n"
        "    inject:\n"
        "      - secret: {env: T}\n"
        "        into: {type: header, name: X}\n"
        "  b.example.com:\n"
        "    inject:\n"
        "      - secret: {item: Foo}\n"
        "        into: {type: header, name: Y}\n"
    ) is True


def test_capture_only_does_not_need_bw():
    # capture は応答からの捕獲で秘密源を持たない。
    assert _needs(
        "hosts:\n"
        "  api.example.com:\n"
        "    capture:\n"
        "      - from: {type: set-cookie, name: sid}\n"
        "        store: sid\n"
        "        into: {type: cookie, name: sid}\n"
    ) is False


def test_substitute_name_form_item_needs_bw():
    # substitute name 形は secret.item を源に取る → unlock 必要 (回帰: P1)。
    assert _needs(
        "hosts:\n"
        "  api.example.com:\n"
        "    substitute:\n"
        "      - name: acct1\n"
        "        secret: {item: ACCT1}\n"
        "        fields:\n"
        "          - {field: password, into: {type: form, name: password}}\n"
    ) is True


def test_substitute_placeholder_form_item_needs_bw():
    # substitute placeholder 形でも secret が item なら unlock 必要。
    assert _needs(
        "hosts:\n"
        "  api.example.com:\n"
        "    substitute:\n"
        "      - placeholder: fakepw\n"
        "        secret: {item: ACCT1, field: password}\n"
        "        into: {type: form, name: password}\n"
    ) is True


def test_substitute_env_only_does_not_need_bw():
    # substitute でも env/file 源なら unlock 不要。
    assert _needs(
        "hosts:\n"
        "  api.example.com:\n"
        "    substitute:\n"
        "      - placeholder: fakepw\n"
        "        secret: {env: ACCT1_PW}\n"
        "        into: {type: form, name: password}\n"
    ) is False


def test_empty_and_nonmapping_do_not_need_bw():
    assert _needs("block_unlisted: true\n") is False
    assert rules_bw.needs_bw({}) is False
    assert rules_bw.needs_bw(None) is False
    assert rules_bw.needs_bw([]) is False


def test_cli_exit_codes():
    with tempfile.TemporaryDirectory() as d:
        item = os.path.join(d, "item.yaml")
        env = os.path.join(d, "env.yaml")
        bad = os.path.join(d, "bad.yaml")
        with open(item, "w") as f:
            f.write("hosts:\n  h:\n    inject:\n      - secret: {item: A}\n        into: {type: header, name: X}\n")
        with open(env, "w") as f:
            f.write("hosts:\n  h:\n    inject:\n      - secret: {env: T}\n        into: {type: header, name: X}\n")
        with open(bad, "w") as f:
            f.write("hosts: [unclosed\n")  # YAML 構文エラー

        def rc(path):
            return subprocess.run(
                [sys.executable, os.path.join(HERE, "rules_bw.py"), path]
            ).returncode

        assert rc(item) == 0       # item 源 → 必要
        assert rc(env) == 1        # env のみ → 不要
        assert rc(bad) == 0        # parse 不能 → fail-closed (必要)
        assert rc("/no/such/file") == 0  # IO 不能 → fail-closed (必要)


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print("ok  rules_bw (%d tests)" % len(fns))


if __name__ == "__main__":
    _run_all()
