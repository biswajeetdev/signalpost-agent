"""Experiment: name->domain guessing + raw-HTML org-number proof on the Builderr 100 sample."""
import json, re, socket, sys, unicodedata, urllib.request, urllib.parse, collections
from concurrent.futures import ThreadPoolExecutor

GOLD = "/Users/biswajeetkumar/signalpost-agent/.claude/worktrees/agent-v1/eval/gold/builderr-sample-100.json"
UA = "signalpost-research/0.1"
LEGAL = {'as','asa','ans','da','enk','sa','ba','nuf','ks','bbl','brl','sf','hf','fli','spa','iks','kf'}
FILL = {'stiftelsen','stiftelse','sameiet','borettslag','og','i','the','holding','norge','norway','group','gruppen'}
CONTACT = ("/kontakt", "/kontakt-oss", "/om-oss", "/contact")
REQS = collections.Counter()


def folds(s):
    s = s.lower(); out = []
    for m in ({'æ':'ae','ø':'o','å':'a'}, {'æ':'e','ø':'o','å':'aa'}):
        t = s
        for k, v in m.items(): t = t.replace(k, v)
        t = unicodedata.normalize('NFKD', t).encode('ascii', 'ignore').decode()
        if t not in out: out.append(t)
    return out


def cands(name):
    ordered = []
    for f in folds(name):
        toks = [t for t in re.split(r'[^a-z0-9]+', f.replace('&', ' og ')) if t]
        a = [t for t in toks if t not in LEGAL]
        b = [t for t in a if t not in FILL]
        for tl in (b, a):
            for seq in (tl, tl[:2], tl[1:], tl[:1], tl[-2:]):
                for x in (''.join(seq), '-'.join(seq)):
                    if len(x) >= 3 and x not in ordered: ordered.append(x)
    return ordered[:10]


def resolves(host):
    try:
        socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM); return True
    except OSError:
        return False


def get(url, tag):
    REQS[tag] += 1
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.geturl(), r.read(1_500_000).decode('utf-8', 'replace')
    except Exception:
        return None, ""


def org_pattern(org):
    return re.compile(r'(?<!\d)(?:%s|%s[\s. ]?%s[\s. ]?%s)(?!\d)' % (org, org[:3], org[3:6], org[6:]))


def proof(org, name, html):
    if org_pattern(org).search(html): return 'org'
    core = [t for t in re.split(r'[^a-zæøå0-9]+', name.lower()) if t and t not in LEGAL]
    low = html.lower()
    if core and ' '.join(core) in low: return 'name'
    return None


def check_site(org, name, base, tag):
    final, html = get(base, tag)
    if not final: return None, None
    p = proof(org, name, html)
    if p == 'org': return final, 'org@home'
    for path in CONTACT[:2]:
        f2, h2 = get(urllib.parse.urljoin(final, path), tag)
        if h2 and org_pattern(org).search(h2): return final, 'org@contact'
    return final, p


def run(r):
    org, name = r['org'], r['name']
    v = (r.get('web') or {}).get('value') or {}
    gold = v.get('registered_domain')
    out = {'org': org, 'name': name, 'gold': gold, 'registry': bool(r.get('website'))}
    if gold:
        _, out['gold_proof'] = check_site(org, name, 'https://www.' + gold + '/' if not gold.startswith('www') else 'https://' + gold, 'gold')
    tried = []
    for label in cands(name):
        for tld in ('no', 'com'):
            host = f'{label}.{tld}'
            if not resolves(host) and not resolves('www.' + host):
                continue
            final, p = check_site(org, name, f'https://{host}/', 'guess')
            tried.append((host, p))
            if p in ('org@home', 'org@contact'):
                out['guess'] = host; out['guess_proof'] = p; out['tried'] = tried; return out
        if len(tried) >= 4: break
    named = [h for h, p in tried if p == 'name']
    if named: out['guess'] = named[0]; out['guess_proof'] = 'name'
    out['tried'] = tried
    return out


rows = json.load(open(GOLD))
with ThreadPoolExecutor(16) as pool:
    results = list(pool.map(run, rows))
json.dump(results, open(sys.argv[1], 'w'), ensure_ascii=False, indent=1)
C = collections.Counter
print('gold proof', C(x.get('gold_proof') for x in results if x['gold']))
print('guess proof', C(x.get('guess_proof') for x in results))
def lab(d): return (d or '').split('.')[0]
agree = C()
for x in results:
    if x.get('guess'):
        agree[(x.get('guess_proof'), 'match_gold' if lab(x['guess']) == lab(x['gold']) else ('no_gold' if not x['gold'] else 'DIFF'))] += 1
print('guess vs gold', agree)
print('requests', REQS)
for x in results:
    if x.get('guess') and x['gold'] and lab(x['guess']) != lab(x['gold']):
        print('  DIFF', x['name'], 'guess', x['guess'], x.get('guess_proof'), 'gold', x['gold'], x.get('gold_proof'))
