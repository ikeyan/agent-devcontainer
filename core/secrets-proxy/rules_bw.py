#!/usr/bin/env python3
"""rules.yaml が Vaultwarden(item) 源を1つでも使うか判定する。

secrets-proxy/entrypoint.sh が起動時に呼び、Vaultwarden の unlock を「item 源を使う rules の
ときだけ」要求するために使う。env/file だけ (または inject 無し) の rules は Vaultwarden を
必要としない (rules.schema.json / addon.py がそう謳う最小サンドボックス向けの源 = bw 不要) ので、
unlock を省略して proxy を起動できる。

bw 源の定義は addon.py の secret 解釈 (`"item" in secret` → ("bw", ...)) と一致させる。schema 上
secretRef は {item,...} / {env} / {file} の oneOf なので、"item" キーの有無で判定すれば十分。

依存は PyYAML のみ (mitmproxy 不要) なので、mitmproxy の無い `make check` でもテストできる。

CLI: `python rules_bw.py <rules.yaml>`
  exit 0 = Vaultwarden(item) 源を使う  → unlock 必要
  exit 1 = 使わない (env/file のみ等)  → unlock 不要
  parse/IO 不能 = 判定不能なので fail-closed で exit 0 (必要扱い)。
  → 呼び出し側は「rc==1 のときだけ省略、それ以外は要求」とすること (本ファイルの実行自体が
     失敗した rc>=2 も要求側に倒れる)。
"""
import sys

import yaml


def _injections(spec):
    """hostSpec から injection のリストを取り出す。

    hostSpec は {inject:[...], capture:[...], substitute:[...]} のオブジェクト / injection の
    配列 (短縮形) / null を取りうる (rules.schema.json の hostSpec oneOf)。capture は秘密源を
    持たない。substitute は別途 _substitute_secrets が扱う。
    """
    if isinstance(spec, list):
        return spec
    if isinstance(spec, dict):
        inj = spec.get("inject")
        return inj if isinstance(inj, list) else []
    return []


def _substitute_secrets(spec):
    """hostSpec の substitute エントリが参照する secret dict を列挙する。

    substitute は 2 形 (rules.schema.json substituteEntry):
      - name 形:        {name, secret:{item}, fields:[...]}        → secret は item のみ
      - placeholder 形: {placeholder, secret:secretRef, into}      → secret は item/env/file
    どちらも `secret` 1 つを持つので、その dict をそのまま返す (item 判定は呼び出し側)。
    """
    if not isinstance(spec, dict):
        return
    subs = spec.get("substitute")
    if not isinstance(subs, list):
        return
    for ent in subs:
        if isinstance(ent, dict) and isinstance(ent.get("secret"), dict):
            yield ent["secret"]


def needs_bw(cfg):
    """cfg (rules.yaml を読んだ dict) が Vaultwarden(item) 源を1つでも使うなら True。

    inject だけでなく substitute も Vaultwarden item を源に取りうる (name 形は常に item)。
    substitute-only の rules でも unlock を要求しないと、起動時の warm で placeholder が
    本物に解決されないまま流れる (fail-open) ので、両方を走査する。
    """
    if not isinstance(cfg, dict):
        return False
    hosts = cfg.get("hosts")
    if not isinstance(hosts, dict):
        return False
    for spec in hosts.values():
        for inj in _injections(spec):
            if not isinstance(inj, dict):
                continue
            sec = inj.get("secret")
            if isinstance(sec, dict) and "item" in sec:
                return True
        for sec in _substitute_secrets(spec):
            if "item" in sec:
                return True
    return False


def main(argv):
    if len(argv) != 2:
        sys.stderr.write("usage: rules_bw.py <rules.yaml>\n")
        return 0  # 引数不正も fail-closed (必要扱い)
    try:
        with open(argv[1]) as f:
            cfg = yaml.safe_load(f) or {}
    except Exception as e:  # parse / IO 不能 = 判定不能 → fail-closed
        sys.stderr.write("rules_bw: rules を解析できません (%s) → bw 必要扱い\n" % e)
        return 0
    return 0 if needs_bw(cfg) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
