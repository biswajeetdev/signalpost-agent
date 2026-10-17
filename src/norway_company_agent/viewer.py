"""Self-contained HTML viewer for a run's envelopes: search, filters, sorting, a side-by-side comparison
of chosen companies, a per-company summary, and every claim with its availability state and clickable
source evidence. No external assets; works on mobile."""
from __future__ import annotations

import html
import json
from typing import Any, Iterable, Mapping

SECTIONS = (
    ("Identity", ("legal_name", "legal_form", "industry", "industry_code", "business_address", "registered_employees", "registry_declared_website", "bankrupt", "under_liquidation", "legal_identity")),
    ("Financials", ("financials.",)),
    ("Leadership & workplaces", ("role", "roles", "registered_workplace", "locations")),
    ("Web & profiles", ("official_website", "social_profile", "social_profiles")),
    ("Hiring", ("hiring_signal", "job_posting")),
    ("Activity", ("dated_news", "public_activity")),
)
STYLE = """
:root{--bg:#f7f7f5;--fg:#1d1d1b;--muted:#6b6b66;--card:#fff;--line:#e4e3de;--ok:#1f7a4d;--no:#8a8a84;--warn:#a15c00;--bad:#b3261e;--link:#1a5fb4}
@media (prefers-color-scheme:dark){:root{--bg:#161614;--fg:#ecebe6;--muted:#a3a29c;--card:#1f1f1d;--line:#34332f;--ok:#5cc28f;--no:#8f8e88;--warn:#e0a24a;--bad:#f2837b;--link:#8ab4f8}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{position:sticky;top:0;background:var(--bg);padding:12px 16px;border-bottom:1px solid var(--line);z-index:1}
h1{font-size:18px;margin:0 0 8px}input{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg);font-size:15px}
main{max-width:980px;margin:0 auto;padding:12px 16px 48px}.meta{color:var(--muted);font-size:13px}
details.co{background:var(--card);border:1px solid var(--line);border-radius:10px;margin:10px 0;padding:0 14px}
details.co>summary{cursor:pointer;padding:12px 0;list-style:none}details.co>summary::-webkit-details-marker{display:none}
.name{font-weight:600}.org{color:var(--muted);font-variant-numeric:tabular-nums;margin-left:6px}
.chips{margin-top:6px;display:flex;flex-wrap:wrap;gap:4px}.chip{font-size:12px;padding:1px 7px;border-radius:99px;border:1px solid var(--line)}
.available{color:var(--ok)}.not_available,.not_applicable{color:var(--no)}.ambiguous,.blocked{color:var(--warn)}.failed{color:var(--bad)}
.summary{margin:4px 0 10px}.unknown{color:var(--muted);font-size:13px}h3{font-size:14px;margin:14px 0 6px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
table{width:100%;border-collapse:collapse;font-size:14px}td{border-top:1px solid var(--line);padding:6px 4px;vertical-align:top;overflow-wrap:anywhere}
td.f{width:34%;color:var(--muted)}a{color:var(--link)}.src{font-size:12px;color:var(--muted)}
.tools{display:flex;flex-wrap:wrap;gap:6px 14px;align-items:center;margin-top:8px;font-size:13px}
.tools select,.tools input[type=number]{width:auto;padding:4px 6px;font-size:13px}.tools input[type=number]{max-width:110px}
.tools label{display:inline-flex;gap:4px;align-items:center;white-space:nowrap}
.pick{margin-left:8px;font-size:12px;color:var(--muted);white-space:nowrap}.pick input{width:auto;vertical-align:middle}
#cmp{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin:10px 0;overflow-x:auto}
#cmp table{min-width:520px}#cmp th{text-align:left;font-size:13px;padding:6px 4px;vertical-align:bottom}#cmp td.f{width:auto;white-space:nowrap}
"""
SCRIPT = """
const $=id=>document.getElementById(id),cards=[...document.querySelectorAll('details.co')],main=document.querySelector('main');
const num=v=>v===''||v===undefined?null:Number(v);
function apply(){const v=$('q').value.trim().toLowerCase(),place=$('place').value,min=num($('rmin').value),max=num($('rmax').value);
 const need=[...document.querySelectorAll('.flt:checked')].map(x=>x.value);let shown=0;
 cards.forEach(d=>{const r=num(d.dataset.rev);const ok=(!v||d.dataset.k.includes(v))&&(!place||d.dataset.place===place)
  &&need.every(f=>d.dataset[f]==='1')&&(min===null||(r!==null&&r>=min*1e6))&&(max===null||(r!==null&&r<=max*1e6));
  d.style.display=ok?'':'none';if(ok)shown++});$('count').textContent=shown+' shown'}
function sort(){const k=$('sort').value;const val=d=>k==='name'?d.dataset.name:num(d.dataset[k]);
 cards.sort((a,b)=>{const x=val(a),y=val(b);if(k==='name')return x.localeCompare(y);if(x===null)return 1;if(y===null)return -1;return y-x});
 cards.forEach(d=>main.appendChild(d))}
function compare(){const picked=cards.filter(d=>d.querySelector('.pick input').checked).slice(0,4),box=$('cmp');
 box.replaceChildren();box.hidden=picked.length<2;if(picked.length<2)return;
 const data=picked.map(d=>JSON.parse(d.dataset.cmp)),t=document.createElement('table'),head=t.insertRow();
 const h=document.createElement('th');h.textContent='Compare ('+picked.length+')';head.appendChild(h);
 data.forEach(c=>{const th=document.createElement('th');th.textContent=c.name;head.appendChild(th)});
 data[0].rows.forEach((row,i)=>{const tr=t.insertRow(),f=tr.insertCell();f.className='f';f.textContent=row[0];
  data.forEach(c=>{const [,text,url]=c.rows[i],td=tr.insertCell();if(url){const a=document.createElement('a');a.href=url;a.rel='noopener';a.textContent=text;td.appendChild(a)}else td.textContent=text})});
 box.appendChild(t)}
['q','place','rmin','rmax'].forEach(id=>$(id).addEventListener('input',apply));
document.querySelectorAll('.flt').forEach(x=>x.addEventListener('change',apply));
$('sort').addEventListener('change',()=>{sort();apply()});
document.querySelectorAll('.pick').forEach(l=>l.addEventListener('click',e=>e.stopPropagation()));
document.querySelectorAll('.pick input').forEach(x=>x.addEventListener('change',()=>{
 if(x.checked&&cards.filter(d=>d.querySelector('.pick input').checked).length>4)x.checked=false;compare()}));
sort();apply();
"""
COMPARE_FIELDS = (
    ("Revenue", "financials.revenue"), ("Operating result", "financials.operating_result"), ("Annual result", "financials.annual_result"),
    ("Equity", "financials.equity"), ("Registered employees", "registered_employees"), ("Website", "official_website"),
)


def _section(field: str) -> str:
    for title, prefixes in SECTIONS:
        if any(field == prefix or (prefix.endswith(".") and field.startswith(prefix)) for prefix in prefixes):
            return title
    return "Other"


def _value(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, dict):
        if "amount" in value:
            return html.escape(f"{value.get('amount'):,} {value.get('currency') or ''}".strip())
        if value.get("url") and value.get("title"):
            extra = " · ".join(str(value[key]) for key in ("date", "published") if value.get(key))
            return f'<a href="{html.escape(str(value["url"]))}" rel="noopener">{html.escape(str(value["title"]))}</a> {html.escape(extra)}'
        if value.get("url"):
            return f'<a href="{html.escape(str(value["url"]))}" rel="noopener">{html.escape(str(value.get("platform") or value["url"]))}</a>'
        if "role" in value:
            name = value.get("name")
            name = ", ".join(map(str, name)) if isinstance(name, list) else name
            return html.escape(f"{value.get('role')}: {name}")
        if "adresse" in value or "poststed" in value:
            return html.escape(", ".join(str(v if not isinstance(v, list) else " ".join(v)) for v in value.values() if v))
        return html.escape(", ".join(f"{k}: {v}" for k, v in value.items() if v not in (None, "", [])))[:300]
    if isinstance(value, list):
        return html.escape(", ".join(map(str, value)))
    text = str(value)
    if text.startswith(("http://", "https://")):
        return f'<a href="{html.escape(text)}" rel="noopener">{html.escape(text)}</a>'
    return html.escape(text)


def _claim_value(claim: Mapping[str, Any]) -> str:
    """URL-valued external claims show their title or platform as the link text, plus any date."""
    value = claim.get("value")
    label = claim.get("title") or claim.get("platform")
    if isinstance(value, str) and value.startswith(("http://", "https://")) and label:
        extra = " · ".join(str(claim[key]) for key in ("published_at",) if claim.get(key))
        return f'<a href="{html.escape(value)}" rel="noopener">{html.escape(str(label))}</a> {html.escape(extra)}'.rstrip()
    return _value(value)


def _sources(claim: Mapping[str, Any], evidence: Mapping[str, Mapping[str, Any]]) -> str:
    links = []
    for evidence_id in claim.get("evidence_ids") or []:
        item = evidence.get(evidence_id) or {}
        url = str(item.get("source_url") or "")
        label = (item.get("source_class") or "source") + (f" · {str(item.get('retrieved_at'))[:10]}" if item.get("retrieved_at") else "")
        span = html.escape(str(item.get("claim_span") or ""))[:200]
        links.append(f'<a href="{html.escape(url)}" title="{span}" rel="noopener">{html.escape(label)}</a>' if url.startswith("http") else html.escape(label))
    return " · ".join(dict.fromkeys(links))


def _first(envelope: Mapping[str, Any], field: str) -> Mapping[str, Any] | None:
    return next((claim for claim in envelope.get("claims") or [] if claim.get("field") == field and claim.get("availability") == "available"), None)


def _source_url(claim: Mapping[str, Any] | None, evidence: Mapping[str, Mapping[str, Any]]) -> str:
    for evidence_id in (claim or {}).get("evidence_ids") or []:
        url = str((evidence.get(evidence_id) or {}).get("source_url") or "")
        if url.startswith("http"):
            return url
    return ""


def _facts(envelope: Mapping[str, Any], evidence: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Filter/sort attributes and comparison rows, each comparison value linked to its source."""
    def amount(field: str) -> float | None:
        try:
            return float(((_first(envelope, field) or {}).get("value") or {}).get("amount"))
        except (TypeError, ValueError):
            return None

    claims = envelope.get("claims") or []
    has = {str(claim.get("field")) for claim in claims if claim.get("availability") == "available"}
    revenue, previous = amount("financials.revenue"), amount("financials.previous.revenue")
    growth = round((revenue - previous) / previous * 100, 1) if revenue is not None and previous and previous > 0 else None
    employees = (_first(envelope, "registered_employees") or {}).get("value")
    address = (_first(envelope, "business_address") or {}).get("value")
    place = str((address or {}).get("poststed") or "").title() if isinstance(address, dict) else ""
    year = str(((_first(envelope, "financials.revenue") or {}).get("reporting_period") or {}).get("end") or "")[:4]
    rows = []
    for label, field in COMPARE_FIELDS:
        claim = _first(envelope, field)
        value = (claim or {}).get("value")
        text = "—" if claim is None else (f"{value.get('amount'):,.0f} {value.get('currency') or ''}".strip() if isinstance(value, dict) and "amount" in value else str(value))
        rows.append([label + (f" ({year})" if field.startswith("financials.") and year else ""), text, _source_url(claim, evidence)])
    rows.append(["Revenue change on previous year", "—" if growth is None else f"{growth:+.1f}%", _source_url(_first(envelope, "financials.previous.revenue"), evidence)])
    hiring, news = _first(envelope, "hiring_signal"), max((c for c in claims if c.get("field") == "dated_news" and c.get("availability") == "available"), key=lambda c: str(c.get("published_at") or ""), default=None)
    rows.append(["Hiring", "yes" if hiring else "no signal found", _source_url(hiring, evidence)])
    rows.append(["Latest dated item", f"{news.get('published_at')}: {news.get('title')}" if news else "none found", str(news.get("value") or "") if news else ""])
    rows.append(["Place", place or "—", ""])
    takeaway = next((point.get("text") for point in (envelope.get("summary") or {}).get("points") or [] if point.get("topic") == "takeaway"), "")
    rows.append(["In short", str(takeaway).removeprefix("In short: ") or "—", ""])
    return {
        "rev": "" if revenue is None else f"{revenue:.0f}", "growth": "" if growth is None else str(growth),
        "emp": str(employees) if isinstance(employees, int) else "", "place": place,
        "hiring": "1" if "hiring_signal" in has else "0", "web": "1" if "official_website" in has else "0",
        "news": "1" if "dated_news" in has else "0", "social": "1" if "social_profile" in has else "0",
        "cmp": json.dumps({"name": str(envelope.get("legal_name") or envelope.get("organisation_number")), "rows": rows}, ensure_ascii=False),
    }


def company_card(envelope: Mapping[str, Any]) -> str:
    evidence = {item["id"]: item for item in envelope.get("evidence") or []}
    facts = _facts(envelope, evidence)
    data = " ".join(f'data-{key}="{html.escape(value)}"' for key, value in facts.items())
    summary = envelope.get("summary") or {}
    modules = envelope.get("modules") or {}
    chips = "".join(f'<span class="chip {html.escape(state)}">{html.escape(module)}: {html.escape(state)}</span>' for module, state in modules.items())
    grouped: dict[str, list[str]] = {}
    for claim in envelope.get("claims") or []:
        state = str(claim.get("availability"))
        note = f'<div class="src">{html.escape(str(claim.get("note")))}</div>' if claim.get("note") and state != "available" else ""
        row = (f'<tr><td class="f">{html.escape(str(claim.get("field")))}</td><td><span class="{html.escape(state)}">{"" if state == "available" else html.escape(state) + " "}</span>'
               f'{_claim_value(claim) if state == "available" else ""}{note}<div class="src">{_sources(claim, evidence)}</div></td></tr>')
        grouped.setdefault(_section(str(claim.get("field"))), []).append(row)
    body = "".join(f"<h3>{html.escape(title)}</h3><table>{''.join(grouped[title])}</table>" for title, _ in (*SECTIONS, ("Other", ())) if title in grouped)
    unknowns = summary.get("unknowns") or []
    changes = envelope.get("changes") or []
    key = f"{envelope.get('organisation_number')} {envelope.get('legal_name') or ''} {summary.get('text') or ''}".lower()
    return (
        f'<details class="co" data-k="{html.escape(key)}" data-name="{html.escape(str(envelope.get("legal_name") or "").lower())}" {data}><summary><span class="name">{html.escape(str(envelope.get("legal_name") or ""))}</span>'
        f'<span class="org">{html.escape(str(envelope.get("organisation_number")))}</span><label class="pick"><input type="checkbox" aria-label="Add to comparison"> compare</label><div class="chips">{chips}</div></summary>'
        f'<p class="summary">{html.escape(str(summary.get("text") or ""))}</p>'
        + (f'<p class="unknown">Unknown: {html.escape("; ".join(unknowns))}</p>' if unknowns else "")
        + (f'<p class="unknown">Changes since previous run: {len(changes)}</p>' if changes else "")
        + body + "</details>"
    )


def render(envelopes: Iterable[Mapping[str, Any]], *, run_id: str, generated_at: str) -> str:
    envelopes = list(envelopes)
    cards = "".join(company_card(item) for item in envelopes)
    places = sorted({_facts(item, {})["place"] for item in envelopes} - {""})
    options = "".join(f'<option value="{html.escape(place)}">{html.escape(place)}</option>' for place in places)
    tools = (
        '<div class="tools"><label>Place <select id="place"><option value="">all</option>' + options + '</select></label>'
        + "".join(f'<label><input type="checkbox" class="flt" value="{key}"> {label}</label>' for key, label in (("hiring", "hiring"), ("web", "verified website"), ("news", "dated activity"), ("social", "social profile")))
        + '<label>Revenue NOK m <input id="rmin" type="number" min="0" step="any" placeholder="min" aria-label="Minimum revenue, NOK million"> – '
        '<input id="rmax" type="number" min="0" step="any" placeholder="max" aria-label="Maximum revenue, NOK million"></label>'
        '<label>Sort <select id="sort"><option value="name">name</option><option value="rev">revenue</option><option value="growth">revenue change</option><option value="emp">employees</option></select></label>'
        '<span id="count" class="meta"></span></div>'
    )
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>Signalpost profiles</title><style>{STYLE}</style></head><body><header><h1>Signalpost profiles</h1>"
        f'<input id="q" type="search" placeholder="Search name, organisation number or summary" aria-label="Search">{tools}'
        f'<div class="meta">{len(envelopes)} companies · run {html.escape(run_id)} · generated {html.escape(generated_at)} · tick "compare" on 2–4 companies to see them side by side · hover a source for its supporting text</div></header>'
        f'<main><section id="cmp" hidden></section>{cards}</main><script>{SCRIPT}</script></body></html>'
    )


def envelopes_from_jsonl(text: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]
