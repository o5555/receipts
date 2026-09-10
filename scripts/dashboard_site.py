#!/usr/bin/env python3
"""Kvittotavlan on the web: build the hosted status page from dashboard.py's model and deploy it.

The page is one static index.html for Vercel. The model is embedded encrypted
(AES-256-CBC through the openssl CLI, PBKDF2-SHA256, 200 000 iterations), the browser
decrypts it with WebCrypto from the key in the URL fragment (#<key>) or a typed key, so
the public Vercel URL exposes no figures, merchants or receipt numbers. The key lives in
<root>/data/dashboard.key (data/ is gitignored) and is created on the first build.

The same key opens /plan, Kvittoplanen: docs/kvittoplanen.md rendered from its markdown
subset (headings, paragraphs, - and 1. lists, > callouts, **bold**, `code`, links) behind a
status strip computed from the model, so the plan page never types a number by hand.

Usage:
  scripts/dashboard_site.py                 # writes out/site/index.html, plan.html and vercel.json
  scripts/dashboard_site.py --deploy        # then `vercel deploy --prod` from out/site
  scripts/dashboard_site.py --print-url     # prints the last deployment URL with the key fragment

Reads only through dashboard.build_model and docs/kvittoplanen.md; writes out/site/ and data/dashboard.key.
"""
from __future__ import annotations

import argparse
import base64
import html as html_mod
import json
import os
import re
import secrets
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from dashboard import AMEX_STATE_LABELS, PLEO_STATE_LABELS, STAGE_LABELS, build_model, to_date  # noqa: E402
from dashboard_html import month_name, plural, sv_amount, sv_int  # noqa: E402

PBKDF2_ITER = 200000
PROJECT = "kvittotavlan"
PROD_URL = f"https://{PROJECT}.vercel.app"
PLAN_SOURCE_REL = Path("docs") / "kvittoplanen.md"

# state -> tone (status palette: good, warn, bad, info, muted); every badge also carries its label
AMEX_TONES = {"booked": "good", "reimbursed": "info", "in-pleo": "muted", "planned": "warn", "held": "warn",
              "ready": "warn", "no-receipt": "bad", "other-entity": "muted", "needs-tag": "warn",
              "personal": "muted", "skip": "muted"}
PLEO_TONES = {"exported": "good", "ok": "good", "corrected": "good", "attached": "good", "flagged": "bad",
              "missing": "bad", "payout": "muted"}
STAGE_TONES = {"no-csv": "muted", "untagged": "warn", "no-plan": "warn", "planned": "warn", "held": "warn",
               "partial": "info", "booked": "good", "nothing-to-book": "muted"}


# ----------------------------------------------------------------------------- key and encryption

def load_key(root, create=True):
    path = Path(root) / "data" / "dashboard.key"
    if path.is_file():
        key = path.read_text(encoding="utf-8").strip()
        if key:
            return key
    if not create:
        return None
    key = secrets.token_urlsafe(24)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return key


def encrypt(plaintext: bytes, key: str) -> str:
    """openssl enc output (Salted__ + 8-byte salt + AES-256-CBC ciphertext) as base64."""
    r, w = os.pipe()
    os.write(w, (key + "\n").encode("utf-8"))
    os.close(w)
    try:
        proc = subprocess.run(
            ["openssl", "enc", "-aes-256-cbc", "-salt", "-pbkdf2", "-iter", str(PBKDF2_ITER), "-md", "sha256",
             "-pass", f"fd:{r}"],
            input=plaintext, capture_output=True, check=True, pass_fds=(r,))
    finally:
        os.close(r)
    return base64.b64encode(proc.stdout).decode("ascii")


# ----------------------------------------------------------------------------- page

def page_payload(model):
    """The subset of the model the page needs, plus label tables so the JS stays dumb."""
    return {
        "generated": model["generated"], "today": model["today"], "entity_name": model["entity_name"],
        "sources": model["sources"], "summary": model["summary"], "todo": model["todo"],
        # private purchases stay out of the page: Oscar does not want them listed
        "amex": {"rows": [r for r in model["amex"]["rows"] if r["state"] != "personal"], "months": model["amex"]["months"],
                 "coverage_end": model["amex"]["coverage_end"], "next_month": model["amex"]["next_month"],
                 "next_month_csv_present": model["amex"]["next_month_csv_present"]},
        "pleo": {"rows": model["pleo"]["rows"], "months": model["pleo"]["months"], "totals": model["pleo"]["totals"],
                 "export_date": model["pleo"]["export_date"], "missing_live_date": model["pleo"]["missing_live"]["date"],
                 "by_route": model["pleo"]["missing_live"]["by_route"]},
        "archive": {"pages": model["archive"]["pages"], "total_sek": model["archive"]["total_sek"],
                    "warnings_total": model["archive"]["warnings_total"], "errors": len(model["archive"]["errors"])},
        "labels": {"amex": dict(AMEX_STATE_LABELS), "pleo": dict(PLEO_STATE_LABELS), "stage": dict(STAGE_LABELS)},
        "tones": {"amex": AMEX_TONES, "pleo": PLEO_TONES, "stage": STAGE_TONES},
    }


CSS = r"""
:root{--bg:#f6f5f1;--card:#fff;--ink:#1b1b1a;--ink2:#5a5955;--muted:#8a8984;--line:#e3e1da;--accent:#2b5fb3;
--good:#1f7a4d;--good-bg:#e3f3ea;--warn:#9a6300;--warn-bg:#fbf1d8;--bad:#b3261e;--bad-bg:#fbe5e3;--info:#2b5fb3;--info-bg:#e4ecfa;
--mutedbg:#ecebe6;--shadow:0 1px 2px rgba(0,0,0,.05)}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#15161a;--card:#1f2127;--ink:#ecebe6;--ink2:#b5b3ab;--muted:#7f7e78;--line:#33353d;--accent:#8db0f2;
--good:#7fd1a2;--good-bg:#1b3327;--warn:#f0c15a;--warn-bg:#3a2f12;--bad:#ff8a80;--bad-bg:#3d1f1d;--info:#8db0f2;--info-bg:#1d2a44;--mutedbg:#2a2c33}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
a{color:var(--accent)}.wrap{max-width:1180px;margin:0 auto;padding:20px 18px 60px}
header{display:flex;flex-wrap:wrap;align-items:baseline;gap:8px 18px;margin-bottom:14px}header h1{font-size:24px;margin:0}header .sub{color:var(--ink2)}
nav{display:flex;gap:6px;margin:12px 0 18px;border-bottom:1px solid var(--line)}nav button{background:none;border:0;border-bottom:3px solid transparent;padding:10px 14px;font:inherit;font-weight:600;color:var(--ink2);cursor:pointer}
nav button.on{color:var(--ink);border-bottom-color:var(--accent)}
nav a.navlink{display:inline-block;padding:10px 14px;font-weight:600;color:var(--ink2);text-decoration:none;border-bottom:3px solid transparent}nav a.navlink.on{color:var(--ink);border-bottom-color:var(--accent)}nav a.navlink:hover{color:var(--ink)}nav a.navlink.right{margin-left:auto}
.plan{max-width:720px}.plan h2{font-size:18px;margin:26px 0 8px}.plan p,.plan li{line-height:1.55}.plan ul,.plan ol{padding-left:22px;margin:8px 0}.plan li{margin:7px 0}.plan li strong{color:var(--ink)}
.plan blockquote{margin:16px 0;padding:12px 16px;border-left:4px solid var(--bad);background:var(--bad-bg);border-radius:0 8px 8px 0}.plan blockquote p{margin:0}
.plan code{background:var(--mutedbg);padding:1px 5px;border-radius:4px;font-size:.9em}
.status{margin:4px 0 14px}.status span.ok{background:var(--good-bg);color:var(--good)}.status span.warn,.status span.stale{background:var(--warn-bg);color:var(--warn)}.status span.missing{background:var(--bad-bg);color:var(--bad)}
.foot{color:var(--muted);font-size:12px;margin-top:30px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:18px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;box-shadow:var(--shadow)}
.tile .n{font-size:26px;font-weight:700;line-height:1.1}.tile .l{color:var(--ink2);font-size:13px;margin-top:2px}.tile .s{color:var(--muted);font-size:12px;margin-top:4px}
.tile.bad .n{color:var(--bad)}.tile.warn .n{color:var(--warn)}.tile.good .n{color:var(--good)}.tile.info .n{color:var(--info)}
h2{font-size:17px;margin:22px 0 10px}h3{font-size:14px;margin:16px 0 8px;color:var(--ink2);text-transform:uppercase;letter-spacing:.04em}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;box-shadow:var(--shadow);overflow:hidden}
.todo{list-style:none;margin:0;padding:0}.todo li{display:grid;grid-template-columns:28px 1fr auto;gap:10px;padding:12px 14px;border-top:1px solid var(--line);align-items:start}.todo li:first-child{border-top:0}
.todo .t{font-weight:600}.todo .d{color:var(--ink2);font-size:14px;margin-top:2px}.todo .who{font-size:12px;color:var(--muted);margin-top:4px}.todo .amt{white-space:nowrap;color:var(--ink2);font-variant-numeric:tabular-nums}
.sev{width:22px;height:22px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;font-size:12px;font-weight:700}
.sev.crit{background:var(--bad-bg);color:var(--bad)}.sev.warn{background:var(--warn-bg);color:var(--warn)}.sev.info{background:var(--info-bg);color:var(--info)}
.badge{display:inline-flex;align-items:center;gap:5px;padding:2px 8px;border-radius:999px;font-size:12px;font-weight:600;white-space:nowrap}
.badge.good{background:var(--good-bg);color:var(--good)}.badge.warn{background:var(--warn-bg);color:var(--warn)}.badge.bad{background:var(--bad-bg);color:var(--bad)}.badge.info{background:var(--info-bg);color:var(--info)}.badge.muted{background:var(--mutedbg);color:var(--ink2)}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0}.chips button{font:inherit;font-size:13px;padding:5px 10px;border-radius:999px;border:1px solid var(--line);background:var(--card);color:var(--ink2);cursor:pointer}
.chips button.on{background:var(--ink);color:var(--bg);border-color:var(--ink)}.chips button b{font-weight:700}
.tools{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:8px 0}.tools input,.tools select{font:inherit;padding:6px 9px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink)}
.tools .cnt{color:var(--muted);font-size:13px;margin-left:auto}
.tbl{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:14px}th{text-align:left;font-size:12px;color:var(--muted);font-weight:600;padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap;position:sticky;top:0;background:var(--card)}
td{padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}td:first-child{white-space:nowrap}td.num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}td.c{text-align:center}
td .m{display:block;color:var(--muted);font-size:12px}td .step{display:block;color:var(--ink2);font-size:13px;margin-top:3px;max-width:420px}
.yes{color:var(--good);font-weight:700}.no{color:var(--bad);font-weight:700}.na{color:var(--muted)}
.months{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px;margin-bottom:6px}
.month{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}.month .h{display:flex;justify-content:space-between;align-items:center;gap:6px}.month .h b{font-size:15px}
.month .r{font-size:13px;color:var(--ink2);margin-top:6px}.bar{display:flex;height:8px;border-radius:4px;overflow:hidden;background:var(--mutedbg);margin-top:8px;gap:2px}.bar i{display:block;height:100%}
.bar .g{background:var(--good)}.bar .w{background:var(--warn)}.bar .b{background:var(--bad)}.bar .i{background:var(--info)}
.src{display:flex;flex-wrap:wrap;gap:6px}.src span{font-size:12px;padding:3px 9px;border-radius:999px;background:var(--mutedbg);color:var(--ink2)}.src span.stale{background:var(--warn-bg);color:var(--warn)}.src span.missing{background:var(--bad-bg);color:var(--bad)}
.legend{font-size:13px;color:var(--ink2)}.legend dt{font-weight:600;color:var(--ink);margin-top:8px}.legend dd{margin:0}
#lock{max-width:420px;margin:80px auto;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:24px}#lock input{width:100%;font:inherit;padding:9px;border:1px solid var(--line);border-radius:8px;margin:10px 0;background:var(--bg);color:var(--ink)}#lock button{font:inherit;padding:8px 14px;border-radius:8px;border:0;background:var(--accent);color:#fff;cursor:pointer}
#lock .err{color:var(--bad);font-size:13px;min-height:18px}
.empty{padding:22px;color:var(--muted);text-align:center}
@media (max-width:640px){.wrap{padding:12px 10px 40px}td .step{max-width:none}header h1{font-size:20px}}
"""

JS = r"""
const $=(s,r=document)=>r.querySelector(s);const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const sek=v=>{if(v==null)return '';const n=Math.round(Math.abs(v)*100)/100;let [i,d]=n.toFixed(2).split('.');i=i.replace(/\B(?=(\d{3})+(?!\d))/g,' ');return (v<0?'-':'')+i+','+d+' SEK'};
const MON=['januari','februari','mars','april','maj','juni','juli','augusti','september','oktober','november','december'];
const svMonth=m=>{if(!m)return '';const [y,mm]=m.split('-');return MON[+mm-1]+' '+y};
const plural=(n,s,p)=>n===1?s:p;
let D=null,tab=new URLSearchParams(location.search).get('tab')||'oversikt',af={state:'',month:'',q:''},pf={state:'',month:'',q:''};

async function decrypt(b64,key){const raw=Uint8Array.from(atob(b64),c=>c.charCodeAt(0));const salt=raw.slice(8,16),ct=raw.slice(16);
const km=await crypto.subtle.importKey('raw',new TextEncoder().encode(key),'PBKDF2',false,['deriveBits']);
const bits=new Uint8Array(await crypto.subtle.deriveBits({name:'PBKDF2',salt,iterations:ITER,hash:'SHA-256'},km,384));
const k=await crypto.subtle.importKey('raw',bits.slice(0,32),'AES-CBC',false,['decrypt']);
const pt=await crypto.subtle.decrypt({name:'AES-CBC',iv:bits.slice(32,48)},k,ct);return JSON.parse(new TextDecoder().decode(pt))}

async function unlock(key,remember){try{D=await decrypt(BLOB,key.trim());if(remember){try{localStorage.setItem('kt-key',key.trim())}catch(e){}}$('#lock').hidden=true;$('#app').hidden=false;render();return true}catch(e){return false}}
async function boot(){let key=(location.hash||'').slice(1);if(!key){try{key=localStorage.getItem('kt-key')||''}catch(e){}}
if(key&&await unlock(key,true)){if(location.hash)history.replaceState(null,'',location.pathname);return}
$('#lock').hidden=false;$('#lockbtn').onclick=async()=>{if(!await unlock($('#lockkey').value,true))$('#lockerr').textContent='Fel nyckel.'};
$('#lockkey').onkeydown=e=>{if(e.key==='Enter')$('#lockbtn').click()}}

function badge(kind,state){const l=D.labels[kind][state]||state,t=D.tones[kind][state]||'muted';return `<span class="badge ${t}">${esc(l)}</span>`}
function yn(v,na){if(na)return '<span class="na">&ndash;</span>';return v?'<span class="yes">&#10003;</span>':'<span class="no">&#10007;</span>'}
function tile(n,l,s,tone){return `<div class="tile ${tone||''}"><div class="n">${esc(n)}</div><div class="l">${esc(l)}</div>${s?`<div class="s">${esc(s)}</div>`:''}</div>`}

function render(){$('#gen').textContent='Uppdaterad '+D.generated.replace('T',' ').replace('Z',' UTC');
document.querySelectorAll('nav button').forEach(b=>{b.classList.toggle('on',b.dataset.tab===tab);b.onclick=()=>{tab=b.dataset.tab;render()}});
const v=$('#view');if(tab==='amex')v.innerHTML=amexView();else if(tab==='pleo')v.innerHTML=pleoView();else v.innerHTML=overview();wire()}

function overview(){const s=D.summary,A=D.amex.rows,P=D.pleo.rows;const cnt=(rows,f)=>rows.filter(f).length;
const aBiz=A.filter(r=>!['personal','skip','needs-tag'].includes(r.state));
const t=[tile(D.todo.length,'punkter att göra',null,D.todo.some(x=>x.severity==='crit')?'bad':'warn'),
tile(aBiz.filter(r=>!r.receipt).length,'Amex-köp utan kvitto','av '+aBiz.length+' företagsköp','bad'),
tile(cnt(A,r=>r.state==='needs-tag'),'Amex-rader att tagga','företag eller privat','warn'),
tile(cnt(A,r=>['held','planned','ready'].includes(r.state)),'Amex-köp att bokföra','kvitto finns, väntar på Fortnox','warn'),
tile(cnt(P,r=>r.state==='missing'),'Pleo-utgifter utan kvitto',sek(P.filter(r=>r.state==='missing').reduce((a,r)=>a+r.amount_sek,0)),'bad'),
tile(cnt(P,r=>r.state==='flagged'),'Pleo-utgifter med fel kvitto',cnt(P,r=>r.state==='corrected')+' rättade sedan listan','bad')].join('');
const todo=D.todo.length?D.todo.map(x=>`<li><span class="sev ${x.severity}">${x.severity==='crit'?'!':x.severity==='warn'?'!':'i'}</span><div><div class="t">${esc(x.title)}</div><div class="d">${esc(x.detail)}</div><div class="who">${esc(x.owner)}${x.link?' · '+esc(x.link):''}</div></div><div class="amt">${x.amount_sek!=null?sek(x.amount_sek):''}</div></li>`).join(''):'<li class="empty">Inget att göra just nu.</li>';
const src=Object.values(D.sources).map(x=>`<span class="${x.state}">${esc(x.label)}: ${esc(x.date||'saknas')}${x.age_days!=null?' ('+x.age_days+' d)':''}</span>`).join('');
return `<div class="tiles">${t}</div><h2>Att göra, viktigast först</h2><div class="card"><ul class="todo">${todo}</ul></div>
<h2>Underlagens ålder</h2><div class="src">${src}</div>
<h2>Vad betyder statusarna</h2><div class="card" style="padding:12px 16px"><dl class="legend">
<dt>Kvitto finns</dt><dd>Underlaget är hämtat (fil i arkivet eller bifogat i Pleo). Säger inget om pengar eller bokföring.</dd>
<dt>Ersatt</dt><dd>Pengarna är utbetalda till Oscar, via en Pleo-utbetalning eller ett Fortnox-verifikat. Amex-köp som redan ersatts via Pleo får inte ersättas igen.</dd>
<dt>Bokförd</dt><dd>Ett verifikat finns i Fortnox. Först då är raden helt klar.</dd>
<dt>Plan på hold</dt><dd>Fortnox-planen för månaden är stoppad tills raderna som redan ersatts via Pleo är avstämda.</dd>
<dt>Fel kvitto bifogat</dt><dd>Pleo-utgiften har ett kvitto, men fel: samma fil som ett annat köp, föregående period, annat bolag eller fel leverantör.</dd></dl></div>`}

function months(list,kind){return `<div class="months">${list.map(m=>{if(kind==='amex'){const tot=m.business||0,f=m.receipts_found||0;
return `<div class="month"><div class="h"><b>${esc(svMonth(m.month))}</b>${badge('stage',m.stage)}</div><div class="r">${m.rows} köp · ${m.business} företag (${sek(m.business_sek)}) · ${m.personal} privat${m.needs_tag_open?` · <b>${m.needs_tag_open} otaggade</b>`:''}</div><div class="r">Kvitto: ${f} av ${tot}</div><div class="bar"><i class="g" style="flex:${f}"></i><i class="b" style="flex:${tot-f}"></i></div></div>`}
const ok=m.with_receipt,miss=m.missing;return `<div class="month"><div class="h"><b>${esc(svMonth(m.month))}</b><span class="badge ${miss?'bad':'good'}">${miss?miss+' utan kvitto':'kvitton klara'}</span></div><div class="r">${m.rows} rader · ${sek(m.amount_sek)} · ${m.exported} exporterade${m.payouts?` · ${m.payouts} utbetalningar`:''}</div><div class="r">Kvitto: ${ok} av ${ok+miss}</div><div class="bar"><i class="g" style="flex:${ok}"></i><i class="b" style="flex:${miss}"></i></div></div>`}).join('')}</div>`}

function chips(rows,kind,f,order){const c={};rows.forEach(r=>c[r.state]=(c[r.state]||0)+1);
return `<div class="chips"><button data-f="state" data-v="" class="${f.state?'':'on'}">Alla <b>${rows.length}</b></button>${order.filter(s=>c[s]).map(s=>`<button data-f="state" data-v="${s}" class="${f.state===s?'on':''}">${esc(D.labels[kind][s])} <b>${c[s]}</b></button>`).join('')}</div>`}
function tools(rows,f,shown){const ms=[...new Set(rows.map(r=>(r.date||'').slice(0,7)).filter(Boolean))].sort().reverse();
return `<div class="tools"><input data-f="q" placeholder="Sök handlare eller belopp" value="${esc(f.q)}"><select data-f="month"><option value="">Alla månader</option>${ms.map(m=>`<option value="${m}" ${f.month===m?'selected':''}>${esc(svMonth(m))}</option>`).join('')}</select><span class="cnt">${shown} rader</span></div>`}
function filt(rows,f){const q=f.q.toLowerCase();return rows.filter(r=>(!f.state||r.state===f.state)&&(!f.month||(r.date||'').startsWith(f.month))&&(!q||(r.vendor+' '+r.merchant+' '+r.amount_sek+' '+(r.receipt_no||'')).toLowerCase().includes(q)))}

function amexView(){const A=D.amex.rows,biz=A.filter(r=>!['personal','skip','needs-tag'].includes(r.state));
const t=[tile(biz.length,'företagsköp',sek(biz.reduce((a,r)=>a+r.amount_sek,0))),tile(biz.filter(r=>r.receipt).length+' av '+biz.length,'har kvitto',null,'good'),
tile(A.filter(r=>r.state==='reimbursed').length,'ersatta via Pleo','får inte ersättas igen','info'),tile(A.filter(r=>r.booked).length,'bokförda i Fortnox',null,'good'),
tile(biz.filter(r=>!r.receipt).length,'saknar kvitto',null,'bad'),tile(A.filter(r=>r.state==='needs-tag').length,'väntar på tagg',null,'warn')].join('');
const rows=filt(A,af);const order=Object.keys(D.labels.amex);
const cov=`Amex-exporterna täcker till och med ${D.amex.coverage_end||'okänt datum'}.${D.amex.next_month&&!D.amex.next_month_csv_present?' CSV för '+svMonth(D.amex.next_month)+' saknas.':''}`;
return `<div class="tiles">${t}</div><p class="sub" style="color:var(--ink2);margin:0 0 8px">${esc(cov)}</p>${months(D.amex.months,'amex')}
<h2>Amex-köp (privata köp visas inte)</h2>${chips(A,'amex',af,order)}${tools(A,af,rows.length)}<div class="card tbl"><table><thead><tr><th>Datum</th><th>Handlare</th><th style="text-align:right">Belopp</th><th>Kort</th><th>Tagg</th><th>Kvitto</th><th>Ersatt</th><th>Bokförd</th><th>Status och nästa steg</th></tr></thead><tbody>
${rows.length?rows.map(r=>{const na=['personal','skip','needs-tag'].includes(r.state);return `<tr><td>${esc(r.date)}</td><td>${esc(r.vendor)}<span class="m">${esc(r.merchant)}</span></td><td class="num">${sek(r.amount_sek)}</td><td>${esc(r.card)}</td><td>${esc({business:'företag',personal:'privat',skip:'ingen','needs-tag':'?'}[r.tag]||r.tag)}${r.entity!=='viseo'?'<span class="m">'+esc(r.entity)+'</span>':''}</td><td class="c">${yn(r.receipt,na)}</td><td class="c">${yn(r.reimbursed,na)}${r.reimbursed_via==='pleo'?'<span class="m">Pleo '+esc(r.reimbursed_date||'')+'</span>':''}</td><td class="c">${yn(r.booked,na)}${r.voucher?'<span class="m">'+esc(r.voucher)+'</span>':''}</td><td>${badge('amex',r.state)}<span class="step">${esc(r.step)}</span></td></tr>`}).join(''):'<tr><td colspan="9" class="empty">Inga rader matchar.</td></tr>'}</tbody></table></div>`}

function pleoView(){const P=D.pleo.rows,card=P.filter(r=>!r.payout);
const t=[tile(card.length,'kortköp i exporten',sek(card.reduce((a,r)=>a+r.amount_sek,0))),tile(card.filter(r=>r.has_receipt||r.state==='attached').length+' av '+card.length,'har kvitto',null,'good'),
tile(P.filter(r=>r.state==='missing').length,'saknar kvitto',sek(P.filter(r=>r.state==='missing').reduce((a,r)=>a+r.amount_sek,0)),'bad'),
tile(P.filter(r=>r.state==='flagged').length,'fel kvitto bifogat','byt ut i Pleo','bad'),tile(P.filter(r=>r.state==='corrected').length,'rättade','gammal fil ligger kvar','good'),
tile(P.filter(r=>r.state==='exported').length,'exporterade till Fortnox',null,'good')].join('');
const rows=filt(P,pf);const order=Object.keys(D.labels.pleo);
const routes=D.pleo.by_route.length?`<h3>Var kvittona finns</h3><div class="chips">${D.pleo.by_route.map(g=>`<button data-f="route" data-v="${esc(g.route)}">${esc(g.route_label)} <b>${g.count}</b> · ${sek(g.amount_sek)} · ${esc(g.owner)}</button>`).join('')}</div>`:'';
const cov=`Pleo-export från ${D.pleo.export_date||'okänt datum'}${D.pleo.missing_live_date?', live-lista utan kvitto från '+D.pleo.missing_live_date:''}.`;
return `<div class="tiles">${t}</div><p style="color:var(--ink2);margin:0 0 8px">${esc(cov)}</p>${months(D.pleo.months,'pleo')}
<h2>Alla Pleo-utgifter</h2>${chips(P,'pleo',pf,order)}${routes}${tools(P,pf,rows.length)}<div class="card tbl"><table><thead><tr><th>Datum</th><th>Kvitto nr</th><th>Handlare</th><th style="text-align:right">Belopp</th><th>Kvitto</th><th>Export</th><th>Status och nästa steg</th></tr></thead><tbody>
${rows.length?rows.map(r=>`<tr><td>${esc(r.date)}</td><td>${esc(r.receipt_no)}${r.source==='live'?'<span class="m">efter exporten</span>':''}</td><td>${esc(r.vendor)}<span class="m">${esc(r.merchant)}${r.type&&r.type!=='Card Purchase'?' · '+esc(r.type):''}</span></td><td class="num">${sek(r.amount_sek)}</td><td class="c">${yn(r.has_receipt||r.state==='attached',r.payout)}</td><td>${esc(r.export_status_label)}</td><td>${badge('pleo',r.state)}<span class="step">${esc(r.step)}</span></td></tr>`).join(''):'<tr><td colspan="7" class="empty">Inga rader matchar.</td></tr>'}</tbody></table></div>`}

function wire(){const f=tab==='amex'?af:pf;document.querySelectorAll('#view [data-f]').forEach(el=>{const k=el.dataset.f;
if(el.tagName==='BUTTON'){el.onclick=()=>{if(k==='route'){f.state='missing';f.q='';pf.route=el.dataset.v}else f[k]=el.dataset.v;render()}}
else{el.oninput=()=>{f[k]=el.value;const pos=el.selectionStart;render();const n=$(`#view [data-f="${k}"]`);if(n&&n.tagName==='INPUT'){n.focus();n.setSelectionRange(pos,pos)}}}})}
boot();
"""


def render_page(payload, key, plan=False):
    plain = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    blob = encrypt(plain, key)
    plan_link = '<a class="navlink right" href="/plan">Planen</a>' if plan else ""
    return f"""<!doctype html>
<html lang="sv"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>Kvittotavlan</title><style>{CSS}</style></head>
<body><div class="wrap">
<div id="lock" hidden><h1 style="font-size:20px;margin:0 0 6px">Kvittotavlan</h1><p style="color:var(--ink2)">Sidan är krypterad. Klistra in nyckeln, eller öppna länken med nyckeln efter #.</p>
<input id="lockkey" type="password" placeholder="Nyckel" autocomplete="off"><div id="lockerr" class="err"></div><button id="lockbtn">Öppna</button></div>
<div id="app" hidden><header><h1>Kvittotavlan</h1><span class="sub">{payload['entity_name']}</span><span class="sub" id="gen"></span></header>
<nav><button data-tab="oversikt">Översikt</button><button data-tab="amex">Amex</button><button data-tab="pleo">Pleo</button>{plan_link}</nav>
<div id="view"></div>
<p style="color:var(--muted);font-size:12px;margin-top:30px">Siffrorna räknas fram av scripts/dashboard.py ur ledgers, exporter, planer och kvittoarkivet. Inget skrivs in för hand.</p></div>
</div><script>const ITER={PBKDF2_ITER};const BLOB="{blob}";{JS}</script></body></html>
"""


# ----------------------------------------------------------------------------- Kvittoplanen (/plan)

_INLINE_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_HEADING = re.compile(r"^(#{1,3})\s+(.*)$")
_ORDERED = re.compile(r"^\d+\.\s+(.*)$")


def esc(text):
    return html_mod.escape(str(text if text is not None else ""), quote=False)


def inline(text):
    """Escape first, then the three inline forms: `code`, **bold**, [text](https://...)."""
    out = esc(text)
    out = _INLINE_CODE.sub(lambda m: f"<code>{m.group(1)}</code>", out)
    out = _BOLD.sub(r"<strong>\1</strong>", out)
    out = _LINK.sub(r'<a href="\2">\1</a>', out)
    return out


def md_to_html(text, skip_h1=False):
    """The markdown subset docs/kvittoplanen.md uses: #, ## and ### headings, paragraphs,
    - lists, 1. lists, > callouts (one paragraph), and the inline forms. Indented lines
    continue the previous list item. Anything else is a paragraph; nothing is executed."""
    parts, para, lst = [], [], None

    def flush_para():
        if para:
            parts.append(f"<p>{inline(' '.join(para))}</p>")
            para.clear()

    def flush_list():
        nonlocal lst
        if lst is None:
            return
        tag, items = lst
        if tag == "blockquote":
            parts.append(f"<blockquote><p>{inline(' '.join(items))}</p></blockquote>")
        else:
            parts.append(f"<{tag}>" + "".join(f"<li>{inline(i)}</li>" for i in items) + f"</{tag}>")
        lst = None

    def start(tag, item):
        nonlocal lst
        flush_para()
        if lst is None or lst[0] != tag:
            flush_list()
            lst = (tag, [])
        lst[1].append(item)

    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            flush_para()
            flush_list()
            continue
        m = _HEADING.match(stripped)
        if m:
            flush_para()
            flush_list()
            level = len(m.group(1))
            if not (level == 1 and skip_h1):
                parts.append(f"<h{level}>{inline(m.group(2))}</h{level}>")
            continue
        if stripped.startswith("> "):
            start("blockquote", stripped[2:])
            continue
        if stripped.startswith("- "):
            start("ul", stripped[2:])
            continue
        m = _ORDERED.match(stripped)
        if m:
            start("ol", m.group(1))
            continue
        if lst is not None and raw.startswith("  "):
            lst[1][-1] += " " + stripped
            continue
        flush_list()
        para.append(stripped)
    flush_para()
    flush_list()
    return "\n".join(parts)


def plan_status(model):
    """The status strip on the plan page: every figure comes from the model, none is typed."""
    s = model["summary"]
    todo = model["todo"]
    rows = model["amex"]["rows"]
    held = sorted(t["key"].split(":", 1)[1] for t in todo if t["key"].startswith("fortnox-held:"))
    paid = sum(1 for r in rows if r.get("reimbursed_via") == "pleo")
    needs_tag = [r for r in rows if r.get("tag") == "needs-tag"]  # the tavla counts by tag, whatever the row's state
    on_private = sum(1 for r in needs_tag if "2004" in str(r.get("card", "")))
    export = model["sources"].get("pleo_export") or {}
    flagged_open = s.get("flagged_open") or 0
    refiled = s.get("flagged_refiled") or 0
    missing = s.get("pleo_missing_live") or 0
    booked = s.get("amex_vouchers_booked") or 0
    chips = [
        ("ok", f"Uppdaterad {model['today']}"),
        ("warn" if any(t["severity"] == "crit" for t in todo) else "ok",
         f"{sv_int(len(todo))} {plural(len(todo), 'punkt', 'punkter')} på tavlan"),
        ("warn" if held else "ok",
         "Fortnox-planer på hold: " + ", ".join(month_name(m) for m in held) if held else "Inga Fortnox-planer på hold"),
        ("warn" if paid else "ok", f"Amex-rader redan ersatta via Pleo: {sv_int(paid)}"),
        ("warn" if flagged_open else "ok", f"Fel kvitto i Pleo: {sv_int(flagged_open)} kvar, {sv_int(refiled)} rättade"),
        ("warn" if needs_tag else "ok",
         f"Otaggade Amex-rader: {sv_int(len(needs_tag))}, varav {sv_int(on_private)} på privatkortet"),
        ("warn" if missing else "ok", f"Pleo-utgifter utan kvitto: {sv_int(missing)}, {sv_amount(s.get('pleo_missing_live_sek') or 0)}"),
        ("ok" if booked else "warn", f"Bokfört i Fortnox: {sv_int(booked)} {plural(booked, 'verifikat', 'verifikat')}"),
    ]
    if s.get("next_amex_month"):
        present = bool(s.get("next_amex_csv_present"))
        chips.append(("ok" if present else "missing",
                      f"Amex-CSV för {month_name(s['next_amex_month'])}: {'finns' if present else 'saknas'}"))
    if export.get("age_days") is not None:
        chips.append((export.get("state") or "ok", f"Pleo-exporten: {sv_int(export['age_days'])} dagar gammal"))
    else:
        chips.append(("missing", "Pleo-export saknas"))
    return '<div class="src status">' + "".join(f'<span class="{tone}">{esc(text)}</span>' for tone, text in chips) + "</div>"


PLAN_JS = r"""
const $=s=>document.querySelector(s);
async function decrypt(b64,key){const raw=Uint8Array.from(atob(b64),c=>c.charCodeAt(0));const salt=raw.slice(8,16),ct=raw.slice(16);
const km=await crypto.subtle.importKey('raw',new TextEncoder().encode(key),'PBKDF2',false,['deriveBits']);
const bits=new Uint8Array(await crypto.subtle.deriveBits({name:'PBKDF2',salt,iterations:ITER,hash:'SHA-256'},km,384));
const k=await crypto.subtle.importKey('raw',bits.slice(0,32),'AES-CBC',false,['decrypt']);
const pt=await crypto.subtle.decrypt({name:'AES-CBC',iv:bits.slice(32,48)},k,ct);return JSON.parse(new TextDecoder().decode(pt))}
async function unlock(key,remember){try{const html=await decrypt(BLOB,key.trim());if(remember){try{localStorage.setItem('kt-key',key.trim())}catch(e){}}$('#plan').innerHTML=html;$('#lock').hidden=true;$('#app').hidden=false;return true}catch(e){return false}}
async function boot(){let key=(location.hash||'').slice(1);if(!key){try{key=localStorage.getItem('kt-key')||''}catch(e){}}
if(key&&await unlock(key,true)){if(location.hash)history.replaceState(null,'',location.pathname);return}
$('#lock').hidden=false;$('#lockbtn').onclick=async()=>{if(!await unlock($('#lockkey').value,true))$('#lockerr').textContent='Fel nyckel.'};
$('#lockkey').onkeydown=e=>{if(e.key==='Enter')$('#lockbtn').click()}}
boot();
"""


def render_plan_page(body_html, key):
    """The plan page: the rendered markdown plus the status strip, encrypted with the site key.
    The browser stores the key after the first unlock, so the tavla and the plan share one paste."""
    blob = encrypt(json.dumps(body_html, ensure_ascii=False).encode("utf-8"), key)
    return f"""<!doctype html>
<html lang="sv"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>Kvittoplanen</title><style>{CSS}</style></head>
<body><div class="wrap">
<div id="lock" hidden><h1 style="font-size:20px;margin:0 0 6px">Kvittoplanen</h1><p style="color:var(--ink2)">Sidan är krypterad. Klistra in nyckeln, eller öppna länken med nyckeln efter #.</p>
<input id="lockkey" type="password" placeholder="Nyckel" autocomplete="off"><div id="lockerr" class="err"></div><button id="lockbtn">Öppna</button></div>
<div id="app" hidden><header><h1>Kvittoplanen</h1><span class="sub">vägen till ett kvittoflöde utan händer</span></header>
<nav><a class="navlink" href="/">Kvittotavlan</a><a class="navlink on" href="/plan">Planen</a></nav>
<div id="plan" class="plan"></div>
<p class="foot">Texten ligger i docs/kvittoplanen.md i receipts-repot. Statusraden räknas fram av scripts/dashboard.py när sidan byggs.</p></div>
</div><script>const ITER={PBKDF2_ITER};const BLOB="{blob}";{PLAN_JS}</script></body></html>
"""


VERCEL_JSON = {
    "cleanUrls": True,
    "headers": [{"source": "/(.*)", "headers": [
        {"key": "X-Robots-Tag", "value": "noindex, nofollow"},
        {"key": "Cache-Control", "value": "no-store"},
        {"key": "Referrer-Policy", "value": "no-referrer"},
    ]}],
}


def build_site(root, today=None, out_dir=None, plan_source=None):
    """Writes index.html (the tavla) and, when docs/kvittoplanen.md exists, plan.html (Kvittoplanen).
    Returns (out_dir, key, model, has_plan)."""
    root = Path(root).resolve()
    out_dir = Path(out_dir) if out_dir else root / "out" / "site"
    out_dir.mkdir(parents=True, exist_ok=True)
    key = load_key(root)
    model = build_model(root, today)
    plan_path = Path(plan_source) if plan_source else root / PLAN_SOURCE_REL
    has_plan = plan_path.is_file()
    html = render_page(page_payload(model), key, plan=has_plan)
    (out_dir / "index.html").write_text(html, encoding="utf-8")
    plan_file = out_dir / "plan.html"
    if has_plan:
        body = plan_status(model) + "\n" + md_to_html(plan_path.read_text(encoding="utf-8"), skip_h1=True)
        plan_file.write_text(render_plan_page(body, key), encoding="utf-8")
    elif plan_file.exists():
        plan_file.unlink()
    (out_dir / "vercel.json").write_text(json.dumps(VERCEL_JSON, indent=2) + "\n", encoding="utf-8")
    (out_dir / ".vercelignore").write_text("*\n!index.html\n!plan.html\n!vercel.json\n", encoding="utf-8")
    return out_dir, key, model, has_plan


def deploy(out_dir, scope=None):
    """vercel deploy --prod from out_dir; returns the production URL. Links the project first
    (scope: the Vercel team slug, needed the first time in a non-interactive shell)."""
    if not (out_dir / ".vercel" / "project.json").is_file():
        cmd = ["vercel", "link", "--yes", "--project", PROJECT]
        if scope:
            cmd += ["--scope", scope]
        subprocess.run(cmd, cwd=out_dir, check=True)
    proc = subprocess.run(["vercel", "deploy", "--prod", "--yes"], cwd=out_dir, capture_output=True, text=True, check=True)
    url = proc.stdout.strip().splitlines()[-1].strip() if proc.stdout.strip() else ""
    (out_dir / "last-deploy.txt").write_text(f"{datetime.now().isoformat(timespec='seconds')} {url}\n", encoding="utf-8")
    return url


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build (and deploy) the hosted Kvittotavlan.")
    ap.add_argument("--root", default=str(REPO))
    ap.add_argument("--today", default=None)
    ap.add_argument("--out-dir", default=None, help="default <root>/out/site")
    ap.add_argument("--deploy", action="store_true", help="run vercel deploy --prod after building")
    ap.add_argument("--scope", default=None, help="Vercel team slug for the first link (e.g. o5555s-projects)")
    ap.add_argument("--print-url", action="store_true", help="print the last deployment URL with the key fragment")
    args = ap.parse_args(argv)
    today = to_date(args.today) if args.today else None
    if args.today and today is None:
        print(f"error: --today must be YYYY-MM-DD, got {args.today!r}", file=sys.stderr)
        return 2
    root = Path(args.root).resolve()
    out_dir = Path(args.out_dir) if args.out_dir else root / "out" / "site"
    if args.print_url:
        last = out_dir / "last-deploy.txt"
        key = load_key(root, create=False)
        if not last.is_file() or not key:
            print("no deployment recorded", file=sys.stderr)
            return 1
        print(f"{PROD_URL}/#{key}  (senaste deploy: {last.read_text(encoding='utf-8').split()[-1]})")
        return 0
    out_dir, key, model, has_plan = build_site(root, today, out_dir)
    size = (out_dir / "index.html").stat().st_size // 1024
    print(f"skrev {out_dir / 'index.html'} ({size} kB); {len(model['amex']['rows'])} Amex-rader, {len(model['pleo']['rows'])} Pleo-rader, "
          f"{len(model['todo'])} punkter att göra")
    if has_plan:
        print(f"skrev {out_dir / 'plan.html'} ({(out_dir / 'plan.html').stat().st_size // 1024} kB) från {PLAN_SOURCE_REL}")
    else:
        print(f"ingen {PLAN_SOURCE_REL}: /plan utelämnad")
    if args.deploy:
        url = deploy(out_dir, args.scope)
        print(f"deployad: {url} (produktion: {PROD_URL})")
        print(f"länk med nyckel: {PROD_URL}/#{key}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
