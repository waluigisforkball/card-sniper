#!/usr/bin/env python3
"""
Card Sniper: alerts on big eBay discounts for Pokemon cards missing from your
Fossil / Team Rocket / Gym Heroes / Gym Challenge collection.

Matching is number-first: "29/62" in a title identifies the card, "holo" claims
are ignored. Benchmark price = cheapest TCGplayer market price across editions
(you want any copy), compared against listing price + shipping.

Usage:
  python sniper.py --selftest      offline parser test, no network
  python sniper.py --dry-run       real eBay search, prints alerts, sends nothing
  python sniper.py                 real run (GitHub Actions does this)
"""
import argparse, base64, csv, io, json, os, re, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# ---------------- Config (edit these) ----------------
DISCOUNT_THRESHOLD = 0.40      # alert when total cost <= 60% of market
MIN_MARKET = 3.00              # ignore cards whose market price is under this ($)
AUCTION_WINDOW_HOURS = 3       # auctions: only alert when ending within this window
DIGEST_SIZE = 10               # deals per daily summary notification, best first
MAX_CHECKS = 80                # listings to inspect per run when screening out reprints
SEARCH_PAGES = 2              # 200 listings per page per query
CSV_PATH = Path("dexcollection.csv")
STATE_PATH = Path("seen.json")
WANT_PATH = Path("wantlist.json")
GOT_PATH = Path("got.txt")

SETS = {  # name -> (pokemontcg.io id, printed total)
    "Fossil": ("base3", 62),
    "Team Rocket": ("base5", 82),
    "Gym Heroes": ("gym1", 132),
    "Gym Challenge": ("gym2", 132),
}
TOTAL_TO_SETS = {}
for _name, (_id, _tot) in SETS.items():
    TOTAL_TO_SETS.setdefault(_tot, []).append(_name)

# eBay search queries. Add more if you notice misses.
QUERIES = [
    "pokemon fossil", "pokemon team rocket", "pokemon gym heroes", "pokemon gym challenge",
    "pokemon wotc 1999 /62", "pokemon wotc 2000 /82", "pokemon wotc 2000 /132",
    "pokemon 1st edition /132", "pokemon 1st edition /62", "pokemon 1st edition /82",
]
CATEGORY_ID = "183454"  # CCG Individual Cards

SKIP_WORDS = re.compile(
    r"\b(psa|bgs|cgc|sgc|ace|graded|slab|lot|bundle|proxy|custom|reprint|digital|"
    r"replica|orica|fan\s*made|sealed|booster|pack|complete set|master set|bulk|"
    r"collection|cards? lot|set of|choose your|pick your|anniversary|celebrations?|"
    r"25th|30th|gold|jumbo|oversized?)\b", re.I)
NUM_RE = re.compile(r"(?<!\d)(\d{1,3})\s*/\s*(62|82|132)(?!\d)")

EBAY_API = "https://api.ebay.com"


# ---------------- Want list ----------------
def read_csv_text(path):
    raw = Path(path).read_bytes()
    for enc in ("utf-16", "utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeError:
            continue
    raise SystemExit("Could not decode CSV")


def owned_numbers(csv_path):
    """{set: {number}} where any edition has Quantity > 0."""
    owned = {s: set() for s in SETS}
    for r in csv.DictReader(io.StringIO(read_csv_text(csv_path)), delimiter=";"):
        s = r["Set"].strip()
        if s in owned and int(r["Quantity"] or 0) > 0:
            owned[s].add(r["Number"].split("/")[0].strip())
    return owned


def best_market(card):
    prices = (card.get("tcgplayer") or {}).get("prices") or {}
    vals = []
    for p in prices.values():
        v = p.get("market") or p.get("mid")
        if v:
            vals.append(v)
    return min(vals) if vals else None


def fetch_set(set_id, headers, tries=5):
    """pokemontcg.io throws random 500s/timeouts, so retry with backoff."""
    import time
    last = None
    for i in range(tries):
        try:
            r = requests.get("https://api.pokemontcg.io/v2/cards", headers=headers, timeout=90,
                             params={"q": f"set.id:{set_id}", "pageSize": 250,
                                     "select": "name,number,tcgplayer"})
            r.raise_for_status()
            return r.json()["data"]
        except requests.RequestException as e:
            last = e
            wait = 5 * (2 ** i)
            print(f"pokemontcg.io {set_id} failed ({e}); retry in {wait}s")
            time.sleep(wait)
    raise last


def load_index(csv_path):
    """Index of EVERY card in the four sets: {"num/total": [ {set,name,number,market,owned} ]}.
    Owned cards are included so the matcher can tell that a title like
    "Brock's Rhydon 2/132" is a card you already have. Cached in wantlist.json:
    refreshed daily or when the CSV changes; a stale cache is used if the site is down."""
    import hashlib
    csv_hash = hashlib.sha1(Path(csv_path).read_bytes()).hexdigest()
    cache = json.loads(WANT_PATH.read_text()) if WANT_PATH.exists() else None
    if cache and (cache.get("v") != 2 or cache.get("csv") != csv_hash):
        cache = None
    if cache:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(cache["at"])
        if age < timedelta(hours=24):
            return cache["index"]
    owned = owned_numbers(csv_path)
    headers = {"X-Api-Key": os.environ["POKEMONTCG_API_KEY"]} if os.environ.get("POKEMONTCG_API_KEY") else {}
    index = {}
    try:
        for set_name, (set_id, total) in SETS.items():
            for c in fetch_set(set_id, headers):
                index.setdefault(f"{c['number']}/{total}", []).append(
                    {"set": set_name, "name": c["name"], "number": c["number"],
                     "market": best_market(c), "owned": c["number"] in owned[set_name]})
    except requests.RequestException:
        if cache:
            print("Card-data site down; using stale cached card list")
            return cache["index"]
        raise
    WANT_PATH.write_text(json.dumps({"v": 2, "csv": csv_hash,
                                     "at": datetime.now(timezone.utc).isoformat(),
                                     "index": index}))
    return index


def read_got():
    """Cards you've picked up since the CSV export, from got.txt (one per line,
    like 'Gym Heroes 12'). Lines starting with // are comments."""
    got = set()
    if not GOT_PATH.exists():
        return got
    for line in GOT_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        sets = [n for n, rx in SET_WORDS.items() if re.search(rx, norm(line))]
        m = re.search(r"\d+", line)
        if len(sets) == 1 and m:
            got.add((sets[0], str(int(m.group()))))
        else:
            print(f"got.txt: couldn't read this line, ignoring it: {line!r}")
    return got


def build_want_list(csv_path):
    """Card index with your CSV plus got.txt applied (got.txt is applied after the
    cache, so editing it never needs a refetch)."""
    index = load_index(csv_path)
    got = read_got()
    marked = 0
    for cards in index.values():
        for c in cards:
            if (c["set"], c["number"]) in got and not c["owned"]:
                c["owned"] = True
                marked += 1
    if got:
        print(f"got.txt: {marked} cards marked off")
    return index


# ---------------- Matching ----------------
def norm(s):
    s = s.lower().replace("\u2019", "'").replace("\u2018", "'")
    s = re.sub(r"[.'`]", "", s)
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", s)).strip()


SET_WORDS = {"Fossil": r"\bfossil\b", "Team Rocket": r"\bteam rocket\b",
             "Gym Heroes": r"\bgym heroes\b", "Gym Challenge": r"\bgym challenge\b"}


def match_title(title, index):
    """Return match dict or None. The number is the source of truth; the index holds
    every card (owned or not) so titles naming a card you own are dropped."""
    if SKIP_WORDS.search(title):
        return None
    nums = NUM_RE.findall(title)
    if len(set(nums)) != 1:      # none, or several cards in one listing
        return None
    num, total = nums[0][0].lstrip("0") or "0", nums[0][1]
    cands = index.get(f"{num}/{total}")
    if not cands:
        return None
    t = norm(title)
    named_sets = [n for n, rx in SET_WORDS.items() if re.search(rx, t)]
    named = [c for c in cands if re.search(r"\b" + re.escape(norm(c["name"])) + r"\b", t)]
    if named:
        if named_sets and not any(c["set"] in named_sets for c in named):
            return None          # title contradicts itself (card from one set, name of another)
        cands = [c for c in named if not named_sets or c["set"] in named_sets]
    elif named_sets:
        cands = [c for c in cands if c["set"] in named_sets]
        if not cands:
            return None          # title names a set this number doesn't belong to
    unowned = [c for c in cands if not c["owned"]]
    if not unowned:
        return None              # it's a card you already have
    if len(cands) > 1 and len(unowned) < len(cands):
        return None              # could be a card you own: too noisy to alert
    notes = []
    if len(cands) > 1:
        notes.append("set unclear: " + " or ".join(c["set"] for c in cands))
    card = min(unowned, key=lambda c: c["market"] if c["market"] else 1e9)
    if not named:
        notes.append(f"title doesn't name the card; number says {card['name']}")
    return {"card": card, "notes": notes}


# ---------------- eBay ----------------
def ebay_token():
    cid = os.environ.get("EBAY_CLIENT_ID", "").strip()
    secret = os.environ.get("EBAY_CLIENT_SECRET", "").strip()
    # Safe diagnostics: never prints the secret itself
    env = "PRODUCTION" if "-PRD-" in cid else "SANDBOX" if "-SBX-" in cid else "unknown"
    print(f"eBay keys: id length={len(cid)} ({env} key), secret length={len(secret)}")
    if not cid or not secret:
        raise SystemExit("EBAY_CLIENT_ID or EBAY_CLIENT_SECRET is empty: check the GitHub secret NAMES are exact")
    # App IDs look like Name-app-PRD-xxxx; Cert IDs start with PRD-. Swapped if the reverse.
    if cid.startswith(("PRD-", "SBX-")) and ("-PRD-" in secret or "-SBX-" in secret):
        raise SystemExit("EBAY_CLIENT_ID and EBAY_CLIENT_SECRET look swapped")
    auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    r = requests.post(f"{EBAY_API}/identity/v1/oauth2/token", timeout=30,
                      headers={"Authorization": f"Basic {auth}",
                               "Content-Type": "application/x-www-form-urlencoded"},
                      data={"grant_type": "client_credentials",
                            "scope": "https://api.ebay.com/oauth/api_scope"})
    if r.status_code != 200:
        raise SystemExit(f"eBay rejected the keys ({r.status_code}): {r.text[:300]}")
    return r.json()["access_token"]


def ebay_search(token, q, options, sort, page):
    r = requests.get(f"{EBAY_API}/buy/browse/v1/item_summary/search", timeout=30,
                     headers={"Authorization": f"Bearer {token}",
                              "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"},
                     params={"q": q, "category_ids": CATEGORY_ID, "limit": 200,
                             "offset": page * 200, "sort": sort,
                             "filter": f"buyingOptions:{{{options}}},deliveryCountry:US"})
    if r.status_code == 429:
        print("eBay rate limit hit, stopping early")
        return []
    r.raise_for_status()
    return r.json().get("itemSummaries", [])


def money(x):
    try:
        return float(x["value"])
    except (KeyError, TypeError, ValueError):
        return None


def listing_cost(item, is_auction):
    base = money(item.get("currentBidPrice")) if is_auction else None
    if base is None:
        base = money(item.get("price"))
    ship = 0.0
    opts = item.get("shippingOptions") or []
    if opts:
        s = money(opts[0].get("shippingCost"))
        ship = s if s is not None else 0.0
    return base, ship


def verify_item(token, item_id, card):
    """Read the listing's item specifics. Returns (keep, notes, year).
    Drops reprints (Year Manufactured after 2001, or an anniversary/celebrations Set) and
    flags a Card Number field that disagrees with the title. Sellers who leave these
    blank (or lie) still get through."""
    try:
        r = requests.get(f"{EBAY_API}/buy/browse/v1/item/{item_id}", timeout=30,
                         headers={"Authorization": f"Bearer {token}",
                                  "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"})
        if r.status_code != 200:
            return True, [], ""
        aspects = {a.get("name", "").lower(): str(a.get("value", ""))
                   for a in r.json().get("localizedAspects", [])}
    except requests.RequestException:
        return True, [], ""
    year = aspects.get("year manufactured", "")
    m = re.search(r"(?:19|20)\d\d", year)
    if m and int(m.group()) > 2001:
        return False, [f"reprint: Year Manufactured {year}"], year
    setname = aspects.get("set", "").lower()
    if re.search(r"anniversary|celebrations|reprint|classic|legendary", setname):
        return False, [f"reprint: Set says {aspects['set']}"], year
    notes = []
    num = aspects.get("card number", "")
    v = num.split("/")[0].strip().lstrip("0")
    if v and v != card["number"]:
        notes.append(f"eBay 'Card Number' field says {num}")
    return True, notes, year


# ---------------- Alerts ----------------
def send_digest(deals, dry):
    """One notification listing the deals. Returns the deals that were included
    (empty list = nothing sent / send failed)."""
    included, parts, size = [], [], 0
    for i, d in enumerate(deals, 1):
        c = d["card"]
        line = (f"{len(included) + 1}. {d['pct']:.0f}% off {c['set']} #{c['number']} {c['name']}: "
                f"${d['total']:.2f} vs ${c['market']:.2f} market")
        extras = [x for x in (d.get("condition"), d.get("year")) if x]
        if d["auction"]:
            extras.append("AUCTION, current bid")
        extras += d["notes"]
        if extras:
            line += " (" + "; ".join(extras) + ")"
        entry = line + "\n" + d["url"] + f"\ngot.txt line: {c['set']} {c['number']}"
        if size + len(entry.encode("utf-8")) > 3600:
            break
        parts.append(entry)
        size += len(entry.encode("utf-8")) + 2
        included.append(d)
    if not included:
        return []
    top = included[0]
    n = len(included)
    title = f"{n} card deal{'s' if n > 1 else ''}: {top['pct']:.0f}% off {top['card']['name']}"
    body = "\n\n".join(parts)
    if dry or not os.environ.get("NTFY_TOPIC"):
        print(f"\n[NOTIFICATION] {title}\n{body}")
        return included
    try:
        r = requests.post(f"https://ntfy.sh/{os.environ['NTFY_TOPIC'].strip()}", timeout=30,
                          data=body.encode("utf-8"),
                          headers={"Title": title.encode("ascii", "ignore").decode(),
                                   "Click": top["url"], "Tags": "money_with_wings"})
    except requests.RequestException as e:
        print(f"ntfy push failed: {e}")
        return []
    if r.status_code != 200:
        print(f"ntfy push failed ({r.status_code}): {r.text[:150]}")
        return []
    return included


# ---------------- Main ----------------
def run(dry):
    want = build_want_list(CSV_PATH)
    print(f"Want list: {sum(1 for v in want.values() for c in v if not c['owned'])} cards missing")
    seen = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}
    token = ebay_token()
    now = datetime.now(timezone.utc)
    window = now + timedelta(hours=AUCTION_WINDOW_HOURS)
    items = {}
    for q in QUERIES:
        for page in range(SEARCH_PAGES):
            for it in ebay_search(token, q, "FIXED_PRICE", "newlyListed", page):
                items[it["itemId"]] = (it, False)
        for it in ebay_search(token, q, "AUCTION", "endingSoonest", 0):
            end = it.get("itemEndDate")
            if end and datetime.fromisoformat(end.replace("Z", "+00:00")) <= window:
                items[it["itemId"]] = (it, True)
    print(f"Scanned {len(items)} listings")
    deals = []
    matched = 0
    for iid, (it, is_auction) in items.items():
        if iid in seen:
            continue
        m = match_title(it.get("title", ""), want)
        if not m or not m["card"]["market"] or m["card"]["market"] < MIN_MARKET:
            continue
        matched += 1
        base, ship = listing_cost(it, is_auction)
        if base is None:
            continue
        total = base + ship
        market = m["card"]["market"]
        if total > market * (1 - DISCOUNT_THRESHOLD):
            continue
        deals.append({"iid": iid, "card": m["card"], "title": it["title"], "total": total,
                      "pct": (1 - total / market) * 100, "auction": is_auction,
                      "ends": it.get("itemEndDate", ""), "notes": list(m["notes"]),
                      "url": it.get("itemWebUrl", ""), "condition": it.get("condition", "")})
    deals.sort(key=lambda d: d["pct"], reverse=True)
    print(f"Matched {matched} listings to your want list; {len(deals)} cleared the discount bar")
    for d in deals[:60]:   # full detail in the log so you can sanity-check
        print(f"  {d['pct']:.0f}% off{' [AUCTION]' if d['auction'] else ''} | {d['card']['set']} "
              f"#{d['card']['number']} {d['card']['name']} | ${d['total']:.2f} vs ${d['card']['market']:.2f} "
              f"| {d['condition'][:12]} | {d['title'][:70]}")
    keep, checks = [], 0
    for d in deals:
        if len(keep) >= DIGEST_SIZE or checks >= MAX_CHECKS:
            break
        checks += 1
        ok, notes, year = verify_item(token, d["iid"], d["card"])
        if not ok:
            print(f"  skipped: {d['title'][:60]} | {notes[0]}")
            seen[d["iid"]] = now.isoformat()
            continue
        d["notes"] += notes
        d["year"] = year
        keep.append(d)
    sent = send_digest(keep, dry)
    for d in sent:
        seen[d["iid"]] = now.isoformat()
    alerts = len(sent)
    if len(deals) > checks:
        print(f"{len(deals) - checks} more deals not inspected this run")
    # prune state older than 30 days
    cutoff = now - timedelta(days=30)
    seen = {k: v for k, v in seen.items() if datetime.fromisoformat(v) > cutoff}
    if not dry:
        STATE_PATH.write_text(json.dumps(seen))
    print(f"Alerts: {alerts}")


def selftest():
    def card(st, name, n, mk, owned):
        return {"set": st, "name": name, "number": n, "market": mk, "owned": owned}
    idx = {
        "2/132": [card("Gym Heroes", "Brock's Rhydon", "2", 40, True),
                  card("Gym Challenge", "Blaine's Charizard", "2", 586, False)],
        "6/132": [card("Gym Heroes", "Lt. Surge's Electabuzz", "6", 53, False),
                  card("Gym Challenge", "Giovanni's Machamp", "6", 85, False)],
        "121/132": [card("Gym Heroes", "Blaine's Gamble", "121", 10, True),
                    card("Gym Challenge", "Sabrina's Psychic Control", "121", 9, False)],
        "29/62": [card("Fossil", "Kabutops", "29", 8, False)],
        "4/82": [card("Team Rocket", "Dark Charizard", "4", 90, False)],
    }
    cases = [  # (title, expected card name or None)
        ("2000 Pokemon Gym Heroes Brock's Rhydon Holo #002/132", None),
        ("Brock\u2019s Rhydon 2/132 Gym Challenge Holo", None),
        ("Blaine's Gamble 121/132 Gym Heroes Regular", None),
        ("Giovanni's Machamp 6/132 Gym Challenge Holo Rare", "Giovanni's Machamp"),
        ("Lt. Surge's Electabuzz 6/132 Gym Heroes Rare", "Lt. Surge's Electabuzz"),
        ("Pokemon card 6/132 wotc", "Lt. Surge's Electabuzz"),      # both unowned: flagged unclear
        ("Pokemon card 2/132 wotc", None),                           # might be one you own
        ("Rocket's Zapdos 15/132 25th Anniversary Holo", None),
        ("Misty 018/132 Gym Heroes Gold Holo 30th Anniversary", None),
        ("PSA 9 Kabutops 29/62", None),
        ("Lot of 10 fossil cards 29/62 3/62", None),
        ("Pokemon Fossil Kabutops 29/62 Holo NM", "Kabutops"),
        ("Mystery holo 4/82 Team Rocket", "Dark Charizard"),
        ("Kabutops 28/62", None),
        ("Pokemon Kabutops 29/64 Neo", None),
    ]
    ok = True
    for t, expect in cases:
        m = match_title(t, idx)
        got = m["card"]["name"] if m else None
        good = got == expect
        ok &= good
        print(f"{'.' if good else 'X'} {t!r} -> {got}" + (f" {m['notes']}" if m and m["notes"] else ""))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    selftest() if a.selftest else run(a.dry_run)
