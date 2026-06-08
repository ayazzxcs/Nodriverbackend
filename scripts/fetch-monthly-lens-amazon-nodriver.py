#!/usr/bin/env python3
import asyncio
import json
import math
import os
import random
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus, urlparse, urlunparse, parse_qs

import nodriver as uc

PRODUCTS_PATH = os.getenv("PRODUCTS_PATH", "products.json")
AMAZON_PRODUCTS_PATH = os.getenv("AMAZON_PRODUCTS_PATH", "amazon-products.json")

MAX_PRODUCTS = int(os.getenv("MONTHLY_LENS_MAX_PRODUCTS", "50"))
START_INDEX = int(os.getenv("MONTHLY_LENS_START_INDEX", "0"))
PRODUCTS_PER_PROXY = int(os.getenv("NODRIVER_PRODUCTS_PER_PROXY", "5"))
DELAY_MS = int(os.getenv("NODRIVER_DELAY_MS", "2500"))
AMAZON_DELAY_MS = int(os.getenv("AMAZON_PAGE_DELAY_MS", "3500"))
MIN_TITLE_MATCH = int(os.getenv("AMAZON_MIN_TITLE_MATCH", "15"))

MATCHES_PATH = "lens-amazon-matches.json"
FAILURES_PATH = "lens-amazon-failures.json"
META_PATH = "lens-amazon-meta.json"

STOP_WORDS = {
    "the", "and", "for", "with", "from", "new", "hot", "sale", "best", "top",
    "high", "quality", "product", "products", "dropshipping", "wholesale", "supplier"
}


def read_json(path, default):
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def safe_url(value):
    try:
        value = str(value or "").strip()
        u = urlparse(value)
        if u.scheme in ("http", "https") and u.netloc:
            return value
    except Exception:
        pass
    return ""


def product_name(p):
    raw = p.get("raw") or {}
    return str(raw.get("productNameEn") or p.get("productNameEn") or p.get("name") or p.get("productName") or "").strip()


def product_image(p):
    raw = p.get("raw") or {}
    img = (
        p.get("image")
        or p.get("productImage")
        or p.get("bigImage")
        or raw.get("productImage")
        or raw.get("bigImage")
        or raw.get("image")
    )
    if isinstance(img, list):
        img = img[0] if img else ""
    return safe_url(img)


def clean_amazon_url(url):
    try:
        u = urlparse(str(url))
        return urlunparse((u.scheme, u.netloc, u.path, "", "", ""))
    except Exception:
        return str(url)


def is_amazon_url(url):
    try:
        host = urlparse(str(url)).netloc.lower()
        return host == "amazon.com" or host.endswith(".amazon.com") or ".amazon." in host
    except Exception:
        return False


def normalize_text(text):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s-]", " ", str(text or "").lower())).strip()


def tokens(text):
    return [w for w in normalize_text(text).split() if len(w) > 2 and w not in STOP_WORDS]


def title_similarity(a, b):
    a_tokens = tokens(a)
    b_tokens = set(tokens(b))
    if not a_tokens or not b_tokens:
        return 0
    hits = sum(1 for t in a_tokens if t in b_tokens)
    return round(min(100, (hits / len(a_tokens)) * 100))


def normalize_review_count(text):
    if not text:
        return 0
    cleaned = str(text).replace(",", "").strip()
    m = re.search(r"(\d+(?:\.\d+)?)(\s*[km])?", cleaned, re.I)
    if not m:
        return 0
    value = float(m.group(1))
    suffix = (m.group(2) or "").strip().lower()
    if suffix == "k":
        value *= 1000
    if suffix == "m":
        value *= 1000000
    return round(value)


def extract_review_count_from_text(text):
    value = str(text or "")
    patterns = [
        r"(\d[\d,\.]*\s*[km]?)\s+(?:global\s+)?ratings?",
        r"(\d[\d,\.]*\s*[km]?)\s+(?:customer\s+)?reviews?",
        r"ratings?\s*[:\-]?\s*(\d[\d,\.]*\s*[km]?)",
        r"reviews?\s*[:\-]?\s*(\d[\d,\.]*\s*[km]?)",
    ]
    for pat in patterns:
        m = re.search(pat, value, re.I)
        if m:
            return normalize_review_count(m.group(1))
    return 0


def demand_score(rating, ratings_total, is_best_seller, match_score):
    rating_score = min(35, max(0, float(rating or 0) * 7))
    review_score = min(45, math.log10(float(ratings_total or 0) + 1) * 11)
    badge_score = 10 if is_best_seller else 0
    match_bonus = min(10, max(0, match_score - 50) / 5)
    return round(min(100, rating_score + review_score + badge_score + match_bonus))


def get_proxies():
    raw = os.getenv("RESIDENTIAL_PROXIES", "").strip()
    proxies = [line.strip() for line in raw.splitlines() if line.strip()]
    single = os.getenv("RESIDENTIAL_PROXY", "").strip()
    if single and single not in proxies:
        proxies.append(single)
    return proxies


def proxy_for_index(index, proxies):
    if not proxies:
        return None
    slot = max(0, index - START_INDEX) // max(1, PRODUCTS_PER_PROXY)
    return proxies[slot % len(proxies)]


async def sleep_ms(ms):
    await asyncio.sleep(max(0, ms) / 1000)


async def start_browser(proxy=None):
    args = [
        "--disable-dev-shm-usage",
        "--disable-setuid-sandbox",
        "--disable-gpu",
        "--window-size=1365,768",
        "--lang=en-US,en",
    ]
    if proxy:
        args.append(f"--proxy-server={proxy}")

    print("Starting browser with Nodriver latest, headless=False, no_sandbox=True")
    return await uc.start(
        browser_args=args,
        headless=False,
        no_sandbox=True,
    )


async def get_text(page):
    try:
        return await page.evaluate("document.body ? document.body.innerText : ''")
    except Exception:
        return ""


async def get_links(page):
    try:
        return await page.evaluate("""
            Array.from(document.querySelectorAll('a[href]')).map(a => ({
                href: a.href || '',
                text: (a.innerText || a.textContent || '').trim()
            }))
        """)
    except Exception:
        return []


async def verify_proxy(browser):
    try:
        page = await browser.get("https://ipv4.webshare.io/")
        await sleep_ms(2500)
        ip_text = (await get_text(page)).strip()
        print(f"Proxy verification IP/page text: {ip_text[:120]}")
    except Exception as e:
        print(f"Proxy verification failed: {e}")


async def find_first_amazon_with_nodriver_lens(browser, image_url, product_title):
    lens_url = f"https://lens.google.com/uploadbyurl?url={quote_plus(image_url)}"
    page = await browser.get(lens_url)
    await sleep_ms(6000 + random.randint(0, 4000))

    text = await get_text(page)
    if re.search(r"captcha|unusual traffic|verify|not a robot", text, re.I):
        raise RuntimeError("Google Lens captcha/bot page detected")

    try:
        await page.evaluate("window.scrollBy(0, 900)")
        await sleep_ms(2200)
        await page.evaluate("window.scrollBy(0, 1200)")
        await sleep_ms(2200)
    except Exception:
        pass

    links = await get_links(page)
    amazon_links = []
    seen = set()

    for item in links:
        href = item.get("href") or ""
        label = item.get("text") or ""
        candidates = [href]

        try:
            qs = parse_qs(urlparse(href).query)
            for key in ("q", "url"):
                for value in qs.get(key, []):
                    candidates.append(value)
        except Exception:
            pass

        for c in candidates:
            if is_amazon_url(c):
                clean = clean_amazon_url(c)
                if clean not in seen:
                    seen.add(clean)
                    amazon_links.append({"url": clean, "title": label})

    print(f"Nodriver Lens extracted {len(links)} links, {len(amazon_links)} Amazon links")
    if amazon_links:
        print(f"First Amazon candidate: {amazon_links[0]['url']}")
        return amazon_links[0]
    return None


async def safe_eval(page, script, default=""):
    try:
        value = await page.evaluate(script)
        return value if value is not None else default
    except Exception:
        return default


async def scrape_amazon_with_nodriver(browser, amazon_url, cj_name):
    page = await browser.get(amazon_url)
    await sleep_ms(AMAZON_DELAY_MS + random.randint(0, 2500))

    text = await get_text(page)
    if re.search(r"captcha|enter the characters you see below|sorry, we just need to make sure|verify you are human", text, re.I):
        raise RuntimeError("Amazon CAPTCHA/bot check detected")

    try:
        await page.evaluate("window.scrollBy(0, Math.floor(200 + Math.random() * 600))")
        await sleep_ms(1500 + random.randint(0, 1500))
    except Exception:
        pass

    title = await safe_eval(page, """
        (() => {
          const selectors = ['#productTitle','h1 span','h1','meta[name="title"]','meta[property="og:title"]'];
          for (const s of selectors) {
            const el = document.querySelector(s);
            if (!el) continue;
            const value = el.getAttribute('content') || el.innerText || el.textContent || '';
            if (value.trim()) return value.trim();
          }
          return '';
        })()
    """)

    rating_text = await safe_eval(page, """
        (() => {
          const selectors = ['#acrPopover span.a-icon-alt','#acrPopover','span.a-icon-alt','[data-hook="rating-out-of-text"]','meta[name="twitter:data1"]'];
          for (const s of selectors) {
            const el = document.querySelector(s);
            if (!el) continue;
            const value = el.getAttribute('content') || el.getAttribute('title') || el.innerText || el.textContent || '';
            if (value.trim()) return value.trim();
          }
          return '';
        })()
    """)

    m = re.search(r"(\d+(?:\.\d+)?)", str(rating_text))
    rating = float(m.group(1)) if m else 0

    reviews_text = await safe_eval(page, """
        (() => {
          const selectors = ['#acrCustomerReviewText','#acrCustomerReviewLink','[data-hook="total-review-count"]','[data-hook="rating-count"]','a[href*="customerReviews"]','a[href*="product-reviews"]'];
          for (const s of selectors) {
            const el = document.querySelector(s);
            if (!el) continue;
            const value = el.innerText || el.textContent || '';
            if (value.trim()) return value.trim();
          }
          return '';
        })()
    """)

    ratings_total = normalize_review_count(reviews_text) or extract_review_count_from_text(text)

    badge_text = await safe_eval(page, """
        (() => {
          const selectors = ['#zeitgeistBadge_feature_div','.ac-badge-text-primary','.badge-wrapper','#acBadge_feature_div'];
          const parts = [];
          for (const s of selectors) {
            const el = document.querySelector(s);
            if (el) parts.push((el.innerText || el.textContent || '').trim());
          }
          const body = document.body ? document.body.innerText : '';
          if (/Best Seller/i.test(body)) parts.push('Best Seller');
          if (/Amazon\\'?s Choice/i.test(body)) parts.push("Amazon's Choice");
          return parts.filter(Boolean).join(' | ');
        })()
    """)

    is_best_seller = bool(re.search(r"best seller|amazon'?s choice", str(badge_text), re.I))
    match_score = title_similarity(cj_name, title)
    score = demand_score(rating, ratings_total, is_best_seller, match_score)

    print(f'Amazon extracted data: title="{title}" ratingText="{rating_text}" reviewsText="{reviews_text}" rating="{rating}" reviews="{ratings_total}"')

    return {
        "title": title,
        "url": clean_amazon_url(amazon_url),
        "rating": rating or "",
        "ratingsTotal": ratings_total or 0,
        "isBestSeller": is_best_seller,
        "badgeText": badge_text,
        "matchScore": match_score,
        "score": score,
        "source": "nodriver-google-lens-amazon",
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
    }


def already_has_amazon(existing, product):
    pid = str(product.get("id") or "")
    name = product_name(product).lower()
    for a in existing:
        aid = str(a.get("productId") or "")
        aname = str(a.get("productName") or "").lower()
        if pid and aid and pid == aid:
            return True
        if name and aname and name == aname:
            return True
    return False


async def main():
    products = read_json(PRODUCTS_PATH, [])
    existing_amazon = read_json(AMAZON_PRODUCTS_PATH, [])
    amazon_signals = list(existing_amazon)
    matches = []
    failures = []
    proxies = get_proxies()

    print(f"Nodriver-only mode. Products loaded: {len(products)}")
    print(f"Residential proxies loaded: {len(proxies)}")
    print(f"Range: start={START_INDEX}, max={MAX_PRODUCTS}")

    end_index = min(len(products), START_INDEX + MAX_PRODUCTS)
    browser = None
    active_proxy = None

    try:
        for i in range(START_INDEX, end_index):
            p = products[i]
            name = product_name(p)
            image = product_image(p)

            if not name or not image:
                failures.append({"index": i, "name": name, "reason": "missing product name or image"})
                continue

            if already_has_amazon(existing_amazon, p):
                print(f"Skip existing Amazon data: {name}")
                continue

            proxy = proxy_for_index(i, proxies)

            if browser is None or proxy != active_proxy:
                if browser:
                    try:
                        await browser.stop()
                    except Exception:
                        pass

                active_proxy = proxy
                print(f"Starting Nodriver browser with proxy: {'configured' if proxy else 'none'}")
                browser = await start_browser(proxy)
                await verify_proxy(browser)

            print(f"Nodriver Lens {i + 1}/{len(products)}: {name}")

            try:
                found = await find_first_amazon_with_nodriver_lens(browser, image, name)

                if not found:
                    print(f"No Amazon match via Nodriver Lens: {name}")
                    failures.append({
                        "index": i,
                        "name": name,
                        "image": image,
                        "provider": "nodriver_lens",
                        "reason": "no amazon link from nodriver lens"
                    })
                    await sleep_ms(DELAY_MS + random.randint(0, 1500))
                    continue

                print(f"Amazon URL found via Nodriver Lens: {found['url']}")
                print(f"Scraping Amazon page with Nodriver: {found['url']}")

                data = await scrape_amazon_with_nodriver(browser, found["url"], name)

                if not data.get("title") or not data.get("rating"):
                    print(f'Amazon data rejected: missing title/rating | title="{data.get("title")}" rating="{data.get("rating")}" reviews="{data.get("ratingsTotal")}"')
                    failures.append({
                        "index": i,
                        "name": name,
                        "image": image,
                        "provider": "nodriver_lens",
                        "amazonUrl": found["url"],
                        "reason": "amazon page missing title/rating data"
                    })
                    continue

                if int(data.get("matchScore") or 0) < MIN_TITLE_MATCH:
                    print(f'Amazon data rejected: weak title match {data.get("matchScore")}/{MIN_TITLE_MATCH} | CJ="{name}" | Amazon="{data.get("title")}"')
                    failures.append({
                        "index": i,
                        "name": name,
                        "image": image,
                        "provider": "nodriver_lens",
                        "amazonUrl": found["url"],
                        "reason": f'weak amazon title match: {data.get("matchScore")}'
                    })
                    continue

                signal = {
                    "productId": p.get("id"),
                    "keyword": p.get("id") or name,
                    "productName": name,
                    "image": image,
                    "title": data["title"],
                    "asin": "",
                    "score": data["score"],
                    "bestRating": data["rating"],
                    "bestRatingsTotal": data["ratingsTotal"],
                    "bestPrice": "",
                    "position": 1,
                    "isBestSeller": data["isBestSeller"],
                    "badgeText": data["badgeText"],
                    "productUrl": data["url"],
                    "matchScore": data["matchScore"],
                    "matchType": "image",
                    "lensProvider": "nodriver_lens",
                    "source": data["source"],
                    "fetchedAt": data["fetchedAt"],
                }

                amazon_signals.append(signal)
                matches.append({
                    "productId": p.get("id"),
                    "productName": name,
                    "productImage": image,
                    "provider": "nodriver_lens",
                    "amazon": data
                })

                print(f"Matched via Nodriver Lens: {name} -> {data['title']} | rating {data['rating']} | reviews {data['ratingsTotal']} | score {data['score']}")

                if len(matches) % 10 == 0:
                    write_json(AMAZON_PRODUCTS_PATH, amazon_signals)
                    write_json(MATCHES_PATH, matches)
                    write_json(FAILURES_PATH, failures)

                await sleep_ms(DELAY_MS + random.randint(0, 2500))

            except Exception as e:
                print(f"Nodriver product failed for {name}: {e}")
                failures.append({"index": i, "name": name, "image": image, "reason": str(e)})
                await sleep_ms(DELAY_MS + random.randint(0, 1500))

    finally:
        if browser:
            try:
                await browser.stop()
            except Exception:
                pass

    write_json(AMAZON_PRODUCTS_PATH, amazon_signals)
    write_json(MATCHES_PATH, matches)
    write_json(FAILURES_PATH, failures)
    write_json(META_PATH, {
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "mode": "nodriver-only google lens + amazon scraping",
        "startIndex": START_INDEX,
        "maxProducts": MAX_PRODUCTS,
        "matches": len(matches),
        "failures": len(failures),
        "proxyCount": len(proxies),
        "note": "Uses Nodriver for both Google Lens and Amazon. No Lens API providers and no Puppeteer."
    })

    print(f"Nodriver monthly Lens complete. Matches: {len(matches)}, failures: {len(failures)}")


if __name__ == "__main__":
    asyncio.run(main())
