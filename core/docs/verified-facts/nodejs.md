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
