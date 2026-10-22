#!/usr/bin/env python3
"""マージ済み compose config のセキュリティ姿勢を pin する (redact_invariants_check.sh §10)。

`docker/podman compose -f ../project/compose.yaml -f compose.yaml config` の出力 (stdin) を読み、
「マージ結果そのもの」の不変条件を assert する。なぜ config を直接見るか: 複数 -f の「後勝ち」で
core が勝つのは core が明示宣言したスカラーだけで、list (cap_add/networks/volumes) は追記マージ、
core 未宣言キーは project 値が通る (canon: facts/docker/compose-project-directory-and-env-resolution)。よって project 層の
編集で崩れうる姿勢は、後勝ちの一般則でなくマージ結果を直接検査して固定する。

pin する条件 (§10):
  A. project 名が呼び出し側の指定 (= project/compose.yaml 自身が宣言する name:) と一致する。消えると
     fallback で 'project' (project/compose.yaml のディレクトリ basename) 等に化け、全 named volume が
     別 namespace に切り替わり既存データから切り離される
     (canon: facts/devcontainer/compose-project-name-fallback)。kit 化により project 名は
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
  F. どのサービスにも container engine の API socket (docker / podman / containerd / cri-o / buildkit) の
     bind が無い (socket 奪取での隔離破壊を封じる)。socket 名の一致 (任意の場所 — macOS の podman machine は
     podman-machine-default-api.sock を一時 dir に置く) と、既定 socket パスの祖先 dir (/run, /var/run,
     /run/podman, /run/user/<uid> 等 — dir ごと bind すれば中の socket に届く) の両方で見る。既定パスの
     出典: podman-system-service(1) (canon: facts/podman/system-service-default-socket-address)、
     docker / containerd / cri-o / buildkit の各 daemon 既定。
  G. security_opt: (1) dev / secrets-proxy の security_opt が core 宣言とちょうど一致する (追加値を
     許さない)。security_opt は sequence なので複数 -f で追記マージされ、project 層の
     `seccomp=unconfined` 1 行で core の tailored seccomp profile が黙って無効化される
     (compose は warning を出さない)。(2) どのサービスにも、core が宣言していない緩和値
     (seccomp/apparmor=unconfined、label=disable / type:spc_t、systempaths=unconfined、podman の
     unmask=、writable-cgroups) が無い。区切りは `=` と旧形式 `:` の両方を解釈する
     (moby daemon/daemon_unix.go parseSecurityOpt)。

--selftest: 各条件について「違反を注入したコピー」を作り、対応する検出器が実際に弾くことを確認する
(negative probe)。検出漏れがあれば exit 1。

CLI: `check_compose_posture.py <expected-project-name> [--selftest]` (stdin = compose config)。
"""
from __future__ import annotations

import copy
import fnmatch
import functools
import re
import sys

import yaml

EXPECTED_DEV_CAPS = {"NET_ADMIN", "NET_RAW", "SETUID", "SETGID"}
SECRET_VOLUMES = {"proxy-confdir", "proxy-bw"}
# F: engine socket の basename (任意の場所)。docker.sock / podman.sock / containerd.sock / crio.sock /
# buildkitd.sock / cri-dockerd.sock と、podman machine の podman-machine-default-api.sock 等の派生名。
ENGINE_SOCK_NAME = re.compile(r"^(?:docker|podman|containerd|crio|buildkitd|cri-dockerd)(?:[-.][^/]*)?\.sock$")
# F: 各 engine の既定 socket パス (path component ごとに fnmatch)。bind source がこれらの祖先 (or 一致)
# なら socket に届く。
ENGINE_SOCK_PATHS = (
    "/var/run/docker.sock",
    "/run/docker.sock",
    "/run/user/*/docker.sock",
    "/run/podman/podman.sock",
    "/var/run/podman/podman.sock",
    "/run/user/*/podman/podman.sock",
    "/run/containerd/containerd.sock",
    "/var/run/crio/crio.sock",
    "/run/buildkit/buildkitd.sock",
)
# G(1): core/compose.yaml が宣言する security_opt (マージ結果がこれとちょうど一致すること)。
EXPECTED_SECURITY_OPT = {
    "dev": ["seccomp=../core/podman/seccomp.json", "systempaths=unconfined"],
    "secrets-proxy": [],
}


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


def _reaches_engine_sock(src: str) -> bool:
    parts = [p for p in src.split("/") if p not in ("", ".")]
    if parts and ENGINE_SOCK_NAME.match(parts[-1]):
        return True
    if not src.startswith("/"):
        return False
    for sock in ENGINE_SOCK_PATHS:
        sp = [p for p in sock.split("/") if p]
        if len(parts) <= len(sp) and all(fnmatch.fnmatchcase(a, b) for a, b in zip(parts, sp)):
            return True
    return False


def check_no_engine_sock(cfg: dict) -> list[str]:
    errs: list[str] = []
    for name, svc in (cfg.get("services") or {}).items():
        if not isinstance(svc, dict):
            continue
        for v in svc.get("volumes") or []:
            if not _vol_is_bind(v):
                continue
            src = _vol_source(v) or ""
            if _reaches_engine_sock(src):
                errs.append(f"F: service '{name}' が container engine の socket (またはその祖先 dir) を bind している: {src}")
    return errs


def _norm_security_opt(opt: str) -> tuple[str, str]:
    # moby parseSecurityOpt と同じ順: bare 語 → '=' 区切り → 旧 ':' 区切り。
    if "=" in opt:
        k, v = opt.split("=", 1)
    elif ":" in opt:
        k, v = opt.split(":", 1)
    else:
        k, v = opt, ""
    return k.strip().lower(), v.strip().lower()


def _is_relaxing(opt: str) -> bool:
    k, v = _norm_security_opt(opt)
    if k == "disable" and v == "":
        return True  # bare `disable` = label=disable (moby)
    if k in ("seccomp", "apparmor", "systempaths") and v == "unconfined":
        return True
    if k == "label" and (v == "disable" or v.replace(":", "=") == "type=spc_t"):
        return True
    if k == "unmask":
        return True  # podman: /proc の既定マスクを外す (systempaths=unconfined 相当)
    if k == "writable-cgroups" and v in ("", "true", "1"):
        return True
    return False


def check_security_opt(cfg: dict) -> list[str]:
    errs: list[str] = []
    services = cfg.get("services") or {}
    for name, expected in EXPECTED_SECURITY_OPT.items():
        svc = services.get(name)
        actual = list((svc or {}).get("security_opt") or []) if isinstance(svc, dict) else None
        if actual is None or sorted(actual) != sorted(expected):
            errs.append(f"G: {name} の security_opt が core 宣言 {expected} とちょうど一致しない (実際: {actual})。"
                        "sequence は追記マージなので project 層の値が core の profile を上書きできる")
    for name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        allowed = EXPECTED_SECURITY_OPT.get(name, [])
        for opt in svc.get("security_opt") or []:
            if isinstance(opt, str) and opt not in allowed and _is_relaxing(opt):
                errs.append(f"G: service '{name}' の security_opt に隔離の緩和値がある: {opt}")
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
        ("engine_sock", check_no_engine_sock),
        ("security_opt", check_security_opt),
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


def _mut_bind(source: str):
    def mut(cfg: dict) -> None:
        cfg["services"]["dev"].setdefault("volumes", []).append(
            {"type": "bind", "source": source, "target": "/probe"}
        )
    return mut


def _mut_security_opt(service: str, opt: str):
    def mut(cfg: dict) -> None:
        svc = cfg["services"].setdefault(service, {})
        svc.setdefault("security_opt", []).insert(0, opt)  # project 層 (先の -f) の追記と同じ位置
    return mut


def _build_mutators(expected_name: str) -> dict[str, list]:
    # 検出器ごとに 1 つ以上の違反注入。F/G は検出の経路 (名前一致 / 祖先 dir / 完全一致 / 緩和値) ごとに置く。
    return {
        "name": [functools.partial(_mut_name, expected_name=expected_name)],
        "dev_caps": [_mut_caps],
        "dev_privileged": [_mut_privileged],
        "dev_networks": [_mut_networks],
        "secret_volumes": [_mut_secret_volume],
        "engine_sock": [
            _mut_bind("/var/run/docker.sock"),
            _mut_bind("/run/podman/podman.sock"),
            _mut_bind("/run/user/1000/podman/podman.sock"),
            _mut_bind("/var/folders/xx/T/podman/podman-machine-default-api.sock"),
            _mut_bind("/run/containerd/containerd.sock"),
            _mut_bind("/var/run"),
            _mut_bind("/run/user/1000"),
            _mut_bind("/"),
        ],
        "security_opt": [
            _mut_security_opt("dev", "seccomp=unconfined"),
            _mut_security_opt("dev", "seccomp:unconfined"),
            _mut_security_opt("dev", "seccomp=../project/weak.json"),
            _mut_security_opt("secrets-proxy", "apparmor=unconfined"),
            _mut_security_opt("secrets-proxy", "systempaths=unconfined"),
            _mut_security_opt("extra", "label=disable"),
            _mut_security_opt("extra", "disable"),
            _mut_security_opt("extra", "unmask=ALL"),
        ],
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
    for name, muts in mutators.items():
        for i, mutate in enumerate(muts):
            mutated = copy.deepcopy(cfg)
            mutate(mutated)
            if not check_by_name[name](mutated):
                problems.append(f"negative: 検出器 '{name}' が注入した違反 #{i} を弾かない")
    # 誤検出側: 正当な bind (workspace / 無関係な .sock) を F が弾かないこと。
    for src in ("/workspace", "/home/u/proj", "/run/host-services/ssh-auth.sock", "/var/run/dbus/system_bus_socket"):
        if _reaches_engine_sock(src):
            problems.append(f"negative: 検出器 'engine_sock' が正当な bind を誤検出: {src}")
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
