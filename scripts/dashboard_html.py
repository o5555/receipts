#!/usr/bin/env python3
"""Renderer for Kvittotavlan, the receipts progress page.

render(model) -> str takes the model dict built by scripts/dashboard.py and returns one
self-contained Swedish HTML page (meta tags, title, font link, one style block, the
content). No doctype, html, head or body tags: the file is opened locally as is and also
published through a wrapper that supplies them.

Every value from the model is escaped with html.escape. Amounts are printed in Swedish
form (1 234,56 SEK, non-breaking space between thousands groups), dates stay ISO and
month headings read like "augusti 2026". Every section degrades to "underlag saknas"
or "inget att göra just nu" when its part of the model is empty. Company names come from
archive.ENTITY_NAMES; the Swedish month list and the plural helper live here and the
collectors in dashboard.py import them. Python 3.9, stdlib only.
"""
from __future__ import annotations

import html
import os
import re
import sys
from typing import Any, Dict, List, Tuple

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from archive import ENTITY_NAMES  # noqa: E402 - sibling module, the one place company names live

NBSP = "\u00a0"
RECEIPT_NO_RE = re.compile(r"\d{7}")

MONTHS_SV = [
    "januari", "februari", "mars", "april", "maj", "juni",
    "juli", "augusti", "september", "oktober", "november", "december",
]

SEVERITY_CLASS = {"crit": "crit", "warn": "warn", "info": "unk"}
SEVERITY_LABEL = {"crit": "kritiskt", "warn": "varning", "info": "info"}
TILE_LABEL = {"ok": "ok", "warn": "varning", "crit": "kritiskt", "dead": "saknas"}
SOURCE_CLASS = {"ok": "ok", "stale": "warn", "missing": "dead"}
SOURCE_LABEL = {"ok": "ok", "stale": "gammal", "missing": "saknas"}
EXPORT_STATUS = {
    "EXPORTED": ("ok", "exporterad"),
    "QUEUED": ("unk", "köad"),
    "NOT_EXPORTED": ("dead", "ej exporterad"),
}
FLAG_CLASS = {"open": "crit", "refiled": "ok", "gone": "dead"}
STAGE_CLASS = {
    "no-csv": "dead", "untagged": "warn", "no-plan": "warn",
    "planned": "unk", "partial": "warn", "booked": "ok", "nothing-to-book": "ok",
}
SOURCE_ORDER = ["pleo_export", "pleo_missing", "worklist", "amex_csv", "amex_ledger", "fortnox_runs", "archive"]
STEP_NAMES = ["CSV", "taggat", "kvitton", "plan", "bokfört"]

EMPTY_DATA = "underlag saknas"
EMPTY_TODO = "inget att göra just nu"


# ---------------------------------------------------------------- formatting helpers

def esc(value: Any) -> str:
    """html.escape of any value; None becomes the empty string."""
    if value is None:
        return ""
    return html.escape(str(value))


def sv_amount(value: Any, unit: str = "SEK") -> str:
    """3570.54 -> '3 570,54 SEK' with U+00A0 between thousands groups and a plain space before the unit."""
    if value is None or value == "":
        return ""
    try:
        x = float(value)
    except (TypeError, ValueError):
        return str(value)
    sign = "-" if x < 0 else ""
    cents = int(round(abs(x) * 100))
    kronor, ore = divmod(cents, 100)
    body = "{:,}".format(kronor).replace(",", NBSP)
    text = "{}{},{:02d}".format(sign, body, ore)
    return "{} {}".format(text, unit) if unit else text


def sv_int(value: Any) -> str:
    """1234 -> '1 234' with U+00A0 as the thousands separator; None counts as 0."""
    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        return str(value)
    sign = "-" if n < 0 else ""
    return sign + "{:,}".format(abs(n)).replace(",", NBSP)


def month_name(ym: Any) -> str:
    """'2026-08' -> 'augusti 2026'; anything else is returned as given."""
    text = str(ym or "")
    if len(text) >= 7 and text[4] == "-" and text[:4].isdigit() and text[5:7].isdigit():
        idx = int(text[5:7]) - 1
        if 0 <= idx < 12:
            return "{} {}".format(MONTHS_SV[idx], text[:4])
    return text


def age_text(days: Any) -> str:
    if days is None:
        return ""
    try:
        n = int(days)
    except (TypeError, ValueError):
        return str(days)
    if n == 0:
        return "i dag"
    if n == 1:
        return "1 dag"
    return "{} dagar".format(sv_int(n))


def stamp_text(generated: Any) -> str:
    """'2026-09-04T14:00:00Z' -> '2026-09-04 14:00 UTC'."""
    text = str(generated or "")
    if len(text) >= 16 and text[10] == "T":
        return "{} {} UTC".format(text[:10], text[11:16])
    return text


def pct(part: Any, whole: Any) -> float:
    try:
        p = float(part or 0)
        w = float(whole or 0)
    except (TypeError, ValueError):
        return 0.0
    if w <= 0:
        return 0.0
    return max(0.0, min(100.0, 100.0 * p / w))


def _d(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _l(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _n(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def plural(n: Any, one: str, many: str) -> str:
    """The Swedish form that goes with a count: plural(1, "rad", "rader") is "rad"."""
    return one if _n(n) == 1 else many


def blocked(label: Any) -> bool:
    """True for a business-row status that keeps the row out of a Fortnox voucher (a missing
    BAS account or a manifest exclusion), so the table shows it as critical."""
    text = str(label or "")
    return text.startswith("utanför planen") or "konto saknas" in text


# ---------------------------------------------------------------- small components

def chip(cls: str, text: Any) -> str:
    return '<span class="chip {}">{}</span>'.format(esc(cls), esc(text))


def num(text: Any) -> str:
    return '<span class="num">{}</span>'.format(esc(text))


def amount(value: Any) -> str:
    return '<span class="num">{}</span>'.format(esc(sv_amount(value)))


def ident(text: Any) -> str:
    return '<span class="id">{}</span>'.format(esc(text))


def empty(text: str) -> str:
    return '<p class="empty">{}</p>'.format(esc(text))


def meter(part: Any, whole: Any, label: str) -> str:
    return '<div class="meter" role="img" aria-label="{}"><span style="width:{:.1f}%"></span></div>'.format(
        esc(label), pct(part, whole))


def segbar(segments: List[Tuple[str, int, str]]) -> str:
    """segments: (css class, count, title). Zero counts are skipped so the bar never shows empty slivers."""
    parts = []
    for cls, count, title in segments:
        n = _n(count)
        if n <= 0:
            continue
        parts.append('<span class="seg {}" style="flex-grow:{}" title="{}"></span>'.format(esc(cls), n, esc(title)))
    if not parts:
        parts.append('<span class="seg none" style="flex-grow:1" title="inga rader"></span>')
    return '<div class="segbar">{}</div>'.format("".join(parts))


def legend(items: List[Tuple[str, str]]) -> str:
    return '<div class="legend">{}</div>'.format("".join(
        '<span><span class="sw {}"></span>{}</span>'.format(esc(cls), esc(text)) for cls, text in items))


def export_chip(status: Any) -> str:
    cls, label = EXPORT_STATUS.get(str(status or "").upper(), ("dead", str(status or "okänd").lower()))
    return chip(cls, label)


def tile(state: str, label: str, value_html: str, sub_html: str, meter_html: str = "") -> str:
    return (
        '<div class="tile {s}">'
        '<div class="tile-head"><span class="tile-k">{l}</span>{c}</div>'
        '<div class="tile-v">{v}</div>{m}'
        '<div class="tile-s">{sub}</div>'
        '</div>'
    ).format(s=esc(state), l=esc(label), c=chip(state, TILE_LABEL.get(state, state)),
             v=value_html, m=meter_html, sub=sub_html)


def h2(number: str, title: str) -> str:
    return '<h2><span class="k">{}</span>{}</h2>'.format(esc(number), esc(title))


def h3(anchor: str, title: str, note: str = "") -> str:
    out = '<h3 id="{}">{}</h3>'.format(esc(anchor), esc(title))
    if note:
        out += '<p class="groupnote">{}</p>'.format(note)
    return out


# ---------------------------------------------------------------- sections

def render_masthead(m: Dict[str, Any]) -> str:
    today = m.get("today") or ""
    entity_name = m.get("entity_name") or "Viseo AB"
    todo = _l(m.get("todo"))
    sources = [_d(v) for v in _d(m.get("sources")).values()]
    missing = sum(1 for s in sources if s.get("state") == "missing")
    stale = sum(1 for s in sources if s.get("state") == "stale")
    if todo:
        top = _d(todo[0])
        title = str(top.get("title") or "")
        lede = "Viktigast just nu: {}".format(title)
        count = top.get("count")
        if count is not None and sv_int(count) not in title:
            lede += " ({} st)".format(sv_int(count))
        if top.get("amount_sek") is not None:
            lede += ", {}".format(sv_amount(top.get("amount_sek")))
        lede += "."
    elif missing or stale:
        parts = []
        if missing:
            parts.append("{} underlag saknas".format(sv_int(missing)))
        if stale:
            parts.append("{} underlag {}".format(sv_int(stale), plural(stale, "är gammalt", "är gamla")))
        lede = "Inget att göra just nu, men {}.".format(" och ".join(parts))
    else:
        lede = "Inget att göra just nu. Allt underlag är i fas."
    return (
        '<header class="masthead">'
        '<div class="eyebrow"><span>{e}</span><span>kvittoflödet</span><span>läget {d}</span></div>'
        '<h1>Kvittotavlan</h1>'
        '<p class="lede">{lede}</p>'
        '</header>'
    ).format(e=esc(entity_name), d=esc(today), lede=esc(lede))


def render_lage(m: Dict[str, Any]) -> str:
    s = _d(m.get("summary"))
    pleo = _d(m.get("pleo"))
    amex = _d(m.get("amex"))
    arch = _d(m.get("archive"))
    sources = _d(m.get("sources"))
    tiles = []

    rows = _n(s.get("pleo_rows"))
    with_receipt = _n(s.get("pleo_with_receipt"))
    missing_live = s.get("pleo_missing_live")
    totals = _d(pleo.get("totals"))
    payouts = _n(totals.get("payouts"))
    missing_cards = _n(totals.get("missing")) if totals else max(0, _n(s.get("pleo_missing_export")) - payouts)
    if rows == 0 and not pleo.get("export_dir"):
        tiles.append(tile("dead", "Pleo-kortet", esc(EMPTY_DATA), esc("ingen Pleo-export i data/pleo/")))
    else:
        live_date = _d(pleo.get("missing_live")).get("date")
        in_export = "{} köp utan kvitto".format(sv_int(missing_cards))
        if payouts > 0:
            in_export = "{} köp och {} {} utan kvitto".format(
                sv_int(missing_cards), sv_int(payouts), plural(payouts, "utbetalning", "utbetalningar"))
        if missing_live is None:
            state = "warn" if missing_cards > 0 else "ok"
            sub = "{} i exporten, ingen live-lista".format(in_export)
        else:
            state = "warn" if _n(missing_live) > 0 else "ok"
            sub = "{} saknas live {}".format(sv_int(missing_live), live_date or "")
            if _n(missing_live) > 0 and s.get("pleo_missing_live_sek") is not None:
                sub += ", {}".format(sv_amount(s.get("pleo_missing_live_sek")))
            sub += ". I exporten: {}.".format(in_export)
        value = '{}<small>av {} har kvitto</small>'.format(num(sv_int(with_receipt)), esc(sv_int(rows)))
        tiles.append(tile(state, "Pleo-kortet", value, esc(sub),
                          meter(with_receipt, rows, "{} av {} har kvitto".format(sv_int(with_receipt), sv_int(rows)))))

    flagged_file = _d(pleo.get("flagged")).get("file")
    total = _n(s.get("flagged_total"))
    open_ = _n(s.get("flagged_open"))
    refiled = _n(s.get("flagged_refiled"))
    if not flagged_file and total == 0:
        tiles.append(tile("dead", "Kvittofel i Pleo", esc(EMPTY_DATA), esc("ingen kvittofel-lista i out/")))
    else:
        state = "crit" if open_ > 0 else "ok"
        value = '{}<small>av {} kvar</small>'.format(num(sv_int(open_)), esc(sv_int(total)))
        sub = "{} med ny fil bifogad".format(sv_int(refiled)) if open_ > 0 else "allt åtgärdat"
        tiles.append(tile(state, "Kvittofel i Pleo", value, esc(sub),
                          meter(total - open_, total, "{} av {} åtgärdade".format(sv_int(total - open_), sv_int(total)))))

    planned = _n(s.get("amex_vouchers_planned"))
    booked = _n(s.get("amex_vouchers_booked"))
    biz = _n(s.get("amex_business_rows"))
    found = _n(s.get("amex_receipts_found"))
    tag_open = _n(s.get("amex_needs_tag_open"))
    if not amex.get("ledger_file") and biz == 0 and planned == 0:
        tiles.append(tile("dead", "Amex till Fortnox", esc(EMPTY_DATA), esc("ingen klassad Amex-ledger i out/")))
    else:
        if tag_open > 0 or planned > booked:
            state = "warn"
        else:
            state = "ok"
        value = '{}<small>av {} verifikat bokförda</small>'.format(num(sv_int(booked)), esc(sv_int(planned)))
        sub = "{} {}, {} {}, {} {}".format(
            sv_int(biz), plural(biz, "företagsrad", "företagsrader"),
            sv_int(found), plural(found, "kvitto funnet", "kvitton funna"),
            sv_int(tag_open), plural(tag_open, "otaggad", "otaggade"))
        if s.get("amex_business_sek") is not None:
            sub = "{} {} ({}), {} {}, {} {}".format(
                sv_int(biz), plural(biz, "företagsrad", "företagsrader"), sv_amount(s.get("amex_business_sek")),
                sv_int(found), plural(found, "kvitto funnet", "kvitton funna"),
                sv_int(tag_open), plural(tag_open, "otaggad", "otaggade"))
        tiles.append(tile(state, "Amex till Fortnox", value, esc(sub),
                          meter(booked, planned, "{} av {} verifikat bokförda".format(sv_int(booked), sv_int(planned)))))

    pages = _n(s.get("archive_pages"))
    errors = _n(s.get("archive_errors"))
    warnings = _n(s.get("archive_warnings"))
    arch_state = str(_d(sources.get("archive")).get("state") or "")
    absent = arch_state == "missing" if arch_state else (pages == 0 and not arch.get("updated"))
    if absent:
        tiles.append(tile("dead", "Kvittoarkivet", esc(EMPTY_DATA), esc("inget arkiv i archive/")))
    else:
        if errors > 0:
            state = "crit"
        elif warnings > 0:
            state = "warn"
        else:
            state = "ok"
        value = '{}<small>{}, {}</small>'.format(num(sv_int(pages)), esc(plural(pages, "sida", "sidor")),
                                                 esc(sv_amount(s.get("archive_sek") or 0)))
        sub = "{} fel, {} {}".format(sv_int(errors), sv_int(warnings), plural(warnings, "varning", "varningar"))
        tiles.append(tile(state, "Kvittoarkivet", value, esc(sub)))

    return '<section id="lage">{}<div class="tiles">{}</div></section>'.format(h2("01", "Läget"), "".join(tiles))


def render_todo(m: Dict[str, Any]) -> str:
    todo = _l(m.get("todo"))
    out = ['<section id="att-gora">', h2("02", "Att göra")]
    if not todo:
        out.append(empty("Inget att göra just nu."))
    else:
        out.append('<ol class="todo">')
        for raw in todo:
            item = _d(raw)
            sev = str(item.get("severity") or "info")
            cls = SEVERITY_CLASS.get(sev, "unk")
            owner = item.get("owner") or "Oscar"
            title = esc(item.get("title"))
            link = item.get("link")
            if link:
                title = '<a href="#{}">{}</a>'.format(esc(link), title)
            nums = []
            if item.get("count") is not None:
                nums.append('<span class="big num">{}</span>'.format(esc(sv_int(item.get("count")))))
            if item.get("amount_sek") is not None:
                nums.append('<span class="amt num">{}</span>'.format(esc(sv_amount(item.get("amount_sek")))))
            out.append('<li class="todo-row {}">'.format(esc(cls)))
            out.append('<div class="todo-chips">{}{}</div>'.format(
                chip(cls, SEVERITY_LABEL.get(sev, sev)), chip("plain", owner)))
            out.append('<p class="todo-title">{}</p>'.format(title))
            out.append('<div class="todo-nums">{}</div>'.format("".join(nums)))
            if item.get("detail"):
                out.append('<p class="todo-detail">{}</p>'.format(esc(item.get("detail"))))
            if item.get("command"):
                out.append('<pre class="cmd"><code>{}</code></pre>'.format(esc(item.get("command"))))
            out.append('</li>')
        out.append('</ol>')
    out.append('</section>')
    return "".join(out)


def render_pleo(m: Dict[str, Any]) -> str:
    pleo = _d(m.get("pleo"))
    out = ['<section id="pleo">', h2("03", "Pleo-kortet")]
    export_dir = pleo.get("export_dir")
    if export_dir:
        out.append('<p class="muted">Exporten {} ({}).</p>'.format(ident(export_dir), esc(pleo.get("export_date") or "")))

    # (a) per month: card purchases with and without receipt, payouts apart (they never carry one)
    months = _l(pleo.get("months"))
    out.append(h3("pleo-manader", "Per månad"))
    if not months:
        out.append(empty("{}: ingen Pleo-export i data/pleo/.".format(EMPTY_DATA)))
    else:
        out.append(legend([("have", "med kvitto"), ("miss", "saknar kvitto"), ("pay", "utbetalning, inget kvitto")]))
        out.append('<ol class="mrows">')
        for raw in months:
            mo = _d(raw)
            rows = _n(mo.get("rows"))
            have = _n(mo.get("with_receipt"))
            miss = _n(mo.get("missing"))
            pay = _n(mo.get("payouts"))
            counts = '{} av {} har kvitto'.format(num(sv_int(have)), num(sv_int(rows)))
            if miss > 0:
                counts += ', {} saknas'.format(amount(mo.get("missing_sek") or 0))
            if pay > 0:
                counts += ', {} {}'.format(num(sv_int(pay)), esc(plural(pay, "utbetalning", "utbetalningar")))
            out.append(
                '<li class="mrow">'
                '<div class="m-name">{name}</div>'
                '<div class="m-counts">{counts}</div>'
                '{bar}'
                '<div class="kv"><span>{e} exporterade</span><span>{q} köade</span><span>{n} ej exporterade</span></div>'
                '</li>'.format(
                    name=esc(month_name(mo.get("month"))), counts=counts,
                    bar=segbar([("have", have, "{} med kvitto".format(sv_int(have))),
                                ("miss", miss, "{} saknar kvitto".format(sv_int(miss))),
                                ("pay", pay, "{} {}".format(sv_int(pay), plural(pay, "utbetalning", "utbetalningar")))]),
                    e=num(sv_int(mo.get("exported"))), q=num(sv_int(mo.get("queued"))),
                    n=num(sv_int(mo.get("not_exported")))))
        totals = _d(pleo.get("totals"))
        if totals:
            t_rows = _n(totals.get("rows"))
            t_have = _n(totals.get("with_receipt"))
            t_miss = _n(totals.get("missing"))
            t_pay = _n(totals.get("payouts"))
            counts = '{} av {} har kvitto, {} saknas av {}'.format(
                num(sv_int(t_have)), num(sv_int(t_rows)),
                amount(totals.get("missing_sek") or 0), amount(totals.get("amount_sek") or 0))
            if t_pay > 0:
                counts += ', {} {} ({})'.format(num(sv_int(t_pay)), esc(plural(t_pay, "utbetalning", "utbetalningar")),
                                                amount(totals.get("payouts_sek") or 0))
            out.append(
                '<li class="mrow total">'
                '<div class="m-name">Totalt</div>'
                '<div class="m-counts">{}</div>'
                '{}'
                '<div class="kv"><span>{} {}</span></div>'
                '</li>'.format(
                    counts,
                    segbar([("have", t_have, "{} med kvitto".format(sv_int(t_have))),
                            ("miss", t_miss, "{} saknar kvitto".format(sv_int(t_miss))),
                            ("pay", t_pay, "{} {}".format(sv_int(t_pay), plural(t_pay, "utbetalning", "utbetalningar")))]),
                    num(sv_int(t_rows)), esc(plural(t_rows, "rad", "rader"))))
        out.append('</ol>')

    # (b) missing live
    live = _d(pleo.get("missing_live"))
    live_rows = [_d(r) for r in _l(live.get("rows"))]
    by_route = [_d(r) for r in _l(live.get("by_route"))]
    note = ""
    if live.get("file"):
        note = 'Enligt {} ({}).'.format(ident(live.get("file")), esc(live.get("date") or ""))
    out.append(h3("pleo-saknas", "Saknar kvitto just nu", note))
    if not live.get("file") and not live_rows:
        out.append(empty("{}: ingen live-lista i out/.".format(EMPTY_DATA)))
    elif not live_rows:
        out.append(empty("{}, alla rader har kvitto.".format(EMPTY_TODO.capitalize())))
    else:
        routes = [r.get("route") for r in by_route]
        leftovers = [r for r in live_rows if r.get("route") not in routes and r.get("route") != "attached"]
        if leftovers:
            by_route.append({"route": None, "route_label": "övrigt", "count": len(leftovers),
                             "amount_sek": sum(float(r.get("amount_sek") or 0) for r in leftovers), "owner": "systemet"})
        # rows the archive already knows as attached: listed last, no owner, nothing to do
        attached = [r for r in live_rows if r.get("route") == "attached"]
        if attached:
            by_route.append({"route": "attached", "route_label": attached[0].get("route_label") or "bifogat sedan listan gjordes",
                             "count": len(attached), "amount_sek": sum(float(r.get("amount_sek") or 0) for r in attached), "owner": None})
        # the live list keys rows by Pleo receipt number when it has one, else by its own id;
        # the expense id is what finds the row in Pleo
        real_numbers = all(RECEIPT_NO_RE.fullmatch(str(r.get("receipt_no") or "")) for r in live_rows)
        out.append('<div class="tablewrap"><table class="ledger">')
        out.append('<thead><tr><th>Datum</th><th>{}</th><th>Leverantör</th><th>Belopp</th><th>Status</th><th>Var kvittot finns</th></tr></thead><tbody>'.format(
            "Kvittonr" if real_numbers else "Id"))
        for grp in by_route:
            members = [r for r in live_rows if r.get("route") == grp.get("route")]
            n = grp.get("count") if grp.get("count") is not None else len(members)
            out.append('<tr class="grp"><td colspan="6"><b>{}</b> {} {} {}, {}</td></tr>'.format(
                esc(grp.get("route_label") or grp.get("route") or "övrigt"),
                chip("ok", "klart") if grp.get("route") == "attached" else chip("plain", grp.get("owner") or "systemet"),
                esc(sv_int(n)), esc(plural(n, "rad", "rader")),
                esc(sv_amount(grp.get("amount_sek") or 0))))
            for r in members:
                vendor = r.get("vendor") or r.get("merchant") or ""
                merchant = r.get("merchant") or ""
                name = esc(vendor)
                if merchant and merchant != vendor:
                    name += '<small>{}</small>'.format(esc(merchant))
                if r.get("type") and "pocket" in str(r.get("type")).lower():
                    name += '<small>eget utlägg</small>'
                rid = ident(r.get("receipt_no"))
                eid = str(r.get("expense_id") or "")
                if eid and not RECEIPT_NO_RE.fullmatch(str(r.get("receipt_no") or "")):
                    rid += '<small class="id" title="{}">{}</small>'.format(esc(eid), esc(eid[:8]))
                out.append('<tr><td class="c">{}</td><td class="c">{}</td><td class="name">{}</td><td class="c">{}</td><td class="c">{}</td><td>{}</td></tr>'.format(
                    esc(r.get("date")), rid, name, amount(r.get("amount_sek")),
                    export_chip(r.get("export_status")), esc(r.get("hint") or r.get("route_label") or "")))
        out.append('</tbody></table></div>')

    # (c) flagged
    flagged = _d(pleo.get("flagged"))
    groups = [_d(g) for g in _l(flagged.get("groups"))]
    note = ""
    if flagged.get("file"):
        note = 'Listan {} ({}).'.format(ident(flagged.get("file")), esc(flagged.get("date") or ""))
    out.append(h3("pleo-fel", "Fel kvitto bifogat", note))
    if not flagged.get("file") and not groups:
        out.append(empty("{}: ingen kvittofel-lista i out/.".format(EMPTY_DATA)))
    elif not groups:
        out.append(empty("{}, inga fel kvar.".format(EMPTY_TODO.capitalize())))
    else:
        for grp in groups:
            chips = [chip("plain", "{} st".format(sv_int(grp.get("count"))))]
            if _n(grp.get("open")) > 0:
                chips.append(chip("crit", "{} {}".format(sv_int(grp.get("open")), plural(grp.get("open"), "öppen", "öppna"))))
            if _n(grp.get("refiled")) > 0:
                chips.append(chip("ok", "{} med ny fil".format(sv_int(grp.get("refiled")))))
            if _n(grp.get("gone")) > 0:
                chips.append(chip("dead", "{} borta".format(sv_int(grp.get("gone")))))
            out.append('<h4 class="grp-h">{} {}</h4>'.format(esc(grp.get("label") or grp.get("key") or ""), "".join(chips)))
            items = [_d(i) for i in _l(grp.get("items"))]
            if not items:
                out.append('<p class="muted small">Inga rader i listan för den här gruppen.</p>')
                continue
            out.append('<div class="tablewrap"><table class="ledger">')
            out.append('<thead><tr><th>Kvittonr</th><th>Datum</th><th>Leverantör</th><th>Belopp</th><th>Export</th><th>Problem</th><th>Status</th></tr></thead><tbody>')
            for it in items:
                vendor = it.get("vendor") or it.get("merchant") or ""
                merchant = it.get("merchant") or ""
                name = esc(vendor)
                if merchant and merchant != vendor:
                    name += '<small>{}</small>'.format(esc(merchant))
                problem = esc(it.get("problem") or "")
                if it.get("check_still_warns"):
                    problem += '<small class="muted">arkivkontrollen varnar fortfarande</small>'
                status = str(it.get("status") or "open")
                out.append('<tr><td class="c">{}</td><td class="c">{}</td><td class="name">{}</td><td class="c">{}</td><td class="c">{}</td><td>{}</td><td class="c">{}</td></tr>'.format(
                    ident(it.get("receipt_no")), esc(it.get("date")), name, amount(it.get("amount_sek")),
                    export_chip(it.get("export_status")), problem,
                    chip(FLAG_CLASS.get(status, "unk"), it.get("status_label") or status)))
            out.append('</tbody></table></div>')
        baseline = flagged.get("baseline_export")
        compared = flagged.get("compared_export")
        if compared:
            out.append('<p class="groupnote">Jämförelse: baslinjen {} mot den nyare exporten {}. Rader med en annan fil räknas som åtgärdade, rader som tappat sin fil står kvar som öppna.</p>'.format(
                ident(baseline), ident(compared)))
        else:
            out.append('<p class="groupnote">Bara baslinjen {} finns, så allt räknas som öppet tills nästa export är nedladdad och ingesterad.</p>'.format(
                ident(baseline or "")))
    same_now = _l(pleo.get("same_file_groups_now"))
    if same_now:
        out.append('<p class="muted small">Samma fil på flera kvittonummer i exporten just nu: {} {} ({}).</p>'.format(
            esc(sv_int(len(same_now))), esc(plural(len(same_now), "grupp", "grupper")),
            ", ".join(ident(" + ".join(str(x) for x in _l(g))) for g in same_now)))
    out.append('</section>')
    return "".join(out)


def amex_steps(mo: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Five stepper states (done, partial, todo) for one ledger month, from its numbers."""
    stage = str(mo.get("stage") or "no-csv")
    rows = _n(mo.get("rows"))
    business = _n(mo.get("business"))
    tag_open = _n(mo.get("needs_tag_open"))
    found = _n(mo.get("receipts_found"))
    plan = _d(mo.get("plan"))
    vouchers = _n(plan.get("vouchers"))
    booked = _n(plan.get("booked"))
    if stage == "no-csv":
        return [("CSV", "partial" if rows > 0 else "todo")] + [(n, "todo") for n in STEP_NAMES[1:]]
    if tag_open == 0:
        tag = "done"
    elif tag_open < rows:
        tag = "partial"
    else:
        tag = "todo"
    if business > 0 and found >= business:
        rec = "done"
    elif found > 0:
        rec = "partial"
    elif business == 0 and tag == "done":
        rec = "done"
    else:
        rec = "todo"
    if plan and vouchers > 0:
        pl = "done"
    elif plan:
        pl = "partial"
    else:
        pl = "todo"
    if vouchers > 0 and booked >= vouchers:
        bk = "done"
    elif booked > 0:
        bk = "partial"
    else:
        bk = "todo"
    if stage == "nothing-to-book":
        # no business rows and no plan: there is nothing to plan or book
        pl = bk = "done"
    return list(zip(STEP_NAMES, ["done", tag, rec, pl, bk]))


def render_amex(m: Dict[str, Any]) -> str:
    amex = _d(m.get("amex"))
    entity = str(m.get("entity") or "viseo")
    out = ['<section id="amex">', h2("04", "Amex till Fortnox")]
    if amex.get("ledger_file"):
        line = 'Ledgern {} ({}), köp till och med {}.'.format(
            ident(amex.get("ledger_file")), esc(amex.get("ledger_date") or ""), esc(amex.get("coverage_end") or ""))
        nxt = amex.get("next_month")
        if nxt:
            line += ' Nästa månad {}: {}.'.format(
                esc(month_name(nxt)), "CSV finns" if amex.get("next_month_csv_present") else "CSV saknas")
        out.append('<p class="muted">{}</p>'.format(line))

    biz = [_d(x) for x in _l(amex.get("business_rows"))]
    months = [_d(x) for x in _l(amex.get("months"))]
    out.append(h3("amex-manader", "Per månad"))
    if not months:
        out.append(empty("{}: ingen klassad Amex-ledger i out/.".format(EMPTY_DATA)))
    else:
        out.append('<div class="cards">')
        for mo in months:
            stage = str(mo.get("stage") or "no-csv")
            steps = "".join('<li class="{}" title="{}">{}</li>'.format(esc(state), esc(state), esc(name)) for name, state in amex_steps(mo))
            plan = _d(mo.get("plan"))
            business = _n(mo.get("business"))
            # every figure sits in its own nowrap span so a narrow card wraps between figures only
            lines = [
                ("företagsrader", "{}, {}".format(num(sv_int(business)), amount(mo.get("business_sek") or 0))),
                ("kvitton funna", "{} av {}".format(num(sv_int(mo.get("receipts_found"))), num(sv_int(business)))),
                ("otaggade", num(sv_int(mo.get("needs_tag_open")))),
            ]
            if plan:
                lines.append(("verifikat bokförda", "{} av {}".format(num(sv_int(plan.get("booked"))), num(sv_int(plan.get("vouchers"))))))
            else:
                lines.append(("verifikat bokförda", esc("ingen plan")))
            other = _n(mo.get("business_other_entity"))
            if other > 0:
                lines.append(("annat bolag", "{} {}".format(num(sv_int(other)), esc(plural(other, "rad", "rader")))))
            outside = sum(1 for r in biz if r.get("month") == mo.get("month") and blocked(r.get("status_label")))
            if outside > 0:
                lines.append(("utanför planen", "{} {}".format(num(sv_int(outside)), esc(plural(outside, "rad", "rader")))))
            dl = "".join('<div><dt>{}</dt><dd>{}</dd></div>'.format(esc(k), v) for k, v in lines)
            plan_note = ""
            if plan:
                plan_note = '<p class="plan-note">plan: {} uppladdade, {} {}, byggd {}</p>'.format(
                    num(sv_int(plan.get("uploaded"))), num(sv_int(plan.get("warnings"))),
                    esc(plural(plan.get("warnings"), "varning", "varningar")), num(plan.get("built") or ""))
            out.append(
                '<div class="card">'
                '<div class="card-head"><h4>{name}</h4>{pill}</div>'
                '<ol class="stepper">{steps}</ol>'
                '<dl class="lines">{dl}</dl>'
                '{plan_note}'
                '<div class="kv"><span>{rows} rader</span><span>{pers} privat</span><span>{skip} skip</span></div>'
                '</div>'.format(
                    name=esc(month_name(mo.get("month"))),
                    pill=chip(STAGE_CLASS.get(stage, "unk"), mo.get("stage_label") or stage),
                    steps=steps, dl=dl, plan_note=plan_note,
                    rows=num(sv_int(mo.get("rows"))), pers=num(sv_int(mo.get("personal"))), skip=num(sv_int(mo.get("skip")))))
        out.append('</div>')

    needs = [_d(x) for x in _l(amex.get("needs_tag"))]
    out.append(h3("amex-otaggat", "Otaggade rader"))
    if not amex.get("ledger_file") and not needs:
        out.append(empty("{}: ingen needs-tag-lista i out/.".format(EMPTY_DATA)))
    elif not needs:
        out.append(empty("{}, alla rader är taggade.".format(EMPTY_TODO.capitalize())))
    else:
        out.append('<div class="tablewrap"><table class="ledger">')
        out.append('<thead><tr><th>Datum</th><th>Referens</th><th>Handlare</th><th>Belopp</th><th>Kort</th><th>Varför</th><th>Svar</th></tr></thead><tbody>')
        for r in needs:
            answered = bool(r.get("answered"))
            out.append('<tr><td class="c">{}</td><td class="c">{}</td><td class="name">{}</td><td class="c">{}</td><td class="c">{}</td><td>{}</td><td class="c">{}</td></tr>'.format(
                esc(r.get("date")), ident(r.get("ref")), esc(r.get("merchant")), amount(r.get("amount_sek")),
                esc(r.get("card")), esc(r.get("why")),
                chip("ok", "besvarad") if answered else chip("warn", "väntar")))
        out.append('</tbody></table></div>')

    out.append(h3("amex-foretag", "Företagsrader"))
    if not amex.get("ledger_file") and not biz:
        out.append(empty("{}: ingen klassad Amex-ledger i out/.".format(EMPTY_DATA)))
    elif not biz:
        out.append(empty("Inga företagsrader i ledgern."))
    else:
        out.append('<div class="tablewrap"><table class="ledger">')
        out.append('<thead><tr><th>Datum</th><th>Handlare</th><th>Belopp</th><th>Kort</th><th>Konto</th><th>Moms</th><th>Kvitto</th><th>Verifikat</th></tr></thead><tbody>')
        current = None
        for r in biz:
            month = r.get("month") or str(r.get("date") or "")[:7]
            if month != current:
                current = month
                group = [x for x in biz if (x.get("month") or str(x.get("date") or "")[:7]) == month]
                total = sum(float(x.get("amount_sek") or 0) for x in group)
                out.append('<tr class="grp"><td colspan="8"><b>{}</b> {} {}, {}</td></tr>'.format(
                    esc(month_name(month)), esc(sv_int(len(group))), esc(plural(len(group), "rad", "rader")), esc(sv_amount(total))))
            vendor = r.get("vendor") or r.get("merchant") or ""
            merchant = r.get("merchant") or ""
            name = esc(vendor)
            if merchant and merchant != vendor:
                name += '<small>{}</small>'.format(esc(merchant))
            if r.get("entity") and str(r.get("entity")) != entity:
                name += '<small>{}</small>'.format(esc(ENTITY_NAMES.get(str(r.get("entity")), r.get("entity"))))
            label = str(r.get("status_label") or "")
            if r.get("voucher"):
                voucher = ident(r.get("voucher"))
            elif blocked(label):
                voucher = chip("crit", label)
            else:
                voucher = '<span class="muted">{}</span>'.format(esc(label))
            receipt = chip("ok", "kvitto") if r.get("receipt") else chip("crit", "saknas")
            out.append('<tr><td class="c">{}</td><td class="name">{}</td><td class="c">{}</td><td class="c">{}</td><td class="c">{}</td><td class="c">{}</td><td class="c">{}</td><td>{}</td></tr>'.format(
                esc(r.get("date")), name, amount(r.get("amount_sek")), esc(r.get("card")),
                ident(r.get("bas_account") or ""), esc(r.get("vat_regime") or ""), receipt, voucher))
        out.append('</tbody></table></div>')
    out.append('</section>')
    return "".join(out)


def render_arkiv(m: Dict[str, Any]) -> str:
    arch = _d(m.get("archive"))
    out = ['<section id="arkiv">', h2("05", "Kvittoarkivet")]
    pages = _n(arch.get("pages"))
    arch_state = str(_d(_d(m.get("sources")).get("archive")).get("state") or "")
    absent = arch_state == "missing" if arch_state else (pages == 0 and not arch.get("updated"))
    if absent:
        out.append(empty("{}: inget arkiv i archive/.".format(EMPTY_DATA)))
        out.append('</section>')
        return "".join(out)
    errors = [str(e) for e in _l(arch.get("errors"))]
    if errors:
        out.append('<div class="callout crit"><p><b>{} fel i arkivkontrollen.</b></p><ul>{}</ul></div>'.format(
            esc(sv_int(len(errors))), "".join('<li>{}</li>'.format(esc(e)) for e in errors)))
    months = [_d(x) for x in _l(arch.get("months"))]
    out.append(h3("arkiv-manader", "Per månad"))
    if not months:
        out.append(empty("Inga sidor i arkivet än."))
    else:
        out.append(legend([("att", "bifogat i Pleo"), ("inp", "i Pleo sedan tidigare"), ("amx", "Amex till Fortnox"), ("none", "utan status")]))
        out.append('<ol class="mrows">')
        for mo in months:
            p = _n(mo.get("pages"))
            att = _n(mo.get("attached"))
            inp = _n(mo.get("in_pleo"))
            amx = _n(mo.get("amex"))
            rest = max(0, p - att - inp - amx)
            warn = _n(mo.get("warnings"))
            out.append(
                '<li class="mrow">'
                '<div class="m-name">{name}</div>'
                '<div class="m-counts">{pages} {sidor}, {sum}</div>'
                '{bar}'
                '<div class="kv">{warn}</div>'
                '</li>'.format(
                    name=esc(month_name(mo.get("month"))), pages=num(sv_int(p)), sidor=esc(plural(p, "sida", "sidor")),
                    sum=amount(mo.get("amount_sek") or 0),
                    bar=segbar([("att", att, "{} bifogade".format(sv_int(att))), ("inp", inp, "{} i Pleo".format(sv_int(inp))),
                                ("amx", amx, "{} Amex".format(sv_int(amx))), ("none", rest, "{} utan status".format(sv_int(rest)))]),
                    warn=chip("warn", "{} {}".format(sv_int(warn), plural(warn, "varning", "varningar"))) if warn > 0 else chip("ok", "inga varningar")))
        out.append('</ol>')
    kinds = [_d(x) for x in _l(arch.get("warning_kinds"))]
    warnings = [_d(x) for x in _l(arch.get("warnings"))]
    out.append(h3("arkiv-varningar", "Varningar"))
    if not kinds and not warnings:
        out.append(empty("Inga varningar från arkivkontrollen."))
    else:
        out.append('<div class="kinds">{}</div>'.format("".join(
            '<span class="chip warn">{} <b class="num">{}</b></span>'.format(esc(k.get("label") or k.get("key")), esc(sv_int(k.get("count")))) for k in kinds)))
        if warnings:
            n_warn = arch.get("warnings_total") if arch.get("warnings_total") is not None else len(warnings)
            out.append('<details><summary>Alla {} {}</summary><ul class="warns">{}</ul></details>'.format(
                esc(sv_int(n_warn)), esc(plural(n_warn, "varning", "varningar")),
                "".join('<li>{}<span>{}</span></li>'.format(ident(w.get("page_id")), esc(w.get("text"))) for w in warnings)))
    status = _d(arch.get("status"))
    methods = _d(arch.get("text_methods"))
    foot = []
    foot.append("Pleo-status: {} bifogade, {} i Pleo, {} utan status. Fortnox: {} bokförda, {} ej bokförda.".format(
        sv_int(status.get("attached")), sv_int(status.get("in_pleo")), sv_int(status.get("no_status")),
        sv_int(status.get("fortnox_booked")), sv_int(status.get("fortnox_open"))))
    if methods:
        foot.append("Kvittotext via {}.".format(", ".join("{} {}".format(k, sv_int(v)) for k, v in methods.items())))
    foot.append("Förhandsvisningar: {} av {} sidor.".format(sv_int(arch.get("previews")), sv_int(pages)))
    if arch.get("updated"):
        foot.append("Index uppdaterat {}.".format(stamp_text(arch.get("updated"))))
    out.append('<p class="muted small">{}</p>'.format(esc(" ".join(foot))))
    out.append('</section>')
    return "".join(out)


def render_underlag(m: Dict[str, Any]) -> str:
    sources = _d(m.get("sources"))
    out = ['<section id="underlag">', h2("06", "Underlag")]
    if not sources:
        out.append(empty("{}: inga källor lästa.".format(EMPTY_DATA)))
        out.append('</section>')
        return "".join(out)
    keys = [k for k in SOURCE_ORDER if k in sources] + [k for k in sources if k not in SOURCE_ORDER]
    out.append('<ol class="sources">')
    for key in keys:
        src = _d(sources.get(key))
        state = str(src.get("state") or "missing")
        when = esc(src.get("date") or "")
        age = age_text(src.get("age_days"))
        if when and age:
            when = "{}, {}".format(when, esc(age))
        elif age:
            when = esc(age)
        out.append(
            '<li class="src-row">'
            '<span>{chip}</span>'
            '<span class="src-lbl">{label}</span>'
            '<span class="src-path">{path}</span>'
            '<span class="src-date">{when}</span>'
            '<span class="src-detail">{detail}</span>'
            '</li>'.format(
                chip=chip(SOURCE_CLASS.get(state, "dead"), SOURCE_LABEL.get(state, state)),
                label=esc(src.get("label") or key), path=esc(src.get("path") or ""),
                when=when or esc("inget datum"), detail=esc(src.get("detail") or EMPTY_DATA)))
    out.append('</ol></section>')
    return "".join(out)


def render_footer(m: Dict[str, Any]) -> str:
    s = _d(m.get("summary"))
    root = m.get("root") or ""
    return (
        '<footer>'
        '<p>Genererad {} av scripts/dashboard.py. Kör om: <code>scripts/dashboard.py</code>.</p>'
        '<p>Arkivkontroll: {} fel, {} varningar. Rot: <code>{}</code>.</p>'
        '</footer>'
    ).format(esc(stamp_text(m.get("generated"))), esc(sv_int(s.get("archive_errors"))),
             esc(sv_int(s.get("archive_warnings"))), esc(root))


def render(model: Dict[str, Any]) -> str:
    """The whole page as one string."""
    m = _d(model)
    parts = [
        HEAD,
        '<div class="wrap">',
        render_masthead(m),
        '<main>',
        render_lage(m),
        render_todo(m),
        render_pleo(m),
        render_amex(m),
        render_arkiv(m),
        render_underlag(m),
        '</main>',
        render_footer(m),
        '</div>',
    ]
    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------- page head and styles

CSS = """
  :root {
    --ground: #f6f7f4;
    --surface: #ffffff;
    --surface-2: #eef1ed;
    --ink: #1c2321;
    --ink-2: #3c4744;
    --muted: #5f6b66;
    --rule: #d8ded9;
    --rule-strong: #b9c3bd;
    --accent: #1e5c4b;
    --accent-ink: #174a3c;
    --accent-soft: #e3efea;
    --ok: #2f7a52;      --ok-soft: #e2f1e7;
    --warn: #a86a12;    --warn-soft: #f7ecd8;
    --crit: #b03a2e;    --crit-soft: #f7e3e0;
    --dead: #6b7570;    --dead-soft: #e8ebe9;
    --unk: #4f5f8a;     --unk-soft: #e4e8f3;
    --shadow: 0 1px 2px rgba(28, 35, 33, 0.06), 0 8px 24px rgba(28, 35, 33, 0.05);
    --display: "Bricolage Grotesque", "Instrument Sans", "Helvetica Neue", Arial, sans-serif;
    --body: "Instrument Sans", "Helvetica Neue", Arial, sans-serif;
    --mono: "JetBrains Mono", ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --ground: #111614;
      --surface: #181e1b;
      --surface-2: #1f2623;
      --ink: #e6ebe7;
      --ink-2: #c9d1cc;
      --muted: #98a39d;
      --rule: #2a332e;
      --rule-strong: #3b4741;
      --accent: #6fbf9e;
      --accent-ink: #8fd1b6;
      --accent-soft: #1c2d26;
      --ok: #7cc79a;    --ok-soft: #1b2f24;
      --warn: #e0a85a;  --warn-soft: #33271a;
      --crit: #e2847a;  --crit-soft: #36201d;
      --dead: #9aa59f;  --dead-soft: #242b28;
      --unk: #9fb0e0;   --unk-soft: #1f2533;
      --shadow: 0 1px 2px rgba(0,0,0,0.3), 0 8px 24px rgba(0,0,0,0.25);
    }
  }
  :root[data-theme="dark"] {
    --ground: #111614;
    --surface: #181e1b;
    --surface-2: #1f2623;
    --ink: #e6ebe7;
    --ink-2: #c9d1cc;
    --muted: #98a39d;
    --rule: #2a332e;
    --rule-strong: #3b4741;
    --accent: #6fbf9e;
    --accent-ink: #8fd1b6;
    --accent-soft: #1c2d26;
    --ok: #7cc79a;    --ok-soft: #1b2f24;
    --warn: #e0a85a;  --warn-soft: #33271a;
    --crit: #e2847a;  --crit-soft: #36201d;
    --dead: #9aa59f;  --dead-soft: #242b28;
    --unk: #9fb0e0;   --unk-soft: #1f2533;
    --shadow: 0 1px 2px rgba(0,0,0,0.3), 0 8px 24px rgba(0,0,0,0.25);
  }

  * { box-sizing: border-box; }
  html { -webkit-text-size-adjust: 100%; }
  body {
    margin: 0;
    background: var(--ground);
    color: var(--ink);
    font-family: var(--body);
    font-size: 15.5px;
    line-height: 1.5;
  }
  a { color: var(--accent-ink); text-decoration-thickness: 1px; text-underline-offset: 2px; }
  a:focus-visible, summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 2px; }
  code, .num, .id { font-family: var(--mono); font-size: 0.92em; }
  .num { font-variant-numeric: tabular-nums; white-space: nowrap; }
  .id { white-space: nowrap; color: var(--ink-2); }
  code { background: var(--surface-2); padding: 1px 5px; border-radius: 4px; }

  .wrap { max-width: 1100px; margin: 0 auto; padding: 36px 22px 80px; }
  @media (min-width: 900px) { .wrap { padding-top: 52px; } }

  header.masthead { display: grid; gap: 12px; padding-bottom: 24px; border-bottom: 1px solid var(--rule-strong); margin-bottom: 8px; }
  .eyebrow { font-family: var(--mono); font-size: 12px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted); display: flex; flex-wrap: wrap; gap: 6px 10px; }
  .eyebrow span + span::before { content: "\\00B7"; margin-right: 10px; }
  h1 { font-family: var(--display); font-weight: 600; font-size: clamp(38px, 6vw, 54px); line-height: 1.02; margin: 0; letter-spacing: -0.015em; }
  .lede { font-size: 19px; line-height: 1.45; color: var(--ink-2); max-width: 70ch; margin: 0; }

  h2 { font-family: var(--display); font-weight: 600; font-size: 26px; line-height: 1.15; margin: 48px 0 14px; letter-spacing: -0.01em; }
  h2 .k { font-family: var(--mono); font-weight: 500; font-size: 12px; letter-spacing: 0.08em; color: var(--muted); display: block; margin-bottom: 6px; text-transform: uppercase; }
  h3 { font-family: var(--display); font-weight: 600; font-size: 18px; margin: 26px 0 8px; letter-spacing: -0.005em; }
  h4.grp-h { font-family: var(--display); font-weight: 600; font-size: 15.5px; margin: 18px 0 8px; display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
  p { margin: 0 0 12px; }
  ul, ol { margin: 0 0 12px; padding-left: 1.25em; }
  .muted { color: var(--muted); }
  .small { font-size: 13.5px; }
  .groupnote { font-size: 14px; color: var(--muted); margin: -4px 0 12px; }
  .empty { font-size: 14.5px; color: var(--muted); padding: 12px 16px; border: 1px dashed var(--rule-strong); border-radius: 8px; background: var(--surface-2); margin: 0 0 12px; }

  .chip { display: inline-flex; align-items: center; gap: 6px; padding: 3px 9px 3px 7px; border-radius: 999px; font-family: var(--mono); font-size: 11.5px; letter-spacing: 0.02em; font-weight: 500; white-space: nowrap; border: 1px solid transparent; vertical-align: middle; }
  .chip::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: currentColor; flex: none; }
  .chip.ok   { color: var(--ok);   background: var(--ok-soft); }
  .chip.warn { color: var(--warn); background: var(--warn-soft); }
  .chip.crit { color: var(--crit); background: var(--crit-soft); }
  .chip.dead { color: var(--dead); background: var(--dead-soft); }
  .chip.unk  { color: var(--unk);  background: var(--unk-soft); }
  .chip.plain { color: var(--accent-ink); background: var(--accent-soft); padding-left: 9px; }
  .chip.plain::before { display: none; }
  .chip .num { font-size: 1em; }
  .legend { display: flex; flex-wrap: wrap; gap: 8px 16px; font-size: 13px; color: var(--muted); margin: 0 0 10px; }
  .legend > span { display: inline-flex; align-items: center; gap: 6px; }
  .sw { display: inline-block; width: 12px; height: 12px; border-radius: 3px; }

  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 12px; margin: 0 0 8px; }
  .tile { position: relative; background: var(--surface); border: 1px solid var(--rule); border-radius: 8px; padding: 18px 16px 14px; box-shadow: var(--shadow); display: grid; gap: 8px; align-content: start; overflow: hidden; }
  .tile::before { content: ""; position: absolute; left: 0; top: 0; right: 0; height: 4px; background: var(--dead); }
  .tile.ok::before { background: var(--ok); }
  .tile.warn::before { background: var(--warn); }
  .tile.crit::before { background: var(--crit); }
  .tile-head { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 6px 8px; }
  .tile-k { font-family: var(--mono); font-size: 11.5px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted); }
  .tile-v { font-family: var(--display); font-size: 34px; font-weight: 600; line-height: 1.05; letter-spacing: -0.02em; }
  .tile-v .num { font-family: var(--display); font-size: 1em; }
  .tile-v small { display: block; font-family: var(--body); font-size: 13.5px; font-weight: 400; color: var(--muted); letter-spacing: 0; margin-top: 4px; }
  .tile-s { font-size: 13.5px; color: var(--ink-2); }
  .meter { height: 6px; border-radius: 3px; background: var(--accent-soft); overflow: hidden; }
  .meter > span { display: block; height: 100%; background: var(--accent); border-radius: 3px; }

  ol.todo { list-style: none; padding: 0; margin: 0 0 8px; display: grid; gap: 10px; }
  .todo-row { display: grid; grid-template-columns: 1fr auto; gap: 4px 18px; padding: 14px 16px; background: var(--surface); border: 1px solid var(--rule); border-left: 4px solid var(--dead); border-radius: 8px; box-shadow: var(--shadow); }
  .todo-row.crit { border-left-color: var(--crit); }
  .todo-row.warn { border-left-color: var(--warn); }
  .todo-row.unk { border-left-color: var(--unk); }
  .todo-chips { grid-column: 1 / -1; display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 4px; }
  .todo-title { grid-column: 1; margin: 0; font-family: var(--display); font-size: 18px; font-weight: 600; line-height: 1.2; }
  .todo-title a { color: var(--ink); text-decoration: none; }
  .todo-title a:hover { text-decoration: underline; }
  .todo-detail { grid-column: 1; margin: 0; color: var(--ink-2); font-size: 14.5px; }
  .todo-nums { grid-column: 2; grid-row: 2 / span 2; text-align: right; display: grid; gap: 2px; align-content: start; }
  .todo-nums .big { font-family: var(--display); font-size: 26px; font-weight: 600; line-height: 1; }
  .todo-nums .amt { font-size: 13px; color: var(--muted); }
  pre.cmd { grid-column: 1 / -1; margin: 8px 0 0; padding: 8px 12px; background: var(--surface-2); border: 1px solid var(--rule); border-radius: 6px; font-size: 12.5px; min-width: 0; overflow-x: auto; white-space: pre-wrap; overflow-wrap: anywhere; }
  pre.cmd code { background: none; padding: 0; font-size: 1em; }

  ol.mrows { list-style: none; padding: 0; margin: 0 0 12px; border: 1px solid var(--rule); border-radius: 8px; background: var(--surface); box-shadow: var(--shadow); overflow: hidden; }
  .mrow { display: grid; grid-template-columns: 150px 1fr 220px 250px; gap: 6px 18px; align-items: center; padding: 11px 16px; border-bottom: 1px solid var(--rule); }
  .mrow:last-child { border-bottom: 0; }
  .mrow.total { background: var(--surface-2); }
  .m-name { font-family: var(--display); font-weight: 600; font-size: 16px; }
  .m-counts { font-size: 14px; color: var(--ink-2); }
  .kv { font-family: var(--mono); font-size: 12px; color: var(--muted); display: flex; flex-wrap: wrap; gap: 4px 14px; white-space: nowrap; }
  .segbar { display: flex; gap: 2px; height: 10px; border-radius: 5px; overflow: hidden; background: var(--surface); }
  .seg { display: block; height: 100%; min-width: 3px; flex-basis: 0; }
  .seg.have, .sw.have { background: var(--accent); }
  .seg.miss, .sw.miss { background: var(--crit-soft); box-shadow: inset 0 0 0 1px var(--crit); }
  .seg.att, .sw.att { background: var(--ok); }
  .seg.inp, .sw.inp { background: var(--unk); }
  .seg.amx, .sw.amx { background: repeating-linear-gradient(135deg, var(--accent) 0 3px, var(--accent-soft) 3px 6px); }
  .seg.pay, .sw.pay { background: var(--dead-soft); box-shadow: inset 0 0 0 1px var(--dead); }
  .seg.none, .sw.none { background: var(--dead-soft); box-shadow: inset 0 0 0 1px var(--rule-strong); }

  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 12px; margin: 0 0 12px; }
  .card { background: var(--surface); border: 1px solid var(--rule); border-radius: 8px; padding: 14px 16px; display: grid; gap: 12px; align-content: start; box-shadow: var(--shadow); }
  .card-head { display: flex; justify-content: space-between; align-items: center; gap: 8px; flex-wrap: wrap; }
  .card h4 { margin: 0; font-family: var(--display); font-size: 17px; font-weight: 600; }
  ol.stepper { list-style: none; margin: 4px 0 0; padding: 0; display: grid; grid-template-columns: repeat(5, 1fr); }
  ol.stepper li { position: relative; text-align: center; font-family: var(--mono); font-size: 10px; letter-spacing: 0.02em; text-transform: uppercase; color: var(--muted); padding-top: 22px; padding-left: 2px; padding-right: 2px; }
  ol.stepper li::before { content: ""; position: absolute; left: 50%; top: 0; width: 16px; height: 16px; margin-left: -8px; border-radius: 50%; border: 2px solid var(--rule-strong); background: var(--surface); z-index: 1; }
  ol.stepper li + li::after { content: ""; position: absolute; top: 7px; left: -50%; right: 50%; height: 2px; background: var(--rule); }
  ol.stepper li.done { color: var(--ink-2); }
  ol.stepper li.done::before { background: var(--accent); border-color: var(--accent); }
  ol.stepper li.done + li.done::after { background: var(--accent); }
  ol.stepper li.partial { color: var(--ink-2); }
  ol.stepper li.partial::before { border-color: var(--warn); background: linear-gradient(90deg, var(--warn) 50%, var(--warn-soft) 50%); }
  dl.lines { margin: 0; display: grid; gap: 4px; font-size: 13.5px; }
  dl.lines div { display: flex; justify-content: space-between; gap: 12px; }
  dl.lines dt { color: var(--muted); }
  dl.lines dd { margin: 0; color: var(--ink); text-align: right; white-space: normal; }
  .plan-note { margin: -4px 0 0; font-family: var(--mono); font-size: 12px; color: var(--muted); }

  .tablewrap { overflow-x: auto; border: 1px solid var(--rule); border-radius: 8px; background: var(--surface); box-shadow: var(--shadow); margin: 0 0 12px; }
  table.ledger { width: 100%; border-collapse: collapse; font-size: 14px; min-width: 720px; }
  table.ledger th { text-align: left; font-family: var(--mono); font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted); font-weight: 500; padding: 11px 14px; border-bottom: 1px solid var(--rule-strong); background: var(--surface-2); }
  table.ledger td { padding: 10px 14px; border-bottom: 1px solid var(--rule); vertical-align: top; }
  table.ledger tr:last-child td { border-bottom: 0; }
  table.ledger td.c { white-space: nowrap; }
  table.ledger td.name { font-weight: 600; color: var(--ink); }
  table.ledger td small, table.ledger td .muted.small { display: block; font-weight: 400; color: var(--muted); font-size: 12.5px; margin-top: 2px; }
  table.ledger tr.grp td { background: var(--surface-2); color: var(--ink-2); font-size: 13.5px; padding: 8px 14px; }
  table.ledger tr.grp td b { color: var(--ink); font-family: var(--display); font-size: 14.5px; margin-right: 6px; }

  .kinds { display: flex; flex-wrap: wrap; gap: 8px; margin: 0 0 12px; }
  details { border: 1px solid var(--rule); border-radius: 8px; background: var(--surface); margin: 0 0 12px; }
  summary { cursor: pointer; padding: 10px 14px; font-weight: 600; }
  ul.warns { list-style: none; margin: 0; padding: 0 14px 12px; display: grid; gap: 6px; font-size: 13.5px; }
  ul.warns li { display: grid; grid-template-columns: minmax(200px, 260px) 1fr; gap: 12px; color: var(--ink-2); }

  ol.sources { list-style: none; padding: 0; margin: 0 0 12px; border: 1px solid var(--rule); border-radius: 8px; background: var(--surface); box-shadow: var(--shadow); overflow: hidden; }
  .src-row { display: grid; grid-template-columns: 88px 210px 1fr auto; gap: 4px 16px; padding: 11px 16px; border-bottom: 1px solid var(--rule); align-items: baseline; }
  .src-row:last-child { border-bottom: 0; }
  .src-lbl { font-weight: 600; }
  .src-path { font-family: var(--mono); font-size: 12.5px; color: var(--ink-2); word-break: break-all; }
  .src-date { font-family: var(--mono); font-size: 12.5px; color: var(--muted); white-space: nowrap; font-variant-numeric: tabular-nums; }
  .src-detail { grid-column: 3 / -1; font-size: 13.5px; color: var(--ink-2); }

  .callout { border-left: 3px solid var(--accent); background: var(--surface); padding: 12px 16px; border-radius: 0 6px 6px 0; margin: 16px 0; font-size: 15px; }
  .callout.crit { border-left-color: var(--crit); }
  .callout ul:last-child, .callout p:last-child { margin-bottom: 0; }

  footer { margin-top: 56px; padding-top: 18px; border-top: 1px solid var(--rule); font-size: 13px; color: var(--muted); }
  footer p { margin-bottom: 8px; }

  @media (max-width: 760px) {
    .mrow, .src-row { grid-template-columns: 1fr; gap: 6px; }
    .src-detail { grid-column: 1; }
    .todo-row { grid-template-columns: 1fr; }
    .todo-nums { grid-column: 1; grid-row: auto; text-align: left; grid-auto-flow: column; justify-content: start; gap: 12px; align-items: baseline; }
    ul.warns li { grid-template-columns: 1fr; gap: 2px; }
  }
  @media (prefers-reduced-motion: reduce) { * { scroll-behavior: auto !important; } }
"""

HEAD = (
    '<meta charset="utf-8">\n'
    '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
    '<title>Kvittotavlan</title>\n'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500;12..96,600'
    '&family=Instrument+Sans:ital,wght@0,400;0,500;0,600;1,400&family=JetBrains+Mono:wght@400;500&display=swap">\n'
    '<style>' + CSS + '</style>'
)

