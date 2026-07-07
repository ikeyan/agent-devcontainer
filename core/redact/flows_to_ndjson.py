"""Dump a mitmproxy flows file as NDJSON on stdout — one HTTP flow per line.

This is the generic engine; redaction is app-specific. Each captured app gets its
own extract/<app>/redact_flow.py that observes that app's flow, defines a
`redact: (str) -> str`, and calls `flows_to_ndjson(redact)` (see that file).
Running THIS module directly applies the identity redact (no redaction) — useful
for a quick unredacted look, never for anything that leaves the machine.
"""

import json
import sys
from collections.abc import Callable

from mitmproxy import http, io


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


def flows_to_ndjson(redact: Callable[[str], str] = lambda s: s) -> None:
    """Read a mitmproxy flows file from stdin and print one NDJSON line per HTTP
    flow to stdout, passing each serialized line through `redact` first.

    `redact` maps a JSON line to a redacted JSON line; the default is identity.
    Redacting the whole line (not per-field) lets an app match secrets wherever
    they appear — URL, headers or body — with one set of patterns.
    """
    with sys.stdin.buffer as fp:
        for f in io.FlowReader(fp).stream():
            if not isinstance(f, http.HTTPFlow):
                continue
            print(redact(json.dumps(to_dict(f), ensure_ascii=False)))


if __name__ == "__main__":
    flows_to_ndjson()
