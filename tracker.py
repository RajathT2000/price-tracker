#!/usr/bin/env python3
"""Free price tracker: original page price, offers, cheaper elsewhere, history, alerts."""
import argparse
import base64
import csv
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup


HERE = Path(__file__).resolve().parent
DB = HERE / "prices.db"
CONFIG = HERE / "config.json"
LOG = HERE / "alerts.log"
HISTORY = HERE / "history.csv"  # every check: time, site, price, offer, result, link
PRODUCTS = HERE / "products.txt"  # one product per line: <link or name> | hour|day
INTERVALS = {"hour": 3600, "day": 86400}
SLACK = 300  # run a few minutes early so an hourly scheduler never skips a slot
MAX_OTHER_PAGES = 8
COUNTRY_CURRENCY = {"AU": "AUD", "IN": "INR", "US": "USD", "GB": "GBP", "NZ": "NZD", "CA": "CAD"}
DDG_REGION = {"AU": "au-en", "IN": "in-en", "US": "us-en", "GB": "uk-en", "NZ": "nz-en", "CA": "ca-en"}
DEAL_FEEDS = {"AU": "https://www.ozbargain.com.au"}
APPLE_WORDS = r"apple|iphone|ipad|airpods?|macbook|imac|mac ?mini|mac ?studio|homepod|airtag|apple ?watch|apple ?tv"
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
    CREATE TABLE IF NOT EXISTS seen(watch TEXT, guid TEXT, PRIMARY KEY(watch, guid));
    CREATE TABLE IF NOT EXISTS checks(
        id INTEGER PRIMARY KEY, product_id INTEGER, ts TEXT, site TEXT, url TEXT, status TEXT,
        price REAL, currency TEXT, offer TEXT, was REAL, page_title TEXT, source TEXT);
    CREATE TABLE IF NOT EXISTS products(
        id INTEGER PRIMARY KEY, query TEXT, url TEXT, name TEXT,
        every TEXT DEFAULT 'day', last_checked REAL DEFAULT 0,
        last_price REAL, last_offer TEXT, last_alert TEXT, added TEXT);
    CREATE TABLE IF NOT EXISTS prices(
        id INTEGER PRIMARY KEY, product_id INTEGER, ts TEXT, store TEXT, url TEXT,
        price REAL, currency TEXT, offer TEXT, source TEXT);
    """)
    return conn


def country():
    return (os.environ.get("COUNTRY") or load_config().get("country") or "").upper() or None


def load_config():
    try:
        return json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return {}


# ---------- fetching / parsing ----------
FEED_HEADERS = {"User-Agent": "price-tracker/1.0 (personal RSS reader)"}


def fetch(url, data=None, headers=None):
    try:
        r = (requests.post(url, data=data, headers=headers or HEADERS, timeout=25) if data
             else requests.get(url, headers=headers or HEADERS, timeout=25))
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
GENERIC = {"price", "prices", "buy", "cheap", "best", "deal", "deals", "sale", "online", "australia", "with", "and",
           "the", "for", "new"}


def relevant(query, title, url):
    """Drop junk search results whose title/link don't mention the product."""
    key = tokens(query) - GENERIC
    if not key:
        return True
    hit = key & (tokens(title) | tokens(urllib.parse.unquote(url).replace("-", " ").replace("_", " ")))
    return len(hit) >= min(2, len(key))


def search(query, n=10):
    found = []  # (url, title)
    c = country()
    ddg = {"q": query}
    if c in DDG_REGION:
        ddg["kl"] = DDG_REGION[c]
    html = fetch("https://html.duckduckgo.com/html/", data=ddg)
    if html:
        for a in BeautifulSoup(html, "html.parser").select("a.result__a"):
            href = a.get("href", "")
            if "uddg=" in href:
                href = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)["uddg"][0]
            if href.startswith("http") and "duckduckgo.com" not in href:
                found.append((href, a.get_text(" ", strip=True)))
    if not found:
        region = f"&cc={c}&mkt=en-{c}&setlang=en-{c}" if c else ""
        html = fetch("https://www.bing.com/search?q=" + urllib.parse.quote(query) + region)
        if html:
            for a in BeautifulSoup(html, "html.parser").select("li.b_algo h2 a"):
                href = bing_target(a.get("href", ""))
                if href.startswith("http"):
                    found.append((href, a.get_text(" ", strip=True)))
    seen, out = set(), []
    for u, title in found:
        if u not in seen and relevant(query, title, u):
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
    log = []  # every site looked at in this check, saved to the checks table

    def note(url, status, info=None, source="other"):
        info = info or {}
        log.append((url, status, info.get("price"), info.get("currency"), info.get("offer"),
                    info.get("was"), (info.get("name") or "")[:200], source))

    if p["url"]:
        html = fetch(p["url"])
        info = extract(html) if html else {}
        if info.get("name") and not p["name"]:
            name = info["name"]
        if info.get("price"):
            orig = {"store": domain(p["url"]), "url": p["url"], "price": info["price"],
                    "currency": info.get("currency"), "offer": info.get("offer"), "source": "original"}
            results.append(orig)
        note(p["url"], "price found" if orig else ("no price on page" if html else "blocked / unreachable"),
             info, "original")
    tried = 0
    queries = [short_name(name) + " price", short_name(name, 5) + " price"]
    if not re.match(r"https?://", p["query"] or ""):
        queries.insert(0, p["query"] + " price")
    urls = []
    for q in dict.fromkeys(queries):
        urls += [u for u in search(q) if u not in urls]
    if not urls:
        note(None, "no search results")
    for u in urls:
        if p["url"] and domain(u) == domain(p["url"]):
            continue
        if is_listing(u):
            note(u, "skipped: search/category page")
            continue
        if tried >= MAX_OTHER_PAGES:
            break
        tried += 1
        html = fetch(u)
        if not html:
            note(u, "blocked / unreachable")
            continue
        info = extract(html)
        want = (orig and orig["currency"]) or COUNTRY_CURRENCY.get(country())
        if not info.get("price"):
            note(u, "no price on page", info)
        elif is_listing(u, info.get("name")):
            note(u, "skipped: search/category page", info)
        elif not same_product(short_name(name, 12), info.get("name")):
            note(u, "different product", info)
        elif want and info.get("currency") and info["currency"] != want:
            note(u, f"other currency ({info['currency']})", info)
        else:
            note(u, "price found", info)
            results.append({"store": domain(u), "url": u, "price": info["price"], "currency": info.get("currency"),
                            "offer": info.get("offer"), "source": "other"})
    save_checks(conn, p["id"], now, log)

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
            url = q if re.match(r"https?://", q) and not is_watch(q) else None
            conn.execute("INSERT INTO products(query,url,every,added) VALUES(?,?,?,?)",
                         (q, url, every, datetime.now().isoformat(timespec="seconds")))
        elif have[q]["every"] != every:
            conn.execute("UPDATE products SET every=? WHERE id=?", (every, have[q]["id"]))
    for q, r in have.items():
        if q not in wanted:
            conn.execute("DELETE FROM prices WHERE product_id=?", (r["id"],))
            conn.execute("DELETE FROM checks WHERE product_id=?", (r["id"],))
            conn.execute("DELETE FROM products WHERE id=?", (r["id"],))
    conn.commit()


def write_products(conn):
    rows = conn.execute("SELECT query, every FROM products ORDER BY id").fetchall()
    head = "# One product per line: <link or name> | hour   (or | day)\n"
    PRODUCTS.write_text(head + "".join(f"{r['query']} | {r['every']}\n" for r in rows))


def is_watch(query):
    return bool(re.match(r"\s*watch\s*:", query or "", re.I))


def watch_pattern(keyword):
    k = keyword.strip().lower()
    body = APPLE_WORDS if k == "apple" else re.escape(k).replace(r"\ ", r"\s*")
    return re.compile(r"\b(?:" + body + r")\b", re.I)


def parse_feed(xml_text):
    """OzBargain-style RSS -> [{guid, title, link, store_url, expired}]."""
    ns = {"ozb": "https://www.ozbargain.com.au"}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    items = []
    for it in root.iter("item"):
        meta = it.find("ozb:meta", ns)
        msg = it.find("ozb:title-msg", ns)
        items.append({
            "guid": (it.findtext("guid") or it.findtext("link") or "").strip(),
            "title": (it.findtext("title") or "").strip(),
            "link": (it.findtext("link") or "").strip(),
            "store_url": meta.get("url") if meta is not None else None,
            "expired": msg is not None and msg.get("type") in ("expired", "out of stock"),
            "cats": " ".join(c.text or "" for c in it.findall("category")),
        })
    return items


def deal_store(title, url):
    m = re.search(r"@\s*([^@]+?)\s*$", title)
    return m.group(1) if m else domain(url)


def save_checks(conn, pid, ts, log):
    conn.executemany("INSERT INTO checks(product_id,ts,site,url,status,price,currency,offer,was,page_title,source)"
                     " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     [(pid, ts, domain(u) if u else None, u, *rest) for u, *rest in log])


def export_history(conn, limit=3000):
    """history.csv: newest first, one row per site per check (GitHub shows it as a table)."""
    rows = conn.execute("""SELECT c.ts, COALESCE(p.name, p.query) AS product, c.site, c.price, c.currency, c.was,
                           c.offer, c.status, c.source, c.page_title, c.url
                           FROM checks c LEFT JOIN products p ON p.id = c.product_id
                           ORDER BY c.ts DESC, c.id DESC LIMIT ?""", (limit,)).fetchall()
    with open(HISTORY, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "product", "site", "price", "currency", "was", "offer / deal", "result", "type",
                    "page title", "link"])
        for r in rows:
            w.writerow([r["ts"], r["product"], r["site"] or "", "" if r["price"] is None else f"{r['price']:.2f}",
                        r["currency"] or "", "" if r["was"] is None else f"{r['was']:.2f}", r["offer"] or "",
                        r["status"], r["source"], r["page_title"] or "", r["url"] or ""])


def check_watch(conn, p):
    """Alert on every new deal matching the keyword (e.g. 'watch: apple')."""
    keyword = p["query"].split(":", 1)[1].strip()
    base = DEAL_FEEDS.get(country() or "AU", DEAL_FEEDS["AU"])
    slug = re.sub(r"[^a-z0-9]+", "-", keyword.lower()).strip("-")
    pat = watch_pattern(keyword)
    items, seen_guids = [], set()
    for feed in (f"{base}/brand/{slug}/feed", f"{base}/deals/feed"):
        for it in parse_feed(fetch(feed, headers=FEED_HEADERS) or ""):
            if it["guid"] and it["guid"] not in seen_guids and pat.search(it["title"] + " " + it["cats"]):
                seen_guids.add(it["guid"])
                items.append(it)
    first = conn.execute("SELECT COUNT(*) FROM seen WHERE watch=?", (p["query"],)).fetchone()[0] == 0
    new = [it for it in items if not it["expired"] and not conn.execute(
        "SELECT 1 FROM seen WHERE watch=? AND guid=?", (p["query"], it["guid"])).fetchone()]
    now = datetime.now().isoformat(timespec="seconds")
    new_guids = {it["guid"] for it in new}
    log = []
    for it in items:
        link = it["store_url"] or it["link"]
        price, _ = parse_price((re.search(r"\$\s?[\d,]+(?:\.\d+)?", it["title"]) or [None])[0])
        status = "new deal" if it["guid"] in new_guids else ("expired deal" if it["expired"] else "deal already seen")
        log.append((link, status, price, COUNTRY_CURRENCY.get(country() or "AU"), it["title"], None,
                    it["title"][:200], "deal"))
    if not items:
        log.append((None, "no matching deals in feed", None, None, None, None, "", "deal"))
    save_checks(conn, p["id"], now, log)
    alerts = []
    for i, it in enumerate(new):
        link = it["store_url"] or it["link"]
        price, _ = parse_price((re.search(r"(?:A?\$|AU\$)\s?[\d,]+(?:\.\d+)?", it["title"]) or [None])[0])
        store = deal_store(it["title"], link)
        conn.execute("INSERT INTO prices(product_id,ts,store,url,price,currency,offer,source) VALUES(?,?,?,?,?,?,?,?)",
                     (p["id"], now, store, link, price, COUNTRY_CURRENCY.get(country() or "AU"), it["title"], "deal"))
        if not first or i < 3:  # first run: show the 3 newest, quietly remember the rest
            alerts.append((it["title"], link))
    for it in items:
        conn.execute("INSERT OR IGNORE INTO seen(watch,guid) VALUES(?,?)", (p["query"], it["guid"]))
    for msg, link in alerts:
        notify(f"{keyword.title()} deal", msg, link)
    conn.execute("UPDATE products SET name=?, last_checked=? WHERE id=?",
                 (f"Watch: {keyword}", time.time(), p["id"]))
    conn.commit()
    results = [{"store": deal_store(it["title"], it["store_url"] or it["link"]), "url": it["store_url"] or it["link"],
                "price": parse_price((re.search(r"\$\s?[\d,]+(?:\.\d+)?", it["title"]) or [None])[0])[0] or 0,
                "currency": COUNTRY_CURRENCY.get(country() or "AU"), "offer": it["title"], "source": "deal"}
               for it in new]
    return results, alerts


def due(p, now=None):
    return (now or time.time()) - (p["last_checked"] or 0) >= INTERVALS[p["every"]] - SLACK


# ---------- CLI ----------
def cmd_add(conn, a):
    target = a.target.strip()
    url = target if re.match(r"https?://", target) else None
    if not url and not is_watch(target):
        for u in search(target + " price"):
            if is_listing(u):
                continue
            html = fetch(u)
            info = extract(html) if html else {}
            if info.get("price") and same_product(target, info.get("name")):
                url = u
                break
    if is_watch(target):
        url = None
    cur = conn.execute("INSERT INTO products(query,url,every,added) VALUES(?,?,?,?)",
                       (target, url, a.every, datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    write_products(conn)
    p = conn.execute("SELECT * FROM products WHERE id=?", (cur.lastrowid,)).fetchone()
    results, _ = (check_watch if is_watch(target) else check)(conn, p)
    p = conn.execute("SELECT * FROM products WHERE id=?", (cur.lastrowid,)).fetchone()
    export_history(conn)
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
    rows = conn.execute("SELECT * FROM checks WHERE product_id=? ORDER BY ts, id", (a.id,)).fetchall()
    for r in rows:
        price = f"{r['currency'] or '':>3} {r['price']:>10,.2f}" if r["price"] is not None else " " * 14
        print(f"{r['ts']}  {price}  {(r['site'] or '-'):<25} {r['status']:<28} {r['offer'] or ''}")
    if not rows:
        print("no history")


def cmd_check(conn, a):
    sync(conn)
    for p in conn.execute("SELECT * FROM products").fetchall():
        if a.all or due(p):
            try:
                results, alerts = (check_watch if is_watch(p["query"]) else check)(conn, p)
                low = min(results, key=lambda r: r["price"]) if results else None
                best = "%s %.2f at %s" % (low["currency"] or "", low["price"], low["store"]) if low else "no price"
                print(f"#{p['id']} {short_name(p['name'] or p['query'], 6)}: {best}, {len(alerts)} alert(s)")
            except Exception as e:
                print(f"#{p['id']} failed: {e}", file=sys.stderr)
    export_history(conn)


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
        conn.execute("DELETE FROM products WHERE id=?", (a.id,)); conn.execute("DELETE FROM prices WHERE product_id=?", (a.id,)); conn.execute("DELETE FROM checks WHERE product_id=?", (a.id,))
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
