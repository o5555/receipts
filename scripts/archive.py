#!/usr/bin/env python3
"""Receipt archive (kvittoarkiv): one markdown page per business receipt, files next to it.

Every receipt becomes <root>/<entity>/<YYYY>/<stem>.md: YAML frontmatter with the key
facts, a short Swedish summary, links to the copied original file(s) and a rendered
preview, and the full receipt text as extracted by scripts/receipt_text.py. The pages
are OBrain-compatible (type, title, created, updated, tags; relative links). Sources
are the Pleo export folder, the Pleo lane's MATCHING.csv, the classified Amex ledger
with its fetched receipt files, or a single file given by hand.

Rules: files are copied, never moved; nothing outside the archive root is written;
no network, no Gmail, no Pleo, no Fortnox. out/gmail-cache.json is read for the mail
subject and sender only. A file already in the archive (same sha256) merges its new
metadata into the existing page; a receipt seen from a second source (same entity,
date, amount and Pleo expense id or Amex ref) merges files and metadata. Ids and the
Pleo attach date are never overwritten: the same file on the same charge under another
expense id is refused with a reason, and a Pleo export adds its export state to an
attached page as 'attached 2026-08-24 (QUEUED)'. Re-running an ingest is a no-op; text
is re-extracted on a merge only when new files arrive or --retext is given. Every
skipped row is printed with its reason, and every created or updated page gets
plausibility warnings (receipt date long before the charge, invoice number seen on
another charge, charge amount missing from the text, receipt made out to another own
company) that `check` repeats.

Usage:
  scripts/archive.py [--root PATH] [--dry-run] [--no-text] [--retext] ingest pleo-export DIR [--entity viseo] [--export-date YYYY-MM-DD]
  scripts/archive.py ingest matching out/receipts/MATCHING.csv [--files-dir DIR] [--entity viseo]
  scripts/archive.py ingest amex --ledger CSV --receipts-dir DIR [--matches JSON ...]
  scripts/archive.py add FILE [FILE ...] --entity E --date D --merchant M --amount-sek X [--card pleo ...]
  scripts/archive.py set ID key=value [key=value ...]
  scripts/archive.py index
  scripts/archive.py find [--entity E] [--vendor SUBSTR] [--month YYYY-MM] [--amount-sek X] [--json] ...
  scripts/archive.py check
  scripts/archive.py sync-fortnox out/fortnox-run-YYYY-MM      (after fortnox_execute.py: verifikat numbers onto the pages)
  scripts/archive.py text FILE [--preview OUT.png]

Root: --root, else $RECEIPTS_ARCHIVE, else <repo>/archive. --dry-run prints the planned
actions and writes nothing; --no-text skips extraction (text_method none); --retext
re-runs extraction and harvesting on every page an ingest merges into.

Library: Archive(root).add(files, meta, text=True) -> Record, .find(**criteria), .get(id),
.set(id, **changes), .write_index(), .check() -> list[str]; module functions fm_dump,
fm_load, sv_amount, parse_amount, make_stem, vendor_for. EXTRACTOR is the text
extraction hook (receipt_text.extract when importable, a stub otherwise); tests replace it.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sys
import unicodedata
from datetime import date as date_type, datetime, timezone
from email.utils import parseaddr
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
DEFAULT_ROOT = Path(os.environ.get("RECEIPTS_ARCHIVE") or (ROOT / "archive"))
ALIASES_PATH = SCRIPTS / "vendor_aliases.json"
RULES_PATH = SCRIPTS / "merchant_rules.json"
GMAIL_CACHE = ROOT / "out" / "gmail-cache.json"

KEY_ORDER = ["type", "title", "id", "entity", "vendor", "merchant", "date", "receipt_date", "amount_sek",
             "currency", "amount", "vat_sek", "vat_rate", "kind", "card", "lane", "invoice_number",
             "vendor_vat_number", "vendor_country", "pleo_expense_id", "pleo_receipt_number", "pleo_status",
             "pleo_category", "amex_ref", "fortnox_voucher", "fortnox_status", "bas_account", "vat_regime",
             "source_kind", "source_ref", "source_from", "source_subject", "source_url", "files", "preview",
             "sha256", "text_method", "text_truncated", "note", "tags", "created", "updated"]
REQUIRED = ["type", "title", "id", "entity", "vendor", "merchant", "date", "amount_sek", "currency", "amount",
            "kind", "card", "lane", "source_kind", "files", "sha256", "text_method", "tags", "created", "updated"]
OPTIONAL = [k for k in KEY_ORDER if k not in REQUIRED]
# Keys the archive derives itself; never taken from ingest metadata or set by hand.
DERIVED = {"type", "id", "title", "files", "preview", "sha256", "text_method", "text_truncated", "tags",
           "created", "updated"}
SETTABLE = (set(OPTIONAL) - DERIVED) | {"pleo_status", "fortnox_voucher", "fortnox_status", "note", "vendor", "tags",
                                          "source_kind"}
# On merge these overwrite when non-empty; every other key (ids included) only fills a blank,
# and pleo_status goes through merge_pleo_status.
OVERWRITE_KEYS = {"fortnox_voucher", "fortnox_status", "source_kind", "source_ref",
                  "source_from", "source_subject", "source_url"}
ID_KEYS = {"pleo_expense_id": "Pleo expense", "amex_ref": "Amex ref"}
# Filled from the receipt text when the source gives nothing; recomputed on --retext.
HARVEST_KEYS = {"receipt_date", "invoice_number", "vendor_vat_number", "vat_sek"}
SOURCE_KEYS = ["source_kind", "source_ref", "source_from", "source_subject", "source_url"]
SOURCE_RANK = {"gmail": 3, "portal": 2, "pleo-export": 2, "paper": 2, "manual": 1}
NUMERIC_KEYS = {"amount_sek", "amount", "vat_sek", "vat_rate"}
KNOWN_ID_RE = re.compile(r"\bGmail\s+([0-9a-f]{16})\b")
DATE_KEYS = {"date", "receipt_date", "created", "updated"}
CARD_LABELS = {"amex-1022": "Amex 1022", "amex-2004": "Amex 2004", "amex-3003": "Amex 3003",
               "amex-1006": "Amex 1006", "pleo": "Pleo-kortet", "pocket": "eget utlagg", "unknown": "okant kort"}
ENTITY_NAMES = {"viseo": "Viseo AB", "5555media": "5555 Media AB"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".tif", ".tiff", ".bmp", ".heic"}
HTML_EXTS = {".html", ".htm"}
RECEIPT_EXTS = IMAGE_EXTS | HTML_EXTS | {".pdf"}
TEXT_CAP = 20000
JUNK_IMAGE_BYTES = 5000
RECEIPT_DATE_WINDOW_DAYS = 45
RECEIPT_DATE_EARLY_DAYS = 10  # a receipt dated further before the charge draws a warning
RECEIPT_DATE_LATE_DAYS = 7  # a receipt dated further after the charge draws a warning
OWN_VAT_NUMBERS = {"SE556840887501"}
NO_TEXT_LINE = "Ingen text kunde lasas ur filen."
TEXT_HEADING = "## Kvittotext"
DASHES_RE = re.compile(r"[ \t]*[\u2013\u2014\u2015][ \t]*")
AMOUNT_RE = re.compile(r"[-+]?(?:\d[\d.,]*|[.,]\d+)")
MINUS_SIGNS = "\u2212\u2010\u2011\u2012\u2013\u2014"
AMOUNT_TOKEN = r"\d+(?:[ \u00a0]\d{3})*(?:[.,]\d{1,2})?"


def warn(msg):
    print(f"warning: {msg}", file=sys.stderr)


# ----------------------------------------------------------------------------- text extraction hook

def _no_text_extract(path, preview_path=None, max_pages=3):
    """EXTRACTOR stub for --no-text: no text, no preview, never touches the helper."""
    return {"text": "", "method": "none", "pages": 0, "preview": False, "error": None}


def _resolve_extractor():
    """receipt_text.extract when the helper imports, else a stub that reports why."""
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    try:
        import receipt_text  # noqa: WPS433 - sibling module, imported on first use only
        return receipt_text.extract
    except Exception as exc:  # noqa: BLE001 - archive must work without the helper
        error = f"receipt_text not importable: {type(exc).__name__}: {exc}"

        def missing(path, preview_path=None, max_pages=3):
            return {"text": "", "method": "none", "pages": 0, "preview": False, "error": error}
        return missing


def _lazy_extract(path, preview_path=None, max_pages=3):
    """Default EXTRACTOR: resolves receipt_text on the first call and replaces itself."""
    global EXTRACTOR
    fn = _resolve_extractor()
    if EXTRACTOR is _lazy_extract:
        EXTRACTOR = fn
    return fn(path, preview_path=preview_path, max_pages=max_pages)


EXTRACTOR = _lazy_extract


# ----------------------------------------------------------------------------- amounts, dates, slugs

def sv_amount(x, cur="SEK"):
    """3570.54 -> '3 570,54 SEK' (ASCII space as thousands separator, comma decimals)."""
    s = f"{abs(float(x)):,.2f}".replace(",", "\x00").replace(".", ",").replace("\x00", " ")
    return f"{s} {cur}".strip()


def parse_amount(s):
    """'-1 234,56' (also with a U+2212 minus) -> -1234.56. Handles NBSP, spaces, comma or dot decimals,
    thousands separators and trailing text ('123,75 SEK'). None when no number."""
    if s is None:
        return None
    if isinstance(s, bool):
        return None
    if isinstance(s, (int, float)):
        return float(s)
    t = str(s)
    for ch in MINUS_SIGNS:
        t = t.replace(ch, "-")
    t = re.sub(r"[\u00a0\u202f\s]", "", t)
    m = AMOUNT_RE.search(t)
    if not m:
        return None
    if re.match(r"[eE][-+]?\d", t[m.end():]):
        return None  # scientific notation is never an amount
    num = m.group(0)
    sign = -1.0 if num.startswith("-") else 1.0
    num = num.lstrip("+-").rstrip(".,")
    if "," in num and "." in num:
        if num.rfind(",") > num.rfind("."):
            num = num.replace(".", "").replace(",", ".")
        else:
            num = num.replace(",", "")
    elif "," in num:
        num = num.replace(",", "") if num.count(",") > 1 else num.replace(",", ".")
    elif num.count(".") > 1:
        num = num.replace(".", "")
    try:
        return sign * float(num)
    except ValueError:
        return None


def iso_date(s):
    """'20-08-2026' / '20.08.2026' / '2026-08-20' -> '2026-08-20'; None when unparseable."""
    s = str(s or "").strip()
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})(?:[T ].*)?", s)
    if m:
        y, mo, d = m.groups()
    else:
        m = re.fullmatch(r"(\d{1,2})[-./](\d{1,2})[-./](\d{4})", s)
        if not m:
            return None
        d, mo, y = m.groups()
    try:
        return date_type(int(y), int(mo), int(d)).isoformat()
    except ValueError:
        return None


def now_stamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slugify(text, max_len=24):
    """ASCII-fold (a a o e for Swedish letters, other non-ASCII dropped), lowercase,
    non-alphanumeric runs -> '-', trimmed, capped, never empty."""
    s = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    if len(s) > max_len:
        s = s[:max_len].rstrip("-")
    return s or "okand"


def make_stem(date, vendor, amount_sek):
    """'2026-07-10', 'Wincher' or 'wincher', 3570.54 -> '2026-07-10-wincher-3570-54'."""
    kr, ore = f"{abs(float(amount_sek)):.2f}".split(".")
    return f"{iso_date(date) or date}-{slugify(vendor)}-{kr}-{ore}"


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def is_empty(v):
    return v is None or v == "" or v == [] or v is False


def plain_dashes(s):
    """Em and en dashes in source text become ' - '; NBSP becomes a space; stripped."""
    s = str(s).replace("\u00a0", " ").replace("\u202f", " ")
    return DASHES_RE.sub(" - ", s).strip()


def title_case(text):
    """Capitalise all-caps or all-lowercase words, keep mixed case ('iCloud') as is."""
    out = []
    for tok in str(text).split(" "):
        out.append(tok.capitalize() if (tok.isupper() or tok.islower()) else tok)
    return " ".join(out)


# ----------------------------------------------------------------------------- vendor canonicalisation

_ALIASES = None
_RULES = None


def load_aliases():
    """scripts/vendor_aliases.json: (vendors dict in file order, stopword set)."""
    global _ALIASES
    if _ALIASES is None:
        try:
            with open(ALIASES_PATH, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            warn(f"{ALIASES_PATH}: {exc}; vendor names fall back to the merchant text")
            data = {}
        stop = set(w.upper() for w in (data.get("generic") or {}).get("merchant_stopwords") or [])
        stop.add("*")
        _ALIASES = (data.get("vendors") or {}, stop)
    return _ALIASES


def load_rules():
    """scripts/merchant_rules.json rules list (name + patterns)."""
    global _RULES
    if _RULES is None:
        try:
            with open(RULES_PATH, encoding="utf-8") as fh:
                _RULES = json.load(fh).get("rules") or []
        except (OSError, ValueError) as exc:
            warn(f"{RULES_PATH}: {exc}; merchant rules not used for vendor names")
            _RULES = []
    return _RULES


# Payment processors whose name leads a card descriptor ('PADDLE.NET* BARTENDER', 'FS *typora.io',
# 'SVEA*PROFFSMAGASIN', 'Zettle_*Rocoto Import'): the merchant is the part after the star.
PROCESSORS = {"PADDLE.NET", "PADDLE", "FS", "ZETTLE", "IZ", "IZETTLE", "SQ", "SUMUP", "PAYPAL", "PP",
              "LINK.COM", "CKO", "SVEA", "KLARNA", "STRIPE", "TST", "SP", "DRI", "2CO", "CHECKOUT",
              "ADYEN", "MOLLIE", "NETS", "BAMBORA", "TRUSTLY"}
STAR_RE = re.compile(r"^\s*([^*]+?)\s*\*\s*(.+?)\s*$")
TLD_RE = re.compile(r"(?<=[A-Za-z0-9])\.(?:io|co|net|ai|dev|app|org|eu|com|se|nu|uk|us|de|dk|no|fi)\b", re.I)


def merchant_core(merchant):
    """The part of a card descriptor that names the merchant. 'LEFT* RIGHT' carries the
    merchant on the left and a product or reference on the right ('1PASSWORD* TRIAL OVER',
    'DISCORD* NITROYEARLY', 'Dropbox*8SNF1BW4CHHL') unless the left part is a payment
    processor, in which case the merchant is on the right ('PADDLE.NET* BARTENDER LISBOA')."""
    text = str(merchant or "").strip()
    m = STAR_RE.match(text)
    if not m:
        return text
    left, right = m.group(1), m.group(2)
    if left.strip().upper().rstrip("_") in PROCESSORS:
        return right
    return left


def is_statement_code(token):
    """N61QQ6964, 8SNF1BW4CHHL, RXVD9D: letters and digits mixed the way statement references
    are, never a brand such as 1PASSWORD (one leading digit, then a word)."""
    if len(token) < 6 or not re.search(r"\d", token) or not re.search(r"[A-Za-z]", token):
        return False
    return len(re.findall(r"\d", token)) >= 2 or re.search(r"[A-Za-z]\d", token) is not None


def clean_merchant(merchant):
    """Merchant text without processor prefix, domain suffix, stopwords and statement codes
    ('KJELL & CO 121 STOCKHOLM' -> 'KJELL & CO 121', 'FS *typora.io' -> 'typora')."""
    stop = load_aliases()[1]
    text = TLD_RE.sub(" ", merchant_core(merchant))
    tokens = re.split(r"[\s*./\\|_]+", text.strip())
    kept = []
    for t in tokens:
        if not t or t.upper() in stop or t.upper() in (k.upper() for k in kept):
            continue
        if is_statement_code(t):
            continue
        kept.append(t)
    return " ".join(kept)


def _display_name(name, merchant_low):
    """Alias and rule names carry grouping hints: drop a trailing parenthetical and pick
    the half of an 'A / B' name that occurs in the merchant text."""
    name = re.sub(r"\s*\([^)]*\)\s*$", "", name).strip()
    if " / " in name:
        parts = [p.strip() for p in name.split(" / ")]
        name = next((p for p in parts if p.lower() in merchant_low), parts[0])
    return name


def _amount_listed(v, amount, currency):
    """True when the alias vendor lists this (amount, currency) among its known charges."""
    want = parse_amount(amount)
    if want is None:
        return False
    cur = str(currency or "").upper()
    for a in v.get("amounts") or []:
        if not isinstance(a, dict):
            continue
        have = parse_amount(a.get("amount"))
        if have is not None and abs(have - want) <= 0.01 and str(a.get("currency") or "").upper() == cur:
            return True
    return False


def vendor_for(merchant, amount=None, currency=None):
    """Canonical vendor for a merchant text: (name or None, slug). Alias vendors first
    (longest matching merchant string wins, regex and exact honoured, slug = alias key;
    among vendors tied on the same merchant string the one listing the charge amount
    wins, so 'ANTHROPIC* CLAUDE SUB' at 22,50 EUR is Claude Pro and at 180 EUR Claude
    Max), then merchant_rules patterns, else the cleaned merchant text (name None)."""
    text = re.sub(r"\s+", " ", str(merchant or "")).strip()
    low = text.lower()
    vendors = load_aliases()[0]
    best = None
    ties = []
    for order, (key, v) in enumerate(vendors.items()):
        length = 0
        rx = v.get("merchant_regex")
        if rx:
            try:
                m = re.search(rx, text, re.I)
            except re.error:
                m = None
            if m:
                length = max(length, len(m.group(0)) or 1)
        for pat in v.get("merchants") or []:
            p = re.sub(r"\s+", " ", str(pat)).strip().lower()
            if not p:
                continue
            if v.get("merchant_exact"):
                if low == p:
                    length = max(length, len(p))
            elif p in low:
                length = max(length, len(p))
        if length and (best is None or length > best[0]):
            best = (length, order, key, v.get("name") or key)
            ties = [(order, key, v)]
        elif length and best is not None and length == best[0]:
            ties.append((order, key, v))
    if best and len(ties) > 1 and amount is not None:
        for order, key, v in ties:
            if _amount_listed(v, amount, currency):
                best = (best[0], order, key, v.get("name") or key)
                break
    if best:
        if best[2].endswith("_other"):
            # a payment processor alias (paddle_other): the vendor is named by the merchant text
            name = title_case(clean_merchant(text)) or title_case(text)
            return name, slugify(name)
        return best[3], slugify(best[2].replace("_", "-"))
    for rule in load_rules():
        for pat in rule.get("patterns") or []:
            if pat and pat.lower() in low:
                name = _display_name(rule.get("name") or pat, low)
                return name, slugify(name)
    cleaned = clean_merchant(text)
    return None, slugify(cleaned)


def resolve_vendor(meta):
    """(vendor name for the page, slug) honouring an explicit meta['vendor']."""
    given = str(meta.get("vendor") or "").strip()
    amount, currency = meta.get("amount"), meta.get("currency")
    if given:
        name, slug = vendor_for(meta.get("merchant") or given, amount, currency)
        return given, slug if (name and name.lower() == given.lower()) else slugify(given)
    name, slug = vendor_for(meta.get("merchant") or "", amount, currency)
    if name:
        return name, slug
    merchant = str(meta.get("merchant") or "")
    return title_case(clean_merchant(merchant)) or title_case(merchant.strip()) or "Okand", slug


# ----------------------------------------------------------------------------- frontmatter

PLAIN_UNSAFE = set(":#'\"[]{},&*!|>%@`\\")
NUMBER_RE = re.compile(r"^[-+]?(?:\d+\.?\d*(?:[eE][-+]?\d+)?|\.\d+(?:[eE][-+]?\d+)?)$")
DATE_LIKE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[Tt ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[-+]\d{2}:?\d{2})?)?$")
WORD_LIKE = {"true", "false", "yes", "no", "on", "off", "y", "n", "null", "~"}


def needs_quote(s, key=None):
    if s == "" or s != s.strip():
        return True
    if any(ch in PLAIN_UNSAFE or ord(ch) < 32 or ord(ch) == 127 for ch in s):
        return True
    if s[0] in "-?":
        return True
    if s.lower() in WORD_LIKE or NUMBER_RE.match(s):
        return True
    if DATE_LIKE_RE.match(s):
        return key not in DATE_KEYS
    return False


def fm_quote(s):
    out = []
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif ord(ch) < 32 or ord(ch) == 127:
            out.append("\\u%04x" % ord(ch))
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def fm_scalar(v, key=None):
    if v is True:
        return "true"
    if v is False:
        return "false"
    if v is None:
        return "~"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError(f"frontmatter number must be finite: {key}={v!r}")
        return repr(v)
    if isinstance(v, str):
        return fm_quote(v) if needs_quote(v, key) else v
    raise TypeError(f"frontmatter value of unsupported type for {key}: {type(v).__name__}")


def fm_dump(d):
    """dict -> '---\\n...\\n---\\n'. Known keys in KEY_ORDER, unknown keys after; None omitted."""
    lines = ["---"]
    keys = [k for k in KEY_ORDER if k in d] + [k for k in d if k not in KEY_ORDER]
    for k in keys:
        v = d[k]
        if v is None:
            continue
        if isinstance(v, (list, tuple)):
            lines.append(f"{k}: [" + ", ".join(fm_scalar(x) for x in v) + "]")
        else:
            lines.append(f"{k}: {fm_scalar(v, k)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def _split_inline_list(s):
    items, buf, quote, i = [], [], None, 0
    while i < len(s):
        ch = s[i]
        if quote:
            buf.append(ch)
            if quote == '"' and ch == "\\" and i + 1 < len(s):
                buf.append(s[i + 1])
                i += 1
            elif ch == quote:
                if quote == "'" and i + 1 < len(s) and s[i + 1] == "'":
                    buf.append("'")
                    i += 1
                else:
                    quote = None
        elif ch in "\"'" and not "".join(buf).strip():
            buf = [ch]
            quote = ch
        elif ch == ",":
            items.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail or items:
        items.append(tail)
    return [x for x in items if x != ""] if not any(x == "" for x in items) else items


def _end_of_quoted(s, quote):
    i = 1
    while i < len(s):
        ch = s[i]
        if quote == '"' and ch == "\\":
            i += 2
            continue
        if ch == quote:
            if quote == "'" and i + 1 < len(s) and s[i + 1] == "'":
                i += 2
                continue
            return i
        i += 1
    raise ValueError(f"unterminated quoted string: {s!r}")


def fm_parse_scalar(s):
    s = s.strip()
    if s == "":
        return None
    if s[0] == '"':
        end = _end_of_quoted(s, '"')
        return json.loads(s[:end + 1], strict=False)
    if s[0] == "'":
        end = _end_of_quoted(s, "'")
        return s[1:end].replace("''", "'")
    s = re.sub(r"\s+#.*$", "", s).strip()
    if s in ("~", "null", "Null", "NULL"):
        return None
    if s in ("true", "True", "TRUE"):
        return True
    if s in ("false", "False", "FALSE"):
        return False
    if NUMBER_RE.match(s):
        try:
            if re.fullmatch(r"[-+]?\d+", s):
                return int(s)
            return float(s)
        except ValueError:
            pass
    return s


def fm_parse_value(s):
    s = s.strip()
    if s.startswith("["):
        end = s.rfind("]")
        if end < 0:
            raise ValueError(f"unterminated inline list: {s!r}")
        inner = s[1:end].strip()
        return [fm_parse_scalar(x) for x in _split_inline_list(inner)] if inner else []
    return fm_parse_scalar(s)


def fm_load(text):
    """'---\\n...\\n---\\n<body>' -> (dict, body). ({}, text) when there is no frontmatter.
    Accepts everything fm_dump writes plus block lists, single quotes, ~/null, true/false."""
    if text.startswith("\ufeff"):
        text = text[1:]
    lines = text.split("\n")
    if not lines or lines[0].rstrip() != "---":
        return {}, text
    end = None
    for i in range(1, len(lines)):
        if lines[i].rstrip() in ("---", "..."):
            end = i
            break
    if end is None:
        return {}, text
    body = "\n".join(lines[end + 1:])
    if body.startswith("\n"):
        body = body[1:]
    data = {}
    i = 1
    while i < end:
        raw = lines[i]
        i += 1
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_][\w.-]*)\s*:(?:\s+(.*)|\s*)$", s)
        if not m:
            raise ValueError(f"frontmatter line {i}: {raw!r}")
        key, val = m.group(1), (m.group(2) or "").strip()
        if val == "" or val.startswith("#"):
            items = []
            while i < end and re.match(r"^\s*-(\s|$)", lines[i]):
                items.append(fm_parse_scalar(re.sub(r"^\s*-\s?", "", lines[i])))
                i += 1
            data[key] = items if items else None
        else:
            data[key] = fm_parse_value(val)
    return data, body


# ----------------------------------------------------------------------------- receipt text and harvest

def clean_text(text):
    """NBSP -> space, dashes -> ' - ', trailing spaces stripped, 3+ blank lines -> 2,
    capped at TEXT_CAP characters. Returns (text, truncated)."""
    t = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    for sep in ("\u2028", "\u2029", "\u0085", "\x0c"):
        t = t.replace(sep, "\n")
    t = t.replace("\u00a0", " ").replace("\u202f", " ")
    t = DASHES_RE.sub(" - ", t)
    t = "\n".join(ln.rstrip() for ln in t.split("\n"))
    t = re.sub(r"\n{4,}", "\n\n\n", t).strip("\n")
    truncated = len(t) > TEXT_CAP
    if truncated:
        t = t[:TEXT_CAP].rstrip()
    return t, truncated


MONTHS = {"january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4,
          "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9,
          "sep": 9, "sept": 9, "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
          "januari": 1, "februari": 2, "mars": 3, "maj": 5, "juni": 6, "juli": 7, "augusti": 8, "okt": 10}
_MONTH_ALT = "|".join(sorted(MONTHS, key=len, reverse=True))
ISO_DATE_RE = re.compile(r"(?<!\d)(\d{4})[-/.](\d{2})[-/.](\d{2})(?!\d)")
NUM_DATE_RE = re.compile(r"(?<![\d.])(\d{1,2})[./](\d{1,2})[./](\d{4})(?!\d)")
DMY_RE = re.compile(r"(?<!\d)(\d{1,2})(?:st|nd|rd|th)?\.?\s+(" + _MONTH_ALT + r")\.?,?\s+(\d{4})(?!\d)", re.I)
MDY_RE = re.compile(r"(?<![A-Za-z])(" + _MONTH_ALT + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})(?!\d)", re.I)


def _safe_date(y, m, d):
    try:
        return date_type(int(y), int(m), int(d))
    except ValueError:
        return None


def text_dates(text):
    """Every date printed in the text as (position, [readings]) in text order; numeric
    D/M vs M/D forms carry both readings."""
    found = []
    for m in ISO_DATE_RE.finditer(text):
        found.append((m.start(), [_safe_date(m.group(1), m.group(2), m.group(3))]))
    for m in NUM_DATE_RE.finditer(text):
        a, b, y = m.groups()
        found.append((m.start(), [_safe_date(y, b, a), _safe_date(y, a, b)]))
    for m in DMY_RE.finditer(text):
        found.append((m.start(), [_safe_date(m.group(3), MONTHS[m.group(2).lower()], m.group(1))]))
    for m in MDY_RE.finditer(text):
        found.append((m.start(), [_safe_date(m.group(3), MONTHS[m.group(1).lower()], m.group(2))]))
    found.sort(key=lambda t: t[0])
    return found


DATE_LABEL_RE = re.compile(r"(?i)\b(?:date\s+paid|paid\s+on|payment\s+date|order\s+date|date\s+of\s+issue"
                           r"|issue\s+date|invoice\s+date|billing\s+date|receipt\s+date|transaction\s+date"
                           r"|purchase\s+date|fakturadatum|orderdatum|betalningsdatum|betaldatum|kvittodatum"
                           r"|kvittosammanst[a\u00e4]llningsdatum|datum\s+f[o\u00f6]r\s+betalning|datum)\b"
                           r"[ \t:]*\n?[ \t]*")
MAIL_HEADER_RE = re.compile(r"^\s*(?:Date|Sent|Datum|Skickat)\s*:", re.I)
MAIL_NEIGHBOUR_RE = re.compile(r"^\s*(?:From|Fr[a\u00e5]n|Subject|[A\u00c4]mne|To|Till|Cc)\s*:", re.I)
SHIPPING_RE = re.compile(r"(?i)skickas|levereras|leverans|deliver|shipping|\bships\b|estimated")


def _ignored_spans(text):
    """Character spans of lines whose dates are not the receipt's own: the Date:/Sent: line of
    a forwarded mail header (next to From:, Subject:, To:) and shipping estimates
    ('Din order beraknas att skickas 2026-03-30')."""
    spans = []
    lines = text.split("\n")
    pos = 0
    for i, line in enumerate(lines):
        end = pos + len(line)
        if MAIL_HEADER_RE.match(line):
            around = range(max(0, i - 4), min(len(lines), i + 5))
            if any(j != i and MAIL_NEIGHBOUR_RE.match(lines[j]) for j in around):
                spans.append((pos, end))
        elif SHIPPING_RE.search(line):
            spans.append((pos, end))
        pos = end + 1
    return spans


def harvest_receipt_date(text, charge_date):
    """The date printed on the receipt: a date right after a receipt label (Date paid, Order
    date, Fakturadatum, Datum, ...) within 45 days of the charge date first, else the first
    such date in text order. Numeric D/M vs M/D forms count only when just one reading is
    in the window; dates on forwarded-mail header lines and shipping estimates never count."""
    try:
        base = date_type.fromisoformat(str(charge_date))
    except (TypeError, ValueError):
        return None
    ignored = _ignored_spans(text)

    def usable(pos, opts):
        if any(a <= pos <= b for a, b in ignored):
            return None
        near = {d for d in opts if d is not None and abs((d - base).days) <= RECEIPT_DATE_WINDOW_DAYS}
        return near.pop().isoformat() if len(near) == 1 else None

    dates = text_dates(text)
    for m in DATE_LABEL_RE.finditer(text):
        for pos, opts in dates:
            if pos < m.end():
                continue
            if pos <= m.end() + 24:
                d = usable(pos, opts)
                if d:
                    return d
            break
    for pos, opts in dates:
        d = usable(pos, opts)
        if d:
            return d
    return None


INVOICE_LABELS = [r"Invoice\s*number", r"Invoice\s*#", r"Invoice\s*no\.?", r"Receipt\s*#", r"Receipt\s*number",
                  r"Fakturanummer", r"Fakturanr\.?", r"Kvittonummer", r"Kvittonr\.?", r"Order\s*number",
                  r"Ordernummer", r"Receipt"]
INVOICE_RES = [re.compile(r"(?<![A-Za-z])" + lab + r"\s*[:#.]?\s*([A-Za-z0-9][A-Za-z0-9._/-]*)", re.I)
               for lab in INVOICE_LABELS]


def harvest_invoice_number(text):
    """Token after the first matching label (labels tried in order); needs a digit."""
    for rx in INVOICE_RES:
        for m in rx.finditer(text):
            tok = m.group(1).rstrip(".,;:/")
            if re.search(r"\d", tok) and not ISO_DATE_RE.fullmatch(tok):
                return tok
    return None


# Label (VAT number, VAT ID, VAT Reg # :, VAT-/momsnummer:, Momsreg.nummer, Moms nr, ... optionally
# followed by "is"), then the id: two letters plus 8 to 13 alphanumerics with at least one digit.
VAT_NUMBER_RE = re.compile(r"(?i:(?:VAT|Moms)(?:[\s.#:/\-]*(?:registreringsnummer|registration|momsnummer|number"
                           r"|nummer|reg|id|no|nr))*(?:\s+is)?)[\s.#:\-]*"
                           r"(?=[A-Z]{2}[0-9A-Z]*\d)([A-Z]{2}[0-9A-Z]{8,13})(?![0-9A-Za-z])")


def harvest_vendor_vat_number(text):
    """First VAT id after a VAT label that is not one of Oscar's own companies."""
    for m in VAT_NUMBER_RE.finditer(text):
        tok = m.group(1)
        if tok.upper() in OWN_VAT_NUMBERS:
            continue
        line_start = text.rfind("\n", 0, m.start()) + 1
        line_end = text.find("\n", m.end())
        line = text[line_start:line_end if line_end >= 0 else len(text)]
        if "5555 media" in line.lower():
            continue
        return tok
    return None


def harvest_vat_sek(text, currency, amount_sek):
    """'converted to SEK is: 699.33' first; else 'Moms 25 % 63,35' / 'VAT (25%): 63.35' for SEK receipts."""
    m = re.search(r"converted to SEK is:?\s*(" + AMOUNT_TOKEN + ")", text, re.I)
    if not m and (currency or "SEK") == "SEK":
        m = re.search(r"(?:Moms|VAT|Mervardesskatt|Merv\u00e4rdesskatt)(?:\s+[A-Za-z]+){0,3}\s*\(?\s*\d{1,2}(?:[.,]\d+)?\s*%\s*\)?"
                      r"\s*:?\s*(?:SEK|kr)?\s*(" + AMOUNT_TOKEN + ")", text, re.I)
    if not m:
        return None
    v = parse_amount(m.group(1))
    if v is None or v <= 0 or (amount_sek and v >= float(amount_sek)):
        return None
    return round(v, 2)


# Oscar's own companies as a receipt names them: lower-case form -> (display name, entity or None).
OWN_COMPANIES = {"5555 media ab": ("5555 Media AB", "5555media"), "5555 media": ("5555 Media AB", "5555media"),
                 "5555 holding ab": ("5555 Holding AB", None), "5555 holding": ("5555 Holding AB", None),
                 "viseo ab": ("Viseo AB", "viseo")}
BUYER_RE = re.compile(r"(?i)(?:k[o\u00f6]pare|bill(?:ed)?\s+to|sold\s+to|invoiced?\s+to|customer|kund|f[o\u00f6]r"
                      r"|till|hi|hej|organi[sz]ation|account|faktureringsadress|attn)\s*:?[ \t]*\n?[ \t]*"
                      r"(5555\s+media(?:\s+ab)?|5555\s+holding(?:\s+ab)?|viseo\s+ab)\b")


def harvest_buyer(text):
    """The own company a receipt is made out to ('Kopare 5555 Media AB', 'For 5555 Media AB',
    'Hi 5555 Media', 'Organization: 5555 Media'): (display name, entity or None), else None."""
    m = BUYER_RE.search(text or "")
    if not m:
        return None
    key = re.sub(r"\s+", " ", m.group(1)).strip().lower()
    return OWN_COMPANIES.get(key)


def harvest(text, rec):
    """Best-effort fields from the receipt text; the caller fills only missing keys. A receipt
    made out to another own company leaves a note so the page says so."""
    if not text:
        return {}
    out = {}
    d = harvest_receipt_date(text, rec.get("date"))
    if d:
        out["receipt_date"] = d
    inv = harvest_invoice_number(text)
    if inv:
        out["invoice_number"] = inv
    vat = harvest_vendor_vat_number(text)
    if vat:
        out["vendor_vat_number"] = vat
    vat_sek = harvest_vat_sek(text, rec.get("currency"), rec.get("amount_sek"))
    if vat_sek is not None:
        out["vat_sek"] = vat_sek
    buyer = harvest_buyer(text)
    if buyer and buyer[1] != rec.get("entity"):
        out["note"] = f"Kvittot ar stallt till {buyer[0]}"
    return out


def plausibility(meta, text, others):
    """Warnings for facts that do not fit the charge, the fix list Oscar works from: a
    receipt_date more than RECEIPT_DATE_EARLY_DAYS before or RECEIPT_DATE_LATE_DAYS after the
    charge date, an invoice_number that is also on a page for another charge (credit notes
    excepted), the charge amount or the vendor name missing from the receipt text, or a
    receipt made out to another own company. others: (id, meta) pairs of every other page."""
    out = []
    date, rd = str(meta.get("date") or ""), str(meta.get("receipt_date") or "")
    try:
        gap = (date_type.fromisoformat(date) - date_type.fromisoformat(rd)).days
    except ValueError:
        gap = 0
    if gap > RECEIPT_DATE_EARLY_DAYS:
        out.append(f"receipt_date {rd} is {gap} days before the charge date {date}; the file may be an earlier "
                   f"period's receipt")
    elif -gap > RECEIPT_DATE_LATE_DAYS:
        out.append(f"receipt_date {rd} is {-gap} days after the charge date {date}; the file may be a later "
                   f"period's receipt")
    inv = str(meta.get("invoice_number") or "")
    amount_sek = float(parse_amount(meta.get("amount_sek")) or 0)
    if inv and meta.get("kind") != "credit-note":
        for oid, om in others:
            if str(om.get("invoice_number") or "") != inv or om.get("kind") == "credit-note":
                continue
            other_amount = float(parse_amount(om.get("amount_sek")) or 0)
            if str(om.get("date")) != date or abs(other_amount - amount_sek) > 0.005:
                out.append(f"invoice_number {inv} is also on {oid} ({om.get('date')} {sv_amount(other_amount)})")
                break
    if text and str(meta.get("text_method") or "none") != "none":
        if not (amount_in_text(text, meta.get("amount")) or amount_in_text(text, meta.get("amount_sek"))):
            shown = sv_amount(amount_sek)
            cur = str(meta.get("currency") or "SEK")
            if cur != "SEK" and meta.get("amount") is not None:
                shown += f" ({sv_amount(meta['amount'], cur)})"
            out.append(f"charge amount {shown} not found in the receipt text")
        if not vendor_in_text(meta, text):
            out.append(f"vendor {meta.get('vendor') or meta.get('merchant')} not named in the receipt text; the file "
                       f"may belong to another vendor")
        buyer = harvest_buyer(text)
        if buyer and buyer[1] != meta.get("entity"):
            entity = str(meta.get("entity") or "")
            out.append(f"receipt is made out to {buyer[0]} but the page is filed under "
                       f"{ENTITY_NAMES.get(entity, entity)}")
    return out


def amount_in_text(text, value):
    """True when the amount is printed in the text as 63.42, 63,42, 1,234.56, 1 234,56,
    6.926,00 or, for whole amounts, 1234, 1 000, 1,000 or 1.000, standing on its own (not
    inside a longer number)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    grouped = "{:,.2f}".format(v)
    forms = {"%.2f" % v, ("%.2f" % v).replace(".", ","), grouped,
             grouped.replace(",", " ").replace(".", ","),
             grouped.replace(",", "\x00").replace(".", ",").replace("\x00", ".")}
    if v.is_integer():
        whole = "{:,.0f}".format(v)
        forms.update({"%d" % v, whole, whole.replace(",", " "), whole.replace(",", ".")})
    return any(re.search(r"(?<![\d.,])" + re.escape(f) + r"(?![\d.,])", text) for f in forms)


def _compact(s):
    """ASCII-folded lower-case letters and digits only, for loose name matching."""
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "", s)


def vendor_in_text(meta, text):
    """True when the first name-like word (3+ letters or digits) of the vendor or of the
    merchant text occurs in the receipt text, spaces and punctuation ignored ('Wpengine'
    matches 'WP Engine'); True as well when neither has such a word."""
    words = []
    for source in (meta.get("vendor"), meta.get("merchant")):
        for tok in re.split(r"[^A-Za-z0-9\u00c0-\u017f]+", str(source or "")):
            if len(_compact(tok)) >= 3:
                words.append(_compact(tok))
                break
    if not words:
        return True
    hay = _compact(text)
    return any(w in hay for w in words)


def text_fit(text, meta):
    """How well a receipt text fits a charge, as a sort key (higher fits better): the charge
    amount printed in the text, then the nearest printed date to the charge date."""
    m = normalise_meta(meta)
    hit = amount_in_text(text, m["amount"]) or amount_in_text(text, m["amount_sek"])
    base = date_type.fromisoformat(m["date"])
    gaps = [abs((d - base).days) for _, opts in text_dates(text) for d in opts if d is not None]
    return (1 if hit else 0, -(min(gaps) if gaps else 10 ** 6))


# ----------------------------------------------------------------------------- page rendering

def card_label(card):
    return CARD_LABELS.get(str(card or ""), CARD_LABELS["unknown"])


ATTACHED_RE = re.compile(r"^attached\s+(\d{4}-\d{2}-\d{2})(?:\s*\((.*)\))?\s*$", re.I)
EXPORT_STATE_RE = re.compile(r"^i Pleo\s*\((.*)\)\s*$", re.I)


def merge_pleo_status(old, new):
    """The status a page keeps when a source reports `new` on top of `old`. The Pleo lane's
    attach date ('attached 2026-08-24') is kept and the export state from a Pleo export
    ('i Pleo (QUEUED)') rides along in parentheses, so both sources settle on one string
    whichever runs last: 'attached 2026-08-24 (QUEUED)'. Anything else takes the new value."""
    old_s, new_s = str(old or "").strip(), str(new or "").strip()
    if not new_s or not old_s:
        return new_s or old_s
    om, nm = ATTACHED_RE.match(old_s), ATTACHED_RE.match(new_s)
    oe, ne = EXPORT_STATE_RE.match(old_s), EXPORT_STATE_RE.match(new_s)
    if om and ne:
        return f"attached {om.group(1)} ({ne.group(1).strip()})"
    if nm and not nm.group(2):
        state = (om.group(2) if om else None) or (oe.group(1) if oe else None)
        if state:
            return f"attached {nm.group(1)} ({state.strip()})"
    return new_s


def status_sentence(rec):
    """'Bifogat i Pleo 2026-08-24.', 'Bifogat i Pleo 2026-08-24 (QUEUED).', 'I Pleo (QUEUED).',
    'Ej bokfort i Fortnox.', 'Bokfort i Fortnox, verifikation A 12.'"""
    if rec.get("lane") == "pleo":
        st = str(rec.get("pleo_status") or "").strip()
        m = ATTACHED_RE.match(st)
        if m:
            state = f" ({m.group(2).strip()})" if m.group(2) else ""
            return f"Bifogat i Pleo {m.group(1)}{state}."
        if not st:
            return "I Pleo."
        if st.lower().startswith("i pleo"):
            return "I" + st[1:].rstrip(".") + "."
        return f"Pleo: {st.rstrip('.')}."
    voucher = str(rec.get("fortnox_voucher") or "").strip()
    if voucher:
        return f"Bokfort i Fortnox, verifikation {voucher}."
    st = str(rec.get("fortnox_status") or "ej bokfort").strip().rstrip(".")
    return f"{st[:1].upper()}{st[1:]} i Fortnox."


def short_status(rec):
    """Index column: the Pleo status for lane pleo, the Fortnox voucher or status otherwise."""
    if rec.get("lane") == "pleo":
        st = str(rec.get("pleo_status") or "").strip()
        if st.lower().startswith("i pleo"):
            st = st[6:].strip().strip("()")
        return f"Pleo: {st}" if st else "Pleo"
    return f"Fortnox: {rec.get('fortnox_voucher') or rec.get('fortnox_status') or 'ej bokfort'}"


def source_line(rec):
    kind = rec.get("source_kind") or "manual"
    ref = str(rec.get("source_ref") or "").strip()
    if kind == "gmail":
        s = f"Gmail {ref}".strip()
        if rec.get("source_subject"):
            s += f', "{rec["source_subject"]}"'
        if rec.get("source_from"):
            s += f" fran {rec['source_from']}"
        return s
    if kind == "pleo-export":
        s = str(rec.get("source_from") or "Pleo export")
        if rec.get("pleo_receipt_number"):
            s += f", kvitto {rec['pleo_receipt_number']}"
        return s
    if kind == "portal":
        return f"Portal {rec.get('source_url') or ref}".strip()
    if kind == "paper":
        return "Papperskvitto" + (f", {ref}" if ref else "")
    return "Manuellt" + (f", {ref}" if ref else "")


def render_head(rec, primary_size=None):
    """Title, summary sentence and the Underlag section (everything above Kvittotext)."""
    vendor = rec.get("vendor") or rec.get("merchant") or "Okand"
    cur = str(rec.get("currency") or "SEK")
    orig = ""
    if cur != "SEK":
        orig = f" ({sv_amount(rec.get('amount') if rec.get('amount') is not None else rec['amount_sek'], cur)})"
    lead = "Kreditnota: " if rec.get("kind") == "credit-note" else ""
    lines = [f"# {rec['title']}", "",
             f"{lead}{vendor}, {sv_amount(rec['amount_sek'])}{orig} den {rec['date']}, "
             f"betalt med {card_label(rec.get('card'))}. {status_sentence(rec)}",
             "", "## Underlag", ""]
    files = list(rec.get("files") or [])
    primary = files[0] if files else ""
    if primary_size is None and rec.path is not None and (rec.dir / primary).exists():
        primary_size = (rec.dir / primary).stat().st_size
    kb = max(1, round((primary_size or 0) / 1024))
    lines.append(f"- Fil: [{primary}]({primary}) ({kb} kB, sha256 {str(rec.get('sha256') or '')[:12]})")
    for f in files[1:]:
        lines.append(f"- Extra: [{f}]({f})")
    if rec.get("preview"):
        lines.append(f"- Forhandsvisning: ![{rec['id']}]({rec['preview']})")
    lines.append(f"- Kalla: {source_line(rec)}")
    pleo = []
    if rec.get("pleo_expense_id"):
        pleo.append(f"utgift {rec['pleo_expense_id']}")
    if rec.get("pleo_receipt_number"):
        pleo.append(f"kvitto {rec['pleo_receipt_number']}")
    if rec.get("pleo_status"):
        pleo.append(str(rec["pleo_status"]))
    if pleo:
        lines.append("- Pleo: " + ", ".join(pleo))
    if rec.get("lane") in ("amex-fortnox", "pocket"):
        lines.append("- Fortnox: " + str(rec.get("fortnox_voucher") or rec.get("fortnox_status") or "ej bokfort"))
    kont = []
    if rec.get("bas_account"):
        kont.append(str(rec["bas_account"]))
    if rec.get("vat_regime"):
        kont.append(f"moms {rec['vat_regime']}")
    elif rec.get("vat_rate") is not None:
        kont.append(f"moms {rec['vat_rate']} %")
    if kont:
        lines.append("- Kontering: " + ", ".join(kont))
    if rec.get("note"):
        lines.append(f"- Anteckning: {rec['note']}")
    return "\n".join(lines) + "\n"


def render_text_section(text):
    if not (text or "").strip():
        return f"{TEXT_HEADING}\n\n{NO_TEXT_LINE}\n"
    fence = "```"
    while fence in text:
        fence += "`"
    return f"{TEXT_HEADING}\n\n{fence}text\n{text}\n{fence}\n"


def split_body(body):
    """(head, text section) of a page body; the text section starts at '## Kvittotext'."""
    idx = body.find(TEXT_HEADING)
    if idx < 0:
        return body, render_text_section("")
    return body[:idx], body[idx:]


def body_text(body):
    """The verbatim receipt text inside the Kvittotext fence ('' when none)."""
    _, section = split_body(body)
    m = re.search(r"^(`{3,})text\n(.*?)\n\1\s*$", section, re.S | re.M)
    return m.group(2) if m else ""


def render_body(rec, text, primary_size=None):
    return render_head(rec, primary_size) + "\n" + render_text_section(text)


def rerender_head(rec):
    """New head, old Kvittotext kept verbatim."""
    _, section = split_body(rec.body or "")
    rec.body = render_head(rec) + "\n" + section


# ----------------------------------------------------------------------------- records and the archive

class Record(dict):
    """Frontmatter of one archive page as a dict, plus .path (the .md), .body and .outcome."""

    def __init__(self, meta=None, path=None, body=""):
        super().__init__(meta or {})
        self.path = Path(path) if path is not None else None
        self.body = body
        self.outcome = ""
        self.shas = {}  # sha256 -> file name for files this run added (also what a dry run plans)

    @property
    def dir(self):
        return self.path.parent if self.path is not None else None

    def rel(self, root):
        return str(self.path.relative_to(root)) if self.path is not None else ""


def read_page(path):
    with open(path, encoding="utf-8") as fh:
        meta, body = fm_load(fh.read())
    return Record(meta, path=path, body=body)


def write_page(rec):
    rec.path.parent.mkdir(parents=True, exist_ok=True)
    with open(rec.path, "w", encoding="utf-8") as fh:
        fh.write(fm_dump(dict(rec)) + "\n" + (rec.body or ""))


def normalise_meta(meta):
    """Validate and coerce ingest metadata: ISO date, rounded positive amounts, defaults
    for currency/amount/kind/card/lane/source_kind, empty values dropped."""
    m = {k: v for k, v in dict(meta).items() if k not in DERIVED}
    for k in list(m):
        if isinstance(m[k], str):
            m[k] = plain_dashes(m[k])
    entity = str(m.get("entity") or "").strip()
    if entity not in ENTITY_NAMES:
        raise ValueError(f"entity must be one of {', '.join(sorted(ENTITY_NAMES))}, got {entity!r}")
    m["entity"] = entity
    d = iso_date(m.get("date"))
    if not d:
        raise ValueError(f"date must be YYYY-MM-DD, got {m.get('date')!r}")
    m["date"] = d
    if not str(m.get("merchant") or "").strip():
        raise ValueError("merchant is required")
    amount_sek = parse_amount(m.get("amount_sek"))
    if amount_sek is None or abs(amount_sek) < 0.005:
        raise ValueError(f"amount_sek must be a non-zero number, got {m.get('amount_sek')!r}")
    m["amount_sek"] = round(abs(amount_sek), 2)
    m["currency"] = str(m.get("currency") or "SEK").upper()
    amount = parse_amount(m.get("amount"))
    m["amount"] = round(abs(amount), 2) if amount is not None else m["amount_sek"]
    if m["currency"] == "SEK":
        m["amount"] = m["amount_sek"]
    for k in ("vat_sek", "vat_rate"):
        if k in m and not is_empty(m[k]):
            v = parse_amount(m[k])
            if v is None or abs(v) < 0.0005:
                m.pop(k)
            else:
                v = round(abs(v), 2)
                m[k] = int(v) if float(v).is_integer() else v
    m["kind"] = m.get("kind") or "receipt"
    m["card"] = m.get("card") or "unknown"
    if not m.get("lane"):
        card = m["card"]
        m["lane"] = "pleo" if card == "pleo" else ("amex-fortnox" if card.startswith("amex") else "pocket")
    if m["lane"] in ("amex-fortnox", "pocket") and not m.get("fortnox_voucher") and not m.get("fortnox_status"):
        m["fortnox_status"] = "ej bokfort"
    m["source_kind"] = m.get("source_kind") or "manual"
    if m.get("receipt_date"):
        rd = iso_date(m["receipt_date"])
        if rd:
            m["receipt_date"] = rd
        else:
            m.pop("receipt_date")
    for k in ("bas_account", "pleo_receipt_number", "invoice_number"):
        if k in m and not is_empty(m[k]):
            m[k] = str(m[k])
    return {k: v for k, v in m.items() if not is_empty(v)}


def _html_among(paths):
    return next((p for p in paths if Path(p).suffix.lower() in HTML_EXTS), None)


class Archive:
    """The receipt archive under one root. dry_run: plan and report, write nothing. retext:
    re-run text extraction and harvesting on every page a merge touches."""

    def __init__(self, root=None, dry_run=False, retext=False):
        self.root = Path(root or DEFAULT_ROOT)
        self.dry_run = dry_run
        self.retext = retext
        self._records = None
        self._file_shas = None
        self._reserved = set()

    # -- loading

    def page_paths(self):
        return sorted(p for p in self.root.glob("*/*/*.md") if p.parent.parent.parent == self.root)

    def records(self):
        """All receipt pages, cached for the life of this Archive."""
        if self._records is None:
            self._records = {}
            for p in self.page_paths():
                try:
                    rec = read_page(p)
                except (OSError, ValueError) as exc:
                    warn(f"{p}: unreadable page skipped: {exc}")
                    continue
                if rec.get("type", "receipt") != "receipt":
                    continue
                self._records[rec.get("id") or p.stem] = rec
        return self._records

    def _register(self, rec):
        """Make a created page visible to later lookups in this run (a dry run registers the
        planned page, so a second row with the same file is reported the way the real run
        would treat it)."""
        self.records()[rec["id"]] = rec
        if self._file_shas is not None:
            for s in self._record_shas(rec):
                self._file_shas.setdefault(s, rec["id"])

    def _shas(self):
        """sha256 of every file listed on every page -> id (primary from frontmatter, extras hashed)."""
        if self._file_shas is None:
            self._file_shas = {}
            for rid, rec in self.records().items():
                if rec.get("sha256"):
                    self._file_shas.setdefault(str(rec["sha256"]), rid)
                for name in (rec.get("files") or [])[1:]:
                    p = rec.dir / name
                    if p.exists():
                        self._file_shas.setdefault(sha256_file(p), rid)
        return self._file_shas

    def get(self, id):
        return self.records().get(id)

    def find(self, **criteria):
        """Pages matching every given criterion: entity, vendor (substring), month, date,
        amount_sek (within 0.005), amex_ref, pleo_expense_id, sha256, or any key by equality."""
        want_amount = None
        if criteria.get("amount_sek") not in (None, ""):
            want_amount = parse_amount(criteria["amount_sek"])
            if want_amount is None:
                raise ValueError(f"amount_sek must be a number, got {criteria['amount_sek']!r}")
        hits = []
        for rec in self.records().values():
            ok = True
            for k, want in criteria.items():
                if want is None or want == "":
                    continue
                if k == "vendor":
                    hay = f"{rec.get('vendor') or ''} {rec.get('merchant') or ''}".lower()
                    ok = str(want).lower() in hay
                elif k == "month":
                    ok = str(rec.get("date") or "").startswith(str(want))
                elif k == "amount_sek":
                    have = parse_amount(rec.get("amount_sek"))
                    ok = have is not None and abs(have - want_amount) <= 0.005
                else:
                    ok = str(rec.get(k) if rec.get(k) is not None else "") == str(want)
                if not ok:
                    break
            if ok:
                hits.append(rec)
        hits.sort(key=lambda r: (str(r.get("date") or ""), str(r.get("id") or "")))
        return hits

    # -- naming

    def _taken(self, d, stem):
        if (str(d), stem) in self._reserved:
            return True
        if not d.exists():
            return False
        return any(p.stem == stem for p in d.iterdir())

    def _unique_stem(self, d, stem, sha):
        cand, n = stem, 1
        while self._taken(d, cand):
            page = d / f"{cand}.md"
            if page.exists():
                try:
                    if str(read_page(page).get("sha256")) == sha:
                        return cand
                except (OSError, ValueError):
                    pass
            n += 1
            cand = f"{stem}-{n}"
        self._reserved.add((str(d), cand))
        return cand

    def _extra_name(self, d, stem, ext, used):
        n = 2
        while True:
            cand = f"{stem}-{n}"
            if cand not in used and not self._taken(d, cand):
                used.add(cand)
                return cand + ext
            n += 1

    # -- add and merge

    def add(self, files, meta, text=True, text_from=None):
        """Archive a receipt: files (first = primary) plus metadata. Merges into an existing
        page when the file or the receipt is already known. Returns the Record with
        .outcome in created / updated / unchanged."""
        paths = [Path(f) for f in files]
        if not paths:
            raise ValueError("at least one file is required")
        for p in paths:
            if not p.is_file():
                raise ValueError(f"file not found: {p}")
        m = normalise_meta(meta)
        seen, uniq, shas = set(), [], []
        for p in paths:
            s = sha256_file(p)
            if s in seen:
                continue
            seen.add(s)
            uniq.append(p)
            shas.append(s)
        paths = uniq
        existing = self._match_existing(m, shas)
        if existing is not None:
            return self._merge(existing, m, paths, shas, text)
        return self._create(m, paths, shas, text, text_from)

    def _match_existing(self, m, shas):
        """The page this receipt already lives on, or None. A file already archived is the
        same receipt only when the charge agrees (entity, date, amount) and no id disagrees:
        a byte-identical file on a different charge, or on the same charge under another
        Pleo expense or Amex ref, is a mis-attached duplicate and is refused with a reason."""
        by_sha = self._shas()
        for s in shas:
            rid = by_sha.get(s)
            if not rid:
                continue
            rec = self.get(rid)
            if rec is None:
                continue
            same = (str(rec.get("entity")) == m["entity"] and str(rec.get("date")) == m["date"]
                    and abs(float(parse_amount(rec.get("amount_sek")) or 0) - m["amount_sek"]) <= 0.005)
            if not same:
                raise ValueError(f"same file as {rid} ({rec.get('date')} {rec.get('vendor')} "
                                 f"{sv_amount(parse_amount(rec.get('amount_sek')) or 0)}) but a different charge; "
                                 f"check the attachment in the source")
            for key, label in ID_KEYS.items():
                have, want = str(rec.get(key) or ""), str(m.get(key) or "")
                if have and want and have != want:
                    raise ValueError(f"same file and charge as {rid} but another {label} ({want}, the page has "
                                     f"{have}); check the attachment in the source")
            return rec
        ids = {k: m.get(k) for k in ("pleo_expense_id", "amex_ref") if m.get(k)}
        if not ids:
            return None
        for rec in self.find(entity=m["entity"], date=m["date"], amount_sek=m["amount_sek"]):
            if any(str(rec.get(k) or "") == str(v) for k, v in ids.items()):
                return rec
        return None

    def _create(self, m, paths, shas, text, text_from):
        vendor, slug = resolve_vendor(m)
        m["vendor"] = vendor
        d = self.root / m["entity"] / m["date"][:4]
        stem = self._unique_stem(d, make_stem(m["date"], slug, m["amount_sek"]), shas[0])
        used = set()
        names = [stem + paths[0].suffix.lower()]
        for p in paths[1:]:
            names.append(self._extra_name(d, stem, p.suffix.lower(), used))
        stamp = now_stamp()
        rec = Record(m, path=d / f"{stem}.md")
        rec.update({"type": "receipt", "id": stem, "title": f"{vendor} {sv_amount(m['amount_sek'])} {m['date']}",
                    "files": names, "sha256": shas[0], "text_method": "none",
                    "tags": ["receipt", m["entity"], slug, m["date"][:7]], "created": stamp, "updated": stamp})
        rec.sources = [str(p) for p in paths]
        rec.shas = dict(zip(shas, names))
        rec.outcome = "created"
        if self.dry_run:
            rec.body = render_body(rec, "", primary_size=paths[0].stat().st_size)
            self._register(rec)
            return rec
        d.mkdir(parents=True, exist_ok=True)
        for src, name in zip(paths, names):
            dst = d / name
            if src.resolve() != dst.resolve():
                shutil.copyfile(src, dst)
        html = None
        if text_from is not None:
            wanted = Path(text_from).resolve()
            html = next((d / n for p, n in zip(paths, names) if p.resolve() == wanted), Path(text_from))
        self._extract(rec, text, html)
        write_page(rec)
        self._register(rec)
        return rec

    def _extract(self, rec, text, text_from=None, reharvest=()):
        """Run EXTRACTOR on the primary (preview for PDFs) and on the html file when one is
        among the files; fill text_method, text_truncated, preview and the harvested
        fields (the keys in reharvest are recomputed rather than kept); render the body.
        Returns the cleaned text."""
        primary = rec.dir / rec["files"][0]
        for k in reharvest:
            rec.pop(k, None)
        result_text, method = "", "none"
        if text:
            preview = rec.dir / f"{rec['id']}-preview.png" if primary.suffix.lower() == ".pdf" else None
            res = EXTRACTOR(str(primary), preview_path=str(preview) if preview else None) or {}
            if res.get("error") and not res.get("text"):
                warn(f"{primary.name}: {res['error']}")
            result_text, method = res.get("text") or "", res.get("method") or "none"
            if preview is not None and preview.exists():
                if preview.stat().st_size > 0:
                    rec["preview"] = preview.name
                else:
                    preview.unlink()
            html = text_from or _html_among([rec.dir / n for n in rec["files"][1:]])
            if html is not None and Path(html).suffix.lower() in HTML_EXTS and Path(html).exists():
                hres = EXTRACTOR(str(html), preview_path=None) or {}
                if (hres.get("text") or "").strip():
                    result_text, method = hres["text"], hres.get("method") or "html"
                elif hres.get("error") and not result_text.strip():
                    warn(f"{Path(html).name}: {hres['error']}")
        cleaned, truncated = clean_text(result_text)
        rec["text_method"] = method if cleaned else "none"
        if truncated:
            rec["text_truncated"] = True
        else:
            rec.pop("text_truncated", None)
        for k, v in harvest(cleaned, rec).items():
            if is_empty(rec.get(k)):
                rec[k] = v
        rec.body = render_body(rec, cleaned)
        return cleaned

    def _record_shas(self, rec):
        """sha256 -> file name for the page's files (planned names when a dry run has not copied them)."""
        out = {}
        for name in rec.get("files") or []:
            p = rec.dir / name
            if p.exists():
                out[sha256_file(p)] = name
        for s, name in getattr(rec, "shas", {}).items():
            out.setdefault(s, name)
        return out

    def _merge_source(self, rec, m):
        """Source fields move as a group: the incoming source replaces the page's source only
        when it is at least as informative (gmail over pleo-export, portal and paper, over
        manual) and actually points somewhere (source_ref). Returns the changed keys."""
        incoming = {k: m[k] for k in SOURCE_KEYS if not is_empty(m.get(k))}
        kind = incoming.get("source_kind")
        if not kind:
            return []
        if not incoming.get("source_ref") and kind != "pleo-export":
            return []  # nothing to point at; a pleo-export row still names its export in source_from
        old_rank = SOURCE_RANK.get(str(rec.get("source_kind") or "manual"), 1)
        new_rank = SOURCE_RANK.get(kind, 1)
        if new_rank < old_rank:
            return []
        if new_rank == old_rank and not incoming.get("source_ref") and rec.get("source_ref"):
            return []  # same rank but the page's source has a ref and the incoming one has none
        changed = []
        for k in SOURCE_KEYS:
            old, new = rec.get(k), incoming.get(k)
            if is_empty(new) and not is_empty(old):
                rec.pop(k)
                changed.append(k)
            elif not is_empty(new) and str(old) != str(new):
                rec[k] = new
                changed.append(k)
        return changed

    def _merge(self, rec, m, paths, shas, text):
        """Merge metadata and new files into an existing page; rewrite only on change."""
        changed = self._merge_source(rec, m)
        for k, v in m.items():
            if k in DERIVED or k in SOURCE_KEYS or is_empty(v):
                continue
            old = rec.get(k)
            if k == "vendor" and not is_empty(old):
                continue
            if k == "pleo_status":
                v = merge_pleo_status(old, v)
            if k in OVERWRITE_KEYS or k == "pleo_status" or is_empty(old):
                if old != v and str(old) != str(v):
                    rec[k] = v
                    changed.append(k)
        have = self._record_shas(rec)
        new_files = [(p, s) for p, s in zip(paths, shas) if s not in have]
        used = set()
        for p, s in new_files:
            name = self._extra_name(rec.dir, rec["id"], p.suffix.lower(), used)
            if not self.dry_run:
                shutil.copyfile(p, rec.dir / name)
            rec["files"] = list(rec.get("files") or []) + [name]
            have[s] = name
            rec.shas[s] = name
            changed.append(f"file {name}")
        html = _html_among([p for p, _ in new_files]) if new_files else None
        textless = rec.get("text_method", "none") == "none"
        if text and not self.dry_run and (self.retext or (new_files and (textless or html is not None))):
            before = ({k: v for k, v in rec.items() if k != "updated"}, body_text(rec.body))
            reharvest = (HARVEST_KEYS - set(m)) if self.retext else ()
            self._extract(rec, True, (rec.dir / have[sha256_file(html)]) if html else None, reharvest)
            if ({k: v for k, v in rec.items() if k != "updated"}, body_text(rec.body)) != before:
                changed.append("text")
        if "vendor" in changed:
            rec["title"] = f"{rec['vendor']} {sv_amount(rec['amount_sek'])} {rec['date']}"
            tags = list(rec.get("tags") or [])
            if len(tags) >= 3:
                tags[2] = slugify(rec["vendor"])
                rec["tags"] = tags
        rec.sources = [str(p) for p in paths]
        rec.changes = changed
        if not changed:
            rec.outcome = "unchanged"
            return rec
        rec["updated"] = now_stamp()
        rerender_head(rec)
        if not self.dry_run:
            write_page(rec)
        rec.outcome = "updated"
        return rec

    # -- set

    def set(self, id, /, **changes):
        """Update settable keys on one page (empty value removes the key); re-renders the
        head, keeps Kvittotext, bumps updated only when something changed."""
        rec = self.get(id)
        if rec is None:
            raise KeyError(f"no page with id {id}")
        bad = sorted(k for k in changes if k not in SETTABLE)
        if bad:
            raise ValueError(f"not settable: {', '.join(bad)} (allowed: {', '.join(sorted(SETTABLE))})")
        if "source_kind" in changes and changes["source_kind"] not in SOURCE_RANK:
            raise ValueError(f"source_kind must be one of {', '.join(sorted(SOURCE_RANK))}, got {changes['source_kind']!r}")
        changed = []
        for k, v in changes.items():
            if isinstance(v, str):
                v = plain_dashes(v)
            if k in NUMERIC_KEYS and not is_empty(v):
                num = parse_amount(v)
                if num is None:
                    raise ValueError(f"{k} must be a number, got {v!r}")
                num = round(abs(num), 2)
                v = int(num) if (k == "vat_rate" and float(num).is_integer()) else num
            elif k == "tags" and isinstance(v, str):
                v = [t.strip() for t in v.split(",") if t.strip()]
            elif k == "text_truncated" and isinstance(v, str):
                v = v.lower() in ("true", "1", "yes")
            elif k == "receipt_date" and not is_empty(v):
                d = iso_date(v)
                if not d:
                    raise ValueError(f"receipt_date must be YYYY-MM-DD, got {v!r}")
                v = d
            elif k in ("bas_account", "pleo_receipt_number", "invoice_number") and not is_empty(v):
                v = str(v)
            if is_empty(v):
                if k in rec and k not in REQUIRED:
                    rec.pop(k)
                    changed.append(k)
                elif k in REQUIRED:
                    raise ValueError(f"{k} cannot be emptied")
            elif rec.get(k) != v:
                rec[k] = v
                changed.append(k)
        if "vendor" in changed:
            rec["title"] = f"{rec['vendor']} {sv_amount(rec['amount_sek'])} {rec['date']}"
            tags = list(rec.get("tags") or [])
            if len(tags) >= 3 and "tags" not in changed:
                tags[2] = slugify(rec["vendor"])
                rec["tags"] = tags
        rec.changes = changed
        if not changed:
            rec.outcome = "unchanged"
            return rec
        rec["updated"] = now_stamp()
        rerender_head(rec)
        if not self.dry_run:
            write_page(rec)
        rec.outcome = "updated"
        return rec

    # -- index

    def render_index(self, stamp):
        recs = list(self.records().values())
        by_entity = {}
        for r in recs:
            by_entity.setdefault(str(r.get("entity") or "okand"), []).append(r)
        order = [e for e in ENTITY_NAMES if e in by_entity] + sorted(e for e in by_entity if e not in ENTITY_NAMES)

        def signed(r):
            a = float(parse_amount(r.get("amount_sek")) or 0)
            return -a if r.get("kind") == "credit-note" else a

        total = sum(signed(r) for r in recs)
        lines = ["# Kvittoarkiv", "",
                 f"Ett kvitto per sida: nyckelfakta i frontmatter, kvittotexten och originalfilen bredvid. "
                 f"{len(recs)} {'kvitto' if len(recs) == 1 else 'kvitton'} totalt, {sv_amount(total)}. "
                 f"Sidan skapas av scripts/archive.py index; redigera inte for hand.", ""]
        for entity in order:
            lines.append(f"## {ENTITY_NAMES.get(entity, entity)}")
            lines.append("")
            months = {}
            for r in by_entity[entity]:
                months.setdefault(str(r.get("date") or "")[:7], []).append(r)
            for month in sorted(months, reverse=True):
                rows = sorted(months[month], key=lambda r: (str(r.get("date") or ""), str(r.get("id") or "")), reverse=True)
                n = len(rows)
                lines.append(f"### {month}")
                lines.append(f"- {n} {'kvitto' if n == 1 else 'kvitton'}, {sv_amount(sum(signed(r) for r in rows))}")
                for r in rows:
                    amount = sv_amount(parse_amount(r.get("amount_sek")) or 0)
                    if r.get("kind") == "credit-note":
                        amount = "kreditnota " + amount
                    lines.append(f"- {r.get('date')} | {r.get('vendor') or r.get('merchant')} | {amount} | "
                                 f"{card_label(r.get('card'))} | {short_status(r)} | [sida]({r.rel(self.root)})")
                lines.append("")
        body = "\n".join(lines).rstrip("\n") + "\n"
        return fm_dump({"type": "index", "title": "Kvittoarkiv", "updated": stamp}) + "\n" + body

    def write_index(self):
        """Regenerate <root>/index.md; untouched when its content is already current."""
        path = self.root / "index.md"
        stamp = now_stamp()
        new = self.render_index(stamp)
        if path.exists():
            try:
                old_meta, old_body = fm_load(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                old_meta, old_body = {}, ""
            if old_body == fm_load(new)[1] and old_meta.get("type") == "index":
                newest = max([p.stat().st_mtime for p in self.page_paths()] or [0.0])
                if path.stat().st_mtime < newest and not self.dry_run:
                    os.utime(path, None)  # content current: mark it verified against the newest page
                return False
        if self.dry_run:
            return True
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new, encoding="utf-8")
        return True

    # -- check

    def check(self):
        """Lint the root. Returns messages; entries starting with 'warning: ' do not fail."""
        errors, warnings = [], []
        seen_sha = {}
        referenced = set()
        parsed = []
        newest = 0.0
        if not self.root.is_dir():
            return [f"archive root missing: {self.root}"]
        for md in self.page_paths():
            rel = str(md.relative_to(self.root))
            try:
                raw = md.read_text(encoding="utf-8")
                meta, body = fm_load(raw)
            except (OSError, ValueError) as exc:
                errors.append(f"{rel}: cannot parse: {exc}")
                continue
            newest = max(newest, md.stat().st_mtime)
            if "\u2014" in raw or "\u2013" in raw:
                errors.append(f"{rel}: contains an em dash or en dash")
            if not meta:
                errors.append(f"{rel}: no frontmatter")
                continue
            if meta.get("type") != "receipt":
                errors.append(f"{rel}: type is {meta.get('type')!r}, expected receipt")
                continue
            missing = [k for k in REQUIRED if k not in meta or meta[k] is None or meta[k] == ""]
            if missing:
                errors.append(f"{rel}: missing required keys: {', '.join(missing)}")
            if meta.get("id") != md.stem:
                errors.append(f"{rel}: id {meta.get('id')!r} differs from the filename stem")
            entity, year = md.parent.parent.name, md.parent.name
            if str(meta.get("entity")) != entity:
                errors.append(f"{rel}: entity {meta.get('entity')!r} does not match the folder {entity}")
            if str(meta.get("date") or "")[:4] != year:
                errors.append(f"{rel}: date {meta.get('date')!r} is not in folder year {year}")
            files = meta.get("files") or []
            if not isinstance(files, list) or not files:
                errors.append(f"{rel}: files must be a non-empty list")
                files = []
            for i, name in enumerate(files):
                p = md.parent / str(name)
                referenced.add(p)
                if not p.is_file():
                    errors.append(f"{rel}: listed file missing: {name}")
                elif i == 0 and sha256_file(p) != str(meta.get("sha256") or ""):
                    errors.append(f"{rel}: primary file {name} does not match sha256 in frontmatter")
            if meta.get("preview"):
                pv = md.parent / str(meta["preview"])
                referenced.add(pv)
                if not pv.is_file():
                    errors.append(f"{rel}: declared preview missing: {meta['preview']}")
            sha = str(meta.get("sha256") or "")
            if sha:
                if sha in seen_sha:
                    errors.append(f"{rel}: same sha256 as {seen_sha[sha]}")
                else:
                    seen_sha[sha] = rel
            if TEXT_HEADING not in body:
                warnings.append(f"{rel}: no Kvittotext section")
            parsed.append((rel, meta, body))
        pages = [(m.get("id") or Path(rel).stem, m) for rel, m, _ in parsed]
        for rel, meta, body in parsed:
            others = [(oid, om) for oid, om in pages if om is not meta]
            for msg in plausibility(meta, body_text(body), others):
                warnings.append(f"{rel}: {msg}")
        for d in sorted(p for p in self.root.glob("*/*") if p.is_dir() and p.parent.parent == self.root):
            for f in sorted(d.iterdir()):
                if f.is_file() and f.suffix.lower() != ".md" and f not in referenced and not f.name.startswith("."):
                    warnings.append(f"{f.relative_to(self.root)}: file not referenced by any page")
        index = self.root / "index.md"
        if not index.exists():
            warnings.append("index.md missing (run archive.py index)")
        elif newest and index.stat().st_mtime < newest:
            warnings.append("index.md is older than the newest page (run archive.py index)")
        return errors + [f"warning: {w}" for w in warnings]


# ----------------------------------------------------------------------------- ingest sources

def gmail_headers(mid):
    """(subject, from address) for a Gmail message id from out/gmail-cache.json, read-only."""
    if not mid:
        return "", ""
    cache = _gmail_cache()
    entry = (cache.get("messages") or {}).get(mid) or {}
    headers = ((entry.get("meta") or {}).get("headers") or {})
    return str(headers.get("subject") or "").strip(), address_of(headers.get("from"))


_GMAIL = None


def _gmail_cache():
    global _GMAIL
    if _GMAIL is None:
        _GMAIL = {}
        if GMAIL_CACHE.is_file():
            try:
                with open(GMAIL_CACHE, encoding="utf-8") as fh:
                    _GMAIL = json.load(fh) or {}
            except (OSError, ValueError) as exc:
                warn(f"{GMAIL_CACHE}: {exc}; mail subjects not available")
    return _GMAIL


def address_of(header):
    """'\"Vercel Inc.\" <invoice+statements@vercel.com>' -> 'invoice+statements@vercel.com'."""
    header = str(header or "").strip()
    if not header:
        return ""
    addr = parseaddr(header)[1]
    return addr or header


def job(label, files, meta, text_from=None):
    return {"label": label, "files": [str(f) for f in files], "meta": meta, "text_from": text_from}


def skip(label, reason):
    return {"label": label, "skip": reason}


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rd = csv.DictReader(fh)
        return list(rd), list(rd.fieldnames or [])


def ingest_pleo_export(dir_path, entity="viseo", export_date=None):
    """Jobs for a Pleo export folder (export_*.csv plus receipts/<Receipt>[a-z].<ext>)."""
    d = Path(dir_path)
    csvs = sorted(d.glob("export_*.csv"))
    if not csvs:
        raise SystemExit(f"{d}: no export_*.csv found")
    if not export_date:
        m = re.search(r"(\d{4}-\d{2}-\d{2})", d.name)
        export_date = m.group(1) if m else date_type.today().isoformat()
        if not m:
            warn(f"{d.name}: no date in the folder name, using today ({export_date}) as export date")
    receipts = d / "receipts"
    by_no = {}
    if receipts.is_dir():
        for name in os.listdir(receipts):
            m = re.fullmatch(r"(\d+)([a-z])?\.([A-Za-z0-9]+)", name)
            if m:
                by_no.setdefault(m.group(1), []).append((m.group(2) or "", name))
    jobs = []
    for path in csvs:
        rows, cols = read_csv(path)
        needed = {"Date", "Receipt", "Expense Type", "Amount", "Source description", "Expense ID"}
        missing = sorted(needed - set(cols))
        if missing:
            raise SystemExit(f"{path}: not a Pleo export, missing columns: {', '.join(missing)}")
        for row in rows:
            date = iso_date(row.get("Date"))
            amount = parse_amount(row.get("Amount"))
            merchant = plain_dashes(re.sub(r"\s+", " ", str(row.get("Source description") or "")))
            no = str(row.get("Receipt") or "").strip()
            label = f"{date or row.get('Date')} {merchant or '?'} {sv_amount(amount) if amount is not None else row.get('Amount')} (kvitto {no or '?'})"
            etype = str(row.get("Expense Type") or "").strip()
            if etype != "Card Purchase":
                if etype.lower().startswith("reimbursement"):
                    jobs.append(skip(label, "reimbursement row (utlagg payout), not a receipt"))
                else:
                    jobs.append(skip(label, f"expense type {etype or '?'}, not a card purchase"))
                continue
            names = [n for _, n in sorted(by_no.get(no, []))]
            if not names:
                jobs.append(skip(label, "no file in receipts/ (receipt missing in Pleo)"))
                continue
            if not date or amount is None:
                jobs.append(skip(label, "unreadable date or amount"))
                continue
            cur = str(row.get("Orig. currency") or row.get("Currency") or "SEK").strip().upper()
            orig = parse_amount(row.get("Orig. amount"))
            urls = [u.strip() for u in str(row.get("Receipt urls") or "").split(",") if u.strip()]
            rate = parse_amount(row.get("Tax Rate"))
            meta = {
                "entity": entity, "card": "pleo", "lane": "pleo", "date": date, "merchant": merchant,
                "amount_sek": abs(amount), "currency": cur,
                "amount": abs(orig) if orig is not None else abs(amount),
                "kind": "credit-note" if amount > 0 else "receipt",
                "pleo_expense_id": str(row.get("Expense ID") or "").strip(),
                "pleo_receipt_number": no, "pleo_category": str(row.get("Category") or "").strip(),
                "bas_account": str(row.get("Account number") or "").strip(),
                "vat_rate": round(rate * 100, 2) if rate else None,
                "vat_sek": abs(parse_amount(row.get("Tax Amount")) or 0) or None,
                "vendor_country": str(row.get("Merchant Country Code") or "").strip(),
                "source_kind": "pleo-export", "source_ref": urls[0] if urls else "",
                "source_from": f"Pleo export {export_date}",
                "pleo_status": f"i Pleo ({str(row.get('Export Status') or '').strip() or 'okand status'})",
            }
            jobs.append(job(label, [receipts / n for n in names], meta))
    return jobs


def parse_pleo_amount(s):
    """'981,09 SEK (99 USD)' -> (981.09, 'USD', 99.0); '129,00 SEK' -> (129.0, 'SEK', 129.0)."""
    s = str(s or "").strip()
    m = re.match(r"^(.*?)\s*SEK\s*(?:\(\s*(.+?)\s*([A-Za-z]{3})\s*\))?\s*$", s)
    if not m:
        amount = parse_amount(s)
        return (abs(amount) if amount is not None else None), "SEK", (abs(amount) if amount is not None else None)
    sek = parse_amount(m.group(1))
    if sek is None:
        return None, "SEK", None
    if m.group(2):
        orig = parse_amount(m.group(2))
        return abs(sek), m.group(3).upper(), abs(orig) if orig is not None else abs(sek)
    return abs(sek), "SEK", abs(sek)


def ingest_matching(csv_path, files_dir=None, entity="viseo"):
    """Jobs for the Pleo lane's MATCHING.csv (rows with file_to_attach)."""
    csv_path = Path(csv_path)
    files_dir = Path(files_dir) if files_dir else csv_path.parent
    rows, cols = read_csv(csv_path)
    needed = {"pleo_receipt_no", "expense_id", "date", "merchant", "pleo_amount", "file_to_attach", "gmail_message_id", "status"}
    missing = sorted(needed - set(cols))
    if missing:
        raise SystemExit(f"{csv_path}: not a MATCHING.csv, missing columns: {', '.join(missing)}")
    jobs = []
    for row in rows:
        date = iso_date(row.get("date"))
        merchant = plain_dashes(re.sub(r"\s+", " ", str(row.get("merchant") or "")))
        no = str(row.get("pleo_receipt_no") or "").strip()
        label = (f"{date or row.get('date')} {merchant or '?'} {str(row.get('pleo_amount') or '').strip()}"
                 f" (kvitto {no or '?'})")
        f = str(row.get("file_to_attach") or "").strip()
        status = str(row.get("status") or "").strip()
        if not f:
            jobs.append(skip(label, f"no file ({status or 'no status'})"))
            continue
        primary = files_dir / f
        if not primary.is_file():
            jobs.append(skip(label, f"file not found: {primary}"))
            continue
        files = [primary]
        extra = str(row.get("extra_file") or "").strip()
        if extra:
            if (files_dir / extra).is_file():
                files.append(files_dir / extra)
            else:
                warn(f"{label}: extra file not found, page gets the primary only: {files_dir / extra}")
        html = files_dir / (primary.stem + ".html")
        text_from = None
        if html.is_file():
            files.append(html)
            text_from = str(html)
        sek, cur, orig = parse_pleo_amount(row.get("pleo_amount"))
        if not date or sek is None:
            jobs.append(skip(label, "unreadable date or amount"))
            continue
        mid = str(row.get("gmail_message_id") or "").strip()
        subject, sender = gmail_headers(mid)
        meta = {
            "entity": entity, "card": "pleo", "lane": "pleo", "date": date, "merchant": merchant,
            "amount_sek": sek, "currency": cur, "amount": orig,
            "pleo_expense_id": str(row.get("expense_id") or "").strip(),
            "pleo_receipt_number": no if no.isdigit() else "",
            "pleo_status": status, "source_kind": "gmail" if mid else "manual", "source_ref": mid,
            "source_subject": subject, "source_from": sender,
        }
        jobs.append(job(label, files, meta, text_from))
    return jobs


def chosen_message(row):
    """(message id, subject, from) chosen by gmail_match for one matches row, or None."""
    mid = str(row.get("known_message_id") or "").strip()
    best = row.get("best")
    if not mid:
        if isinstance(best, str):
            mid = best.strip()
        elif isinstance(best, dict):
            mid = str(best.get("id") or best.get("message_id") or "").strip()
    if not mid:
        return None
    cand = next((c for c in row.get("candidates") or [] if isinstance(c, dict)
                 and mid in (c.get("message_id"), c.get("id"))), None)
    if cand is None and isinstance(best, dict):
        cand = best
    cand = cand or {}
    return mid, str(cand.get("subject") or "").strip(), address_of(cand.get("from"))


def load_matches(paths):
    """row_key -> (message id, subject, from) from amex_receipts / gmail_match JSON files."""
    out = {}
    for p in paths or []:
        try:
            with open(p, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            warn(f"{p}: {exc}; matches file ignored")
            continue
        rows = data.get("rows") if isinstance(data, dict) else data
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            key = str(row.get("row_key") or row.get("ref") or "").strip()
            chosen = chosen_message(row)
            if key and chosen:
                out[key] = chosen
    return out


FOREIGN_SPEND_RE = re.compile(r"Foreign Spend Amount:\s*([\d.,]+)\s+([A-Za-z][A-Za-z ]*?)\s*(?:Commission|Currency|$)", re.I)
CURRENCY_NAMES = {"EUROPEAN UNION EURO": "EUR", "EURO": "EUR", "UNITED STATES DOLLAR": "USD", "US DOLLAR": "USD",
                  "POUND STERLING": "GBP", "BRITISH POUND": "GBP", "DANISH KRONE": "DKK", "NORWEGIAN KRONE": "NOK",
                  "SWISS FRANC": "CHF", "JAPANESE YEN": "JPY", "POLISH ZLOTY": "PLN", "CANADIAN DOLLAR": "CAD",
                  "AUSTRALIAN DOLLAR": "AUD", "CZECH KORUNA": "CZK", "HUNGARIAN FORINT": "HUF", "SWEDISH KRONA": "SEK"}


def foreign_amount(ext):
    """(amount, currency code) from an Amex ledger ext column 'Foreign Spend Amount: 316,75
    EUROPEAN UNION EURO ...'; None when absent or the currency name is unknown."""
    m = FOREIGN_SPEND_RE.search(str(ext or ""))
    if not m:
        return None
    amount = parse_amount(m.group(1))
    name = re.sub(r"\s+", " ", m.group(2)).strip().upper()
    code = CURRENCY_NAMES.get(name)
    if amount is None or not code:
        if name and not code:
            warn(f"unknown currency name in the ledger: {name!r}; amount kept in SEK")
        return None
    return abs(amount), code


def ingest_amex(ledger_csv, receipts_dir, matches=()):
    """Jobs for the classified Amex ledger and its fetched receipt files (<ref>_<name>).
    Business rows without a file are reported as skipped, one line each."""
    rows, cols = read_csv(ledger_csv)
    needed = {"date", "card", "amount", "merchant", "tag", "ref"}
    missing = sorted(needed - set(cols))
    if missing:
        raise SystemExit(f"{ledger_csv}: not a classify.py ledger, missing columns: {', '.join(missing)}")
    ledger = {}
    for row in rows:
        ref = str(row.get("ref") or "").strip()
        if ref:
            ledger[ref] = row
    chosen = load_matches(matches)
    d = Path(receipts_dir)
    if not d.is_dir():
        raise SystemExit(f"{d}: receipts dir not found")
    groups = {}
    jobs = []
    for name in sorted(os.listdir(d)):
        p = d / name
        if not p.is_file() or name.startswith("."):
            continue
        if "_" not in name:
            if p.suffix.lower() in RECEIPT_EXTS:
                jobs.append(skip(name, "no <ref>_ prefix, cannot tie to a ledger row"))
            continue
        groups.setdefault(name.split("_", 1)[0], []).append(p)
    with_file = set()
    for ref in sorted(groups):
        files = groups[ref]
        row = ledger.get(ref)
        label = f"{ref} ({len(files)} file{'s' if len(files) != 1 else ''})"
        if row is None:
            jobs.append(skip(label, "ref not in the ledger"))
            continue
        date = iso_date(row.get("date"))
        amount = parse_amount(row.get("amount"))
        merchant = plain_dashes(re.sub(r"\s+", " ", str(row.get("merchant") or "")))
        label = f"{date or row.get('date')} {merchant or '?'} {sv_amount(amount) if amount is not None else row.get('amount')} ({ref})"
        keep = []
        for p in files:
            if p.suffix.lower() in IMAGE_EXTS and p.stat().st_size < JUNK_IMAGE_BYTES:
                jobs.append(skip(f"{label} file {p.name}", f"junk image dropped ({p.stat().st_size} bytes)"))
            else:
                keep.append(p)
        tag = str(row.get("tag") or "").strip()
        if tag != "business":
            jobs.append(skip(label, f"tag {tag or 'blank'}, only business rows are archived"))
            continue
        if not keep:
            jobs.append(skip(label, "only junk files"))
            continue
        if not date or amount is None:
            jobs.append(skip(label, "unreadable date or amount in the ledger"))
            continue
        with_file.add(ref)
        pdfs = [p for p in keep if p.suffix.lower() == ".pdf"]
        imgs = [p for p in keep if p.suffix.lower() in IMAGE_EXTS]
        rest = [p for p in keep if p not in pdfs and p not in imgs]
        ordered = pdfs + imgs + rest
        card = str(row.get("card") or "").strip()
        meta = {
            "entity": str(row.get("entity") or "").strip() or "viseo",
            "card": f"amex-{card}" if card else "unknown", "lane": "amex-fortnox",
            "date": date, "merchant": merchant, "amount_sek": abs(amount), "currency": "SEK",
            "kind": "credit-note" if amount < 0 else "receipt", "amex_ref": ref,
            "bas_account": str(row.get("bas_account") or "").strip(),
            "vat_regime": str(row.get("vat_regime") or "").strip(),
            "note": str(row.get("konto_name") or "").strip(),
            "fortnox_status": "ej bokfort",
        }
        foreign = foreign_amount(row.get("ext"))
        if foreign:
            meta["amount"], meta["currency"] = foreign
        known = KNOWN_ID_RE.search(" ".join(str(row.get(k) or "") for k in ("confirm", "why", "pack_status")))
        if known:
            mid = known.group(1)
            subject, sender = gmail_headers(mid)
            meta.update({"source_kind": "gmail", "source_ref": mid, "source_subject": subject, "source_from": sender})
        elif ref in chosen:
            mid, subject, sender = chosen[ref]
            if not subject and not sender:
                subject, sender = gmail_headers(mid)
            meta.update({"source_kind": "gmail", "source_ref": mid, "source_subject": subject, "source_from": sender})
        else:
            meta.update({"source_kind": "manual", "source_ref": ordered[0].name})
        jobs.append(job(label, ordered, meta))
    for ref, row in ledger.items():
        if str(row.get("tag") or "").strip() != "business" or ref in with_file or ref in groups:
            continue
        amount = parse_amount(row.get("amount"))
        merchant = plain_dashes(re.sub(r"\s+", " ", str(row.get("merchant") or "")))
        label = (f"{iso_date(row.get('date')) or row.get('date')} {merchant or '?'} "
                 f"{sv_amount(amount) if amount is not None else row.get('amount')} ({ref})")
        jobs.append(skip(label, f"no receipt file in {d}"))
    return jobs


# ----------------------------------------------------------------------------- running jobs and reporting

def order_duplicates(jobs, text=True):
    """Rows of one batch that share a primary file but not a charge: the row the receipt text
    fits best (its amount printed in the text, then the nearest printed date) runs first, so
    the file lands on the charge it shows and the other rows are refused with a reason. Rows
    keep their positions otherwise. Without text the batch order stands."""
    groups = {}
    for i, jb in enumerate(jobs):
        if jb.get("skip") or not jb.get("files"):
            continue
        try:
            groups.setdefault(sha256_file(jb["files"][0]), []).append(i)
        except OSError:
            continue
    out = list(jobs)
    for idx in groups.values():
        if len(idx) < 2 or not text:
            continue
        charges = set()
        for i in idx:
            try:
                m = normalise_meta(jobs[i]["meta"])
                charges.add((m["entity"], m["date"], m["amount_sek"]))
            except ValueError:
                charges.add(("invalid", i))
        if len(charges) < 2:
            continue
        body = (EXTRACTOR(jobs[idx[0]]["files"][0], preview_path=None) or {}).get("text") or ""
        if not body.strip():
            continue

        def fit(i):
            try:
                return text_fit(body, jobs[i]["meta"])
            except ValueError:
                return (-1, -(10 ** 6))
        ranked = sorted(idx, key=lambda i: (tuple(-x for x in fit(i)), i))
        for pos, i in zip(idx, ranked):
            out[pos] = jobs[i]
    return out


def run_jobs(archive, jobs, text=True):
    results = {"created": [], "updated": [], "unchanged": [], "skipped": []}
    for jb in order_duplicates(jobs, text=text):
        if jb.get("skip"):
            results["skipped"].append((jb["label"], jb["skip"]))
            continue
        try:
            rec = archive.add(jb["files"], jb["meta"], text=text, text_from=jb.get("text_from"))
        except ValueError as exc:
            results["skipped"].append((jb["label"], str(exc)))
            continue
        except (OSError, KeyError) as exc:
            results["skipped"].append((jb["label"], f"error: {exc}"))
            continue
        results[rec.outcome].append((jb["label"], rec))
    return results


def report(results, archive, refresh_index=True):
    """Per-row lines, the summary line and every skipped row with its reason."""
    dry = archive.dry_run
    verb = {"created": "would create" if dry else "created", "updated": "would update" if dry else "updated"}
    for kind in ("created", "updated"):
        for label, rec in results[kind]:
            extra = ""
            if kind == "updated" and getattr(rec, "changes", None):
                extra = f" [{', '.join(rec.changes)}]"
            src = f"  <- {', '.join(Path(s).name for s in getattr(rec, 'sources', []))}" if kind == "created" else ""
            print(f"{verb[kind]} {rec.rel(archive.root)}{extra}  ({label}){src}")
    if not dry:
        pages = list(archive.records().items())
        for kind in ("created", "updated"):
            for label, rec in results[kind]:
                others = [(rid, r) for rid, r in pages if r is not rec]
                for msg in plausibility(rec, body_text(rec.body or ""), others):
                    print(f"warning: {rec.rel(archive.root)}: {msg}")
    for label, reason in results["skipped"]:
        print(f"{'would skip' if dry else 'skipped'} {label}: {reason}")
    c, u, n, s = (len(results[k]) for k in ("created", "updated", "unchanged", "skipped"))
    if dry:
        print(f"would create {c}, would update {u}, unchanged {n}, would skip {s}")
    else:
        print(f"created {c}, updated {u}, unchanged {n}, skipped {s}")
    if refresh_index and (c or u) and not dry:
        if archive.write_index():
            print(f"index refreshed: {archive.root / 'index.md'}")


# ----------------------------------------------------------------------------- cli

def parse_kv(items):
    out = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"expected key=value, got {item!r}")
        k, v = item.split("=", 1)
        k = k.strip().replace("-", "_")
        if not k:
            raise SystemExit(f"empty key in {item!r}")
        out[k] = v
    return out


def cmd_ingest(args, archive):
    text = not args.no_text
    if args.source == "pleo-export":
        jobs = ingest_pleo_export(args.dir, entity=args.entity, export_date=args.export_date)
    elif args.source == "matching":
        jobs = ingest_matching(args.csv, files_dir=args.files_dir, entity=args.entity)
    elif args.source == "amex":
        jobs = ingest_amex(args.ledger, args.receipts_dir, matches=args.matches or [])
    else:
        raise SystemExit(f"unknown ingest source {args.source!r}")
    results = run_jobs(archive, jobs, text=text)
    report(results, archive)
    return 0


def cmd_add(args, archive):
    meta = {}
    for k in ("entity", "date", "merchant", "amount_sek", "currency", "amount", "vendor", "card", "lane", "kind",
              "source_kind", "source_ref", "source_from", "source_subject", "source_url", "pleo_expense_id",
              "pleo_receipt_number", "pleo_status", "pleo_category", "amex_ref", "bas_account", "vat_regime",
              "vat_rate", "vat_sek", "receipt_date", "invoice_number", "vendor_country", "vendor_vat_number",
              "fortnox_voucher", "fortnox_status", "note"):
        v = getattr(args, k, None)
        if v is not None and v != "":
            meta[k] = v
    if meta.get("source_kind") == "gmail" and meta.get("source_ref") and not (meta.get("source_subject") or meta.get("source_from")):
        subject, sender = gmail_headers(meta["source_ref"])
        meta.setdefault("source_subject", subject)
        meta.setdefault("source_from", sender)
    amount = parse_amount(meta.get("amount_sek"))
    label = f"{meta.get('date')} {meta.get('merchant')} {sv_amount(abs(amount)) if amount is not None else meta.get('amount_sek')}"
    results = run_jobs(archive, [job(label, args.files, meta)], text=not args.no_text)
    report(results, archive)
    return 1 if results["skipped"] else 0


def cmd_set(args, archive):
    changes = parse_kv(args.changes)
    try:
        rec = archive.set(args.id, **changes)
    except (KeyError, ValueError) as exc:
        print(f"error: {exc.args[0] if exc.args else exc}", file=sys.stderr)
        return 1
    if rec.outcome == "unchanged":
        print(f"unchanged {rec.rel(archive.root)}")
    else:
        verb = "would update" if archive.dry_run else "updated"
        print(f"{verb} {rec.rel(archive.root)} [{', '.join(rec.changes)}]")
        if not archive.dry_run and archive.write_index():
            print(f"index refreshed: {archive.root / 'index.md'}")
    return 0


def cmd_index(args, archive):
    n = len(archive.records())
    if archive.write_index():
        verb = "would write" if archive.dry_run else "wrote"
        print(f"{verb} {archive.root / 'index.md'} ({n} kvitton)")
    else:
        print(f"unchanged {archive.root / 'index.md'} ({n} kvitton)")
    return 0


def cmd_find(args, archive):
    crit = {k: getattr(args, k) for k in ("entity", "vendor", "month", "date", "amount_sek", "amex_ref",
                                           "pleo_expense_id", "sha256", "id") if getattr(args, k, None)}
    try:
        hits = archive.find(**crit)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps([dict(r) for r in hits], ensure_ascii=False, indent=1))
        return 0
    for r in hits:
        print(f"{r.get('id')} | {r.get('vendor')} | {sv_amount(parse_amount(r.get('amount_sek')) or 0)} | "
              f"{r.get('date')} | {r.get('card')} | {r.rel(archive.root)}")
    if not hits:
        print("no match", file=sys.stderr)
    return 0


def cmd_check(args, archive):
    msgs = archive.check()
    errors = [m for m in msgs if not m.startswith("warning: ")]
    for m in msgs:
        print(m)
    n = len(archive.records())
    print(f"check: {n} pages, {len(errors)} errors, {len(msgs) - len(errors)} warnings")
    return 1 if errors else 0


def cmd_text(args, archive):
    res = EXTRACTOR(str(args.file), preview_path=str(args.preview) if args.preview else None)
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0 if res and res.get("method") != "none" else 1


def sync_fortnox(archive, plan_dir):
    """Carry the executor's result onto the Amex pages: for every ref in
    <plan_dir>/execution.json that has a voucher, set fortnox_voucher ('A 12') and
    fortnox_status ('bokfort <date of the verifikat log line>') on the page with that
    amex_ref. Returns dict(updated, unchanged, no_page, no_voucher)."""
    plan_dir = Path(plan_dir)
    state_path = plan_dir / "execution.json"
    if not state_path.exists():
        raise FileNotFoundError(f"{state_path} not found; nothing has been executed for this plan yet")
    with open(state_path, encoding="utf-8") as fh:
        state = json.load(fh)
    rows = state.get("rows") or {}
    booked = {}
    for line in state.get("log") or []:
        m = re.match(r"(\d{4}-\d{2}-\d{2}) \S+ verifikat skapat: (\S+) ->", str(line))
        if m:
            booked[m.group(2)] = m.group(1)
    out = {"updated": [], "unchanged": [], "no_page": [], "no_voucher": []}
    for ref, st in sorted(rows.items()):
        if not isinstance(st, dict) or not st.get("voucher_number"):
            out["no_voucher"].append(ref)
            continue
        voucher = f"{st.get('voucher_series') or ''} {st['voucher_number']}".strip()
        hits = archive.find(amex_ref=ref)
        if not hits:
            out["no_page"].append((ref, voucher))
            continue
        status = f"bokfort {booked.get(ref, '')}".strip()
        rec = archive.set(hits[0]["id"], fortnox_voucher=voucher, fortnox_status=status)
        (out["unchanged"] if rec.outcome == "unchanged" else out["updated"]).append((hits[0]["id"], voucher))
    return out


def cmd_sync_fortnox(args, archive):
    try:
        res = sync_fortnox(archive, args.plan_dir)
    except (FileNotFoundError, ValueError, KeyError) as exc:
        print(f"error: {exc.args[0] if exc.args else exc}", file=sys.stderr)
        return 1
    verb = "would update" if archive.dry_run else "updated"
    for id_, v in res["updated"]:
        print(f"{verb} {id_} [fortnox_voucher {v}]")
    for id_, v in res["unchanged"]:
        print(f"unchanged {id_} (verifikat {v})")
    for ref, v in res["no_page"]:
        print(f"no page with amex_ref {ref} (verifikat {v}); archive its receipt first (ingest amex or add --amex-ref)")
    for ref in res["no_voucher"]:
        print(f"no verifikat yet for {ref} in execution.json")
    print(f"{verb} {len(res['updated'])}, unchanged {len(res['unchanged'])}, no page {len(res['no_page'])}, "
          f"no verifikat {len(res['no_voucher'])}")
    if res["updated"] and not archive.dry_run and archive.write_index():
        print(f"index refreshed: {archive.root / 'index.md'}")
    return 0


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=argparse.SUPPRESS, help="archive root (default $RECEIPTS_ARCHIVE or <repo>/archive)")
    common.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS, help="print planned actions, write nothing")
    common.add_argument("--no-text", action="store_true", default=argparse.SUPPRESS, help="skip text extraction (text_method none)")
    common.add_argument("--retext", action="store_true", default=argparse.SUPPRESS,
                        help="re-run text extraction and harvesting on every page an ingest merges into")
    ap = argparse.ArgumentParser(description="Receipt archive: markdown pages with frontmatter, files next to them (see docstring).",
                                 parents=[common])
    sub = ap.add_subparsers(dest="command", metavar="command")
    sub.required = True

    ing = sub.add_parser("ingest", parents=[common], help="archive receipts from a source")
    src = ing.add_subparsers(dest="source", metavar="source")
    src.required = True
    p = src.add_parser("pleo-export", parents=[common], help="Pleo export folder (export_*.csv plus receipts/)")
    p.add_argument("dir")
    p.add_argument("--entity", default="viseo", choices=sorted(ENTITY_NAMES))
    p.add_argument("--export-date", default=None, help="YYYY-MM-DD, default the date in the folder name")
    p = src.add_parser("matching", parents=[common], help="Pleo lane MATCHING.csv with fetched files")
    p.add_argument("csv")
    p.add_argument("--files-dir", default=None, help="where file_to_attach lives (default the CSV folder)")
    p.add_argument("--entity", default="viseo", choices=sorted(ENTITY_NAMES))
    p = src.add_parser("amex", parents=[common], help="classified Amex ledger plus fetched receipt files")
    p.add_argument("--ledger", required=True, help="classify.py CSV output")
    p.add_argument("--receipts-dir", required=True, help="files named <ref>_<anything>")
    p.add_argument("--matches", nargs="*", default=[], help="amex_receipts.py / gmail_match.py matches JSON files")

    p = sub.add_parser("add", parents=[common], help="archive one receipt by hand (first FILE is primary)")
    p.add_argument("files", nargs="+")
    p.add_argument("--entity", required=True, choices=sorted(ENTITY_NAMES))
    p.add_argument("--date", required=True, help="charge date YYYY-MM-DD")
    p.add_argument("--merchant", required=True)
    p.add_argument("--amount-sek", required=True, dest="amount_sek")
    for opt in ("currency", "amount", "vendor", "card", "lane", "kind", "source-kind", "source-ref", "source-from",
                "source-subject", "source-url", "pleo-expense-id", "pleo-receipt-number", "pleo-status", "pleo-category",
                "amex-ref", "bas-account", "vat-regime", "vat-rate", "vat-sek", "receipt-date", "invoice-number",
                "vendor-country", "vendor-vat-number", "fortnox-voucher", "fortnox-status", "note"):
        p.add_argument("--" + opt, dest=opt.replace("-", "_"), default=None)

    p = sub.add_parser("set", parents=[common], help="update keys on one page")
    p.add_argument("id")
    p.add_argument("changes", nargs="+", metavar="key=value")

    sub.add_parser("index", parents=[common], help="regenerate <root>/index.md")

    p = sub.add_parser("find", parents=[common], help="search frontmatter")
    for opt in ("entity", "vendor", "month", "date", "amount-sek", "amex-ref", "pleo-expense-id", "sha256", "id"):
        p.add_argument("--" + opt, dest=opt.replace("-", "_"), default=None)
    p.add_argument("--json", action="store_true")

    sub.add_parser("check", parents=[common], help="lint the whole root")

    p = sub.add_parser("sync-fortnox", parents=[common],
                       help="copy verifikat numbers from a plan's execution.json onto the Amex pages")
    p.add_argument("plan_dir", help="out/fortnox-run-YYYY-MM (must contain execution.json)")

    p = sub.add_parser("text", parents=[common], help="print receipt_text JSON for one file")
    p.add_argument("file")
    p.add_argument("--preview", default=None, help="write page 1 preview PNG here")
    return ap


def main(argv=None):
    global EXTRACTOR
    ap = build_parser()
    args = ap.parse_args(argv)
    args.root = getattr(args, "root", None)
    args.dry_run = getattr(args, "dry_run", False)
    args.no_text = getattr(args, "no_text", False)
    args.retext = getattr(args, "retext", False)
    if args.no_text:
        EXTRACTOR = _no_text_extract
    archive = Archive(args.root, dry_run=args.dry_run, retext=args.retext and not args.no_text)
    if args.dry_run:
        print(f"dry run: nothing is written under {archive.root}", file=sys.stderr)
    handlers = {"ingest": cmd_ingest, "add": cmd_add, "set": cmd_set, "index": cmd_index,
                "find": cmd_find, "check": cmd_check, "text": cmd_text, "sync-fortnox": cmd_sync_fortnox}
    return handlers[args.command](args, archive)


if __name__ == "__main__":
    sys.exit(main())
