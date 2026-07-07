// /util/httpCache.ts の vendored copy (kit の self-containment のため。原本と独立に保守する)。
import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

// 宛先 URL ごとに応答本文を http-cache/<host>/<path> に保存する単純なファイルキャッシュ。
// 既定は commit 済みのスナップショットを offline で読む (make check 等の決定性のため)。
// HTTP_CACHE_REFRESH=1 で live 取得して上書きし、その diff をレビューして commit する。
// 安定パスなので git で中身を diff できる (cacache 等の content-addressed と違い「何が変わったか」が読める)。
const CACHE_ROOT = fileURLToPath(new URL("../http-cache/", import.meta.url));

function cachePath(url: URL): string {
  let p = url.pathname.replace(/^\/+/, "");
  if (p === "" || p.endsWith("/")) p += "index";
  // query は読めるファイル名にできないので短いハッシュを後置する (path だけなら可読のまま)。
  if (url.search) p += `__${createHash("sha256").update(url.search).digest("hex").slice(0, 8)}`;
  return path.join(CACHE_ROOT, url.host, p);
}

export interface CachedFetchOptions {
  /** live 取得してキャッシュを更新する。既定は env HTTP_CACHE_REFRESH の有無。 */
  refresh?: boolean;
}

/** URL を取得して本文 (utf8) を返す。既定は commit 済みキャッシュを offline で読む。 */
export async function cachedFetch(input: string | URL, opts: CachedFetchOptions = {}): Promise<string> {
  const url = new URL(input);
  const file = cachePath(url);
  const refresh = opts.refresh ?? process.env.HTTP_CACHE_REFRESH != null;
  if (!refresh) {
    try {
      return await readFile(file, "utf8");
    } catch {
      throw new Error(
        `http-cache miss: ${url} → ${path.relative(process.cwd(), file)} (HTTP_CACHE_REFRESH=1 で取得・保存)`,
      );
    }
  }
  const res = await fetch(url);
  if (!res.ok) throw new Error(`fetch ${url} failed: ${res.status} ${res.statusText}`);
  const body = await res.text();
  await mkdir(path.dirname(file), { recursive: true });
  await writeFile(file, body);
  return body;
}
