#!/usr/bin/env python3
"""Text extraction and preview rendering for receipt files.

Importable and a CLI. The one entry point is extract(), which never raises:

    extract(path, preview_path=None, max_pages=3) -> {
        "text": str,
        "method": "pdfkit" | "ocr" | "image-ocr" | "html" | "none",
        "pages": int,
        "preview": bool,
        "error": None | str,
    }

- .html/.htm: parsed here with html.parser (script, style and head dropped,
  block tags become newlines, entities unescaped, whitespace collapsed per
  line). Method html, pages 1, never a preview.
- .pdf: the Swift helper. PDFKit page text joined with a blank line (method
  pdfkit); when that is shorter than 40 characters the pages are rendered and
  read with Vision OCR (method ocr, up to max_pages pages). With preview_path
  page 1 is rendered to a PNG 1200 px wide.
- images (.png .jpg .jpeg .gif .webp .tif .tiff .bmp .heic): Vision OCR,
  method image-ocr, pages 1, never a preview file (callers show the image).
- anything else: method none, error "unsupported".

Whenever the final text is empty the method is none. "pages" is the number
of pages the text came from (all pages for pdfkit, the OCR'd pages for ocr).

The helper scripts/receipt_text.swift is compiled with `swiftc -O` into
scripts/bin/receipt-text when the binary is missing or older than the
source. If swiftc is missing or the build fails, extract() reports method
none with the compiler error and still never raises. Read-only on every
input; the only files written are the preview PNG and the compiled binary.

Usage:
  scripts/receipt_text.py FILE [--preview OUT.png] [--max-pages N]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.abspath(__file__))
SWIFT_SOURCE = os.path.join(HERE, "receipt_text.swift")
BIN_DIR = os.path.join(HERE, "bin")
BINARY = os.path.join(BIN_DIR, "receipt-text")

HTML_EXTENSIONS = {".html", ".htm"}
PDF_EXTENSIONS = {".pdf"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".tif", ".tiff", ".bmp", ".heic"}

COMPILE_TIMEOUT = 600
EXTRACT_TIMEOUT = 300

METHODS = {"pdfkit", "ocr", "image-ocr", "html", "none"}


def _result(text="", method="none", pages=0, preview=False, error=None):
    return {"text": text, "method": method, "pages": pages, "preview": preview, "error": error}


# ---------------------------------------------------------------- helper binary


def _tail(text, limit=1500):
    text = (text or "").strip()
    return text if len(text) <= limit else "..." + text[-limit:]


def ensure_binary():
    """Compile the Swift helper when missing or stale. Returns None or an error string."""
    if not os.path.isfile(SWIFT_SOURCE):
        return "helper source missing: %s" % SWIFT_SOURCE
    try:
        stale = (not os.path.isfile(BINARY)) or os.path.getmtime(BINARY) < os.path.getmtime(SWIFT_SOURCE)
    except OSError:
        stale = True
    if not stale:
        return None
    swiftc = shutil.which("swiftc")
    if not swiftc:
        return "swiftc not found; cannot build %s" % BINARY
    os.makedirs(BIN_DIR, exist_ok=True)
    temp = "%s.build-%d" % (BINARY, os.getpid())
    try:
        proc = subprocess.run(
            [swiftc, "-O", "-o", temp, SWIFT_SOURCE],
            capture_output=True,
            text=True,
            timeout=COMPILE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        _unlink(temp)
        return "swiftc timed out after %d s" % COMPILE_TIMEOUT
    except OSError as exc:
        _unlink(temp)
        return "swiftc could not run: %s" % exc
    if proc.returncode != 0 or not os.path.isfile(temp):
        _unlink(temp)
        return "swiftc failed (exit %d): %s" % (proc.returncode, _tail(proc.stderr))
    try:
        os.chmod(temp, 0o755)
        os.replace(temp, BINARY)
    except OSError as exc:
        _unlink(temp)
        return "could not install helper binary: %s" % exc
    return None


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _run_helper(path, preview_path, max_pages):
    error = ensure_binary()
    if error:
        return _result(error=error)
    command = [BINARY, path, "--max-pages", str(int(max_pages))]
    if preview_path:
        parent = os.path.dirname(os.path.abspath(preview_path))
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as exc:
            return _result(error="cannot create preview dir: %s" % exc)
        command += ["--preview", preview_path]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=EXTRACT_TIMEOUT)
    except subprocess.TimeoutExpired:
        return _result(error="helper timed out after %d s" % EXTRACT_TIMEOUT)
    except OSError as exc:
        return _result(error="helper could not run: %s" % exc)
    line = (proc.stdout or "").strip()
    if not line:
        return _result(error="helper produced no output (exit %d): %s" % (proc.returncode, _tail(proc.stderr)))
    try:
        data = parse_helper_output(line)
    except ValueError as exc:
        return _result(error="helper output is not JSON: %s (%s)" % (exc, _tail(line, 200)))
    if not isinstance(data, dict):
        return _result(error="helper output is not an object")
    text = data.get("text")
    method = data.get("method")
    error_text = data.get("error")
    out = _result(
        text=text if isinstance(text, str) else "",
        method=method if method in METHODS else "none",
        pages=int(data.get("pages") or 0),
        preview=bool(data.get("preview")) and bool(preview_path) and os.path.isfile(preview_path),
        error=str(error_text) if error_text not in (None, "") else None,
    )
    if not out["text"].strip():
        out["text"] = ""
        out["method"] = "none"
    return out


def parse_helper_output(out):
    """The one JSON object the helper prints. JSONSerialization leaves U+2028, U+2029 and
    U+0085 unescaped inside the text, and str.splitlines() breaks on them, so the output is
    parsed whole and only split on real newlines when something else precedes the object."""
    out = out.strip()
    try:
        return json.loads(out)
    except ValueError:
        return json.loads(out.rsplit("\n", 1)[-1])


# ------------------------------------------------------------------------ html

BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "br", "dd", "details", "dialog", "div", "dl", "dt",
    "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6",
    "header", "hr", "li", "main", "nav", "ol", "option", "p", "pre", "section", "summary",
    "table", "tbody", "tfoot", "thead", "tr", "ul",
}
CELL_TAGS = {"td", "th"}
SKIP_TAGS = {"script", "style", "head", "noscript", "template", "title", "svg"}


class _TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in SKIP_TAGS:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag in BLOCK_TAGS:
            self.parts.append("\n")
        elif tag in CELL_TAGS:
            self.parts.append("  ")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() not in SKIP_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in SKIP_TAGS:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.skip_depth:
            return
        if tag in BLOCK_TAGS:
            self.parts.append("\n")
        elif tag in CELL_TAGS:
            self.parts.append("  ")

    def handle_data(self, data):
        if not self.skip_depth and data:
            self.parts.append(data)


_CHARSET = re.compile(rb"charset\s*=\s*[\"']?\s*([A-Za-z0-9_.:-]+)", re.IGNORECASE)


def _decode_html(raw):
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", errors="replace")
    match = _CHARSET.search(raw[:4096])
    if match:
        codec = match.group(1).decode("ascii", errors="ignore")
        try:
            return raw.decode(codec)
        except (LookupError, UnicodeDecodeError):
            pass
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


def html_to_text(markup):
    """Visible text of an HTML document: one line per block element, whitespace
    collapsed per line, no blank lines (nested blocks would otherwise pad every
    label and value with empty lines)."""
    parser = _TextParser()
    parser.feed(markup)
    parser.close()
    lines = []
    for line in "".join(parser.parts).split("\n"):
        line = re.sub(r"[^\S\n]+", " ", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _extract_html(path):
    with open(path, "rb") as handle:
        raw = handle.read()
    text = html_to_text(_decode_html(raw))
    if not text:
        return _result(pages=1, error="no visible text")
    return _result(text=text, method="html", pages=1)


# --------------------------------------------------------------------- extract


def extract(path, preview_path=None, max_pages=3):
    """Extract receipt text (and optionally a page 1 preview PNG). Never raises."""
    try:
        path = os.fspath(path)
        if preview_path is not None:
            preview_path = os.fspath(preview_path)
        if not os.path.isfile(path):
            return _result(error="not found: %s" % path)
        try:
            max_pages = max(1, int(max_pages))
        except (TypeError, ValueError):
            max_pages = 3
        ext = os.path.splitext(path)[1].lower()
        if ext in HTML_EXTENSIONS:
            return _extract_html(path)
        if ext in PDF_EXTENSIONS:
            return _run_helper(path, preview_path, max_pages)
        if ext in IMAGE_EXTENSIONS:
            return _run_helper(path, None, max_pages)
        return _result(error="unsupported")
    except Exception as exc:  # noqa: BLE001 - the contract is "never raise"
        return _result(error="%s: %s" % (type(exc).__name__, exc))


# ------------------------------------------------------------------------- cli


def main(argv=None):
    parser = argparse.ArgumentParser(description="Extract receipt text and render a preview; prints JSON.")
    parser.add_argument("file")
    parser.add_argument("--preview", metavar="OUT.png", help="write page 1 of a PDF as PNG (1200 px wide)")
    parser.add_argument("--max-pages", type=int, default=3, help="pages to OCR when a PDF has no text layer")
    args = parser.parse_args(argv)
    result = extract(args.file, preview_path=args.preview, max_pages=args.max_pages)
    json.dump(result, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0 if result["error"] is None else 1


if __name__ == "__main__":
    sys.exit(main())
