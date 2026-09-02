"""Dump a mitmproxy flows file as NDJSON on stdout — one HTTP flow per line.

This is the generic engine; redaction is app-specific. Each captured app gets its
own extract/<app>/redact_flow.py that observes that app's flow, defines a
`redact: (str) -> str`, and calls `flows_to_ndjson(redact)` (see that file).
Running THIS module directly applies the identity redact (no redaction) — useful
for a quick unredacted look, never for anything that leaves the machine.
"""

import json
import sys
from collections.abc import Callable, Iterable

from mitmproxy import http, io


def host_in_domains(host: str, domains: Iterable[str]) -> bool:
    """host が domains のいずれかの root の subtree に入るか (dot 境界厳守 — substring では
    notexample.com を拾ってしまう)。domains は小文字の root ("example.com" 等) を渡す。
    host 側は case-insensitive、末尾 dot の FQDN 表記も同一視する。

    filter するアプリはこの matcher を iter_flow_dicts / flows_to_ndjson(keep_domains=) 経由で
    使い、同じ tuple を SELFCHECK_DOMAINS として module トップレベルに宣言する — 宣言と実装を
    同じ値にしておけば、check-redact-selfcheck が宣言から導く境界 probe が実装そのものを検査する。"""
    h = host.lower().rstrip(".")
    return any(h == d or h.endswith("." + d) for d in domains)


def to_dict(f: http.HTTPFlow) -> dict:
    r, p = f.request, f.response
    d = {
        "method": r.method,
        "url": r.pretty_url,
        "request_headers": dict(r.headers),
        "request_body": r.get_text(strict=False),
    }
    if p:
        d |= {
            "status_code": p.status_code,
            "response_headers": dict(p.headers),
            "response_body": p.get_text(strict=False),
        }
    return d


def iter_flow_dicts(fp, keep_domains: Iterable[str] | None = None):
    """Yield one dict (see to_dict) per HTTP flow in a mitmproxy flows stream.

    This is THE read loop — apps that need a custom __main__ (e.g. a two-pass
    scan over all flows) consume this generator instead of re-implementing
    FlowReader + isinstance + filter, so loop semantics can't drift per app.

    `keep_domains` (optional) drops every flow whose host is outside the given
    domain subtrees (dot-boundary match, see host_in_domains). Pass the
    module-level SELFCHECK_DOMAINS tuple here so the declaration checked by
    check-redact-selfcheck and the implemented filter can never drift apart.
    The host compared is `request.host` — the actual connection target — NOT
    `pretty_host`, which prefers the client-supplied Host header: an
    adversarial sandboxed process (SECURITY-MODEL: same-uid hostile child) can
    forge the Host header on traffic to an arbitrary server, and a pretty_host
    filter would keep that flow and misattribute it to an allowed domain.
    """
    for f in io.FlowReader(fp).stream():
        if not isinstance(f, http.HTTPFlow):
            continue
        if keep_domains is not None and not host_in_domains(f.request.host, keep_domains):
            continue
        yield to_dict(f)


def flows_to_ndjson(
    redact: Callable[[str], str] = lambda s: s,
    keep_domains: Iterable[str] | None = None,
) -> None:
    """Read a mitmproxy flows file from stdin and print one NDJSON line per HTTP
    flow to stdout, passing each serialized line through `redact` first.

    `redact` maps a JSON line to a redacted JSON line; the default is identity.
    Redacting the whole line (not per-field) lets an app match secrets wherever
    they appear — URL, headers or body — with one set of patterns.

    `keep_domains` is forwarded to iter_flow_dicts (see there for the contract
    and the pretty_host caveat).
    """
    with sys.stdin.buffer as fp:
        for d in iter_flow_dicts(fp, keep_domains):
            print(redact(json.dumps(d, ensure_ascii=False)))


if __name__ == "__main__":
    flows_to_ndjson()
