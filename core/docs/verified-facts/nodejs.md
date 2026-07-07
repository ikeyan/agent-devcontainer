# Node.js

confidence tag の凡例: [README](README.md)。

- Node の組み込み `fetch` (undici) は **既定では `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` を読まない**。
  `--use-env-proxy` を付けて初めて env から proxy 設定を解釈する。これが無いと、compose で
  `HTTP_PROXY=http://secrets-proxy:8080` を設定していても `fetch` は直結し、secrets-proxy の注入/捕獲を
  素通りする (firewall が宛先を弾くか、fake 資格情報が直送される)。`--use-env-proxy` は `NODE_OPTIONS`
  でも受理される (`NODE_OPTIONS=--max-old-space-size=4096 --use-env-proxy`)。
  検証 (node v24.17.0): `node --help` に `--use-env-proxy  parse proxy settings from
  HTTP_PROXY/HTTPS_PROXY/NO_PROXY` と記載。`HTTP_PROXY=http://127.0.0.1:9 NODE_OPTIONS=--use-env-proxy
  node -e 'fetch(...)'` は proxy 経由になり、フラグ無しでは宛先へ直結する (挙動差を確認)。
  `[docs(--help)][empirical]`

## TypeScript 型ストリッピング (.ts 直接実行)

- Node は `.ts` を追加ツール無しで直接実行できる。Node 22.6 で `--experimental-strip-types` フラグとして
  導入され、Node 23.6 からはフラグ無しで既定有効になった。対応するのは型注釈を消すだけで済む
  「erasable syntax」に限られ、`enum` / `namespace` / パラメータプロパティなど実行時コードを生成する
  構文は非対応 (`--experimental-transform-types` が要る)。出典: nodejs.org/api/typescript.html。
  `[docs]`
- Node 24 (v24.17.0) では上記の型ストリッピングに警告も出ない。実測: `core/podman/gen-seccomp.ts`
  (import 文と `interface`/型注釈のみ含む erasable syntax) を `node podman/gen-seccomp.ts` で
  tsx 無し直接実行し、出力が commit 済み `podman/seccomp.json` と byte 一致・stderr 出力が空である
  ことを確認した (検証日 2026-07-07)。これにより `core/Makefile` の `SECCOMP_GEN` から
  `node --import tsx` を撤去できる (tsx/node_modules への依存が丸ごと不要)。 `[empirical]`
