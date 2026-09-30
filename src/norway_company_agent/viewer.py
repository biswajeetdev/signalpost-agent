"""Self-contained HTML viewer for a run's envelopes: search, per-company summary, and every claim
with its availability state and clickable source evidence. No external assets; works on mobile."""
from __future__ import annotations

import html
import json
from typing import Any, Iterable, Mapping

SECTIONS = (
    ("Identity", ("legal_name", "legal_form", "industry", "industry_code", "business_address", "registered_employees", "registry_declared_website", "bankrupt", "under_liquidation", "legal_identity")),
    ("Financials", ("financials.",)),
    ("Leadership & workplaces", ("role", "roles", "registered_workplace", "locations")),
    ("Web & profiles", ("official_website", "social_profile", "social_profiles")),
    ("Hiring", ("job_posting",)),
    ("Activity", ("public_activity",)),
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
"""
SCRIPT = """
const q=document.getElementById('q');q.addEventListener('input',()=>{const v=q.value.trim().toLowerCase();
document.querySelectorAll('details.co').forEach(d=>{d.style.display=!v||d.dataset.k.includes(v)?'':'none'})});
"""


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


def _sources(claim: Mapping[str, Any], evidence: Mapping[str, Mapping[str, Any]]) -> str:
    links = []
    for evidence_id in claim.get("evidence_ids") or []:
        item = evidence.get(evidence_id) or {}
        url = str(item.get("source_url") or "")
        label = (item.get("source_class") or "source") + (f" · {str(item.get('retrieved_at'))[:10]}" if item.get("retrieved_at") else "")
        span = html.escape(str(item.get("claim_span") or ""))[:200]
        links.append(f'<a href="{html.escape(url)}" title="{span}" rel="noopener">{html.escape(label)}</a>' if url.startswith("http") else html.escape(label))
    return " · ".join(dict.fromkeys(links))


def company_card(envelope: Mapping[str, Any]) -> str:
    evidence = {item["id"]: item for item in envelope.get("evidence") or []}
    summary = envelope.get("summary") or {}
    modules = envelope.get("modules") or {}
    chips = "".join(f'<span class="chip {html.escape(state)}">{html.escape(module)}: {html.escape(state)}</span>' for module, state in modules.items())
    grouped: dict[str, list[str]] = {}
    for claim in envelope.get("claims") or []:
        state = str(claim.get("availability"))
        note = f'<div class="src">{html.escape(str(claim.get("note")))}</div>' if claim.get("note") and state != "available" else ""
        row = (f'<tr><td class="f">{html.escape(str(claim.get("field")))}</td><td><span class="{html.escape(state)}">{"" if state == "available" else html.escape(state) + " "}</span>'
               f'{_value(claim.get("value")) if state == "available" else ""}{note}<div class="src">{_sources(claim, evidence)}</div></td></tr>')
        grouped.setdefault(_section(str(claim.get("field"))), []).append(row)
    body = "".join(f"<h3>{html.escape(title)}</h3><table>{''.join(grouped[title])}</table>" for title, _ in (*SECTIONS, ("Other", ())) if title in grouped)
    unknowns = summary.get("unknowns") or []
    changes = envelope.get("changes") or []
    key = f"{envelope.get('organisation_number')} {envelope.get('legal_name') or ''} {summary.get('text') or ''}".lower()
    return (
        f'<details class="co" data-k="{html.escape(key)}"><summary><span class="name">{html.escape(str(envelope.get("legal_name") or ""))}</span>'
        f'<span class="org">{html.escape(str(envelope.get("organisation_number")))}</span><div class="chips">{chips}</div></summary>'
        f'<p class="summary">{html.escape(str(summary.get("text") or ""))}</p>'
        + (f'<p class="unknown">Unknown: {html.escape("; ".join(unknowns))}</p>' if unknowns else "")
        + (f'<p class="unknown">Changes since previous run: {len(changes)}</p>' if changes else "")
        + body + "</details>"
    )


def render(envelopes: Iterable[Mapping[str, Any]], *, run_id: str, generated_at: str) -> str:
    envelopes = list(envelopes)
    cards = "".join(company_card(item) for item in envelopes)
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>Signalpost profiles</title><style>{STYLE}</style></head><body><header><h1>Signalpost profiles</h1>"
        f'<input id="q" type="search" placeholder="Search name, organisation number or summary" aria-label="Search">'
        f'<div class="meta">{len(envelopes)} companies · run {html.escape(run_id)} · generated {html.escape(generated_at)} · hover a source for its supporting text</div></header>'
        f"<main>{cards}</main><script>{SCRIPT}</script></body></html>"
    )


def envelopes_from_jsonl(text: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]
