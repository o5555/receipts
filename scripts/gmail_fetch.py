#!/usr/bin/env python3
"""Read-only receipt fetcher on top of Thor's Gmail DWD helper.

Reuses gmail_dwd_read.py (allowlist, audit log, gmail.readonly scope). Adds:
  links   <msg_id>                 list attachments and https links in the message
  save    <msg_id> <out_dir>       save every attachment of the message into out_dir
  html    <msg_id> <out.html>      save the text/html body (for HTML-only receipts)

Usage:
  scripts/gmail_fetch.py links 1a00b4197febe90c --reason "..."
  scripts/gmail_fetch.py save  19f4dac84ce2b2d9 out/receipts --reason "..."
"""
import argparse
import base64
import html
import json
import re
import sys
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, "/Users/odin/thor/projects/email-access")
import gmail_dwd_read as g  # noqa: E402

MAILBOX = "oscar.sandstrom@viseo.se"


def fetch(session, msg_id):
    return g.gmail_get(session, f"users/{quote(MAILBOX)}/messages/{quote(msg_id)}", params={"format": "full"})


def walk(part):
    yield part
    for p in part.get("parts", []) or []:
        yield from walk(p)


def body_text(part):
    data = (part.get("body") or {}).get("data")
    if not data:
        return ""
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")


def links(msg):
    out = []
    for p in walk(msg["payload"]):
        if p.get("mimeType") in ("text/html", "text/plain"):
            txt = html.unescape(body_text(p))
            out += re.findall(r'https?://[^\s"\'<>)]+', txt)
    seen, res = set(), []
    for u in out:
        if u not in seen:
            seen.add(u)
            res.append(u)
    return res


def cmd_links(a):
    s = g.session_for(MAILBOX)
    m = fetch(s, a.message_id)
    g.audit("read", MAILBOX, a.reason, {"message_id": a.message_id, "format": "full", "via": "gmail_fetch links"})
    hdr = g.headers_by_name(m["payload"])
    print(json.dumps({"subject": hdr.get("subject"), "from": hdr.get("from"), "date": hdr.get("date"),
                      "attachments": g.attachment_metadata(m["payload"]),
                      "links": [u for u in links(m) if not re.search(r"unsubscribe|mailto:|w3\.org", u)]},
                     ensure_ascii=False, indent=1))


def cmd_save(a):
    s = g.session_for(MAILBOX)
    m = fetch(s, a.message_id)
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    saved = []
    for att in g.attachment_metadata(m["payload"]):
        if not att["attachment_id"]:
            continue
        d = g.gmail_get(s, f"users/{quote(MAILBOX)}/messages/{quote(a.message_id)}/attachments/{quote(att['attachment_id'])}")
        data = base64.urlsafe_b64decode(d["data"] + "=" * (-len(d["data"]) % 4))
        name = (a.prefix + "_" if a.prefix else "") + re.sub(r"[^\w.\-]+", "_", att["filename"])
        (out / name).write_bytes(data)
        saved.append({"file": str(out / name), "bytes": len(data), "mime": att["mime_type"]})
    g.audit("attachment", MAILBOX, a.reason, {"message_id": a.message_id, "saved": [x["file"] for x in saved]})
    print(json.dumps(saved, indent=1))


def cmd_html(a):
    s = g.session_for(MAILBOX)
    m = fetch(s, a.message_id)
    g.audit("read", MAILBOX, a.reason, {"message_id": a.message_id, "format": "full", "via": "gmail_fetch html"})
    parts = [body_text(p) for p in walk(m["payload"]) if p.get("mimeType") == "text/html"]
    Path(a.out).write_text("\n".join(parts), encoding="utf-8")
    print(a.out, sum(len(p) for p in parts), "chars")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(required=True)
    p = sub.add_parser("links"); p.add_argument("message_id"); p.add_argument("--reason", required=True); p.set_defaults(f=cmd_links)
    p = sub.add_parser("save"); p.add_argument("message_id"); p.add_argument("out_dir"); p.add_argument("--prefix", default="")
    p.add_argument("--reason", required=True); p.set_defaults(f=cmd_save)
    p = sub.add_parser("html"); p.add_argument("message_id"); p.add_argument("out"); p.add_argument("--reason", required=True); p.set_defaults(f=cmd_html)
    a = ap.parse_args()
    try:
        a.f(a)
    except g.AccessError as e:
        print(e, file=sys.stderr); return 1


if __name__ == "__main__":
    sys.exit(main() or 0)
