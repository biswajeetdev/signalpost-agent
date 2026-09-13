"""Experiment v2: request-free registry corroboration axes on raw HTML, with a page cache.

Candidates per company: Builderr gold domain, registry website, registry email domain,
name guesses. For each resolving candidate fetch homepage + /kontakt (cached on disk) and test:
org number, exact registry email, email domain == site domain, registry phone, street+postcode.
"""
import csv, gzip, hashlib, json, re, socket, sys, unicodedata, urllib.parse, urllib.request, collections
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

LEGAL = {'as','asa','ans','da','enk','sa','ba','nuf','ks','bbl','brl','sf','hf','fli','spa','iks','kf'}
FILL = {'stiftelsen','stiftelse','sameiet','borettslag','og','i','the','holding','norge','norway','group','gruppen'}


def cands(name):
    ordered = []
    for mapping in ({'æ':'ae','ø':'o','å':'a'}, {'æ':'e','ø':'o','å':'aa'}):
        f = name.lower()
        for k, v in mapping.items(): f = f.replace(k, v)
        f = unicodedata.normalize('NFKD', f).encode('ascii', 'ignore').decode()
        toks = [t for t in re.split(r'[^a-z0-9]+', f.replace('&', ' og ')) if t]
        a = [t for t in toks if t not in LEGAL]
        b = [t for t in a if t not in FILL]
        for tl in (b, a):
            for seq in (tl, tl[:2], tl[1:], tl[:1], tl[-2:]):
                for x in (''.join(seq), '-'.join(seq)):
                    if len(x) >= 3 and x not in ordered: ordered.append(x)
    return ordered[:10]

TMP = Path(__file__).parent
CACHE = TMP / "pagecache"; CACHE.mkdir(exist_ok=True)
GOLD = "/Users/biswajeetkumar/signalpost-agent/.claude/worktrees/agent-v1/eval/gold/builderr-sample-100.json"
BULK = "/Users/biswajeetkumar/signalpost-agent/starter-kit/signalpost-starter-kit/brreg-enheter.csv"
UA = "signalpost-research/0.1"
FREE = set('gmail.com hotmail.com online.no outlook.com yahoo.com yahoo.no live.no icloud.com me.com hotmail.no msn.com live.com getmail.no frisurf.no broadpark.no c2i.net start.no altibox.no lyse.net telenor.no ebnett.no mail.com gmx.com protonmail.com proton.me outlook.no epost.no enivest.net tele2.no chello.no'.split())
REQS = collections.Counter()


def fetch(url):
    key = CACHE / hashlib.sha256(url.encode()).hexdigest()
    if key.exists():
        d = json.loads(key.read_text()); return d["final"], d["html"]
    REQS["http"] += 1
    final, html = None, ""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
        with urllib.request.urlopen(req, timeout=8) as r:
            final, html = r.geturl(), r.read(1_500_000).decode("utf-8", "replace")
    except Exception:
        pass
    key.write_text(json.dumps({"final": final, "html": html}))
    return final, html


def resolves(host):
    try:
        socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM); return True
    except OSError:
        return False


def digits(s):
    return re.sub(r"\D", "", s or "")


def axes(reg, site_domain, html):
    low = html.lower(); found = set()
    org = reg["org"]
    if re.search(r"(?<!\d)(?:%s|%s[\s. ]?%s[\s. ]?%s)(?!\d)" % (org, org[:3], org[3:6], org[6:]), html):
        found.add("org")
    if reg["email"] and reg["email"] in low:
        found.add("email_exact")
    if reg["email_domain"] and site_domain and (reg["email_domain"] == site_domain or reg["email_domain"].endswith("." + site_domain)):
        found.add("email_domain")
    flat = digits(html)
    for ph in reg["phones"]:
        if len(ph) >= 8 and ph[-8:] in flat:
            found.add("phone")
    if reg["street"] and reg["postcode"] and reg["street"] in low and reg["postcode"] in low:
        found.add("address")
    return found


def regdomain(url):
    host = (urllib.parse.urlparse(url or "").hostname or "").lower().removeprefix("www.")
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def evaluate(reg, domain):
    base = f"https://{domain}/"
    if not (resolves(domain) or resolves("www." + domain)):
        return None
    final, html = fetch(base)
    if not final:
        return {"domain": domain, "reachable": False, "axes": []}
    site = regdomain(final)
    found = axes(reg, site, html)
    if not found & {"org", "email_exact", "phone"}:
        _, h2 = fetch(urllib.parse.urljoin(final, "/kontakt"))
        found |= axes(reg, site, h2)
    name_hit = " ".join(t for t in re.split(r"[^a-zæøå0-9]+", reg["name"].lower()) if t and t not in {"as", "asa"}) in html.lower()
    return {"domain": domain, "final_domain": site, "reachable": True, "axes": sorted(found), "name": name_hit}


def main():
    gold = json.load(open(GOLD)); wanted = {r["org"] for r in gold}
    reg = {}
    with gzip.open(BULK, "rt", newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["organisasjonsnummer"] in wanted:
                em = (r.get("epostadresse") or "").strip().lower()
                d = em.split("@")[-1] if "@" in em else ""
                adr = (r.get("forretningsadresse.adresse") or "").strip().lower()
                reg[r["organisasjonsnummer"]] = {
                    "org": r["organisasjonsnummer"], "name": r["navn"], "email": em,
                    "email_domain": d if d and d not in FREE else "",
                    "phones": [digits(r.get("telefon")), digits(r.get("mobil"))],
                    "street": adr.split("\n")[0] if adr else "", "postcode": (r.get("forretningsadresse.postnummer") or "").strip(),
                    "website": regdomain(r.get("hjemmeside") if "//" in (r.get("hjemmeside") or "") else "https://" + (r.get("hjemmeside") or "")),
                }

    def run(g):
        rg = reg[g["org"]]
        gd = ((g.get("web") or {}).get("value") or {}).get("registered_domain")
        sources = collections.OrderedDict()
        for dom, src in [(gd, "gold"), (rg["website"], "registry_web"), (rg["email_domain"], "email_domain")] + [(f"{c}.{t}", "guess") for c in cands(rg["name"])[:6] for t in ("no", "com")]:
            if dom and "." in dom:
                sources.setdefault(dom, []).append(src)
        results = []
        for dom, srcs in sources.items():
            ev = evaluate(rg, dom)
            if ev:
                ev["sources"] = srcs; results.append(ev)
        return {"org": g["org"], "name": rg["name"], "gold": gd, "reg": {k: bool(v) for k, v in rg.items() if k not in ("org", "name")}, "candidates": results}

    with ThreadPoolExecutor(16) as pool:
        out = list(pool.map(run, gold))
    json.dump(out, open(TMP / "proof_results.json", "w"), ensure_ascii=False, indent=1)
    print("http requests (uncached)", REQS)


if __name__ == "__main__":
    main()
