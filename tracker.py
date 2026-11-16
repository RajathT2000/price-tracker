#!/usr/bin/env python3
"""Free price tracker: original page price, offers, cheaper elsewhere, history, alerts."""
import argparse
import base64
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup


HERE = Path(__file__).resolve().parent
DB = HERE / "prices.db"
CONFIG = HERE / "config.json"
LOG = HERE / "alerts.log"
PRODUCTS = HERE / "products.txt"  # one product per line: <link or name> | hour|day
INTERVALS = {"hour": 3600, "day": 86400}
SLACK = 300  # run a few minutes early so an hourly scheduler never skips a slot
MAX_OTHER_PAGES = 8
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
SYMBOLS = [("A$", "AUD"), ("AU$", "AUD"), ("C$", "CAD"), ("US$", "USD"), ("Rs.", "INR"),
           ("Rs", "INR"), ("₹", "INR"), ("€", "EUR"), ("£", "GBP"), ("¥", "JPY"), ("$", None)]
ISO = {"USD", "AUD", "INR", "EUR", "GBP", "CAD", "JPY", "NZD", "SGD", "AED"}
OFFER_RE = re.compile(
    r"\b\d{1,2}\s?%\s?off\b|\bsave\s+(?:[$₹€£]|rs\.?)?\s?[\d,.]+\b|\bcoupon\b[^.\n|]{0,40}"
    r"|\blimited[- ]time deal\b|\bdeal of the day\b|\bbank offer\b[^.\n|]{0,40}"
    r"|\bflash sale\b|\bclearance\b|\bon sale\b|\bspecial price\b|\bprice drop\b",
    re.I)
OFFER_BOX = re.compile(r"offer|deal|coupon|promo|discount|saving|badge|sale", re.I)
WAS_BOX = re.compile(r"strike|was|old|original|list-?price|compare|mrp|basis|regular", re.I)


# ---------- storage ----------
def connect():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS products(
        id INTEGER PRIMARY KEY, query TEXT, url TEXT, name TEXT,
        every TEXT DEFAULT 'day', last_checked REAL DEFAULT 0,
        last_price REAL, last_offer TEXT, last_alert TEXT, added TEXT);
    CREATE TABLE IF NOT EXISTS prices(
        id INTEGER PRIMARY KEY, product_id INTEGER, ts TEXT, store TEXT, url TEXT,
        price REAL, currency TEXT, offer TEXT, source TEXT);
    """)
    return conn


def load_config():
    try:
        return json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return {}


# ---------- fetching / parsing ----------
def fetch(url, data=None):
    try:
        r = (requests.post(url, data=data, headers=HEADERS, timeout=25) if data
             else requests.get(url, headers=HEADERS, timeout=25))
    except requests.RequestException:
        return None
    if r.status_code != 200 or blocked(r.text):
        return None
    return r.text


def blocked(html):
    """Bot-check / captcha pages: treated as 'no data' (never bypassed)."""
    head = (html or "")[:200000].lower()
    return bool(re.search(r"captcha|are you a robot|robot check|access denied|click the button below to continue "
                          r"shopping|verify you are human|unusual traffic|pardon our interruption", head)) and len(
        re.findall(r"price", head)) < 5


def parse_price(text):
    """'₹1,299.00' -> (1299.0, 'INR'); '1.299,50 €' -> (1299.5, 'EUR')."""
    if text is None:
        return None, None
    if isinstance(text, (int, float)):
        return float(text), None
    s = str(text).strip()
    cur = None
    for sym, code in SYMBOLS:
        if sym in s:
            cur = code
            break
    m = re.search(r"\b(" + "|".join(ISO) + r")\b", s.upper())
    if m:
        cur = m.group(1)
    m = re.search(r"\d[\d.,\s]*", s)
    if not m:
        return None, cur
    num = m.group(0).strip().replace(" ", "")
    if "," in num and "." in num:
        num = num.replace(",", "") if num.rfind(".") > num.rfind(",") else num.replace(".", "").replace(",", ".")
    elif "," in num:
        head, tail = num.rsplit(",", 1)
        num = num.replace(",", "") if len(tail) == 3 else head.replace(",", "") + "." + tail
    num = num.rstrip(".")
    try:
        val = float(num)
    except ValueError:
        return None, cur
    return (val, cur) if val > 0 else (None, cur)


def _walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _jsonld(soup):
    out = {}
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except ValueError:
            continue
        for d in _walk(data):
            t = d.get("@type")
            types = t if isinstance(t, list) else [t]
            if "Product" in types and not out.get("name"):
                out["name"] = d.get("name")
            if any(x in types for x in ("Offer", "AggregateOffer")) and "price" not in out:
                p = d.get("price") or d.get("lowPrice")
                if p is None and isinstance(d.get("priceSpecification"), dict):
                    p = d["priceSpecification"].get("price")
                val, cur = parse_price(p)
                if val:
                    out["price"], out["currency"] = val, d.get("priceCurrency") or cur
            if d.get("priceType") and re.search(r"ListPrice|Strikethrough", str(d["priceType"])):
                val, _ = parse_price(d.get("price"))
                if val:
                    out["was"] = val
    return out


def _first_price(soup, selectors):
    for sel in selectors:
        for el in soup.select(sel):
            raw = el.get("content") or el.get_text(" ", strip=True)
            val, cur = parse_price(raw)
            if val:
                return val, cur
    return None, None


def extract(html):
    """Return {name, price, currency, was, offer} from a product page."""
    soup = BeautifulSoup(html, "html.parser")
    info = _jsonld(soup)
    if not info.get("price"):
        val, cur = _first_price(soup, [
            "meta[property='product:price:amount']", "meta[property='og:price:amount']",
            "#corePriceDisplay_desktop_feature_div .a-price:not(.a-text-price) .a-offscreen",
            "#corePrice_feature_div .a-price:not(.a-text-price) .a-offscreen",
            "#priceblock_dealprice", "#priceblock_ourprice", ".a-price:not(.a-text-price) .a-offscreen",
            "[itemprop='price']", "div.Nx9bqj", "div._30jeq3",
        ])
        if val:
            info["price"], info["currency"] = val, cur
    if not info.get("currency"):
        m = soup.select_one("meta[property='product:price:currency'], meta[property='og:price:currency'],"
                            " [itemprop='priceCurrency']")
        if m:
            info["currency"] = m.get("content") or m.get_text(strip=True)
    if not info.get("price"):
        for el in soup.find_all(attrs={"class": re.compile("price", re.I)}):
            if el.find_parent(["del", "s"]) or el.name in ("del", "s") or WAS_BOX.search(" ".join(el.get("class", []))):
                continue
            val, cur = parse_price(el.get_text(" ", strip=True)[:40])
            if val and re.search(r"[$₹€£¥]|rs\.?|[A-Z]{3}", el.get_text()[:40], re.I):
                info["price"], info["currency"] = val, info.get("currency") or cur
                break
    if not info.get("name"):
        for sel in ["meta[property='og:title']", "#productTitle", "h1", "title"]:
            el = soup.select_one(sel)
            if el:
                info["name"] = (el.get("content") or el.get_text(" ", strip=True)).strip()
                if info["name"]:
                    break
    if not info.get("was"):
        cands = soup.select(".a-price.a-text-price .a-offscreen, .basisPrice .a-offscreen, del, s")
        cands += [e for e in soup.find_all(attrs={"class": WAS_BOX}) if "price" in " ".join(e.get("class", [])).lower()]
        for el in cands:  # only the first one: later ones belong to related products
            val, _ = parse_price(el.get_text(" ", strip=True)[:40])
            if val:
                if info.get("price") and info["price"] < val < info["price"] * 5:
                    info["was"] = val
                break
    offers = []
    if info.get("was") and info.get("price") and info["was"] > info["price"]:
        pct = round(100 * (1 - info["price"] / info["was"]))
        offers.append(f"{pct}% off (was {info['was']:g})")
    for el in soup.find_all(attrs={"class": OFFER_BOX}) + soup.find_all(attrs={"id": OFFER_BOX}):
        for m in OFFER_RE.finditer(el.get_text(" ", strip=True)[:500]):
            s = re.sub(r"\s+", " ", m.group(0)).strip()
            if s.lower() not in (o.lower() for o in offers):
                offers.append(s)
        if len(offers) >= 4:
            break
    info["offer"] = "; ".join(offers[:4]) or None
    if info.get("currency"):
        info["currency"] = str(info["currency"]).upper()
    return info


# ---------- searching other stores ----------
def search(query, n=10):
    urls = []
    html = fetch("https://html.duckduckgo.com/html/", data={"q": query})
    if html:
        for a in BeautifulSoup(html, "html.parser").select("a.result__a"):
            href = a.get("href", "")
            if "uddg=" in href:
                href = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)["uddg"][0]
            if href.startswith("http") and "duckduckgo.com" not in href:
                urls.append(href)
    if not urls:
        html = fetch("https://www.bing.com/search?q=" + urllib.parse.quote(query))
        if html:
            for a in BeautifulSoup(html, "html.parser").select("li.b_algo h2 a"):
                href = bing_target(a.get("href", ""))
                if href.startswith("http"):
                    urls.append(href)
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out[:n]


def bing_target(href):
    """bing.com/ck/a?...&u=a1<base64url> -> real URL."""
    if "bing.com/ck/" not in href:
        return href
    u = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("u", [""])[0]
    try:
        b = u[2:]
        return base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)).decode()
    except Exception:
        return ""


def tokens(s):
    return {t for t in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(t) > 1}


def same_product(a, b):
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return False
    models = {t for t in ta if re.search(r"\d", t) and re.search(r"[a-z]", t)}
    if models and not models & tb:
        return False
    return len(ta & tb) / min(len(ta), len(tb)) >= 0.6


LISTING = re.compile(r"/s\?|[?&](k|q|query|search)=|/search\b|/collections?/(?!.*/products/)|/c/|/category|/categories/"
                     r"|/browse/|/shop/?$|/deals?/?$", re.I)


def is_listing(url, name=""):
    """Search / category pages show many products; their 'price' is not this product's."""
    return bool(LISTING.search(url or "")) or bool(re.search(r"\bresults? for\b|\d+\s+results\b", name or "", re.I))


def domain(url):
    d = urllib.parse.urlparse(url or "").netloc.lower()
    return d[4:] if d.startswith("www.") else d


def url_name(url):
    """'.../apple-airpods-pro-2nd-gen-mtjv3zaa-68009' -> 'apple airpods pro 2nd gen mtjv3zaa'."""
    segs = [x for x in urllib.parse.urlparse(url).path.split("/") if "-" in x or "_" in x]
    if not segs:
        return url
    words = re.split(r"[-_+]+", max(segs, key=len).rsplit(".", 1)[0])
    while words and words[-1].isdigit():
        words.pop()
    return " ".join(words) or url


def short_name(name, words=8):
    name = re.sub(r"[()\[\]]", " ", re.split(r"\s[|:\-–]\s|,", name or "")[0])
    return " ".join(name.split()[:words])


# ---------- notifications ----------
def notify(title, message, link=None):
    line = f"{datetime.now():%Y-%m-%d %H:%M}  {title}: {message}" + (f"  {link}" if link else "")
    print(line)
    with open(LOG, "a") as f:
        f.write(line + "\n")
    cfg = load_config()
    if sys.platform == "darwin" and cfg.get("desktop", True):
        esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
        subprocess.run(["osascript", "-e", f'display notification "{esc(message)}" with title "{esc(title)}"'],
                       capture_output=True)
    topic = os.environ.get("NTFY_TOPIC") or cfg.get("ntfy_topic")
    if topic:
        try:
            headers = {"Title": title.encode("utf-8"), "Tags": "moneybag"}
            if link:
                headers["Click"] = link
            requests.post(f"https://ntfy.sh/{topic}", data=(message + (f"\n{link}" if link else "")).encode(),
                          headers=headers, timeout=15)
        except Exception:
            pass


# ---------- core ----------
def check(conn, p):
    now = datetime.now().isoformat(timespec="seconds")
    results, orig = [], None
    name = p["name"] or (url_name(p["url"]) if p["url"] and p["query"] == p["url"] else p["query"])
    if p["url"]:
        html = fetch(p["url"])
        info = extract(html) if html else {}
        if info.get("name") and not p["name"]:
            name = info["name"]
        if info.get("price"):
            orig = {"store": domain(p["url"]), "url": p["url"], "price": info["price"],
                    "currency": info.get("currency"), "offer": info.get("offer"), "source": "original"}
            results.append(orig)
    tried = 0
    queries = [short_name(name) + " price", short_name(name, 5) + " price"]
    if not re.match(r"https?://", p["query"] or ""):
        queries.insert(0, p["query"] + " price")
    urls = []
    for q in dict.fromkeys(queries):
        urls += [u for u in search(q) if u not in urls]
    for u in urls:
        if (p["url"] and domain(u) == domain(p["url"])) or is_listing(u):
            continue
        if tried >= MAX_OTHER_PAGES:
            break
        tried += 1
        html = fetch(u)
        if not html:
            continue
        info = extract(html)
        if (not info.get("price") or is_listing(u, info.get("name"))
                or not same_product(short_name(name, 12), info.get("name"))):
            continue
        if orig and orig["currency"] and info.get("currency") and info["currency"] != orig["currency"]:
            continue
        results.append({"store": domain(u), "url": u, "price": info["price"], "currency": info.get("currency"),
                        "offer": info.get("offer"), "source": "other"})

    prev_low = conn.execute("SELECT MIN(price) FROM prices WHERE product_id=?", (p["id"],)).fetchone()[0]
    for r in results:
        conn.execute("INSERT INTO prices(product_id,ts,store,url,price,currency,offer,source) VALUES(?,?,?,?,?,?,?,?)",
                     (p["id"], now, r["store"], r["url"], r["price"], r["currency"], r["offer"], r["source"]))

    alerts = []
    fmt = lambda r: f"{r['currency'] or ''} {r['price']:,.2f}".strip()
    if orig and p["last_price"] and orig["price"] < p["last_price"]:
        alerts.append((f"Price drop: {fmt(orig)} at {orig['store']} (was {p['last_price']:,.2f})", orig["url"]))
    if orig and orig["offer"] and orig["offer"] != p["last_offer"]:
        alerts.append((f"Offer at {orig['store']}: {orig['offer']} — now {fmt(orig)}", orig["url"]))
    others = sorted((r for r in results if r["source"] == "other"), key=lambda r: r["price"])
    key = p["last_alert"]
    if others and (not orig or others[0]["price"] < orig["price"]):
        best = others[0]
        k = f"{best['store']}|{best['price']}"
        if k != p["last_alert"] and (prev_low is None or best["price"] < prev_low or
                                     (orig and best["price"] < orig["price"])):
            ref = f" (original {fmt(orig)})" if orig else ""
            alerts.append((f"Cheaper: {fmt(best)} at {best['store']}{ref}", best["url"]))
            key = k
    if prev_low is not None and results:
        low = min(results, key=lambda r: r["price"])
        if low["price"] < prev_low and not any(low["url"] == u for _, u in alerts):
            alerts.append((f"New lowest: {fmt(low)} at {low['store']} (previous low {prev_low:,.2f})", low["url"]))

    for msg, link in alerts:
        notify(short_name(name, 6) or "Price alert", msg, link)
    conn.execute("UPDATE products SET name=?, last_checked=?, last_price=?, last_offer=?, last_alert=? WHERE id=?",
                 (name, time.time(), orig["price"] if orig else p["last_price"],
                  orig["offer"] if orig else p["last_offer"], key, p["id"]))
    conn.commit()
    return results, alerts


def parse_products(text):
    """'<link or name> | hour' per line; '#' comments; interval defaults to day."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        target, _, every = line.rpartition("|") if "|" in line else (line, "", "day")
        every = every.strip().lower()
        if every not in INTERVALS:
            target, every = line, "day"
        out.append((target.strip(), every))
    return out


def sync(conn):
    """Make the database match products.txt (add new lines, update intervals, drop removed lines)."""
    if not PRODUCTS.exists():
        return
    wanted = dict(parse_products(PRODUCTS.read_text()))
    have = {r["query"]: r for r in conn.execute("SELECT * FROM products")}
    for q, every in wanted.items():
        if q not in have:
            url = q if re.match(r"https?://", q) else None
            conn.execute("INSERT INTO products(query,url,every,added) VALUES(?,?,?,?)",
                         (q, url, every, datetime.now().isoformat(timespec="seconds")))
        elif have[q]["every"] != every:
            conn.execute("UPDATE products SET every=? WHERE id=?", (every, have[q]["id"]))
    for q, r in have.items():
        if q not in wanted:
            conn.execute("DELETE FROM prices WHERE product_id=?", (r["id"],))
            conn.execute("DELETE FROM products WHERE id=?", (r["id"],))
    conn.commit()


def write_products(conn):
    rows = conn.execute("SELECT query, every FROM products ORDER BY id").fetchall()
    head = "# One product per line: <link or name> | hour   (or | day)\n"
    PRODUCTS.write_text(head + "".join(f"{r['query']} | {r['every']}\n" for r in rows))


def due(p, now=None):
    return (now or time.time()) - (p["last_checked"] or 0) >= INTERVALS[p["every"]] - SLACK


# ---------- CLI ----------
def cmd_add(conn, a):
    target = a.target.strip()
    url = target if re.match(r"https?://", target) else None
    if not url:
        for u in search(target + " price"):
            if is_listing(u):
                continue
            html = fetch(u)
            info = extract(html) if html else {}
            if info.get("price") and same_product(target, info.get("name")):
                url = u
                break
    cur = conn.execute("INSERT INTO products(query,url,every,added) VALUES(?,?,?,?)",
                       (target, url, a.every, datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    write_products(conn)
    p = conn.execute("SELECT * FROM products WHERE id=?", (cur.lastrowid,)).fetchone()
    results, _ = check(conn, p)
    p = conn.execute("SELECT * FROM products WHERE id=?", (cur.lastrowid,)).fetchone()
    print(f"Added #{p['id']}: {p['name']} (every {p['every']})")
    print(f"  page: {p['url'] or 'none found, tracking by name'}")
    for r in sorted(results, key=lambda r: r["price"]):
        print(f"  {r['currency'] or '':>3} {r['price']:>10,.2f}  {r['store']:<25} {r['offer'] or ''}  {r['url']}")
    if p["url"] and not any(r["source"] == "original" for r in results):
        print("  original page gave no price (site blocks scripts or no price shown); other stores still checked")
    if not results:
        print("  no price found yet; will retry on schedule")


def cmd_list(conn, a):
    for p in conn.execute("SELECT * FROM products ORDER BY id"):
        low = conn.execute("SELECT price, store FROM prices WHERE product_id=? ORDER BY price LIMIT 1",
                           (p["id"],)).fetchone()
        last = datetime.fromtimestamp(p["last_checked"]).strftime("%Y-%m-%d %H:%M") if p["last_checked"] else "never"
        print(f"#{p['id']} [{p['every']}] {p['name'] or p['query']}\n    now {p['last_price'] or '-'}"
              f" | lowest {f'{low[0]:,.2f} at {low[1]}' if low else '-'} | checked {last}\n    {p['url'] or ''}")


def cmd_history(conn, a):
    rows = conn.execute("SELECT ts,store,price,currency,offer,url FROM prices WHERE product_id=? ORDER BY ts, price",
                        (a.id,)).fetchall()
    for r in rows:
        print(f"{r['ts']}  {r['currency'] or '':>3} {r['price']:>10,.2f}  {r['store']:<25} {r['offer'] or ''}")
    if not rows:
        print("no history")


def cmd_check(conn, a):
    sync(conn)
    for p in conn.execute("SELECT * FROM products").fetchall():
        if a.all or due(p):
            try:
                results, alerts = check(conn, p)
                low = min(results, key=lambda r: r["price"]) if results else None
                best = "%s %.2f at %s" % (low["currency"] or "", low["price"], low["store"]) if low else "no price"
                print(f"#{p['id']} {short_name(p['name'] or p['query'], 6)}: {best}, {len(alerts)} alert(s)")
            except Exception as e:
                print(f"#{p['id']} failed: {e}", file=sys.stderr)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="tracker.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("add"); s.add_argument("target"); s.add_argument("--every", choices=INTERVALS, default="day")
    sub.add_parser("list")
    s = sub.add_parser("remove"); s.add_argument("id", type=int)
    s = sub.add_parser("every"); s.add_argument("id", type=int); s.add_argument("interval", choices=INTERVALS)
    s = sub.add_parser("history"); s.add_argument("id", type=int)
    s = sub.add_parser("check"); s.add_argument("--all", action="store_true")
    sub.add_parser("test-notify")
    a = ap.parse_args(argv)
    conn = connect()
    if a.cmd == "add":
        cmd_add(conn, a)
    elif a.cmd == "list":
        cmd_list(conn, a)
    elif a.cmd == "remove":
        conn.execute("DELETE FROM products WHERE id=?", (a.id,)); conn.execute("DELETE FROM prices WHERE product_id=?", (a.id,))
        conn.commit(); write_products(conn); print(f"removed #{a.id}")
    elif a.cmd == "every":
        conn.execute("UPDATE products SET every=? WHERE id=?", (a.interval, a.id)); conn.commit(); write_products(conn)
        print(f"#{a.id} now checked every {a.interval}")
    elif a.cmd == "history":
        cmd_history(conn, a)
    elif a.cmd == "check":
        cmd_check(conn, a)
    elif a.cmd == "test-notify":
        notify("Price tracker", "Test alert: ₹999.00 at example.com", "https://example.com")


if __name__ == "__main__":
    main()
