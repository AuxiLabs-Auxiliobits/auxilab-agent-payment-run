#!/usr/bin/env python3
"""Pre-Payment Validation Agent - lightweight web UI (standard library only).

This UI intentionally uses ONLY the Python standard library (http.server) so it
has zero third-party dependencies beyond the core engine (pandas). That avoids
the version churn in heavier UI frameworks and means `python app.py` just works
on any supported Python (3.9-3.13) with nothing extra to install.

Upload a payment batch CSV/JSON, get back a colour-coded status table, the flag
summary, the batch readiness score and the CFO narrative - plus a downloadable
authorisation pack (Markdown / JSON / CSV).

    python app.py            # then open http://127.0.0.1:7860

The reference data (source of truth) is read from ./reference_data.
"""
from __future__ import annotations

import html
import os
import tempfile
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from prepay import config
from prepay.pipeline import run_validation

_PROVIDERS = ["template", "anthropic", "groq", "openai"]

# token -> directory holding a run's authorisation pack (for downloads)
_PACK_DIRS: dict[str, Path] = {}

_STATUS_STYLE = {
    "Clear": ("#0f7b3f", "#e6f4ea"),
    "Review Required": ("#8a5a00", "#fff4e0"),
    "Hold": ("#b00020", "#fde7ea"),
}

_PAGE_CSS = """
* { box-sizing: border-box; }
body { font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
  margin: 0; background: #f6f7f9; color: #1d1d1f; }
.wrap { max-width: 1000px; margin: 0 auto; padding: 32px 20px 64px; }
h1 { font-size: 24px; margin: 0 0 4px; }
.sub { color: #6b7280; margin: 0 0 24px; font-size: 14px; }
.card { background: #fff; border: 1px solid #e5e7eb; border-radius: 12px;
  padding: 20px 22px; margin-bottom: 20px; box-shadow: 0 1px 2px rgba(0,0,0,.04); }
label { font-weight: 600; font-size: 14px; display: block; margin-bottom: 6px; }
input[type=file], select { padding: 8px; border: 1px solid #d1d5db; border-radius: 8px;
  width: 100%; background: #fff; font-size: 14px; }
.row { display: flex; gap: 16px; flex-wrap: wrap; }
.row > div { flex: 1 1 240px; }
button { margin-top: 16px; background: #111827; color: #fff; border: 0;
  padding: 11px 20px; border-radius: 8px; font-size: 15px; font-weight: 600; cursor: pointer; }
button:hover { background: #000; }
.kpis { display: flex; gap: 14px; flex-wrap: wrap; }
.kpi { flex: 1 1 150px; background: #fafafa; border: 1px solid #eee; border-radius: 10px; padding: 12px 14px; }
.kpi .n { font-size: 22px; font-weight: 700; }
.kpi .l { font-size: 12px; color: #6b7280; text-transform: uppercase; letter-spacing: .04em; }
.decision { font-size: 16px; font-weight: 700; padding: 10px 14px; border-radius: 8px; margin: 4px 0 0; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #eee; vertical-align: top; }
th { background: #fafafa; font-size: 12px; text-transform: uppercase; letter-spacing: .03em; color: #6b7280; }
.badge { display: inline-block; padding: 2px 9px; border-radius: 999px; font-size: 12px; font-weight: 600; }
.narr { white-space: pre-wrap; line-height: 1.5; font-size: 14px; }
.dl a { display: inline-block; margin-right: 10px; margin-top: 6px; padding: 8px 14px;
  border: 1px solid #d1d5db; border-radius: 8px; text-decoration: none; color: #111827; font-size: 14px; }
.dl a:hover { background: #f3f4f6; }
.err { color: #b00020; }
.note { font-size: 12px; color: #6b7280; margin-top: 6px; }
code { background: #f3f4f6; padding: 1px 5px; border-radius: 4px; }
"""


def _providers_options(selected: str) -> str:
    opts = []
    for p in _PROVIDERS:
        sel = " selected" if p == selected else ""
        opts.append(f'<option value="{p}"{sel}>{p}</option>')
    return "".join(opts)


def _page(body: str) -> bytes:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Pre-Payment Validation Agent</title>"
        f"<style>{_PAGE_CSS}</style></head><body><div class='wrap'>"
        "<h1>Pre-Payment Validation Agent</h1>"
        "<p class='sub'>Upload a proposed payment batch. The agent runs six "
        "deterministic checks against the vendor master, invoice register and "
        "90-day payment history, then produces a controller authorisation pack "
        "with a CFO narrative. The LLM only ever sees de-identified aggregates.</p>"
        f"{body}"
        "</div></body></html>"
    ).encode("utf-8")


def _form(selected_provider: str, error: str = "") -> str:
    err = f"<p class='err'>{html.escape(error)}</p>" if error else ""
    return (
        "<div class='card'>"
        f"{err}"
        "<form method='POST' action='/validate' enctype='multipart/form-data'>"
        "<div class='row'>"
        "<div><label>Payment batch (CSV or JSON)</label>"
        "<input type='file' name='batch' accept='.csv,.json' required></div>"
        "<div><label>Narrative provider</label>"
        f"<select name='provider'>{_providers_options(selected_provider)}</select>"
        "<p class='note'>Non-template providers need an API key in your "
        "environment; otherwise it falls back to the offline template.</p></div>"
        "</div>"
        "<button type='submit'>Validate batch</button>"
        "</form></div>"
        "<div class='card'><strong>Required columns:</strong> "
        "<code>payment_id</code>, <code>vendor_id</code>, "
        "<code>invoice_number</code>, <code>payment_amount</code> (or "
        "<code>amount</code>). Include <code>approval_status</code> "
        "(<code>Approved</code>) and an approver column so approved payments "
        "aren't held.</div>"
    )


def _parse_multipart(body: bytes, boundary: bytes):
    """Minimal multipart/form-data parser (no cgi module - removed in 3.13)."""
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes]] = {}
    delim = b"--" + boundary
    for seg in body.split(delim):
        if seg in (b"", b"--", b"--\r\n", b"\r\n"):
            continue
        if seg.startswith(b"\r\n"):
            seg = seg[2:]
        if seg.endswith(b"\r\n"):
            seg = seg[:-2]
        head, sep, data = seg.partition(b"\r\n\r\n")
        if not sep:
            continue
        name = filename = None
        for line in head.split(b"\r\n"):
            k, _, v = line.partition(b":")
            if k.strip().lower() != b"content-disposition":
                continue
            for piece in v.decode("utf-8", "replace").split(";"):
                piece = piece.strip()
                if piece.startswith("name="):
                    name = piece[5:].strip('"')
                elif piece.startswith("filename="):
                    filename = piece[9:].strip('"')
        if name is None:
            continue
        if filename is not None:
            files[name] = (filename, data)
        else:
            fields[name] = data.decode("utf-8", "replace").strip()
    return fields, files


def _render_results(result, token: str) -> str:
    s = result.score
    sym = config.CURRENCY_SYMBOL
    decision = str(s["decision"])
    if decision.upper().startswith("CLEAR"):
        dcol, dbg = _STATUS_STYLE["Clear"]
    elif decision.upper().startswith("HOLD"):
        dcol, dbg = _STATUS_STYLE["Hold"]
    else:
        dcol, dbg = _STATUS_STYLE["Review Required"]

    kpis = (
        "<div class='kpis'>"
        f"<div class='kpi'><div class='n'>{s['integrity_score']}/100</div>"
        "<div class='l'>Integrity score</div></div>"
        f"<div class='kpi'><div class='n'>{sym}{s['total_value']:,.0f}</div>"
        "<div class='l'>Total batch value</div></div>"
        f"<div class='kpi'><div class='n'>{s['clear_count']} / {s['review_count']} / {s['hold_count']}</div>"
        "<div class='l'>Clear / Review / Hold</div></div>"
        f"<div class='kpi'><div class='n'>{sym}{s['flagged_value']:,.0f}</div>"
        "<div class='l'>Flagged value</div></div>"
        "</div>"
    )

    cols = ["payment_id", "vendor_name", "invoice_number", "amount", "status", "flags"]
    df = result.status_df
    present = [c for c in cols if c in df.columns]
    header = "".join(f"<th>{html.escape(c.replace('_', ' '))}</th>" for c in present)
    body_rows = []
    for _, row in df.iterrows():
        cells = []
        for c in present:
            val = row[c]
            if c == "amount" and val == val and val is not None:  # not NaN
                try:
                    text = f"{sym}{float(val):,.2f}"
                except (TypeError, ValueError):
                    text = html.escape(str(val))
            elif c == "status":
                fg, bg = _STATUS_STYLE.get(str(val), ("#374151", "#eef"))
                text = (f"<span class='badge' style='color:{fg};background:{bg}'>"
                        f"{html.escape(str(val))}</span>")
            elif isinstance(val, (list, tuple)):
                text = html.escape(", ".join(str(x) for x in val))
            else:
                text = html.escape("" if val is None or val != val else str(val))
            cells.append(f"<td>{text}</td>")
        body_rows.append(f"<tr>{''.join(cells)}</tr>")
    table = (f"<table><thead><tr>{header}</tr></thead>"
             f"<tbody>{''.join(body_rows)}</tbody></table>")

    downloads = (
        "<div class='dl'>"
        f"<a href='/download/{token}/authorisation_pack.md'>Download Markdown pack</a>"
        f"<a href='/download/{token}/authorisation_pack.json'>Download JSON pack</a>"
        f"<a href='/download/{token}/payment_status.csv'>Download status CSV</a>"
        "</div>"
    )

    return (
        "<div class='card'>"
        f"<h1 style='font-size:18px'>{html.escape(str(result.batch_id))}</h1>"
        f"<p class='decision' style='color:{dcol};background:{dbg}'>{html.escape(decision)}</p>"
        f"{kpis}</div>"
        f"<div class='card'><label>Controller / CFO narrative</label>"
        f"<div class='narr'>{html.escape(result.narrative)}</div></div>"
        f"<div class='card'><label>Payment-by-payment status</label>{table}</div>"
        f"<div class='card'><label>Authorisation pack</label>{downloads}</div>"
        "<div class='card'><a href='/'>&larr; Validate another batch</a></div>"
    )


class Handler(BaseHTTPRequestHandler):
    server_version = "PrepayValidator/1.0"

    def _send(self, body: bytes, status: int = 200,
              content_type: str = "text/html; charset=utf-8", extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # keep the console clean
        return

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send(_page(_form(config.LLM_PROVIDER)))
            return
        if parsed.path.startswith("/download/"):
            self._serve_download(parsed.path)
            return
        self._send(_page("<div class='card'>Not found. <a href='/'>Home</a></div>"), 404)

    def _serve_download(self, path: str):
        parts = path.strip("/").split("/")
        if len(parts) != 3:
            self._send(b"Bad request", 400, "text/plain")
            return
        _, token, name = parts
        base = _PACK_DIRS.get(token)
        if base is None:
            self._send(b"Expired or unknown download", 404, "text/plain")
            return
        target = (base / name).resolve()
        if base.resolve() not in target.parents or not target.is_file():
            self._send(b"Not found", 404, "text/plain")
            return
        data = target.read_bytes()
        ctype = ("application/json" if name.endswith(".json")
                 else "text/csv" if name.endswith(".csv")
                 else "text/markdown")
        self._send(data, 200, f"{ctype}; charset=utf-8",
                   {"Content-Disposition": f'attachment; filename="{name}"'})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/validate":
            self._send(_page("<div class='card'>Not found. <a href='/'>Home</a></div>"), 404)
            return
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype or "boundary=" not in ctype:
            self._send(_page(_form(config.LLM_PROVIDER, "Upload a file to validate.")), 400)
            return
        boundary = ctype.split("boundary=", 1)[1].strip().strip('"').encode()
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        fields, files = _parse_multipart(body, boundary)
        provider = fields.get("provider", config.LLM_PROVIDER)

        if "batch" not in files or not files["batch"][1]:
            self._send(_page(_form(provider, "Please choose a CSV or JSON file.")), 400)
            return

        filename, filedata = files["batch"]
        suffix = Path(filename).suffix.lower() or ".csv"
        tmp = Path(tempfile.mkdtemp(prefix="prepay_in_")) / ("batch" + suffix)
        tmp.write_bytes(filedata)

        try:
            result = run_validation(str(tmp), provider=provider)
        except Exception as exc:  # surface a friendly error in the UI
            self._send(_page(_form(provider, f"Could not validate batch: {exc}")), 400)
            return

        token = uuid.uuid4().hex
        out_dir = Path(tempfile.mkdtemp(prefix="prepay_out_"))
        result.write_outputs(out_dir)
        _PACK_DIRS[token] = out_dir

        self._send(_page(_render_results(result, token)))


def main():
    host = os.getenv("GRADIO_SERVER_NAME", os.getenv("HOST", "127.0.0.1"))
    port = int(os.getenv("PORT", "7860"))
    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}"
    print("Pre-Payment Validation Agent - web UI")
    print(f"  Serving on {url}  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        httpd.server_close()


if __name__ == "__main__":
    main()
