#!/usr/bin/env python3
"""
Nodriver-only Lens + Amazon enrichment for DropTrend test mode.

Keeps same output files/fields as old JS pipeline:
- amazon-products.json
- lens-amazon-matches.json
- lens-amazon-failures.json
- lens-amazon-meta.json

Env:
RESIDENTIAL_PROXIES = one proxy per line, e.g. http://user:pass@host:port
MONTHLY_LENS_MAX_PRODUCTS=50
MONTHLY_LENS_START_INDEX=0
NODRIVER_PRODUCTS_PER_PROXY=50
NODRIVER_DELAY_MS=2500
AMAZON_PAGE_DELAY_MS=3500
AMAZON_MIN_TITLE_MATCH=15
"""
import asyncio, json, math, os, random, re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus, urlparse, parse_qs, urlunparse
import nodriver as uc

PRODUCTS_PATH=os.getenv('PRODUCTS_PATH','products.json')
AMAZON_PRODUCTS_PATH=os.getenv('AMAZON_PRODUCTS_PATH','amazon-products.json')
MAX_PRODUCTS=int(os.getenv('MONTHLY_LENS_MAX_PRODUCTS','50'))
START_INDEX=int(os.getenv('MONTHLY_LENS_START_INDEX','0'))
PRODUCTS_PER_PROXY=int(os.getenv('NODRIVER_PRODUCTS_PER_PROXY','50'))
DELAY_MS=int(os.getenv('NODRIVER_DELAY_MS','2500'))
AMAZON_DELAY_MS=int(os.getenv('AMAZON_PAGE_DELAY_MS','3500'))
MIN_TITLE_MATCH=int(os.getenv('AMAZON_MIN_TITLE_MATCH','15'))
STOP={'the','and','for','with','from','new','hot','sale','best','top','high','quality','product','products','dropshipping','wholesale','supplier'}

def read_json(path, default):
    p=Path(path)
    if not p.exists(): return default
    try: return json.loads(p.read_text(encoding='utf-8'))
    except Exception: return default

def write_json(path, data):
    Path(path).write_text(json.dumps(data,indent=2,ensure_ascii=False),encoding='utf-8')

def safe_url(v):
    try:
        s=str(v or '').strip(); u=urlparse(s)
        return s if u.scheme in ('http','https') and u.netloc else ''
    except Exception: return ''

def product_name(p):
    r=p.get('raw') or {}
    return str(r.get('productNameEn') or p.get('productNameEn') or p.get('name') or p.get('productName') or '').strip()

def product_image(p):
    r=p.get('raw') or {}
    img=p.get('image') or p.get('productImage') or p.get('bigImage') or r.get('productImage') or r.get('bigImage') or r.get('image')
    if isinstance(img,list): img=img[0] if img else ''
    return safe_url(img)

def is_amazon_url(url):
    try:
        h=urlparse(str(url)).netloc.lower()
        return h=='amazon.com' or h.endswith('.amazon.com') or '.amazon.' in h
    except Exception: return False

def clean_amazon_url(url):
    try:
        u=urlparse(str(url)); return urlunparse((u.scheme,u.netloc,u.path,'','',''))
    except Exception: return str(url)

def norm_text(t): return re.sub(r'\s+',' ',re.sub(r'[^a-z0-9\s-]',' ',str(t or '').lower())).strip()
def toks(t): return [w for w in norm_text(t).split() if len(w)>2 and w not in STOP]
def title_similarity(a,b):
    at=toks(a); bt=set(toks(b))
    if not at or not bt: return 0
    return round(min(100, sum(1 for t in at if t in bt)/len(at)*100))

def normalize_review_count(text):
    if not text: return 0
    m=re.search(r'(\d+(?:\.\d+)?)(\s*[km])?',str(text).replace(',',''),re.I)
    if not m: return 0
    v=float(m.group(1)); s=(m.group(2) or '').strip().lower()
    if s=='k': v*=1000
    if s=='m': v*=1000000
    return round(v)

def extract_review_count_from_text(text):
    for pat in [r'(\d[\d,\.]*\s*[km]?)\s+(?:global\s+)?ratings?',r'(\d[\d,\.]*\s*[km]?)\s+(?:customer\s+)?reviews?',r'ratings?\s*[:\-]?\s*(\d[\d,\.]*\s*[km]?)',r'reviews?\s*[:\-]?\s*(\d[\d,\.]*\s*[km]?)']:
        m=re.search(pat,str(text or ''),re.I)
        if m: return normalize_review_count(m.group(1))
    return 0

def demand_score(rating,ratings_total,is_best_seller,match_score):
    rating_score=min(35,max(0,float(rating or 0)*7))
    review_score=min(45,math.log10(float(ratings_total or 0)+1)*11)
    badge_score=10 if is_best_seller else 0
    match_bonus=min(10,max(0,match_score-50)/5)
    return round(min(100,rating_score+review_score+badge_score+match_bonus))

def proxies():
    raw=os.getenv('RESIDENTIAL_PROXIES','').strip()
    out=[x.strip() for x in raw.splitlines() if x.strip()]
    single=os.getenv('RESIDENTIAL_PROXY','').strip()
    if single: out.append(single)
    return out

def proxy_for_index(i, ps):
    if not ps: return None
    slot=max(0,i-START_INDEX)//max(1,PRODUCTS_PER_PROXY)
    return ps[slot%len(ps)]

async def sleep_ms(ms): await asyncio.sleep(max(0,ms)/1000)
async def start_browser(proxy=None):
    args=['--no-sandbox','--disable-dev-shm-usage','--window-size=1365,768','--lang=en-US,en']
    if proxy: args.append(f'--proxy-server={proxy}')
    return await uc.start(browser_args=args, headless=True)
async def body_text(page):
    try: return await page.evaluate("document.body ? document.body.innerText : ''")
    except Exception: return ''
async def links(page):
    try:
        return await page.evaluate("""Array.from(document.querySelectorAll('a[href]')).map(a=>({href:a.href||'',text:(a.innerText||a.textContent||'').trim()}))""")
    except Exception: return []
async def safe_eval(page, js, default=''):
    try:
        v=await page.evaluate(js); return v if v is not None else default
    except Exception: return default

async def find_first_amazon_lens(browser, image_url, name):
    page=await browser.get('https://lens.google.com/uploadbyurl?url='+quote_plus(image_url))
    await sleep_ms(6000+random.randint(0,3000))
    txt=await body_text(page)
    if re.search(r'captcha|unusual traffic|verify|not a robot',txt,re.I):
        raise RuntimeError('Google Lens captcha/bot page detected')
    for y in (900,1200):
        try: await page.evaluate(f'window.scrollBy(0,{y})')
        except Exception: pass
        await sleep_ms(1800)
    items=await links(page)
    out=[]; seen=set()
    for it in items:
        href=it.get('href') or ''; label=it.get('text') or ''
        cands=[href]
        try:
            q=parse_qs(urlparse(href).query)
            for k in ('q','url'):
                for v in q.get(k,[]): cands.append(v)
        except Exception: pass
        for c in cands:
            if is_amazon_url(c):
                clean=clean_amazon_url(c)
                if clean not in seen:
                    seen.add(clean); out.append({'url':clean,'title':label})
    print(f'Nodriver Lens extracted {len(items)} links, {len(out)} Amazon links')
    if out: print(f'First Amazon candidate: {out[0]["url"]}')
    return out[0] if out else None

async def scrape_amazon(browser, amazon_url, cj_name):
    page=await browser.get(amazon_url)
    await sleep_ms(AMAZON_DELAY_MS+random.randint(0,2500))
    txt=await body_text(page)
    if re.search(r'captcha|enter the characters you see below|sorry, we just need to make sure|verify you are human',txt,re.I):
        raise RuntimeError('Amazon CAPTCHA/bot check detected')
    try: await page.evaluate('window.scrollBy(0, Math.floor(200 + Math.random() * 600))')
    except Exception: pass
    await sleep_ms(1200+random.randint(0,1500))
    title=await safe_eval(page,"""(()=>{for(const s of ['#productTitle','h1 span','h1','meta[name="title"]','meta[property="og:title"]']){const e=document.querySelector(s); if(e){const v=e.getAttribute('content')||e.innerText||e.textContent||''; if(v.trim()) return v.trim();}} return '';})()""")
    rating_text=await safe_eval(page,"""(()=>{for(const s of ['#acrPopover span.a-icon-alt','#acrPopover','span.a-icon-alt','[data-hook="rating-out-of-text"]','meta[name="twitter:data1"]']){const e=document.querySelector(s); if(e){const v=e.getAttribute('content')||e.getAttribute('title')||e.innerText||e.textContent||''; if(v.trim()) return v.trim();}} return '';})()""")
    m=re.search(r'(\d+(?:\.\d+)?)',str(rating_text)); rating=float(m.group(1)) if m else 0
    reviews_text=await safe_eval(page,"""(()=>{for(const s of ['#acrCustomerReviewText','#acrCustomerReviewLink','[data-hook="total-review-count"]','[data-hook="rating-count"]','a[href*="customerReviews"]','a[href*="product-reviews"]']){const e=document.querySelector(s); if(e){const v=e.innerText||e.textContent||''; if(v.trim()) return v.trim();}} return '';})()""")
    ratings_total=normalize_review_count(reviews_text) or extract_review_count_from_text(txt)
    badge_text=await safe_eval(page,"""(()=>{const parts=[]; for(const s of ['#zeitgeistBadge_feature_div','.ac-badge-text-primary','.badge-wrapper','#acBadge_feature_div']){const e=document.querySelector(s); if(e) parts.push((e.innerText||e.textContent||'').trim());} const b=document.body?document.body.innerText:''; if(/Best Seller/i.test(b)) parts.push('Best Seller'); if(/Amazon\'?s Choice/i.test(b)) parts.push("Amazon's Choice"); return parts.filter(Boolean).join(' | ');})()""")
    is_best=bool(re.search(r"best seller|amazon'?s choice",str(badge_text),re.I))
    match=title_similarity(cj_name,title); score=demand_score(rating,ratings_total,is_best,match)
    print(f'Amazon extracted data: title="{title}" ratingText="{rating_text}" reviewsText="{reviews_text}" rating="{rating}" reviews="{ratings_total}"')
    return {'title':title,'url':clean_amazon_url(amazon_url),'rating':rating or '', 'ratingsTotal':ratings_total or 0,'isBestSeller':is_best,'badgeText':badge_text,'matchScore':match,'score':score,'source':'nodriver-google-lens-amazon','fetchedAt':datetime.now(timezone.utc).isoformat()}

def already_has(existing,p):
    pid=str(p.get('id') or ''); name=product_name(p).lower()
    for a in existing:
        if pid and str(a.get('productId') or '')==pid: return True
        if name and str(a.get('productName') or '').lower()==name: return True
    return False

async def main():
    products=read_json(PRODUCTS_PATH,[]); existing=read_json(AMAZON_PRODUCTS_PATH,[])
    amazon_signals=list(existing); matches=[]; failures=[]; ps=proxies()
    print(f'Nodriver-only mode. Products loaded: {len(products)}')
    print(f'Residential proxies loaded: {len(ps)}')
    end=min(len(products),START_INDEX+MAX_PRODUCTS); browser=None; active_proxy=None
    try:
        for i in range(START_INDEX,end):
            p=products[i]; name=product_name(p); image=product_image(p)
            if not name or not image:
                failures.append({'index':i,'name':name,'reason':'missing product name or image'}); continue
            if already_has(existing,p):
                print(f'Skip existing Amazon data: {name}'); continue
            proxy=proxy_for_index(i,ps)
            if browser is None or proxy!=active_proxy:
                if browser:
                    try: await browser.stop()
                    except Exception: pass
                active_proxy=proxy; print(f'Starting Nodriver browser with proxy: {"configured" if proxy else "none"}')
                browser=await start_browser(proxy)
            print(f'Nodriver Lens {i+1}/{len(products)}: {name}')
            try:
                found=await find_first_amazon_lens(browser,image,name)
                if not found:
                    print(f'No Amazon match via Nodriver Lens: {name}')
                    failures.append({'index':i,'name':name,'image':image,'provider':'nodriver_lens','reason':'no amazon link from nodriver lens'}); continue
                print(f'Amazon URL found via Nodriver Lens: {found["url"]}')
                print(f'Scraping Amazon page with Nodriver: {found["url"]}')
                data=await scrape_amazon(browser,found['url'],name)
                if not data.get('title') or not data.get('rating'):
                    print(f'Amazon data rejected: missing title/rating | title="{data.get("title")}" rating="{data.get("rating")}" reviews="{data.get("ratingsTotal")}"')
                    failures.append({'index':i,'name':name,'image':image,'provider':'nodriver_lens','amazonUrl':found['url'],'reason':'amazon page missing title/rating data'}); continue
                if int(data.get('matchScore') or 0)<MIN_TITLE_MATCH:
                    print(f'Amazon data rejected: weak title match {data.get("matchScore")}/{MIN_TITLE_MATCH} | CJ="{name}" | Amazon="{data.get("title")}"')
                    failures.append({'index':i,'name':name,'image':image,'provider':'nodriver_lens','amazonUrl':found['url'],'reason':f'weak amazon title match: {data.get("matchScore")}' }); continue
                signal={'productId':p.get('id'),'keyword':p.get('id') or name,'productName':name,'image':image,'title':data['title'],'asin':'','score':data['score'],'bestRating':data['rating'],'bestRatingsTotal':data['ratingsTotal'],'bestPrice':'','position':1,'isBestSeller':data['isBestSeller'],'badgeText':data['badgeText'],'productUrl':data['url'],'matchScore':data['matchScore'],'matchType':'image','lensProvider':'nodriver_lens','source':data['source'],'fetchedAt':data['fetchedAt']}
                amazon_signals.append(signal); matches.append({'productId':p.get('id'),'productName':name,'productImage':image,'provider':'nodriver_lens','amazon':data})
                print(f'Matched via Nodriver Lens: {name} -> {data["title"]} | rating {data["rating"]} | reviews {data["ratingsTotal"]} | score {data["score"]}')
                if len(matches)%10==0:
                    write_json(AMAZON_PRODUCTS_PATH,amazon_signals); write_json('lens-amazon-matches.json',matches); write_json('lens-amazon-failures.json',failures)
                await sleep_ms(DELAY_MS+random.randint(0,2500))
            except Exception as e:
                print(f'Nodriver product failed for {name}: {e}')
                failures.append({'index':i,'name':name,'image':image,'reason':str(e)})
                await sleep_ms(DELAY_MS+random.randint(0,1500))
    finally:
        if browser:
            try: await browser.stop()
            except Exception: pass
    write_json(AMAZON_PRODUCTS_PATH,amazon_signals); write_json('lens-amazon-matches.json',matches); write_json('lens-amazon-failures.json',failures)
    write_json('lens-amazon-meta.json',{'updatedAt':datetime.now(timezone.utc).isoformat(),'mode':'nodriver-only google lens + amazon scraping','startIndex':START_INDEX,'maxProducts':MAX_PRODUCTS,'matches':len(matches),'failures':len(failures),'proxyCount':len(ps),'note':'Uses Nodriver for both Google Lens and Amazon. No Lens API providers and no Puppeteer.'})
    print(f'Nodriver monthly Lens complete. Matches: {len(matches)}, failures: {len(failures)}')

if __name__=='__main__': asyncio.run(main())
