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
SEARCH_PAGES = 2               # 200 listings per page per query
CSV_PATH = Path("dexcollection.csv")
STATE_PATH = Path("seen.json")
WANT_PATH = Path("wantlist.json")

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
    r"collection|cards? lot|set of|choose your|pick your)\b", re.I)
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


def build_want_list(csv_path):
    """{number: [ {set, name, number, market} ]} for cards you don't own.
    Cached in wantlist.json: refreshed daily or when the CSV changes. If the
    card-data site is down, a stale cache is used instead of failing."""
    import hashlib
    csv_hash = hashlib.sha1(Path(csv_path).read_bytes()).hexdigest()
    cache = json.loads(WANT_PATH.read_text()) if WANT_PATH.exists() else None
    if cache and cache.get("csv") == csv_hash:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(cache["at"])
        if age < timedelta(hours=24):
            return cache["want"]
    owned = owned_numbers(csv_path)
    headers = {"X-Api-Key": os.environ["POKEMONTCG_API_KEY"]} if os.environ.get("POKEMONTCG_API_KEY") else {}
    want = {}
    try:
        for set_name, (set_id, _tot) in SETS.items():
            for c in fetch_set(set_id, headers):
                if c["number"] in owned[set_name]:
                    continue
                want.setdefault(c["number"], []).append(
                    {"set": set_name, "name": c["name"], "number": c["number"],
                     "market": best_market(c)})
    except requests.RequestException:
        if cache and cache.get("csv") == csv_hash:
            print("Card-data site down; using stale cached want list")
            return cache["want"]
        raise
    WANT_PATH.write_text(json.dumps({"csv": csv_hash,
                                     "at": datetime.now(timezone.utc).isoformat(),
                                     "want": want}))
    return want


# ---------------- Matching ----------------
def match_title(title, want):
    """Return match dict or None. Number is the source of truth."""
    if SKIP_WORDS.search(title):
        return None
    nums = NUM_RE.findall(title)
    if len(set(nums)) != 1:      # none, or several cards in one listing
        return None
    num, total = nums[0][0].lstrip("0") or "0", int(nums[0][1])
    cands = [w for w in want.get(num, []) if SETS[w["set"]][1] == total]
    if not cands:
        return None
    notes = []
    low = title.lower()
    if len(cands) > 1:  # /132 is shared by Gym Heroes and Gym Challenge
        by_set = [c for c in cands if c["set"].lower() in low]
        if by_set:
            cands = by_set
    if len(cands) > 1:
        by_name = [c for c in cands if c["name"].lower().split("'s ")[-1].split()[0] in low]
        if len(by_name) == 1:
            cands = by_name
    if len(cands) > 1:
        notes.append("set unclear (Gym Heroes vs Gym Challenge)")
    card = min(cands, key=lambda c: c["market"] if c["market"] else 1e9)
    core = re.sub(r"[^a-z ]", "", card["name"].lower().replace("'s", "")).split()
    if core and not any(w in low for w in core):
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


def specifics_conflict(token, item, card):
    """Optional second check: compare eBay's own Card Number field to the title."""
    try:
        r = requests.get(f"{EBAY_API}/buy/browse/v1/item/{item['itemId']}", timeout=30,
                         headers={"Authorization": f"Bearer {token}",
                                  "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"})
        if r.status_code != 200:
            return None
        for a in r.json().get("localizedAspects", []):
            if a.get("name", "").lower() == "card number":
                v = a.get("value", "").split("/")[0].strip().lstrip("0")
                if v and v != card["number"]:
                    return f"eBay 'Card Number' field says {a['value']}"
    except requests.RequestException:
        pass
    return None


# ---------------- Alerts ----------------
def send_alert(d, dry):
    title = f"{d['pct']:.0f}% off: {d['card']['name']} {d['card']['number']} ({d['card']['set']})"
    lines = [f"${d['total']:.2f} total vs ${d['card']['market']:.2f} market",
             d["title"][:100]]
    if d["auction"]:
        lines.append(f"AUCTION ends {d['ends']} (current bid, may rise)")
    lines += [f"Note: {n}" for n in d["notes"]]
    body = "\n".join(lines)
    if dry or not os.environ.get("NTFY_TOPIC"):
        print(f"\n[ALERT] {title}\n{body}\n{d['url']}")
        return
    requests.post(f"https://ntfy.sh/{os.environ['NTFY_TOPIC']}", timeout=30,
                  data=body.encode("utf-8"),
                  headers={"Title": title.encode("utf-8"), "Click": d["url"],
                           "Priority": "high" if d["auction"] else "default",
                           "Tags": "money_with_wings"})


# ---------------- Main ----------------
def run(dry):
    want = build_want_list(CSV_PATH)
    print(f"Want list: {sum(len(v) for v in want.values())} cards")
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
    alerts = 0
    for iid, (it, is_auction) in items.items():
        if iid in seen:
            continue
        m = match_title(it.get("title", ""), want)
        if not m or not m["card"]["market"] or m["card"]["market"] < MIN_MARKET:
            continue
        base, ship = listing_cost(it, is_auction)
        if base is None:
            continue
        total = base + ship
        market = m["card"]["market"]
        if total > market * (1 - DISCOUNT_THRESHOLD):
            continue
        notes = list(m["notes"])
        conflict = specifics_conflict(token, it, m["card"])
        if conflict:
            notes.append(conflict)
        send_alert({"card": m["card"], "title": it["title"], "total": total,
                    "pct": (1 - total / market) * 100, "auction": is_auction,
                    "ends": it.get("itemEndDate", ""), "notes": notes,
                    "url": it.get("itemWebUrl", "")}, dry)
        seen[iid] = now.isoformat()
        alerts += 1
    # prune state older than 30 days
    cutoff = now - timedelta(days=30)
    seen = {k: v for k, v in seen.items() if datetime.fromisoformat(v) > cutoff}
    if not dry:
        STATE_PATH.write_text(json.dumps(seen))
    print(f"Alerts: {alerts}")


def selftest():
    want = {
        "29": [{"set": "Fossil", "name": "Kabutops", "number": "29", "market": 8.0}],
        "6": [{"set": "Gym Heroes", "name": "Koga's Ditto", "number": "6", "market": 50.0},
              {"set": "Gym Challenge", "name": "Giovanni's Gyarados", "number": "6", "market": 40.0}],
        "4": [{"set": "Team Rocket", "name": "Dark Dragonite", "number": "4", "market": 30.0}],
    }
    cases = [
        ("Pokemon Fossil Kabutops 29/62 Holo NM", "Kabutops 29"),
        ("Kabutops 29/62 HOLO rare 1999 wotc", "Kabutops 29"),
        ("Koga's Ditto 6/132 Gym Heroes", "Koga's Ditto 6"),
        ("Rare holo Pokemon card 6 / 132 Giovanni's Gyarados", "Giovanni's Gyarados 6"),
        ("Pokemon card 6/132 wotc", "ambiguous -> flagged"),
        ("PSA 9 Kabutops 29/62", None),
        ("Lot of 10 fossil cards 29/62 3/62", None),
        ("Mystery holo 4/82 Team Rocket", "Dark Dragonite 4 (name not in title)"),
        ("Kabutops 28/62", None),
        ("Pokemon Kabutops 29/64 Neo", None),
    ]
    ok = True
    for t, expect in cases:
        m = match_title(t, want)
        got = None if not m else f"{m['card']['name']} {m['card']['number']}" + (f" | {m['notes']}" if m['notes'] else "")
        print(f"{'.' if (got is None) == (expect is None) else 'X'} {t!r} -> {got}")
        ok &= (got is None) == (expect is None)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    selftest() if a.selftest else run(a.dry_run)
