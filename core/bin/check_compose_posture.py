#!/usr/bin/env python3
"""マージ済み compose config のセキュリティ姿勢を pin する (redact_invariants_check.sh §10)。

`docker/podman compose -f ../project/compose.yaml -f compose.yaml config` の出力 (stdin) を読み、
「マージ結果そのもの」の不変条件を assert する。なぜ config を直接見るか: 複数 -f の「後勝ち」で
core が勝つのは core が明示宣言したスカラーだけで、list (cap_add/networks/volumes) は追記マージ、
core 未宣言キーは project 値が通る (docs/verified-facts/docker.md「compose 複数 -f」)。よって project 層の
編集で崩れうる姿勢は、後勝ちの一般則でなくマージ結果を直接検査して固定する。

pin する条件 (§10):
  A. project 名が呼び出し側の指定 (= project/compose.yaml 自身が宣言する name:) と一致する。消えると
     fallback で 'project' (project/compose.yaml のディレクトリ basename) 等に化け、全 named volume が
     別 namespace に切り替わり既存データから切り離される
     (docs/verified-facts/devcontainers-cli.md「project 名の fallback」)。kit 化により project 名は
     consumer ごとに変わる (@@PROJECT_NAME@@) ため、リテラル固定値でなく呼び出し側 (redact_invariants_check.sh
     が project/compose.yaml から読んだ実際の name:) と突き合わせる。
  B. dev の cap_add がちょうど {NET_ADMIN, NET_RAW, SETUID, SETGID}。project 層が cap を追記して
     権限を広げていないこと (list は追記マージなので後勝ちで守れない)。
  C. dev に privileged: true が無い。
  D. dev の networks が devnet のみ (proxy-upstream に居ない = dev に v6 egress 経路が無い)。
     devnet は core/compose.yaml が宣言する固定のネットワーク名 (project 名とは無関係) なので
     consumer が変わっても値は変化しない。
  E. proxy-confdir / proxy-bw volume が secrets-proxy 以外のサービスに mount されない
     (CA 秘密鍵 / BW_SESSION が dev 等へ漏れない)。
  F. どのサービスにも docker.sock の bind が無い (docker socket 奪取での隔離破壊を封じる)。

--selftest: 各条件について「違反を注入したコピー」を作り、対応する検出器が実際に弾くことを確認する
(negative probe)。検出漏れがあれば exit 1。

CLI: `check_compose_posture.py <expected-project-name> [--selftest]` (stdin = compose config)。
"""
from __future__ import annotations

import copy
import functools
import sys

import yaml

EXPECTED_DEV_CAPS = {"NET_ADMIN", "NET_RAW", "SETUID", "SETGID"}
SECRET_VOLUMES = {"proxy-confdir", "proxy-bw"}
DOCKER_SOCK_NAMES = ("docker.sock",)


def _vol_source(v: object) -> str | None:
    if isinstance(v, dict):
        s = v.get("source")
        return s if isinstance(s, str) else None
    if isinstance(v, str):
        return v.split(":", 1)[0]
    return None


def _vol_is_bind(v: object) -> bool:
    if isinstance(v, dict):
        return v.get("type") == "bind"
    if isinstance(v, str):
        src = v.split(":", 1)[0]
        return src.startswith("/") or src.startswith(".")
    return False


def _net_keys(nets: object) -> set[str]:
    if isinstance(nets, dict):
        return set(nets.keys())
    if isinstance(nets, list):
        return set(nets)
    return set()


def check_name(cfg: dict, expected_name: str) -> list[str]:
    name = cfg.get("name")
    if name != expected_name:
        return [f"A: project 名が {expected_name!r} でない (実際: {name!r})。project/compose.yaml の name: が消えた/変わった可能性"]
    return []


def check_dev_caps(cfg: dict) -> list[str]:
    dev = (cfg.get("services") or {}).get("dev") or {}
    caps = set(dev.get("cap_add") or [])
    if caps != EXPECTED_DEV_CAPS:
        return [f"B: dev の cap_add が {sorted(EXPECTED_DEV_CAPS)} でない (実際: {sorted(caps)})"]
    return []


def check_dev_not_privileged(cfg: dict) -> list[str]:
    dev = (cfg.get("services") or {}).get("dev") or {}
    if dev.get("privileged"):
        return ["C: dev に privileged: true がある"]
    return []


def check_dev_networks(cfg: dict) -> list[str]:
    dev = (cfg.get("services") or {}).get("dev") or {}
    nets = _net_keys(dev.get("networks"))
    if nets != {"devnet"}:
        return [f"D: dev の networks が {{devnet}} でない (実際: {sorted(nets)})。proxy-upstream 所属は v6 経路を開く"]
    return []


def check_secret_volumes_isolated(cfg: dict) -> list[str]:
    errs: list[str] = []
    for name, svc in (cfg.get("services") or {}).items():
        if name == "secrets-proxy" or not isinstance(svc, dict):
            continue
        for v in svc.get("volumes") or []:
            src = _vol_source(v)
            if src in SECRET_VOLUMES:
                errs.append(f"E: secrets-proxy 専用 volume '{src}' が service '{name}' に mount されている")
    return errs


def check_no_docker_sock(cfg: dict) -> list[str]:
    errs: list[str] = []
    for name, svc in (cfg.get("services") or {}).items():
        if not isinstance(svc, dict):
            continue
        for v in svc.get("volumes") or []:
            if not _vol_is_bind(v):
                continue
            src = _vol_source(v) or ""
            if any(src.rstrip("/").endswith(n) for n in DOCKER_SOCK_NAMES):
                errs.append(f"F: service '{name}' が docker socket を bind している: {src}")
    return errs


def _build_checks(expected_name: str) -> list[tuple[str, "object"]]:
    # "name" だけ expected_name を要るので functools.partial で束縛し、他の check_* と同じ
    # `fn(cfg) -> list[str]` 形に揃える (validate/selftest からは一様に呼べる)。
    return [
        ("name", functools.partial(check_name, expected_name=expected_name)),
        ("dev_caps", check_dev_caps),
        ("dev_privileged", check_dev_not_privileged),
        ("dev_networks", check_dev_networks),
        ("secret_volumes", check_secret_volumes_isolated),
        ("docker_sock", check_no_docker_sock),
    ]


def _mut_name(cfg: dict, expected_name: str) -> None:
    # expected_name が何であっても確実に不一致にする (project 名が偶然 "project" 等の
    # fallback 値そのものを選んでいても negative probe が意味を保つように)。
    cfg["name"] = f"{expected_name}-mutated-by-selftest"


def _mut_caps(cfg: dict) -> None:
    cfg["services"]["dev"].setdefault("cap_add", []).append("SYS_ADMIN")


def _mut_privileged(cfg: dict) -> None:
    cfg["services"]["dev"]["privileged"] = True


def _mut_networks(cfg: dict) -> None:
    nets = cfg["services"]["dev"].get("networks")
    if isinstance(nets, dict):
        nets["proxy-upstream"] = None
    else:
        cfg["services"]["dev"]["networks"] = {"devnet": None, "proxy-upstream": None}


def _mut_secret_volume(cfg: dict) -> None:
    cfg["services"]["dev"].setdefault("volumes", []).append(
        {"type": "volume", "source": "proxy-bw", "target": "/steal"}
    )


def _mut_docker_sock(cfg: dict) -> None:
    cfg["services"]["dev"].setdefault("volumes", []).append(
        {"type": "bind", "source": "/var/run/docker.sock", "target": "/var/run/docker.sock"}
    )


def _build_mutators(expected_name: str) -> dict[str, "object"]:
    return {
        "name": functools.partial(_mut_name, expected_name=expected_name),
        "dev_caps": _mut_caps,
        "dev_privileged": _mut_privileged,
        "dev_networks": _mut_networks,
        "secret_volumes": _mut_secret_volume,
        "docker_sock": _mut_docker_sock,
    }


def validate(cfg: dict, checks: list) -> list[str]:
    errs: list[str] = []
    for _name, fn in checks:
        errs.extend(fn(cfg))
    return errs


def selftest(cfg: dict, checks: list, mutators: dict) -> list[str]:
    """各条件について違反を注入したコピーを検出器が弾くことを確認 (negative probe)。"""
    problems: list[str] = []
    check_by_name = dict(checks)
    for name, mutate in mutators.items():
        mutated = copy.deepcopy(cfg)
        mutate(mutated)
        if not check_by_name[name](mutated):
            problems.append(f"negative: 検出器 '{name}' が注入した違反を弾かない")
    return problems


def main() -> int:
    argv = sys.argv[1:]
    selftest_mode = "--selftest" in argv
    positional = [a for a in argv if a != "--selftest"]
    if len(positional) != 1:
        print("usage: check_compose_posture.py <expected-project-name> [--selftest]", file=sys.stderr)
        return 2
    expected_name = positional[0]

    cfg = yaml.safe_load(sys.stdin)
    if not isinstance(cfg, dict):
        print("check_compose_posture: stdin が compose config の mapping でない", file=sys.stderr)
        return 1

    checks = _build_checks(expected_name)
    errs = validate(cfg, checks)
    if selftest_mode:
        errs = errs + selftest(cfg, checks, _build_mutators(expected_name))

    if errs:
        for e in errs:
            print(f"NG: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
