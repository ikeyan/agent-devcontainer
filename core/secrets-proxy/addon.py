"""mitmproxy addon: 秘密情報の注入・捕獲(トークン化)・危険エンドポイントの遮断。

セキュリティモデル
------------------
dev コンテナは「侵害される前提」。秘密の実体 (静的: Vaultwarden のパスワード/トークン、
動的: ログインで得るクッキーやアクセストークン) を dev に渡さない。

3 つの機構:

1) inject  — 静的な秘密 (Vaultwarden) を宛先ホストごとにリクエストへ注入する。
2) capture — 応答からクッキーやトークンを捕獲して proxy に保管し、dev には
             プレースホルダ (代替トークン) に置換して返す。以降 dev がその
             プレースホルダを送れば、proxy が本物に戻して上流へ送る。
             これでパスワードも、それで得た本物のクッキー/トークンも dev に渡らない。
3) block   — 危険なエンドポイント (ホスト/メソッド/パス正規表現) を 403 で遮断する。

不変条件 (アンチ持ち出し):
- 応答では「本物の値」の全出現をプレースホルダへ置換してから dev に返す
  → 本物の値が dev 側に存在しえない。
- 捕獲した秘密は「束縛ホスト (bind)」宛のリクエストにしか復元注入しない
  → 別ホストへ持ち出す経路が無い。
- リクエストに本物の値そのものが現れたら即ブロック (detect_leaks)。dev は本物を
  持っていないはずなので、現れた時点で漏洩 = 異常。
"""

import base64
import json
import logging
import os
import re
import secrets as pysecrets
import subprocess
from string import Template

import yaml
from mitmproxy import ctx, http

log = logging.getLogger("secrets-proxy")

RULES_PATH = os.environ.get("SECRETS_PROXY_RULES", "/config/rules.yaml")

# 本文を文字列として安全に書き換えてよい Content-Type
_TEXTUAL = (
    "text/", "application/json", "application/javascript", "application/xml",
    "application/x-www-form-urlencoded", "+json", "+xml",
)


def _is_textual(ct: str) -> bool:
    ct = (ct or "").lower()
    return any(t in ct for t in _TEXTUAL)


def _host_to_regex(host: str) -> str:
    """"example.com" -> 完全一致(+任意ポート)、"*.example.com" -> サブドメイン一致。"""
    host = host.lower()
    if host.startswith("*."):
        # サブドメインは必須 ("*.x.com" は a.x.com に一致、apex の x.com には一致しない)
        base = re.escape(host[2:])
        return rf"^(.+\.){base}(:\d+)?$"
    return rf"^{re.escape(host)}(:\d+)?$"


def _apply_encode(value: str, encode):
    """into.encode に従い、載せる前の値をエンコードする。encode 省略時は素通し。"""
    if encode is None:
        return value
    if encode == "base64":
        return base64.b64encode(value.encode("utf-8")).decode("ascii")
    raise RuntimeError(f"unknown encode: {encode!r}")


def _json_path(data, path: str):
    """ドット記法の簡易 JSON パス。例: "access_token" / "data.token" / "items.0.v"。"""
    cur = data
    for part in path.split("."):
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
        if cur is None:
            return None
    if isinstance(cur, str):
        return cur
    return json.dumps(cur, separators=(",", ":"))


def _safe_path(path: str) -> str:
    """観測/ブロックログ用に request path から秘密を落とす。secret 流出を防ぐ proxy 自身が log 経由で
    秘密を漏らさないため。query は丸ごと除去し (署名・token が載りうる)、path segment のうち token 状の
    もの — 16 文字以上、または 8 文字以上で英字と数字が混在 (webhook 鍵・API key・signed id 等) — を
    `<redacted>` に伏せる。短い route 名 (api/v1/users 等) は残し discovery/監査の手掛かりを保つ。
    保証: query は必ず除去。segment の秘匿は best-effort (長さ+文字種 heuristic; 低エントロピーな短い
    秘密は伏せきれない — 既知の捕獲済み秘密を運ぶ request 自体は _scan_leak が別途遮断する)。"""
    base = path.split("?", 1)[0]
    return "/".join(
        "<redacted>"
        if len(s) >= 16 or (len(s) >= 8 and re.search(r"[A-Za-z]", s) and re.search(r"\d", s))
        else s
        for s in base.split("/")
    )


class SecretsProxy:
    def __init__(self):
        # inject (静的注入)
        self.inject = {}            # host(lower) -> [injection]
        self.inject_wild = []       # [(regex, [injection])]
        # capture (応答からの捕獲 = トークン化)
        self.capture = {}           # host(lower) -> [capture]
        self.capture_wild = []      # [(regex, [capture])]
        # substitute (プレースホルダ -> 秘密の解決 / pre-seed)
        self.substitute = {}        # host(lower) -> [spec]
        self.substitute_wild = []   # [(regex, [spec])]
        # その他のポリシー
        self.allow_hosts = []       # [regex] 注入なしで通す
        self.passthrough = []       # [regex str] MITM しない
        self.block_rules = []       # [dict]
        self.block_unlisted = True
        self.detect_leaks = True
        # 観測モード (allowlist 割り出し用)。既定 off = dev/redact の挙動は一切変えない。rules ではなく
        # 運用フラグなので RULES_PATH と同じく env で渡す。記録内容と位置は request() を参照。
        self.log_requests = os.environ.get(
            "SECRETS_PROXY_LOG_REQUESTS", "").strip().lower() in ("1", "true", "yes", "on")

        self._static = {}           # (item, field) -> value  (Vaultwarden キャッシュ)
        self.salt = pysecrets.token_hex(4)   # プレースホルダの予測困難化 (起動ごと)
        self.cap_store = {}         # key -> {"value", "ph", "bind":[regex]}

    # ===================== 設定読み込み =====================
    def load(self, loader):
        with open(RULES_PATH) as f:
            cfg = yaml.safe_load(f) or {}

        self.block_unlisted = bool(cfg.get("block_unlisted", True))
        self.detect_leaks = bool(cfg.get("detect_leaks", True))

        for host, spec in (cfg.get("hosts") or {}).items():
            host_l = host.lower()
            injs, caps, subs = self._parse_host_spec(host_l, spec)
            if host_l.startswith("*."):
                rx = re.compile(_host_to_regex(host_l))
                if injs:
                    self.inject_wild.append((rx, injs))
                if caps:
                    self.capture_wild.append((rx, caps))
                if subs:
                    self.substitute_wild.append((rx, subs))
            else:
                if injs:
                    self.inject[host_l] = injs
                if caps:
                    self.capture[host_l] = caps
                if subs:
                    self.substitute[host_l] = subs

        self.allow_hosts = [re.compile(_host_to_regex(h)) for h in (cfg.get("allow_hosts") or [])]
        self.passthrough = [_host_to_regex(h) for h in (cfg.get("passthrough_hosts") or [])]
        self.block_rules = [self._parse_block(b) for b in (cfg.get("block") or [])]

        self._assert_unique_stores()

        log.info(
            "secrets-proxy: inject=%d(+%d wild) capture=%d(+%d wild) "
            "substitute=%d(+%d wild) "
            "allow=%d passthrough=%d block=%d block_unlisted=%s detect_leaks=%s",
            len(self.inject), len(self.inject_wild), len(self.capture),
            len(self.capture_wild), len(self.substitute), len(self.substitute_wild),
            len(self.allow_hosts), len(self.passthrough),
            len(self.block_rules), self.block_unlisted, self.detect_leaks,
        )

    def _assert_unique_stores(self):
        """capture(store) と substitute(subst:ph) は cap_store の同一キースペースに載るため、
        重複すると後勝ちで別の秘密が静かに混ざる。load 時に fail-closed で弾く。"""
        seen = set()

        def claim(key, what):
            if key in seen:
                raise RuntimeError(f"cap_store キー衝突: {what} が {key!r} を重複使用 (一意にすること)")
            seen.add(key)

        for caps in [*self.capture.values(), *(v for _, v in self.capture_wild)]:
            for cap in caps:
                claim(cap["store"], f"capture store {cap['store']!r}")
        for specs in [*self.substitute.values(), *(v for _, v in self.substitute_wild)]:
            for sp_ in specs:
                claim(f"subst:{sp_['ph']}", f"substitute placeholder {sp_['ph']!r}")

    def _parse_host_spec(self, host_l, spec):
        # 後方互換: host の値がリストなら inject のみ
        if spec is None:
            return [], [], []
        if isinstance(spec, list):
            return [self._norm_injection(i) for i in spec], [], []
        injs = [self._norm_injection(i) for i in (spec.get("inject") or [])]
        caps = [self._norm_capture(c) for c in (spec.get("capture") or [])]
        subs = []
        for s in (spec.get("substitute") or []):
            subs.extend(self._norm_substitute(host_l, s))
        return injs, caps, subs

    @staticmethod
    def _norm_secret_source(secret):
        """secret 参照を (kind, *args) のソース記述子へ正規化する (cache key 兼用)。
          Vaultwarden : ("bw", item, field)
          環境変数     : ("env", varname)   — bw 不要
          ファイル     : ("file", path)     — bw 不要 (中身を strip して使う)
        env/file は Vaultwarden を持たない最小サンドボックス (redact 等) で、注入する
        トークンを Vaultwarden 無しに渡すための源。"""
        if "item" in secret:
            return ("bw", secret["item"], secret.get("field", "password"))
        if "env" in secret:
            return ("env", secret["env"])
        if "file" in secret:
            return ("file", secret["file"])
        raise RuntimeError(f"inject secret: item/env/file のいずれかが必要: {secret!r}")

    @classmethod
    def _norm_injection(cls, inj):
        secret = inj["secret"]
        target = inj["into"]
        # match (任意): 同一ホストに複数エンドポイントがある場合に注入先を絞る。
        # 省略時はそのホスト宛の全リクエストに注入する。秘密を不要なエンドポイントへ
        # 撒かないため、ホストを共有する API では path/method で絞ること。
        match = inj.get("match") or {}
        methods = match.get("methods")
        return {
            "source": cls._norm_secret_source(secret),
            "type": target["type"],          # header | cookie | query | form | json
            "name": target.get("name"),
            "value_tmpl": target.get("value", "${secret}"),
            "encode": target.get("encode"),  # None | "base64"
            "match_path_rx": re.compile(match["path"]) if match.get("path") else None,
            "match_methods": {m.upper() for m in methods} if methods else None,
        }

    @staticmethod
    def _norm_into(target):
        """capture/substitute の復元先 into を {type, name, encode} に正規化する (必須)。"""
        if not target or "type" not in target or "name" not in target:
            raise RuntimeError(f"into は必須で type/name が要る: {target!r}")
        return {"type": target["type"], "name": target["name"], "encode": target.get("encode")}

    @classmethod
    def _norm_substitute(cls, host_l, s):
        """substitute 1 エントリを spec のリストへ正規化する。
        name 形は fields[] ごとに 1 spec (placeholder=@@SECRET_<name>_<field>@@) へ展開。
        spec: {"ph", "source", "bind_rx", "into"}。
        bind 省略時は宣言ホスト host_l に束縛する (空 bind を「全ホスト一致」にせず exfil を防ぐ。
        capture が捕獲元ホストへ既定束縛するのと対称)。"""
        bind_rx = [re.compile(_host_to_regex(h)) for h in (s.get("bind") or [])] \
            or [re.compile(_host_to_regex(host_l))]
        if "name" in s:
            item = s["secret"]["item"]
            out = []
            for fe in s["fields"]:
                field = fe["field"]
                out.append({
                    "ph": f"@@SECRET_{s['name']}_{field}@@",
                    "source": ("bw", item, field),
                    "bind_rx": bind_rx,
                    "into": cls._norm_into(fe["into"]),
                })
            return out
        return [{
            "ph": s["placeholder"],
            "source": cls._norm_secret_source(s["secret"]),
            "bind_rx": bind_rx,
            "into": cls._norm_into(s["into"]),
        }]

    @classmethod
    def _norm_capture(cls, c):
        src = c["from"]                      # {type, name} (into と同形)
        if not isinstance(src, dict) or "type" not in src or "name" not in src:
            raise RuntimeError(f"capture.from は {{type, name}} が要る: {src!r}")
        into = cls._norm_into(c.get("into"))
        # capture の復元値はサーバ発行の本物をそのまま戻すため encode は無意味
        # (encode すると応答 redaction が本物を取りこぼす)。明示指定は fail-closed で弾く。
        if into.get("encode") is not None:
            raise RuntimeError("capture.into は encode 非対応 (inject/substitute の into で使う)")
        return {
            "from": src["type"],             # set-cookie | header | json | regex
            "name": src["name"],             # cookie/header 名 | json パス | 正規表現
            "store": c["store"],             # 保管キー = プレースホルダ名
            "bind_rx": [re.compile(_host_to_regex(h)) for h in (c.get("bind") or [])],
            "into": into,
        }

    @staticmethod
    def _parse_block(b):
        methods = b.get("methods")
        return {
            "host_rx": re.compile(_host_to_regex(b["host"])) if b.get("host") else None,
            "methods": {m.upper() for m in methods} if methods else None,
            "path_rx": re.compile(b["path"]) if b.get("path") else None,
            "reason": b.get("reason", "blocked by policy"),
        }

    # ===================== 静的秘密 (Vaultwarden) =====================
    def _bw_get_item(self, ref):
        out = subprocess.run(
            ["bw", "get", "item", ref, "--raw", "--nointeraction"],
            capture_output=True, text=True, env=os.environ,
        )
        if out.returncode != 0:
            raise RuntimeError(f"bw get item {ref!r} failed: {out.stderr.strip()}")
        return json.loads(out.stdout)

    @staticmethod
    def _extract(item, field):
        login = item.get("login") or {}
        if field == "password":
            return login.get("password")
        if field == "username":
            return login.get("username")
        if field == "notes":
            return item.get("notes")
        if field == "uri":
            uris = login.get("uris") or []
            return uris[0]["uri"] if uris else None
        name = field[len("fields."):] if field.startswith("fields.") else field
        for f in item.get("fields") or []:
            if f.get("name") == name:
                return f.get("value")
        return None

    def _resolve_source(self, source):
        """ソース記述子 (kind, *args) を実値へ解決する。env/file は Vaultwarden 非依存。
        結果は source をキーにキャッシュする。"""
        if source in self._static:
            return self._static[source]
        kind = source[0]
        if kind == "bw":
            _, item_ref, field = source
            if field == "totp":
                out = subprocess.run(
                    ["bw", "get", "totp", item_ref, "--raw", "--nointeraction"],
                    capture_output=True, text=True, env=os.environ,
                )
                value = out.stdout.strip() if out.returncode == 0 else None
            else:
                value = self._extract(self._bw_get_item(item_ref), field)
        elif kind == "env":
            value = os.environ.get(source[1])
        elif kind == "file":
            try:
                with open(source[1]) as f:
                    value = f.read().strip()
            except OSError as e:
                raise RuntimeError(f"secret file 読み取り失敗: {source[1]!r}: {e}")
        else:
            raise RuntimeError(f"unknown secret source: {source!r}")
        if not value:
            raise RuntimeError(f"secret not found: {source!r}")
        self._static[source] = value
        return value

    # ===================== 起動時 =====================
    def running(self):
        if self.passthrough:
            ctx.options.update(ignore_hosts=self.passthrough)
        warmed = failed = 0
        for injs in [*self.inject.values(), *(v for _, v in self.inject_wild)]:
            for inj in injs:
                src = inj["source"]
                if src[0] == "bw" and src[2] == "totp":
                    continue
                try:
                    self._resolve_source(src)
                    warmed += 1
                except Exception as e:
                    failed += 1
                    # warning であって error ではない: 暖機は単なる事前キャッシュで、
                    # 解決できない秘密は request 時に 502 で表面化する。ここで error を
                    # 吐くと mitmproxy の ErrorCheck が「起動時エラー」と見なして
                    # mitmdump を即終了させてしまう ("Errors logged during startup")。
                    log.warning("secrets-proxy: warm failed %r: %s", src, e)
        for specs in [*self.substitute.values(), *(v for _, v in self.substitute_wild)]:
            for sp_ in specs:
                if sp_["source"][0] == "bw" and sp_["source"][2] == "totp":
                    log.warning(
                        "secrets-proxy: substitute %s は totp 非対応 (静的 pre-seed のため) — skip",
                        sp_["ph"],
                    )
                    continue
                try:
                    # encode は warm 時に値へ焼き込む。以降 subst(置換)/redact(応答)/leak(検知) は
                    # すべて「on-wire の形」(= encode 済み) を使うので取りこぼしが無い。
                    val = _apply_encode(self._resolve_source(sp_["source"]), sp_["into"].get("encode"))
                    self.cap_store[f"subst:{sp_['ph']}"] = {
                        "value": val, "ph": sp_["ph"],
                        "bind": sp_["bind_rx"], "into": sp_["into"],
                    }
                    warmed += 1
                except Exception as e:
                    failed += 1
                    # 解決失敗は fail-closed: unresolved の sentinel を残し、その placeholder が
                    # 宣言場所に現れたリクエストは 502 で遮断する (placeholder が fake のまま
                    # 上流へ素通りするのを防ぐ)。substitute は request 時でなく warm 時に解決
                    # するので、ここで沈黙すると素通り = fail-open になってしまう。
                    self.cap_store[f"subst:{sp_['ph']}"] = {
                        "value": None, "ph": sp_["ph"],
                        "bind": sp_["bind_rx"], "into": sp_["into"], "unresolved": True,
                    }
                    log.warning("secrets-proxy: substitute warm failed %r: %s", sp_["ph"], e)
        msg = "secrets-proxy: warmed %d static secret(s), %d failure(s)"
        (log.warning if failed else log.info)(msg, warmed, failed)

    # ===================== ルックアップ =====================
    def _match_inject(self, host):
        host = host.lower()
        if host in self.inject:
            return self.inject[host]
        for rx, v in self.inject_wild:
            if rx.match(host):
                return v
        return None

    def _match_capture(self, host):
        host = host.lower()
        out = list(self.capture.get(host, []))
        for rx, v in self.capture_wild:
            if rx.match(host):
                out += v
        return out

    def _match_substitute(self, host):
        host = host.lower()
        out = list(self.substitute.get(host, []))
        for rx, v in self.substitute_wild:
            if rx.match(host):
                out += v
        return out

    def _is_allowed(self, host):
        if self._match_inject(host) is not None:
            return True
        if self._match_capture(host):
            return True
        if self._match_substitute(host):
            return True
        h = host.lower()
        return any(rx.match(h) for rx in self.allow_hosts)

    # ===================== リクエスト =====================
    def request(self, flow: http.HTTPFlow):
        req = flow.request
        host = req.pretty_host

        # 0) 観測ログ。block より前に置き、許可も拒否も全部記録する (allowlist 割り出し +
        #    監査)。path/query に秘密が載りうるので _safe_path で query 除去 + token 状 segment を伏せる。
        if self.log_requests:
            log.info("secrets-proxy: REQ %s %s %s", req.method, host, _safe_path(req.path))

        # 1) 危険エンドポイントの遮断
        reason = self._blocked(req, host)
        if reason:
            flow.response = http.Response.make(
                403, f"secrets-proxy: blocked ({reason})\n".encode(),
                {"Content-Type": "text/plain"})
            log.warning("secrets-proxy: BLOCK-ENDPOINT %s %s%s (%s)",
                        req.method, host, _safe_path(req.path), reason)
            return

        # 2) allowlist 外の遮断
        if self.block_unlisted and not self._is_allowed(host):
            flow.response = http.Response.make(
                403, b"secrets-proxy: host not allowed\n", {"Content-Type": "text/plain"})
            log.warning("secrets-proxy: BLOCK %s", host)
            return

        # 3) リーク検知: dev が本物の捕獲済み秘密を持っているリクエストは異常 → 遮断
        leaked = self._scan_leak(req)
        if leaked:
            flow.response = http.Response.make(
                403, b"secrets-proxy: secret leak blocked\n", {"Content-Type": "text/plain"})
            log.error("secrets-proxy: LEAK BLOCKED real value of %r outbound to %s", leaked, host)
            return

        # 4) プレースホルダ -> 本物 (束縛ホスト宛のみ)。解決不能 substitute は fail-closed (502)。
        subst_block = self._subst_request(req, host)
        if subst_block:
            flow.response = http.Response.make(
                502, b"secrets-proxy: unresolved secret placeholder\n",
                {"Content-Type": "text/plain"})
            log.error("secrets-proxy: BLOCK unresolved substitute to %s (%s)", host, subst_block)
            return

        # 5) 静的注入 (Vaultwarden)。match があれば path/method 一致時のみ注入する。
        injs = self._match_inject(host)
        if injs:
            try:
                for inj in injs:
                    if not self._inject_matches(inj, req):
                        continue
                    val = _apply_encode(self._resolve_source(inj["source"]), inj["encode"])
                    rendered = Template(inj["value_tmpl"]).safe_substitute(secret=val)
                    self._apply(req, inj, rendered)
            except Exception as e:
                log.error("secrets-proxy: inject failed for %s: %s", host, e)
                flow.response = http.Response.make(
                    502, b"secrets-proxy: secret resolution failed\n",
                    {"Content-Type": "text/plain"})

    def _blocked(self, req, host):
        h = host.lower()
        for r in self.block_rules:
            if r["host_rx"] and not r["host_rx"].match(h):
                continue
            if r["methods"] and req.method.upper() not in r["methods"]:
                continue
            if r["path_rx"] and not r["path_rx"].search(req.path):
                continue
            return r["reason"]
        return None

    def _scan_leak(self, req):
        if not self.detect_leaks or not self.cap_store:
            return None
        blob = self._request_blob(req)
        for key, ent in self.cap_store.items():
            if ent["value"] and ent["value"] in blob:
                return key
        return None

    @staticmethod
    def _request_blob(req):
        parts = [f"{k}: {v}" for k, v in req.headers.items()]
        parts.append(req.path)
        if _is_textual(req.headers.get("content-type", "")):
            try:
                parts.append(req.get_text(strict=False) or "")
            except Exception:
                pass
        return "\n".join(parts)

    def _subst_request(self, req, host):
        """placeholder を本物へ (束縛ホスト宛のみ)。warm で解決できなかった substitute の
        placeholder が宣言場所に現れたら、fail-closed のため遮断理由を返す (呼び出し側が 502)。"""
        if not self.cap_store:
            return None
        h = host.lower()
        blob = None   # 遅延評価: 非束縛エントリが存在する場合のみ生成
        for ent in self.cap_store.values():
            bound = (not ent["bind"]) or any(rx.match(h) for rx in ent["bind"])
            into = ent.get("into")
            if ent.get("unresolved"):
                # 解決不能 substitute。placeholder が宣言場所に来たら遮断 (素通りさせない)。
                if bound and into and self._location_contains(req, into, ent["ph"]):
                    return f"unresolved substitute {ent['ph']}"
                continue
            if not ent["value"]:
                continue
            if bound:
                if into:
                    self._subst_in_location(req, into, ent["ph"], ent["value"])
            else:
                if blob is None:
                    blob = self._request_blob(req)
                if ent["ph"] in blob:
                    log.warning("secrets-proxy: placeholder %s sent to non-bound host %s "
                                "(not substituted)", ent["ph"], host)
        return None

    @staticmethod
    def _location_contains(req, into, ph):
        """into が指す場所に ph が現れているか (読み取り専用; _subst_in_location と同じ場所判定)。"""
        t, name = into["type"], into["name"]
        if t == "header":
            v = req.headers.get(name)
            return bool(v and ph in v)
        if t == "cookie":
            return name in req.cookies and ph in req.cookies[name]
        if t == "query":
            return name in req.query and ph in req.query[name]
        if t == "form":
            try:
                form = req.urlencoded_form
            except Exception:
                return False
            return name in form and ph in form[name]
        if t == "json":
            if not _is_textual(req.headers.get("content-type", "")):
                return False
            try:
                data = json.loads(req.get_text(strict=False) or "")
            except (json.JSONDecodeError, TypeError):
                return False
            parts = name.split(".")
            cur = data
            for p in parts[:-1]:
                if isinstance(cur, dict) and p in cur:
                    cur = cur[p]
                else:
                    return False
            key = parts[-1]
            return isinstance(cur, dict) and isinstance(cur.get(key), str) and ph in cur[key]
        return False

    @staticmethod
    def _subst_in_location(req, into, ph, value):
        """into が指す場所に出た ph だけを value へ置換する。他の場所は触らない。"""
        t, name = into["type"], into["name"]
        if t == "header":
            v = req.headers.get(name)
            if v and ph in v:
                req.headers[name] = v.replace(ph, value)
        elif t == "cookie":
            if name in req.cookies:
                v = req.cookies[name]
                if ph in v:
                    req.cookies[name] = v.replace(ph, value)
        elif t == "query":
            if name in req.query:
                v = req.query[name]
                if ph in v:
                    req.query[name] = v.replace(ph, value)
        elif t == "form":
            try:
                form = req.urlencoded_form
            except Exception:
                return
            if name in form:
                v = form[name]
                if ph in v:
                    form[name] = v.replace(ph, value)
        elif t == "json":
            if not _is_textual(req.headers.get("content-type", "")):
                return
            try:
                data = json.loads(req.get_text(strict=False) or "")
            except (json.JSONDecodeError, TypeError):
                return
            parts = name.split(".")
            cur = data
            for p in parts[:-1]:
                if isinstance(cur, dict) and p in cur:
                    cur = cur[p]
                else:
                    return
            key = parts[-1]
            if isinstance(cur, dict) and isinstance(cur.get(key), str) and ph in cur[key]:
                cur[key] = cur[key].replace(ph, value)
                req.set_text(json.dumps(data, separators=(",", ":")))

    @staticmethod
    def _inject_matches(inj, req):
        """inj["match"] (path 正規表現 / methods) が無ければ常に True。"""
        if inj["match_methods"] and req.method.upper() not in inj["match_methods"]:
            return False
        if inj["match_path_rx"] and not inj["match_path_rx"].search(req.path):
            return False
        return True

    def _apply(self, req, inj, value):
        t, name = inj["type"], inj["name"]
        if t == "header":
            req.headers[name] = value
        elif t == "cookie":
            req.cookies[name] = value
        elif t == "query":
            req.query[name] = value
        elif t == "form":
            req.urlencoded_form[name] = value
        elif t == "json":
            self._apply_json(req, name, value)
        else:
            raise RuntimeError(f"unknown inject type {t!r}")

    @staticmethod
    def _apply_json(req, path, value):
        """JSON 本文の dot パス (例: "refreshToken" / "auth.token") に値を設定する。
        本文が空/非 JSON なら {} から作り、中間オブジェクトは必要に応じて作成する。"""
        try:
            raw = req.get_text(strict=False)
        except Exception:
            raw = None
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            data = {}
        if not isinstance(data, dict):
            raise RuntimeError("json inject: request body is not a JSON object")
        cur = data
        parts = path.split(".")
        for p in parts[:-1]:
            nxt = cur.get(p)
            if not isinstance(nxt, dict):
                nxt = {}
                cur[p] = nxt
            cur = nxt
        cur[parts[-1]] = value
        req.set_text(json.dumps(data, separators=(",", ":")))
        if "json" not in req.headers.get("content-type", "").lower():
            req.headers["content-type"] = "application/json; charset=utf-8"

    # ===================== レスポンス =====================
    def _host_is_bind_target(self, host):
        """このホストが cap_store のいずれかの bind 対象か (= 別ホスト宣言の secret の復元先)。"""
        h = host.lower()
        for ent in self.cap_store.values():
            if ent.get("value") and any(rx.match(h) for rx in ent["bind"]):
                return True
        return False

    def _host_needs_response_guard(self, host):
        """応答を redact/capture すべきホストか。自前の capture/substitute/inject stanza に加え、
        cap_store の bind 対象ホストも含める。別ホスト宣言の secret は _subst_request が bind 一致で
        このホストへ復元してしまうし、inject した静的秘密が応答に反射されることもあるので、
        いずれも応答 redact で dev への流出を防ぐ。"""
        return bool(self._match_capture(host)) or bool(self._match_substitute(host)) \
            or self._host_is_bind_target(host) or bool(self._match_inject(host))

    def responseheaders(self, flow: http.HTTPFlow):
        host = flow.request.pretty_host
        if self._host_needs_response_guard(host):
            flow.response.stream = False

    def response(self, flow: http.HTTPFlow):
        host = flow.request.pretty_host
        if not self._host_needs_response_guard(host):
            return
        # 1) 先に新規トークンを capture する (blanket redaction の「前」に原応答から抽出)。
        #    逆順だと、token 再発行で既存値が応答に出たとき redaction が先に placeholder へ潰し、
        #    _store_capture が placeholder を本物として保存 → 復元不能 + 正規 placeholder が
        #    leak 誤検知される。
        captured = []
        for cap in self._match_capture(host):
            try:
                value = self._extract_response(flow, cap)
            except Exception as e:
                log.error("secrets-proxy: capture %s extract failed at %s: %s",
                          cap["store"], host, e)
                continue
            if not value:
                continue
            ph = self._store_capture(cap, value, host)
            captured.append((cap["store"], ph))
        # 2) cap_store 全体の本物値を応答から placeholder へ (substitute/capture + 上の新規 capture 分)。
        for ent in list(self.cap_store.values()):
            if ent["value"]:
                self._redact_response(flow, ent["value"], ent["ph"])
        # 3) このホストへ inject した静的秘密が応答に反射されても dev に返さない。inject は cap_store に
        #    placeholder を持たない (復元もしない) ので、固定マーカーで潰すだけにする。on-wire は
        #    into.encode で符号化されうるので、生値と符号化後の値の両方を redact する。
        for inj in (self._match_inject(host) or []):
            raw = self._static.get(inj["source"])
            if not raw:
                continue
            for val in {raw, _apply_encode(raw, inj["encode"])}:
                self._redact_response(flow, val, f"@@REDACTED_{self.salt}@@")
        for store, ph in captured:
            log.info("secrets-proxy: captured %s from %s -> %s", store, host, ph)

    @staticmethod
    def _extract_response(flow, cap):
        resp = flow.response
        src = cap["from"]
        name = cap["name"]
        if src == "set-cookie":
            cookies = resp.cookies
            if name in cookies:
                return cookies[name][0]
            return None
        if src == "header":
            return resp.headers.get(name)
        if src == "json":
            return _json_path(json.loads(resp.get_text(strict=False)), name)
        if src == "regex":
            m = re.search(name, resp.get_text(strict=False))
            return m.group(1) if m else None
        raise RuntimeError(f"unknown capture source {src!r}")

    def _store_capture(self, cap, value, host):
        key = cap["store"]
        bind = cap["bind_rx"] or [re.compile(_host_to_regex(host.lower()))]
        ph = f"@@CRED_{key}_{self.salt}@@"
        # 再捕獲 (トークン更新) は同じプレースホルダのまま値だけ差し替える
        self.cap_store[key] = {"value": value, "ph": ph, "bind": bind, "into": cap["into"]}
        return ph

    @staticmethod
    def _redact_response(flow, value, ph):
        """本物の値の全出現をプレースホルダに置換し、dev に渡さない。"""
        if len(value) < 8:
            log.warning("secrets-proxy: captured value is short (%d chars); "
                        "blanket redaction may over-match", len(value))
        resp = flow.response

        # ヘッダ (Set-Cookie を含む)
        new_fields, changed = [], False
        for k, v in resp.headers.fields:
            vs = v.decode("latin-1")
            if value in vs:
                vs = vs.replace(value, ph)
                changed = True
            new_fields.append((k, vs.encode("latin-1")))
        if changed:
            resp.headers.fields = tuple(new_fields)

        # 本文: Content-Type に依らず、既知の秘密が本文に現れたら必ず置換する (fail-closed)。
        # 非 textual (Content-Type 欠落/誤り) でも redact しないと本物が dev に漏れる。値が無ければ no-op。
        try:
            body = resp.get_text(strict=False)
        except Exception:
            body = None
        if body and value in body:
            resp.set_text(body.replace(value, ph))


addons = [SecretsProxy()]
