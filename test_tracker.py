import json
import time

import pytest

import tracker as t

ORIG = "https://www.shop-a.com/p/sony-wh-1000xm5"
OTHER = "https://store-b.com/sony-wh1000xm5-headphones"
WRONG = "https://store-c.com/sony-wh-1000xm4"


def ld_page(name, price, cur="USD", was=None):
    offer = {"@type": "Offer", "price": str(price), "priceCurrency": cur}
    if was:
        offer["priceSpecification"] = [{"@type": "UnitPriceSpecification", "priceType":
                                        "https://schema.org/StrikethroughPrice", "price": str(was)}]
    return (f'<html><head><script type="application/ld+json">'
            f'{json.dumps({"@type": "Product", "name": name, "offers": offer})}</script></head><body></body></html>')


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(t, "DB", tmp_path / "p.db")
    monkeypatch.setattr(t, "LOG", tmp_path / "alerts.log")
    monkeypatch.setattr(t, "PRODUCTS", tmp_path / "products.txt")
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    cfg = tmp_path / "c.json"
    cfg.write_text('{"desktop": false}')
    monkeypatch.setattr(t, "CONFIG", cfg)
    pages = {}
    monkeypatch.setattr(t, "fetch", lambda url, data=None: pages.get(url))
    monkeypatch.setattr(t, "search", lambda q, n=10: [ORIG, OTHER, WRONG])
    return pages


def add(conn, every="day"):
    conn.execute("INSERT INTO products(query,url,every) VALUES(?,?,?)", (ORIG, ORIG, every))
    conn.commit()
    return conn.execute("SELECT * FROM products ORDER BY id DESC").fetchone()


def reload(conn, pid):
    return conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()


# --- parsing ---
@pytest.mark.parametrize("raw,val,cur", [
    ("₹1,299.00", 1299.0, "INR"), ("$49.99", 49.99, None), ("A$ 1,049", 1049.0, "AUD"),
    ("1.299,50 €", 1299.5, "EUR"), ("Rs. 15,990", 15990.0, "INR"), ("£7", 7.0, "GBP"),
    ("USD 12.50", 12.5, "USD"), ("free", None, None), (None, None, None),
])
def test_parse_price(raw, val, cur):
    assert t.parse_price(raw) == (val, cur)


def test_extract_jsonld_with_discount():
    info = t.extract(ld_page("Sony WH-1000XM5", 299, was=399))
    assert info["name"] == "Sony WH-1000XM5" and info["price"] == 299 and info["currency"] == "USD"
    assert info["was"] == 399 and "25% off" in info["offer"]


def test_extract_amazon_style():
    html = """<html><body><span id="productTitle"> Echo Dot (5th Gen) </span>
    <div id="corePriceDisplay_desktop_feature_div"><span class="a-price"><span class="a-offscreen">₹4,499.00</span></span>
    <span class="a-price a-text-price"><span class="a-offscreen">₹5,499.00</span></span></div>
    <div class="deal-badge">Limited time deal</div></body></html>"""
    info = t.extract(html)
    assert info["name"] == "Echo Dot (5th Gen)" and info["price"] == 4499 and info["currency"] == "INR"
    assert info["was"] == 5499 and "18% off" in info["offer"] and "Limited time deal" in info["offer"]


def test_extract_meta_and_generic():
    html = """<html><head><meta property="og:title" content="Kettle 1.7L">
    <meta property="product:price:amount" content="39.95"><meta property="product:price:currency" content="AUD">
    </head><body><div class="coupon-box">Coupon: extra 10% off at checkout</div></body></html>"""
    info = t.extract(html)
    assert (info["name"], info["price"], info["currency"]) == ("Kettle 1.7L", 39.95, "AUD")
    assert "10% off" in info["offer"]
    g = t.extract('<h1>Lamp</h1><span class="old-price">$30</span><span class="product-price">$20.00</span>')
    assert g["price"] == 20 and g["was"] == 30


def test_extract_no_price():
    assert not t.extract("<html><title>Nothing</title></html>").get("price")


def test_same_product_rejects_other_model():
    assert t.same_product("Sony WH-1000XM5 Headphones", "SONY WH 1000XM5 wireless headphones black")
    assert not t.same_product("Sony WH-1000XM5 Headphones", "Sony WH-1000XM4 Headphones")
    assert not t.same_product("Echo Dot 5th Gen", "Garden hose 20m")


def test_due_per_product_interval():
    now = time.time()
    assert t.due({"every": "hour", "last_checked": now - 3600}, now)
    assert not t.due({"every": "hour", "last_checked": now - 600}, now)
    assert not t.due({"every": "day", "last_checked": now - 7200}, now)
    assert t.due({"every": "day", "last_checked": now - 86400}, now)
    assert t.due({"every": "day", "last_checked": 0}, now)


# --- check flow ---
def test_history_saved_and_cheaper_elsewhere_alert(env):
    env[ORIG] = ld_page("Sony WH-1000XM5", 399)
    env[OTHER] = ld_page("Sony WH-1000XM5 Headphones", 349)
    env[WRONG] = ld_page("Sony WH-1000XM4", 199)  # different model: must be ignored
    conn = t.connect()
    p = add(conn)
    results, alerts = t.check(conn, p)
    assert {r["store"] for r in results} == {"shop-a.com", "store-b.com"}
    assert len(conn.execute("SELECT * FROM prices").fetchall()) == 2
    msgs = " ".join(m for m, _ in alerts)
    assert "Cheaper: USD 349.00 at store-b.com" in msgs and OTHER in [u for _, u in alerts]

    # same prices next run: no repeat alert, history keeps growing
    _, alerts = t.check(conn, reload(conn, p["id"]))
    assert alerts == []
    assert len(conn.execute("SELECT * FROM prices").fetchall()) == 4


def test_drop_on_original_and_offer_alert(env):
    env[ORIG] = ld_page("Sony WH-1000XM5", 399)
    conn = t.connect()
    p = add(conn)
    _, alerts = t.check(conn, p)
    assert alerts == []  # first check is just the baseline
    env[ORIG] = ld_page("Sony WH-1000XM5", 329, was=399)
    _, alerts = t.check(conn, reload(conn, p["id"]))
    msgs = " ".join(m for m, _ in alerts)
    assert "Price drop: USD 329.00 at shop-a.com (was 399.00)" in msgs
    assert "Offer at shop-a.com: 18% off" in msgs
    assert all(u == ORIG for _, u in alerts)
    assert "Price drop" in (t.LOG.read_text())


def test_other_currency_ignored(env):
    env[ORIG] = ld_page("Sony WH-1000XM5", 399, "AUD")
    env[OTHER] = ld_page("Sony WH-1000XM5", 300, "USD")
    conn = t.connect()
    results, alerts = t.check(conn, add(conn))
    assert [r["store"] for r in results] == ["shop-a.com"] and alerts == []


def test_blocked_page_does_not_crash(env):
    conn = t.connect()
    results, alerts = t.check(conn, add(conn))
    assert results == [] and alerts == []
    assert reload(conn, 1)["last_checked"] > 0


def test_cli_add_by_name_list_history_every_remove(env, capsys, monkeypatch):
    env[ORIG] = ld_page("Sony WH-1000XM5", 399)
    env[OTHER] = ld_page("Sony WH-1000XM5", 349)
    t.main(["add", "Sony WH-1000XM5", "--every", "hour"])
    out = capsys.readouterr().out
    assert "Added #1" in out and "every hour" in out and ORIG in out
    t.main(["list"]); assert "lowest 349.00 at store-b.com" in capsys.readouterr().out
    t.main(["history", "1"]); assert "store-b.com" in capsys.readouterr().out
    t.main(["every", "1", "day"]); assert "every day" in capsys.readouterr().out
    t.main(["check"])  # just checked, not due: no new rows
    assert len(t.connect().execute("SELECT * FROM prices").fetchall()) == 2
    t.main(["check", "--all"])
    assert len(t.connect().execute("SELECT * FROM prices").fetchall()) == 4
    t.main(["remove", "1"]); t.main(["list"])
    assert "#1" not in capsys.readouterr().out.split("removed #1")[-1]


# --- live-site lessons ---
def test_bot_check_pages_are_treated_as_no_data():
    assert t.blocked("<html><title>Amazon.com</title>Click the button below to continue shopping</html>")
    assert t.blocked("<title>Pardon Our Interruption</title>")
    assert not t.blocked(ld_page("Kettle", 20))


def test_bing_redirect_links_are_decoded():
    import base64
    real = "https://www.umart.com.au/product/apple-airpods-pro-2"
    enc = base64.urlsafe_b64encode(real.encode()).decode().rstrip("=")
    assert t.bing_target(f"https://www.bing.com/ck/a?!&&p=abc&u=a1{enc}&ntb=1") == real
    assert t.bing_target(real) == real


def test_search_and_category_pages_are_skipped(env):
    assert t.is_listing("https://www.amazon.com.au/apple-airpods-pro-2/s?k=apple+airpods+pro+2")
    assert t.is_listing("https://reebelo.com.au/collections/apple-airpods-pro-2")
    assert t.is_listing("https://www.kogan.com/au/c/airpods-pro-2/")
    assert t.is_listing("https://x.com/p/1", '1-48 of 776 results for "airpods"')
    assert not t.is_listing("https://shop.com/collections/audio/products/airpods-pro-2")
    assert not t.is_listing("https://www.umart.com.au/product/apple-airpods-pro-2nd-gen")
    listing = "https://store-d.com/search?q=sony+wh-1000xm5"
    env[ORIG] = ld_page("Sony WH-1000XM5", 399)
    env[listing] = ld_page("Sony WH-1000XM5", 99)
    t.search = lambda q, n=10: [listing]
    conn = t.connect()
    results, alerts = t.check(conn, add(conn))
    assert [r["store"] for r in results] == ["shop-a.com"] and alerts == []


def test_was_price_ignores_related_products():
    html = """<h1>AirPods Pro 2</h1><span class="price">$359.99</span><span class="price-original">$359.99</span>
    <div class="related"><span class="original-price">$899.99</span></div>"""
    info = t.extract(html)
    assert info["price"] == 359.99 and not info.get("was") and not info["offer"]


def test_unreadable_link_searched_by_its_words(env, monkeypatch):
    url = "https://www.umart.com.au/product/apple-airpods-pro-2nd-gen-wireless-mtjv3zaa-68009"
    assert t.url_name(url) == "apple airpods pro 2nd gen wireless mtjv3zaa"
    seen = []
    monkeypatch.setattr(t, "search", lambda q, n=10: seen.append(q) or [])
    conn = t.connect()
    conn.execute("INSERT INTO products(query,url,every) VALUES(?,?,?)", (url, url, "day"))
    t.check(conn, conn.execute("SELECT * FROM products").fetchone())
    assert seen and seen[0].startswith("apple airpods pro")


def test_search_name_keeps_generation_in_brackets():
    n = t.short_name("AirPods Pro (2nd Generation) With MagSafe Case | Costco")
    assert "2nd" in n and "Costco" not in n


# --- products.txt (GitHub Actions) ---
def test_parse_products():
    text = "# comment\n\nhttps://a.com/x | hour\nApple AirPods Pro 2|DAY\nKettle 1.7L\nTV | weekly\n"
    assert t.parse_products(text) == [("https://a.com/x", "hour"), ("Apple AirPods Pro 2", "day"),
                                      ("Kettle 1.7L", "day"), ("TV | weekly", "day")]


def test_sync_adds_updates_and_removes(env):
    conn = t.connect()
    t.PRODUCTS.write_text(f"{ORIG} | hour\nSony WH-1000XM5\n")
    t.sync(conn)
    rows = {r["query"]: r for r in conn.execute("SELECT * FROM products")}
    assert rows[ORIG]["url"] == ORIG and rows[ORIG]["every"] == "hour"
    assert rows["Sony WH-1000XM5"]["url"] is None and rows["Sony WH-1000XM5"]["every"] == "day"
    conn.execute("INSERT INTO prices(product_id,price) VALUES(?,1)", (rows[ORIG]["id"],)); conn.commit()
    t.PRODUCTS.write_text("Sony WH-1000XM5 | hour\n")
    t.sync(conn)
    rows = {r["query"]: r for r in conn.execute("SELECT * FROM products")}
    assert list(rows) == ["Sony WH-1000XM5"] and rows["Sony WH-1000XM5"]["every"] == "hour"
    assert conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0] == 0


def test_check_reads_products_file(env, capsys):
    env[ORIG] = ld_page("Sony WH-1000XM5", 399)
    env[OTHER] = ld_page("Sony WH-1000XM5", 349)
    t.PRODUCTS.write_text(f"{ORIG} | hour\n")
    t.main(["check"])
    out = capsys.readouterr().out
    assert "USD 349.00 at store-b.com, 1 alert(s)" in out and "Cheaper" in t.LOG.read_text()


def test_cli_keeps_products_file_in_sync(env, capsys):
    env[ORIG] = ld_page("Sony WH-1000XM5", 399)
    t.main(["add", ORIG, "--every", "hour"])
    assert t.parse_products(t.PRODUCTS.read_text()) == [(ORIG, "hour")]
    t.main(["every", "1", "day"])
    assert t.parse_products(t.PRODUCTS.read_text()) == [(ORIG, "day")]
    t.main(["remove", "1"])
    assert t.parse_products(t.PRODUCTS.read_text()) == []


def test_ntfy_topic_from_env(env, monkeypatch):
    sent = []
    monkeypatch.setattr(t.requests, "post", lambda url, **k: sent.append((url, k["headers"])))
    monkeypatch.setenv("NTFY_TOPIC", "my-topic")
    t.notify("Kettle", "Cheaper: AUD 30.00 at b.com", "https://b.com/k")
    assert sent[0][0] == "https://ntfy.sh/my-topic" and sent[0][1]["Click"] == "https://b.com/k"


def test_workflow_runs_hourly_and_saves_history():
    wf = (t.HERE / ".github/workflows/track.yml").read_text()
    assert 'cron: "17 * * * *"' in wf and "python tracker.py check" in wf
    assert "secrets.NTFY_TOPIC" in wf and "git push" in wf and "prices.db" in wf
